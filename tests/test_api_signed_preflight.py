"""Совместимость проверяется по подписанному манифесту, не данным клиента."""
import base64
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from shared.api import handle
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.preflight import NAMES

ROOT = Path(__file__).resolve().parents[1]
TARGET = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}


@pytest.fixture
def signed_request(tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [deepcopy(TARGET)]}
    payload = tmp_path / 'payload'
    payload.mkdir()
    artifacts = [*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]
    for index, artifact in enumerate(artifacts):
        data = ('fictional test artifact ' + str(index)).encode()
        file = payload / artifact['file']
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        artifact['sha256'] = sha256(data).hexdigest()
    path = tmp_path / 'bundle.zip'
    build_package(manifest, '1', payload, path)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(path, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    query = {'api_major': 1, 'request_id': 'test', 'operation': 'inspect-fresh-package', 'params': {
        'package_path': str(path), 'envelope': base64.b64encode(envelope).decode(),
        'trusted_public_key': base64.b64encode(public).decode(), 'expected_product_id': manifest['product_id'],
        'expected_version': '1', 'target': deepcopy(TARGET), 'install_root': str(tmp_path / 'install'),
        'required_bytes': {name: 0 for name in NAMES}, 'reserve_bytes': 0}}
    return query, path, manifest


def test_signed_preflight_python_and_cli_are_read_only(signed_request):
    query, path, _ = signed_request
    response = handle(query)
    assert response['ok'] and response['result']['declared_target'] == 'supported'
    assert response['result']['preflight']['checks_passed']
    assert 'platform_compatibility' in response['result']['preflight']['not_checked']
    result = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                            input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert result.returncode == 0 and json.loads(result.stdout)['result']['product_id'] == response['result']['product_id']
    assert not Path(query['params']['install_root']).exists()
    assert path.exists()


def test_cannot_override_signed_manifest_or_select_undeclared_target(signed_request):
    query, _, manifest = signed_request
    query['params']['target']['execution'] = 'docker'
    assert handle(query)['error']['code'] == 'target_not_supported'
    manifest['compatibility']['targets'].append(deepcopy(query['params']['target']))
    query['params']['manifest'] = manifest
    assert handle(query)['error']['code'] == 'invalid_request'


def test_untrusted_key_tampering_and_target_failure_do_not_touch_destination(signed_request):
    query, path, _ = signed_request
    public_key = query['params']['trusted_public_key']
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    assert handle(query)['error']['code'] == 'verification_failed'
    query['params']['target']['os'] = 'SECRET-SENTINEL'
    response = handle(query)
    assert response['error']['code'] == 'verification_failed'
    assert 'SECRET-SENTINEL' not in json.dumps(response)
    assert not Path(query['params']['install_root']).exists()
    query['params']['trusted_public_key'] = public_key
    query['params']['target'] = deepcopy(TARGET)
    path.write_bytes(path.read_bytes() + b'tamper')
    assert handle(query)['error']['code'] == 'verification_failed'


def test_absent_compatibility_is_not_treated_as_supported(signed_request, monkeypatch):
    query, _, manifest = signed_request
    del manifest['compatibility']
    monkeypatch.setattr('shared.api.verify_signed_package', lambda *args, **kwargs: {
        'manifest': manifest, 'product_id': manifest['product_id'], 'version': '1', 'files': []})
    assert handle(query)['error']['code'] == 'target_not_supported'


def test_operation_is_unavailable_without_signature_support(signed_request, monkeypatch):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.api._signature_available', lambda: False)
    capabilities = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})
    assert 'inspect-fresh-package' not in capabilities['result']['operations']
    assert handle(query)['error']['code'] == 'unsupported_operation'


def native_query(query):
    result = deepcopy(query)
    result['operation'] = 'inspect-native-fresh-package'
    del result['params']['target']
    return result


def test_native_inspection_uses_detected_target(signed_request, monkeypatch):
    query, _, _ = signed_request
    query = native_query(query)
    monkeypatch.setattr('shared.api.native_target', lambda: deepcopy(TARGET))
    result = handle(query)
    assert result['ok'] and result['result']['process_target'] == TARGET
    assert not Path(query['params']['install_root']).exists()
    query['params']['target'] = {'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'}
    assert handle(query)['error']['code'] == 'invalid_request'


def test_native_inspection_refuses_mismatch_and_unknown_platform(signed_request, monkeypatch):
    import shared.api as api
    query, _, _ = signed_request
    query = native_query(query)
    monkeypatch.setattr(api, 'native_target', lambda: {'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'})
    assert handle(query)['error']['code'] == 'target_not_supported'
    def unknown():
        raise api.HostTargetError('SECRET-SENTINEL')
    monkeypatch.setattr(api, 'native_target', unknown)
    result = handle(query)
    assert result['error']['code'] == 'unsupported_platform'
    assert 'SECRET-SENTINEL' not in json.dumps(result)
    assert not Path(query['params']['install_root']).exists()
    query['language'] = 'de'
    assert handle(query)['error']['message'] == 'Die Prozessplattform wird nicht unterstützt'


def test_native_inspection_requires_signature_support(signed_request, monkeypatch):
    query, _, _ = signed_request
    query = native_query(query)
    monkeypatch.setattr('shared.api._signature_available', lambda: False)
    capabilities = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})
    assert 'inspect-native-fresh-package' not in capabilities['result']['operations']
    assert handle(query)['error']['code'] == 'unsupported_operation'


def test_native_inspection_current_process_and_cli(signed_request):
    from shared.host_target import HostTargetError, native_target
    try:
        target = native_target()
    except HostTargetError:
        pytest.skip('Среда процесса не входит в тестовый профиль')
    if target != TARGET:
        pytest.skip('Пакет фикстуры рассчитан на Linux x86_64')
    query, _, _ = signed_request
    query = native_query(query)
    response = handle(query)
    assert response['ok'] and response['result']['process_target'] == target
    cli = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                         input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert cli.returncode == 0
    assert json.loads(cli.stdout)['result']['process_target'] == target


def prepare_native(query, destination):
    from shared.package_prepare import prepare_native_signed_package
    params = query['params']
    return prepare_native_signed_package(params['package_path'], base64.b64decode(params['envelope']), destination,
        trusted_public_key=base64.b64decode(params['trusted_public_key']),
        expected_product_id=params['expected_product_id'], expected_version=params['expected_version'])


def test_preparation_checks_signed_target_before_publication(signed_request, tmp_path, monkeypatch):
    from shared.package import PackageError
    query, _, _ = signed_request
    destination = tmp_path / 'prepared-release'
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: {
        'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'})
    with pytest.raises(PackageError):
        prepare_native(query, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob('.homensai-prepare-*'))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    result = prepare_native(query, destination)
    assert result['process_target'] == TARGET
    assert result['files'] and destination.exists()


def test_preparation_rejects_unknown_host_and_wrong_key(signed_request, tmp_path, monkeypatch):
    from shared.host_target import HostTargetError
    from shared.package import PackageError
    query, _, _ = signed_request
    def unknown():
        raise HostTargetError('SECRET-SENTINEL')
    monkeypatch.setattr('shared.package_prepare.native_target', unknown)
    destination = tmp_path / 'prepared-release'
    with pytest.raises(PackageError) as error:
        prepare_native(query, destination)
    assert 'SECRET-SENTINEL' not in str(error.value)
    assert not destination.exists()
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    with pytest.raises(PackageError):
        prepare_native(query, destination)
    assert not destination.exists()


def fresh_prepare(query):
    from shared.fresh_prepare import prepare_fresh_native
    params = query['params']
    return prepare_fresh_native(params['package_path'], base64.b64decode(params['envelope']),
        params['install_root'], trusted_public_key=base64.b64decode(params['trusted_public_key']),
        expected_product_id=params['expected_product_id'], expected_version=params['expected_version'],
        required_bytes=params['required_bytes'], reserve_bytes=params['reserve_bytes'])


def test_fresh_signed_preparation_creates_only_a_staged_release(signed_request, monkeypatch):
    from shared.fresh_prepare import FreshPrepareError
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    root = Path(query['params']['install_root'])
    result = fresh_prepare(query)
    assert result['status'] == 'prepared' and result['process_target'] == TARGET
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'prepared'
    assert json.loads((root / 'services/active.json').read_text()) == {'release': '1'}
    assert (root / 'releases/app-1/locks/demo-linux.lock').exists()
    assert not list((root / 'data').iterdir())
    (root / 'data/keep.txt').write_text('preserve')
    with pytest.raises(FreshPrepareError):
        fresh_prepare(query)
    assert (root / 'data/keep.txt').read_text() == 'preserve'


def test_fresh_failure_preserves_partial_root_for_recovery(signed_request, monkeypatch):
    from shared.fresh_prepare import FreshPrepareError
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    def fail(*args, **kwargs):
        raise OSError('SECRET-SENTINEL')
    monkeypatch.setattr('shared.fresh_prepare.FilesystemPorts.stage_release', fail)
    root = Path(query['params']['install_root'])
    with pytest.raises(FreshPrepareError) as error:
        fresh_prepare(query)
    assert 'SECRET-SENTINEL' not in str(error.value)
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'
    assert not (root / 'services/active.json').exists()
    with pytest.raises(FreshPrepareError):
        fresh_prepare(query)


def test_fresh_preparation_rejects_wrong_key_before_writing(signed_request):
    from shared.fresh_prepare import FreshPrepareError
    query, _, _ = signed_request
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    with pytest.raises(FreshPrepareError):
        fresh_prepare(query)
    assert not Path(query['params']['install_root']).exists()


def test_fresh_refuses_external_product_paths_before_creating_root(signed_request, tmp_path, monkeypatch):
    import shared.fresh_prepare as fresh
    query, _, _ = signed_request
    original = fresh.verify_signed_package
    def outside(*args, **kwargs):
        metadata = original(*args, **kwargs)
        metadata['manifest']['paths'] = {name: str(tmp_path / 'outside' / name) for name in NAMES}
        return metadata
    monkeypatch.setattr(fresh, 'verify_signed_package', outside)
    monkeypatch.setattr(fresh, 'native_target', lambda: deepcopy(TARGET))
    with pytest.raises(fresh.FreshPrepareError):
        fresh_prepare(query)
    assert not Path(query['params']['install_root']).exists()
    assert not (tmp_path / 'outside').exists()


def test_fresh_detects_inconsistent_signed_snapshots(signed_request, monkeypatch):
    import shared.fresh_prepare as fresh
    query, _, _ = signed_request
    original = fresh.verify_signed_package
    def changed(*args, **kwargs):
        metadata = original(*args, **kwargs)
        metadata['manifest']['product_name'] = 'Different synthetic manifest'
        return metadata
    monkeypatch.setattr(fresh, 'verify_signed_package', changed)
    monkeypatch.setattr(fresh, 'native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    root = Path(query['params']['install_root'])
    with pytest.raises(fresh.FreshPrepareError):
        fresh_prepare(query)
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'
    assert not (root / 'services/active.json').exists()


def fresh_resume(query):
    from shared.fresh_prepare import resume_fresh_native
    params = query['params']
    return resume_fresh_native(params['package_path'], base64.b64decode(params['envelope']),
        params['install_root'], trusted_public_key=base64.b64decode(params['trusted_public_key']),
        expected_product_id=params['expected_product_id'], expected_version=params['expected_version'], reserve_bytes=0)


@pytest.mark.parametrize('phase', ['stage', 'switch'])
def test_fresh_resume_after_interruption(signed_request, monkeypatch, phase):
    from shared.fresh_prepare import FreshPrepareError
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    method = 'stage_release' if phase == 'stage' else 'switch_pointer'
    def stop(*args, **kwargs):
        raise SystemExit('simulated process interruption')
    with monkeypatch.context() as patch:
        patch.setattr('shared.fresh_prepare.FilesystemPorts.' + method, stop)
        with pytest.raises(SystemExit):
            fresh_prepare(query)
    root = Path(query['params']['install_root'])
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'
    result = fresh_resume(query)
    assert result['status'] == 'prepared'
    assert json.loads((root / 'services/active.json').read_text()) == {'release': '1'}
    (root / 'data/keep').write_text('preserve')
    with pytest.raises(FreshPrepareError):
        fresh_resume(query)
    assert (root / 'data/keep').read_text() == 'preserve'


@pytest.mark.parametrize('change', ['data', 'prepared', 'release', 'old-marker', 'wrong-key', 'lock', 'hardlink'])
def test_fresh_resume_refuses_foreign_or_altered_state(signed_request, monkeypatch, change):
    from shared.fresh_prepare import FreshPrepareError
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    def stop(*args, **kwargs):
        raise SystemExit('simulated interruption')
    with monkeypatch.context() as patch:
        patch.setattr('shared.fresh_prepare.FilesystemPorts.stage_release', stop)
        with pytest.raises(SystemExit):
            fresh_prepare(query)
    root = Path(query['params']['install_root'])
    if change == 'data':
        (root / 'data/user-file').write_text('keep')
    elif change == 'prepared':
        (root / '.homensai-control/prepared/locks/demo-linux.lock').write_text('tamper')
    elif change == 'release':
        (root / 'releases/unrelated').mkdir()
        (root / 'releases/unrelated/keep').write_text('keep')
    elif change == 'old-marker':
        path = root / '.homensai-fresh.json'
        state = json.loads(path.read_text())
        state['format'] = 1
        path.write_text(json.dumps(state))
    elif change == 'wrong-key':
        query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    elif change == 'lock':
        (root / 'services/operation.lock').write_text('user data')
    elif change == 'hardlink':
        try:
            os.link(root / '.homensai-fresh.json', root / 'marker-copy')
        except (AttributeError, OSError, NotImplementedError):
            pytest.skip('Жёсткие ссылки недоступны')
    with pytest.raises(FreshPrepareError):
        fresh_resume(query)
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'
    if change == 'data':
        assert (root / 'data/user-file').read_text() == 'keep'
    if change == 'release':
        assert (root / 'releases/unrelated/keep').read_text() == 'keep'
    if change == 'lock':
        assert (root / 'services/operation.lock').read_text() == 'user data'


def test_fresh_resume_rechecks_free_space_before_staging(signed_request, monkeypatch):
    from types import SimpleNamespace
    from shared.fresh_prepare import FreshPrepareError
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    def stop(*args, **kwargs):
        raise SystemExit('simulated interruption')
    with monkeypatch.context() as patch:
        patch.setattr('shared.fresh_prepare.FilesystemPorts.stage_release', stop)
        with pytest.raises(SystemExit):
            fresh_prepare(query)
    monkeypatch.setattr('shared.fresh_prepare.shutil.disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(FreshPrepareError):
        fresh_resume(query)
    root = Path(query['params']['install_root'])
    assert not (root / 'services/active.json').exists()
    assert not (root / 'releases/app-1').exists()
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'


@pytest.mark.parametrize('phase', ['after_journal', 'before_snapshot'])
def test_fresh_resume_before_snapshot_is_ready(signed_request, monkeypatch, phase):
    import shared.fresh_prepare as fresh
    query, _, _ = signed_request
    monkeypatch.setattr(fresh, 'native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    original = fresh._atomic_write
    def crash_after_journal(path, value):
        original(path, value)
        raise SystemExit('simulated interruption after journal')
    def crash_before_snapshot(*args, **kwargs):
        raise SystemExit('simulated interruption before snapshot')
    with monkeypatch.context() as patch:
        patch.setattr(fresh, '_atomic_write' if phase == 'after_journal' else 'prepare_native_signed_package',
                      crash_after_journal if phase == 'after_journal' else crash_before_snapshot)
        with pytest.raises(SystemExit):
            fresh_prepare(query)
    root = Path(query['params']['install_root'])
    assert not (root / '.homensai-control/prepared').exists()
    assert fresh_resume(query)['status'] == 'prepared'
    assert (root / 'releases/app-1/locks/demo-linux.lock').exists()


def test_fresh_initial_and_resume_share_exclusive_lock(signed_request, monkeypatch):
    import shared.fresh_prepare as fresh
    query, _, _ = signed_request
    monkeypatch.setattr(fresh, 'native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    original = fresh.prepare_native_signed_package
    attempted = []
    def concurrently(*args, **kwargs):
        with pytest.raises(fresh.FreshPrepareError):
            fresh_resume(query)
        attempted.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(fresh, 'prepare_native_signed_package', concurrently)
    assert fresh_prepare(query)['status'] == 'prepared'
    assert attempted == [True]


@pytest.mark.parametrize('change', ['partial', 'data', 'bootstrap', 'old-v2'])
def test_fresh_early_resume_refuses_untrusted_state(signed_request, monkeypatch, change):
    import shared.fresh_prepare as fresh
    query, _, _ = signed_request
    monkeypatch.setattr(fresh, 'native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    def crash(*args, **kwargs):
        raise SystemExit('synthetic')
    with monkeypatch.context() as patch:
        patch.setattr(fresh, 'prepare_native_signed_package', crash)
        with pytest.raises(SystemExit):
            fresh_prepare(query)
    root = Path(query['params']['install_root'])
    if change == 'partial':
        (root / '.homensai-control/prepared').mkdir()
        (root / '.homensai-control/prepared/keep').write_text('keep')
    elif change == 'data':
        (root / 'data').mkdir()
        (root / 'data/keep').write_text('keep')
    elif change == 'bootstrap':
        (root / '.homensai-bootstrap.lock').write_text('keep')
    elif change == 'old-v2':
        marker = root / '.homensai-fresh.json'
        state = json.loads(marker.read_text())
        state['format'] = 2
        marker.write_text(json.dumps(state))
    with pytest.raises(fresh.FreshPrepareError):
        fresh_resume(query)
    if change == 'partial':
        assert (root / '.homensai-control/prepared/keep').read_text() == 'keep'
    if change == 'data':
        assert (root / 'data/keep').read_text() == 'keep'
    if change == 'bootstrap':
        assert (root / '.homensai-bootstrap.lock').read_text() == 'keep'
    assert not (root / 'services/active.json').exists()


def test_fresh_resume_after_real_process_exit(signed_request):
    from shared.host_target import HostTargetError, native_target
    try:
        if native_target() != TARGET:
            pytest.skip('Проверка процесса использует фикстуру Linux x86_64')
    except HostTargetError:
        pytest.skip('Платформа процесса не поддерживается')
    query, _, _ = signed_request
    script = '''\
import base64, json, os, sys
import shared.fresh_prepare as fresh
p = json.load(sys.stdin)['params']
original = fresh._atomic_write
def interrupted(path, state):
    original(path, state)
    os._exit(73)
fresh._atomic_write = interrupted
fresh.prepare_fresh_native(p['package_path'], base64.b64decode(p['envelope']), p['install_root'],
    trusted_public_key=base64.b64decode(p['trusted_public_key']), expected_product_id=p['expected_product_id'],
    expected_version=p['expected_version'], required_bytes=p['required_bytes'], reserve_bytes=p['reserve_bytes'])
'''
    child = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT, input=json.dumps(query).encode(),
                           capture_output=True, timeout=30)
    assert child.returncode == 73 and child.stderr == b''
    root = Path(query['params']['install_root'])
    lock = (root / '.homensai-bootstrap.lock').read_bytes()
    assert json.loads(lock[1:])['status'] == 'held'
    assert fresh_resume(query)['status'] == 'prepared'
    assert json.loads((root / 'services/active.json').read_text()) == {'release': '1'}


def status_query(query):
    value = deepcopy(query)
    value['operation'] = 'fresh-status'
    for field in ('target', 'required_bytes', 'reserve_bytes'):
        del value['params'][field]
    return value


def test_api_reports_fresh_status_without_claiming_files_verified(signed_request, monkeypatch):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    fresh_prepare(query)
    root = Path(query['params']['install_root'])
    marker = root / '.homensai-fresh.json'
    before = marker.read_bytes()
    query = status_query(query)
    assert 'fresh-status' in handle({'api_major': 1, 'request_id': 't', 'operation': 'capabilities', 'params': {}})['result']['operations']
    result = handle(query)
    assert result['ok'] and result['result']['reported_status'] == 'prepared'
    assert result['result']['completion_verified'] is False
    assert result['result']['product_id'] == query['params']['expected_product_id']
    assert marker.read_bytes() == before
    assert str(root) not in json.dumps(result)
    # Статус описывает журнал, не целостность установленного выпуска.
    (root / 'releases/app-1/locks/demo-linux.lock').write_text('tamper')
    assert handle(query)['result']['completion_verified'] is False
    assert handle(query)['result']['reported_status'] == 'prepared'


def test_api_fresh_status_reports_partial_and_refuses_untrusted_key(signed_request, monkeypatch):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    def fail(*args, **kwargs):
        raise SystemExit('synthetic interruption')
    with monkeypatch.context() as patch:
        patch.setattr('shared.fresh_prepare.FilesystemPorts.stage_release', fail)
        with pytest.raises(SystemExit):
            fresh_prepare(query)
    query = status_query(query)
    assert handle(query)['result']['reported_status'] == 'preparing'
    query['language'] = 'en'
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    error = handle(query)['error']
    assert error['code'] == 'fresh_status_failed'
    assert error['message'] == 'Could not read preparation state'
    assert str(query['params']['install_root']) not in json.dumps(error)


def test_cli_fresh_status_reports_prepared(signed_request):
    from shared.host_target import HostTargetError, native_target
    try:
        if native_target() != TARGET:
            pytest.skip('Фикстура рассчитана на Linux x86_64')
    except HostTargetError:
        pytest.skip('Платформа процесса не поддерживается')
    query, _, _ = signed_request
    fresh_prepare(query)
    query = status_query(query)
    result = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                            input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stderr == b''
    assert json.loads(result.stdout) == handle(query)
    query['params']['install_root'] += '-nonexistent'
    result = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                            input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'fresh_status_failed'
