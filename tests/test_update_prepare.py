"""Подготовка подписанного обновления без запуска и сетевых обращений."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import zipfile

import pytest

from test_api_signed_preflight import signed_request
from test_systemd_pointer_signed import signed_pointer
from examples.demo_product.installer_client import InstallerClient
from shared import api
from shared.effect_receipt import read_effect_receipt
from shared.fresh_prepare import prepare_fresh_native
from shared.host_target import native_target
from shared.package import build_package
from shared.package_signing import sign_package
from shared.systemd_fresh_start import _lifecycle_lock
from shared.update_prepare import UpdatePrepareError, prepare_signed_linux_update

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Подготовка рассчитана на Linux')


@pytest.mark.parametrize('change', ['none', 'runtime-binary', 'runtime-extra', 'missing-recipe',
                                    'conflicting-recipe', 'launcher', 'adopt-pointer'])
def test_pointer_update_requires_identical_signed_runtime(signed_pointer, tmp_path, change):
    from shared.systemd_pointer import LAUNCHER_PATH, RECIPE_PATH
    payload, trusted, build = signed_pointer
    if change == 'adopt-pointer':
        recipe = (payload / RECIPE_PATH).read_bytes()
        (payload / RECIPE_PATH).unlink()
    old, old_envelope, root = build('old')
    if change == 'adopt-pointer':
        (payload / RECIPE_PATH).write_bytes(recipe)
    if change == 'runtime-binary':
        (payload / 'runtime-linux/bin/python').write_bytes(b'different synthetic interpreter')
    elif change == 'runtime-extra':
        (payload / 'runtime-linux/extra').write_text('synthetic change')
    elif change == 'missing-recipe':
        (payload / RECIPE_PATH).unlink()
    elif change == 'conflicting-recipe':
        (payload / 'systemd-user/portable-v1.json').write_text('conflicting')
    elif change == 'launcher':
        (payload / LAUNCHER_PATH).write_text('modified synthetic launcher')
    new, new_envelope, _ = build('new', version='2')
    work = tmp_path / 'pointer-update-work'
    work.mkdir(mode=0o700)
    options = dict(new_package=str(new), new_envelope=new_envelope, new_key=trusted['trusted_public_key'],
        old_package=str(old), old_envelope=old_envelope, old_key=trusted['trusted_public_key'],
        product_id=trusted['expected_product_id'], old_version='1', new_version='2',
        install_root=str(root), work_dir=str(work), modes=('autonomous',), migration_ids=(), operation_id='f' * 32)
    pointer = root / 'services/active.json'
    before = pointer.read_bytes()
    if change == 'none':
        result = prepare_signed_linux_update(**options)
        assert result['state'] == 'prepared' and not result['update_started']
        response = api.handle(api_query(options))
        assert response['ok'] and response['result'] == result
    else:
        with pytest.raises(UpdatePrepareError):
            prepare_signed_linux_update(**options)
        response = api.handle(api_query(options))
        assert response['error']['code'] == 'update_prepare_failed'
        assert str(root) not in json.dumps(response)
        assert not list(work.iterdir())
    assert pointer.read_bytes() == before and not (root / 'services/update.json').exists()


@pytest.fixture
def update(signed_request, tmp_path):
    if native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}:
        pytest.skip('Подготовка рассчитана на Linux x86-64')
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    query, old_package, manifest = signed_request
    params = query['params']
    old_key = base64.b64decode(params['trusted_public_key'])
    old_envelope = base64.b64decode(params['envelope'])
    prepare_fresh_native(old_package, old_envelope, params['install_root'], trusted_public_key=old_key,
                         expected_product_id=manifest['product_id'], expected_version='1',
                         required_bytes=params['required_bytes'], reserve_bytes=0)
    payload = tmp_path / 'payload'
    (payload / 'new.py').write_bytes(b'# synthetic new release\n')
    new_package = tmp_path / 'new.zip'
    build_package(manifest, '2', payload, new_package)
    private = ed.Ed25519PrivateKey.generate()
    new_key = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    new_envelope = sign_package(new_package, private_key=private, expected_product_id=manifest['product_id'],
                                expected_version='2')
    work = tmp_path / 'update-work'
    work.mkdir(mode=0o700)
    options = {'new_package': str(new_package), 'new_envelope': new_envelope, 'new_key': new_key,
               'old_package': str(old_package), 'old_envelope': old_envelope, 'old_key': old_key,
               'product_id': manifest['product_id'], 'old_version': '1', 'new_version': '2',
               'install_root': params['install_root'], 'work_dir': str(work), 'modes': ('autonomous',),
               'migration_ids': (), 'operation_id': 'a' * 32}
    return options, manifest, private, payload


def test_signed_update_preparation_is_idempotent_and_does_not_start_update(update):
    options, _, _, _ = update
    root, work = Path(options['install_root']), Path(options['work_dir'])
    pointer = root / 'services/active.json'
    before = pointer.read_bytes()
    result = prepare_signed_linux_update(**options)
    assert result['state'] == 'prepared' and result['update_started'] is False
    assert result['runtime_verified'] is False and result['services_verified'] is False
    staged = work / result['payload_name']
    assert (staged / 'new.py').read_bytes() == b'# synthetic new release\n'
    assert pointer.read_bytes() == before and not (root / 'services/update.json').exists()
    assert prepare_signed_linux_update(**options) == result
    assert pointer.read_bytes() == before
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**{**options, 'migration_ids': ('different',)})
    assert (staged / 'new.py').read_bytes() == b'# synthetic new release\n'
    (staged / 'new.py').write_bytes(b'tampered')
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**options)
    assert pointer.read_bytes() == before


def test_work_directory_cannot_overlap_installation(update):
    options, _, _, _ = update
    root = Path(options['install_root'])
    control = root / '.homensai-control'
    before = sorted(control.iterdir())
    for work in (root, control, root.parent):
        with pytest.raises(UpdatePrepareError):
            prepare_signed_linux_update(**{**options, 'work_dir': str(work)})
    assert sorted(control.iterdir()) == before


def test_invalid_publisher_and_existing_destination_never_write(update):
    options, _, _, _ = update
    work = Path(options['work_dir'])
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**{**options, 'new_key': b'x' * 32})
    assert not list(work.iterdir())
    existing = work / ('payload-' + options['operation_id'])
    existing.mkdir()
    (existing / 'keep').write_bytes(b'preserve')
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**options)
    assert (existing / 'keep').read_bytes() == b'preserve'
    assert not (work / ('prepare-' + options['operation_id'] + '.json')).exists()


def test_signed_update_rejects_unsafe_wheel_before_preparation(update, tmp_path, monkeypatch):
    options, original_manifest, private, payload = update
    manifest = deepcopy(original_manifest)
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('../../escape', 'synthetic unsafe path')
    artifact = manifest['runtime']['vendor_wheels'][0]
    (payload / artifact['file']).write_bytes(data.getvalue())
    artifact['sha256'] = hashlib.sha256(data.getvalue()).hexdigest()
    package = tmp_path / 'unsafe-update.zip'
    build_package(manifest, '2', payload, package)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='2')
    options = {**options, 'new_package': str(package), 'new_envelope': envelope}
    pointer = Path(options['install_root']) / 'services/active.json'
    before = pointer.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('payload extraction occurred before wheel audit')
    monkeypatch.setattr('shared.update_prepare.prepare_native_signed_package', forbidden)
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**options)
    response = api.handle(api_query(options, language='de'))
    assert response['error']['code'] == 'update_prepare_failed'
    assert str(tmp_path) not in json.dumps(response)
    assert pointer.read_bytes() == before and not list(Path(options['work_dir']).iterdir())
    assert not (tmp_path / 'escape').exists()


def test_existing_update_state_blocks_new_preparation(update):
    options, _, _, _ = update
    work = Path(options['work_dir'])
    state = Path(options['install_root']) / 'services/update.json'
    state.write_bytes(b'{"synthetic":"unrelated state"}\n')
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**options)
    assert state.read_bytes() == b'{"synthetic":"unrelated state"}\n'
    assert not list(work.iterdir())


def test_interrupted_preparation_stays_pending_and_is_not_repeated(update, monkeypatch):
    import shared.update_prepare as module
    class Crash(BaseException):
        pass
    options, _, _, _ = update
    original = module.prepare_native_signed_package
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(module, 'prepare_native_signed_package', interrupted)
        with pytest.raises(Crash):
            prepare_signed_linux_update(**options)
    work = Path(options['work_dir'])
    record = read_effect_receipt(work / ('prepare-' + options['operation_id'] + '.json'))
    assert record['status'] == 'pending' and calls == [1]
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**options)
    assert read_effect_receipt(work / ('prepare-' + options['operation_id'] + '.json')) == record
    assert calls == [1]


def test_real_sigkill_after_extraction_keeps_pending_and_does_not_repeat(update):
    options, _, _, _ = update
    payload = dict(options)
    for name in ('new_envelope', 'new_key', 'old_envelope', 'old_key'):
        payload[name] = base64.b64encode(payload[name]).decode('ascii')
    script = r'''
import base64, json, os, signal, sys
import shared.update_prepare as module
params = json.load(sys.stdin)
for name in ('new_envelope', 'new_key', 'old_envelope', 'old_key'):
    params[name] = base64.b64decode(params[name])
params['modes'] = tuple(params['modes'])
params['migration_ids'] = tuple(params['migration_ids'])
original = module.prepare_native_signed_package
def interrupted(*args, **kwargs):
    original(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
module.prepare_native_signed_package = interrupted
module.prepare_signed_linux_update(**params)
'''
    result = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps(payload), text=True,
                            capture_output=True, timeout=120)
    assert result.returncode == -signal.SIGKILL, result.stderr
    work = Path(options['work_dir'])
    marker = work / ('prepare-' + options['operation_id'] + '.json')
    destination = work / ('payload-' + options['operation_id'])
    assert read_effect_receipt(marker)['status'] == 'pending' and (destination / 'new.py').is_file()
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**options)
    assert read_effect_receipt(marker)['status'] == 'pending'


def test_lifecycle_contention_and_incompatible_signed_manifest_are_rejected(update, tmp_path):
    options, manifest, private, payload = update
    work = Path(options['work_dir'])
    with _lifecycle_lock(options['install_root']):
        with pytest.raises(UpdatePrepareError):
            prepare_signed_linux_update(**options)
    assert not list(work.iterdir())
    changed = deepcopy(manifest)
    changed['paths']['data'] = 'different-data'
    package = tmp_path / 'incompatible.zip'
    build_package(changed, '2', payload, package)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'],
                            expected_version='2')
    with pytest.raises(UpdatePrepareError):
        prepare_signed_linux_update(**{**options, 'new_package': str(package), 'new_envelope': envelope})
    assert not list(work.iterdir())


def api_query(options, language='en'):
    params = dict(options)
    for name in ('new_envelope', 'new_key', 'old_envelope', 'old_key'):
        params[name] = base64.b64encode(params[name]).decode('ascii')
    params['modes'], params['migration_ids'] = list(params['modes']), list(params['migration_ids'])
    params['idempotency_key'] = params.pop('operation_id')
    return {'api_major': 1, 'request_id': 'update-prepare-test', 'operation': 'prepare-signed-linux-update',
            'language': language, 'params': params}


def test_api_and_independent_client_prepare_same_signed_update(update):
    jsonschema = pytest.importorskip('jsonschema')
    options, _, _, _ = update
    query = api_query(options)
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 29 and query['operation'] in caps['mutating_operations']
    response = api.handle(query)
    assert response['ok'] is True and response['result']['state'] == 'prepared'
    assert response['result']['update_started'] is False and response['result']['services_verified'] is False
    result = InstallerClient(timeout=90).call(query['operation'], query['params'], min_minor=29, language='en')
    assert result == response['result']
    assert not (Path(options['install_root']) / 'services/update.json').exists()


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_api_invalid_publisher_is_sanitized_and_does_not_write(update, language):
    options, _, _, _ = update
    query = api_query(options, language)
    query['params']['new_key'] = base64.b64encode(b'x' * 32).decode('ascii')
    response = api.handle(query)
    assert response['error']['code'] == 'update_prepare_failed'
    assert options['install_root'] not in json.dumps(response) and options['new_package'] not in json.dumps(response)
    assert not list(Path(options['work_dir']).iterdir())


def test_api_does_not_advertise_or_run_without_signature_support(update, monkeypatch):
    options, _, _, _ = update
    query = api_query(options)
    monkeypatch.setattr(api, '_signature_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'
    assert not list(Path(options['work_dir']).iterdir())
