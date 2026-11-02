"""Совместимость проверяется по подписанному манифесту, не данным клиента."""
import base64
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

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
    query, package, _ = signed_request
    query = native_query(query)
    monkeypatch.setattr('shared.api.native_target', lambda: deepcopy(TARGET))
    result = handle(query)
    assert result['ok'] and result['result']['process_target'] == TARGET
    estimate = result['result']['storage_estimate']
    assert estimate['minimum_bytes'] >= package.stat().st_size * 2 + 5 * 1024 * 1024
    assert estimate['reserve_bytes'] == 0 and estimate['free_bytes'] >= estimate['minimum_bytes']
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


def test_native_inspection_rejects_existing_empty_root(signed_request, monkeypatch):
    query, _, _ = signed_request
    query = native_query(query)
    root = Path(query['params']['install_root'])
    root.mkdir()
    monkeypatch.setattr('shared.api.native_target', lambda: deepcopy(TARGET))
    assert handle(query)['error']['code'] == 'preflight_failed'
    assert root.is_dir() and not list(root.iterdir())


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


def api_prepare_query(query):
    value = deepcopy(query)
    value['operation'] = 'prepare-native-release'
    del value['params']['target']
    value['params']['idempotency_key'] = 'a' * 32
    return value


def test_api_prepare_repeat_and_conflicting_request(signed_request, monkeypatch):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    query = api_prepare_query(query)
    capabilities = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})
    assert 'prepare-native-release' in capabilities['result']['mutating_operations']
    first = handle(query)
    assert first['ok'] and first['result']['transaction'] == 'a' * 32
    assert first['result']['installation_complete'] is False
    root = Path(query['params']['install_root'])
    (root / 'data/keep').write_text('preserve')
    again = handle(query)
    assert again == first and (root / 'data/keep').read_text() == 'preserve'
    conflict = deepcopy(query)
    conflict['params']['reserve_bytes'] = 1
    assert handle(conflict)['error']['code'] == 'conflict'
    conflict = deepcopy(query)
    conflict['params']['idempotency_key'] = 'b' * 32
    assert handle(conflict)['error']['code'] == 'conflict'
    assert (root / 'data/keep').read_text() == 'preserve'
    status = status_query({**query, 'params': {**query['params'], 'target': deepcopy(TARGET)}})
    del status['params']['idempotency_key']
    assert handle(status)['result']['reported_status'] == 'prepared'


def test_api_prepare_retries_after_interrupted_stage(signed_request, monkeypatch):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    query = api_prepare_query(query)
    def fail(*args, **kwargs):
        raise OSError('SECRET-SENTINEL')
    with monkeypatch.context() as patch:
        patch.setattr('shared.fresh_prepare.FilesystemPorts.stage_release', fail)
        response = handle(query)
    assert response['error']['code'] == 'fresh_prepare_failed'
    assert 'SECRET-SENTINEL' not in json.dumps(response)
    resumed = handle(query)
    assert resumed['ok'] and resumed['result']['transaction'] == 'a' * 32
    assert resumed['result']['status'] == 'prepared'


def test_api_prepare_concurrent_duplicate_does_not_create_two_operations(signed_request, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from shared.adapters_fs import FilesystemPorts
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    query = api_prepare_query(query)
    entered, release = threading.Event(), threading.Event()
    original = FilesystemPorts.stage_release
    def hold(self, *args):
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test timed out')
        return original(self, *args)
    with monkeypatch.context() as patch:
        patch.setattr(FilesystemPorts, 'stage_release', hold)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handle, deepcopy(query))
            try:
                assert entered.wait(10)
                concurrent = handle(deepcopy(query))
                assert concurrent['error']['code'] == 'fresh_prepare_failed'
            finally:
                release.set()
            first = future.result(timeout=20)
    assert first['ok'] and handle(query) == first


def test_api_prepare_rejects_bad_key_before_write(signed_request):
    query, _, _ = signed_request
    query = api_prepare_query(query)
    query['params']['idempotency_key'] = 'SECRET-SENTINEL'
    result = handle(query)
    assert result['error']['code'] == 'invalid_request'
    assert 'SECRET-SENTINEL' not in json.dumps(result)
    assert not Path(query['params']['install_root']).exists()


def test_api_prepare_invalid_sizes_and_missing_crypto_do_not_write(signed_request, monkeypatch):
    query, _, _ = signed_request
    query = api_prepare_query(query)
    query['params']['required_bytes']['data'] = True
    assert handle(query)['error']['code'] == 'invalid_request'
    query['params']['required_bytes']['data'] = 0
    monkeypatch.setattr('shared.api._signature_available', lambda: False)
    capabilities = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})
    assert 'prepare-native-release' not in capabilities['result']['operations']
    assert 'prepare-native-release' not in capabilities['result']['mutating_operations']
    assert handle(query)['error']['code'] == 'unsupported_operation'
    assert not Path(query['params']['install_root']).exists()


@pytest.mark.parametrize('interrupted_status', ['preparing', 'prepared'])
def test_cli_prepare_recovers_after_real_process_exit(signed_request, interrupted_status):
    from shared.host_target import HostTargetError, native_target
    try:
        if native_target() != TARGET:
            pytest.skip('Фикстура рассчитана на Linux x86_64')
    except HostTargetError:
        pytest.skip('Платформа процесса не поддерживается')
    query, _, _ = signed_request
    query = api_prepare_query(query)
    script = '''\
import json, os, sys
import shared.fresh_prepare as fresh
from shared.api import handle
query = json.load(sys.stdin)
original = fresh._atomic_write
def interrupted(path, state):
    original(path, state)
    if state['status'] == sys.argv[1]:
        os._exit(73)
fresh._atomic_write = interrupted
handle(query)
'''
    child = subprocess.run([sys.executable, '-B', '-c', script, interrupted_status], cwd=ROOT, input=json.dumps(query).encode(),
                           capture_output=True, timeout=30)
    assert child.returncode == 73
    command = [sys.executable, '-B', '-m', 'shared.api']
    result = subprocess.run(command, cwd=ROOT, input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stderr == b''
    response = json.loads(result.stdout)
    assert response['result']['transaction'] == 'a' * 32
    assert response['result']['status'] == 'prepared' and response['result']['installation_complete'] is False
    again = subprocess.run(command, cwd=ROOT, input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert again.returncode == 0 and json.loads(again.stdout) == response


def test_api_replay_reverifies_package_and_rejects_tampering(signed_request, monkeypatch):
    query, path, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    query = api_prepare_query(query)
    assert handle(query)['ok']
    root = Path(query['params']['install_root'])
    (root / 'data/keep').write_text('preserve')
    with path.open('ab') as stream:
        stream.write(b'tamper')
    assert handle(query)['error']['code'] == 'fresh_prepare_failed'
    assert (root / 'data/keep').read_text() == 'preserve'


def test_api_verifies_prepared_release_against_signed_package(signed_request, monkeypatch):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    fresh_prepare(query)
    root = Path(query['params']['install_root'])
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    query = status_query(query)
    query['operation'] = 'verify-native-release'
    capabilities = handle({'api_major': 1, 'request_id': 't', 'operation': 'capabilities', 'params': {}})['result']
    assert capabilities['api_minor'] >= 10 and 'verify-native-release' in capabilities['operations']
    assert 'verify-native-release' not in capabilities['mutating_operations']
    result = handle(query)
    assert result['ok'] and result['result']['release_files_match'] is True
    assert result['result']['installation_complete'] is False
    assert str(root) not in json.dumps(result)
    assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('change', ['file', 'extra_file', 'empty_dir', 'pointer', 'state', 'key'])
def test_api_release_verification_refuses_modified_state(signed_request, monkeypatch, change):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    fresh_prepare(query)
    root = Path(query['params']['install_root'])
    release = root / 'releases/app-1'
    query = status_query(query)
    query['operation'] = 'verify-native-release'
    query['language'] = 'en'
    if change == 'file':
        (release / 'locks/demo-linux.lock').write_text('tampered')
    elif change == 'extra_file':
        (release / 'unexpected').write_text('unexpected')
    elif change == 'empty_dir':
        (release / 'unexpected').mkdir()
    elif change == 'pointer':
        (root / 'services/active.json').write_text('{"release":"other"}')
    elif change == 'state':
        marker = root / '.homensai-fresh.json'
        value = json.loads(marker.read_text())
        value['status'] = 'preparing'
        marker.write_text(json.dumps(value))
    else:
        query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    result = handle(query)
    assert not result['ok'] and result['error']['code'] == 'fresh_verify_failed'
    assert result['error']['message'] == 'Could not verify the prepared release'
    assert str(root) not in json.dumps(result)


def test_verify_native_release_request_matches_json_schema(signed_request):
    jsonschema = pytest.importorskip('jsonschema')
    query, _, _ = signed_request
    query = status_query(query)
    query['operation'] = 'verify-native-release'
    schema = json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text())
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(query)
    query['params']['extra'] = 'forbidden'
    assert not validator.is_valid(query)
    assert handle(query)['error']['code'] == 'invalid_request'


def test_cli_verifies_native_release(signed_request):
    from shared.host_target import HostTargetError, native_target
    try:
        if native_target() != TARGET:
            pytest.skip('Фикстура рассчитана на Linux x86_64')
    except HostTargetError:
        pytest.skip('Платформа процесса не поддерживается')
    query, _, _ = signed_request
    fresh_prepare(query)
    query = status_query(query)
    query['operation'] = 'verify-native-release'
    process = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                             input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert process.returncode == 0 and process.stderr == b''
    assert json.loads(process.stdout) == handle(query)
    (Path(query['params']['install_root']) / 'releases/app-1/locks/demo-linux.lock').write_text('tamper')
    process = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                             input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert process.returncode == 1 and process.stderr == b''
    assert json.loads(process.stdout)['error']['code'] == 'fresh_verify_failed'


@pytest.mark.parametrize('change', ['file', 'extra_file', 'empty_dir', 'pointer', 'missing_release'])
def test_api_prepare_repeat_rejects_modified_release_without_repair(signed_request, monkeypatch, change):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    query = api_prepare_query(query)
    assert handle(query)['ok']
    root = Path(query['params']['install_root'])
    release = root / 'releases/app-1'
    keep = root / 'data/keep'
    keep.write_text('preserve')
    if change == 'file':
        changed = release / 'locks/demo-linux.lock'
        changed.write_text('tampered')
    elif change == 'extra_file':
        changed = release / 'unexpected'
        changed.write_text('keep')
    elif change == 'empty_dir':
        changed = release / 'unexpected'
        changed.mkdir()
    elif change == 'pointer':
        changed = root / 'services/active.json'
        changed.write_text('{"release":"other"}')
    else:
        changed = release
        release.rename(root / 'releases/displaced')
    result = handle(query)
    assert not result['ok'] and result['error']['code'] == 'fresh_prepare_failed'
    assert str(root) not in json.dumps(result)
    assert keep.read_text() == 'preserve'
    if change == 'missing_release':
        assert not changed.exists() and (root / 'releases/displaced').is_dir()
    else:
        assert changed.exists()
        if change == 'file':
            assert changed.read_text() == 'tampered'
        elif change == 'pointer':
            assert changed.read_text() == '{"release":"other"}'


def test_independent_product_client_recovers_from_lost_prepare_response(tmp_path, monkeypatch):
    from examples.demo_product.installer_client import InstallerClient, InstallerClientError
    from shared.host_target import HostTargetError, native_target
    try:
        if native_target() != TARGET:
            pytest.skip('Сквозная фикстура рассчитана на Linux x86_64')
    except HostTargetError:
        pytest.skip('Платформа процесса не поддерживается')
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-demo'
    manifest['product_name'] = 'Synthetic Demo Product'
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [deepcopy(TARGET)]}
    payload = tmp_path / 'payload'
    payload.mkdir()
    (payload / 'main.py').write_bytes((ROOT / 'examples/demo_product/main.py').read_bytes())
    (payload / 'release.json').write_bytes((ROOT / 'examples/demo_product/releases/1/release.json').read_bytes())
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        data = ('fictional runtime ' + str(index)).encode()
        file = payload / artifact['file']
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        artifact['sha256'] = sha256(data).hexdigest()
    package = tmp_path / 'demo.zip'
    build_package(manifest, '1', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    root = tmp_path / 'install'
    shared = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(public).decode(),
              'expected_product_id': manifest['product_id'], 'expected_version': '1', 'install_root': str(root)}
    preparation = {**shared, 'required_bytes': {name: 0 for name in NAMES}, 'reserve_bytes': 0,
                   'idempotency_key': 'd' * 32}
    client = InstallerClient(timeout=60)
    original = subprocess.run
    calls = []

    def lose_response(argv, **kwargs):
        request = json.loads(kwargs['input'])
        calls.append(request['operation'])
        response = original(argv, **kwargs)
        if request['operation'] == 'prepare-native-release':
            assert response.returncode == 0
            raise subprocess.TimeoutExpired(argv, 60)
        return response

    with monkeypatch.context() as patch:
        patch.setattr('examples.demo_product.installer_client.subprocess.run', lose_response)
        with pytest.raises(InstallerClientError) as exc:
            client.call('prepare-native-release', preparation, min_minor=8, language='en')
        assert exc.value.code == 'transport_error'
    assert calls == ['capabilities', 'prepare-native-release']
    assert client.call('fresh-status', shared, min_minor=7)['reported_status'] == 'prepared'
    repeated = client.call('prepare-native-release', preparation, min_minor=8, language='en')
    assert repeated['transaction'] == 'd' * 32 and repeated['installation_complete'] is False
    verified = client.call('verify-native-release', shared, min_minor=10, language='de')
    assert verified['release_files_match'] is True and verified['installation_complete'] is False
    script = root / 'releases/app-1/main.py'
    assert script.read_bytes() == (payload / 'main.py').read_bytes()
    # Test-owned processes exercise the prepared artifact; the installer API
    # itself does not install services or start the product.
    data = root / 'data'
    migration = subprocess.run([sys.executable, '-B', str(script), 'migrate', '--data-dir', str(data)],
                               capture_output=True, timeout=30)
    assert migration.returncode == 0
    with sqlite3.connect(data / 'demo.sqlite3') as conn:
        assert conn.execute('SELECT text FROM entries').fetchall() == [('synthetic-schema-1',)]
    processes = []
    with (tmp_path / 'server.log').open('w+', encoding='utf-8') as log:
        try:
            server = subprocess.Popen([sys.executable, '-B', str(script), 'serve', '--data-dir', str(data),
                                       '--port', '0'], stdout=log, stderr=subprocess.DEVNULL)
            processes.append(server)
            worker = subprocess.Popen([sys.executable, '-B', str(script), 'worker', '--data-dir', str(data)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(worker)
            deadline = time.monotonic() + 30
            port = None
            heartbeat = data / 'worker-heartbeat.json'
            while time.monotonic() < deadline:
                log.seek(0)
                line = log.readline()
                if line:
                    port = json.loads(line)['port']
                if port and heartbeat.is_file():
                    beat = json.loads(heartbeat.read_text(encoding='utf-8'))
                    if beat['pid'] == worker.pid and beat['release'] == '1':
                        break
                if any(process.poll() is not None for process in processes):
                    pytest.fail('Синтетический процесс завершился при запуске')
                time.sleep(0.05)
            else:
                pytest.fail('Синтетические процессы не подтвердили готовность')
            # The API subprocess connects only to the local test server.
            health = client.call('check-local-health', {
                'port': port, 'product_id': 'homensai-test-demo', 'version': '1',
                'path': '/health', 'timeout_seconds': 3}, min_minor=12, language='de')
            assert health == {'health_response_matches': True, 'process_identity_verified': False}
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    (root / 'releases/app-1/main.py').write_text('tampered')
    with pytest.raises(InstallerClientError) as exc:
        client.call('prepare-native-release', preparation, min_minor=8, language='en')
    assert exc.value.code == 'fresh_prepare_failed'
    assert (root / 'releases/app-1/main.py').read_text() == 'tampered'


@pytest.mark.parametrize('reserve', [0, 4096])
def test_fresh_prepare_refuses_underestimated_package_space_before_creation(signed_request, monkeypatch, reserve):
    from types import SimpleNamespace
    from shared.preflight import preflight_fresh
    query, path, manifest = signed_request
    query['params']['reserve_bytes'] = reserve
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    minimum = path.stat().st_size * 2 + 5 * 1024 * 1024
    monkeypatch.setattr('shared.fresh_prepare.shutil.disk_usage',
                        lambda _: SimpleNamespace(free=minimum + reserve - 1))
    root = Path(query['params']['install_root'])
    # Caller-provided zeros alone would otherwise pass the existing preflight.
    assert preflight_fresh(manifest, root, required_bytes={name: 0 for name in NAMES},
                           reserve_bytes=reserve)['checks_passed']
    monkeypatch.setattr('shared.api.native_target', lambda: deepcopy(TARGET))
    assert handle(native_query(query))['error']['code'] == 'preflight_failed'
    response = handle(api_prepare_query(query))
    assert response['error']['code'] == 'fresh_prepare_failed'
    assert not root.exists()


def test_fresh_prepare_rechecks_space_after_lock_before_extraction(signed_request, monkeypatch):
    from types import SimpleNamespace
    import shared.fresh_prepare as fresh
    query, _, _ = signed_request
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: deepcopy(TARGET))
    root = Path(query['params']['install_root'])
    original = fresh._check_fresh_storage
    calls = []

    def fill_after_check(*args):
        calls.append(1)
        result = original(*args)
        if len(calls) == 1:
            monkeypatch.setattr(fresh.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
        return result

    monkeypatch.setattr(fresh, '_check_fresh_storage', fill_after_check)
    response = handle(api_prepare_query(query))
    assert response['error']['code'] == 'fresh_prepare_failed' and len(calls) == 2
    assert root.is_dir() and not (root / '.homensai-fresh.json').exists()
    assert not (root / '.homensai-control').exists()


@pytest.mark.parametrize('enough', [False, True])
def test_api_resume_before_snapshot_respects_package_space_and_reserve(signed_request, monkeypatch, enough):
    from types import SimpleNamespace
    import shared.fresh_prepare as fresh

    query, package, _ = signed_request
    monkeypatch.setattr(fresh, 'native_target', lambda: deepcopy(TARGET))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: deepcopy(TARGET))
    query = api_prepare_query(query)
    query['params']['reserve_bytes'] = 4096
    root = Path(query['params']['install_root'])

    def fail_before_snapshot(*args, **kwargs):
        raise OSError('synthetic interruption')

    with monkeypatch.context() as patch:
        patch.setattr(fresh, 'prepare_native_signed_package', fail_before_snapshot)
        assert handle(query)['error']['code'] == 'fresh_prepare_failed'
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'
    assert not (root / '.homensai-control/prepared').exists()
    minimum = package.stat().st_size * 2 + 5 * 1024 * 1024
    free = minimum + query['params']['reserve_bytes'] - (0 if enough else 1)
    monkeypatch.setattr(fresh.shutil, 'disk_usage', lambda _: SimpleNamespace(free=free))
    if not enough:
        def unexpected_extraction(*args, **kwargs):
            pytest.fail('extraction must not start below the reserved minimum')
        monkeypatch.setattr(fresh, 'prepare_native_signed_package', unexpected_extraction)
    result = handle(query)
    if enough:
        assert result['ok'] and result['result']['status'] == 'prepared'
        assert (root / 'releases/app-1/locks/demo-linux.lock').is_file()
    else:
        assert result['error']['code'] == 'fresh_prepare_failed'
        assert not (root / '.homensai-control/prepared').exists()
        assert not (root / 'releases').exists()
        assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'
