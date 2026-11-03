"""Проверка переносимого рецепта: только синтетические подписанные выпуски."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import pytest
from synthetic_wheel import structural_wheel

from shared.fresh_prepare import prepare_fresh_native
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package, verify_signed_package
from shared.preflight import NAMES
from shared.systemd_portable import (RECIPE_PATH, RECIPE_V1, PortableUnitError,
                                      plan_portable_signed_user_units, publish_portable_signed_user_units)
from shared.systemd_user_load import (UnitLoadError, load_portable_signed_user_units,
                                      inspect_pending_portable_user_units, portable_signed_user_group)
from shared import systemd_user_publish as pub

ROOT = Path(__file__).resolve().parents[1]
TARGET = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}


@pytest.fixture
def portable_release(tmp_path, monkeypatch):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    if os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1':
        monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: dict(TARGET))
        monkeypatch.setattr('shared.package_prepare.native_target', lambda: dict(TARGET))
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [dict(TARGET)]}
    manifest['secrets'] = []
    for component in manifest['components']:
        component['cmd'] = ['@runtime/bin/python', '-B', '@release/demo/main.py', '--data-dir', '@data']
    payload = tmp_path / 'payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        data = (structural_wheel() if artifact['file'].endswith('.whl')
                else ('synthetic runtime ' + str(index)).encode())
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        artifact['sha256'] = hashlib.sha256(data).hexdigest()
    recipe = payload / RECIPE_PATH
    recipe.parent.mkdir()
    recipe.write_bytes(RECIPE_V1)
    (payload / 'demo').mkdir()
    (payload / 'demo/main.py').write_text('# synthetic signed application\n')
    package = tmp_path / 'portable.zip'
    build_package(manifest, '1', payload, package)
    key = ed.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=key, expected_product_id=manifest['product_id'], expected_version='1')
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    roots = [tmp_path / 'first root', tmp_path / 'второй % root']
    for root in roots:
        prepare_fresh_native(package, envelope, root, **options,
                             required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    return package, envelope, options, roots


def test_one_signed_recipe_derives_distinct_units_for_two_roots(portable_release):
    package, envelope, options, roots = portable_release
    original_package = hashlib.sha256(package.read_bytes()).hexdigest()
    plans = [plan_portable_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))
             for root in roots]
    name = options['expected_product_id'] + '-web.service'
    assert all(plan['state'] == 'planned' and plan['services_started'] is False
               and plan['publication_readiness_verified'] is False for plan in plans)
    assert all(plan['generator'] == 'homensai-systemd-user-portable-v1' for plan in plans)
    assert str(roots[0]) in plans[0]['unit_texts'][name]
    assert str(roots[1]).replace('%', '%%') in plans[1]['unit_texts'][name]
    assert plans[0]['units'][0]['sha256'] != plans[1]['units'][0]['sha256']
    assert hashlib.sha256(package.read_bytes()).hexdigest() == original_package
    for root in roots:
        assert not (root / '.homensai-control/user-systemd-publication.json').exists()
        assert not (root / '.homensai-control/user-systemd-reload.json').exists()


def test_portable_plan_refuses_wrong_key_and_modified_recipe(portable_release):
    package, envelope, options, roots = portable_release
    with pytest.raises(PortableUnitError):
        plan_portable_signed_user_units(package, envelope, str(roots[0]),
            **{**options, 'trusted_public_key': b'x' * 32}, modes=('autonomous',))
    recipe = roots[0] / 'releases/app-1' / RECIPE_PATH
    recipe.write_bytes(RECIPE_V1 + b'changed')
    with pytest.raises(PortableUnitError):
        plan_portable_signed_user_units(package, envelope, str(roots[0]), **options, modes=('autonomous',))
    assert recipe.read_bytes().endswith(b'changed')
    assert plan_portable_signed_user_units(package, envelope, str(roots[1]),
                                           **options, modes=('autonomous',))['state'] == 'planned'


@pytest.mark.parametrize('change', ['unknown_recipe', 'ambiguous_units'])
def test_portable_plan_rejects_signed_unsupported_or_ambiguous_recipe(portable_release, tmp_path, change):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    package, envelope, options, _ = portable_release
    metadata = verify_signed_package(package, envelope, **options)
    payload = tmp_path / 'payload'
    if change == 'unknown_recipe':
        (payload / RECIPE_PATH).write_bytes(RECIPE_V1.replace(b'portable-v1', b'portable-v2'))
    else:
        (payload / 'systemd-user/other.service').write_text('[Service]\nExecStart=/bin/false\n')
    other_package = tmp_path / 'other.zip'
    build_package(metadata['manifest'], '1', payload, other_package)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    other_options = {**options, 'trusted_public_key': public}
    other_envelope = sign_package(other_package, private_key=private,
        expected_product_id=options['expected_product_id'], expected_version='1')
    root = tmp_path / 'other-root'
    prepare_fresh_native(other_package, other_envelope, root, **other_options,
                         required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    with pytest.raises(PortableUnitError):
        plan_portable_signed_user_units(other_package, other_envelope, str(root),
                                        **other_options, modes=('autonomous',))
    assert not (root / '.homensai-control/user-systemd-publication.json').exists()


def fake_manager(directory):
    calls = []
    def run(argv, *, timeout, env):
        calls.append(argv[4:])
        if '--value' in argv:
            return str(directory) + '\n'
        name = argv[-1]
        return (f'Id={name}\nLoadState=not-found\nActiveState=inactive\nSubState=dead\n'
                'FragmentPath=\nDropInPaths=\nNeedDaemonReload=no\n')
    return run, calls


@pytest.fixture
def portable_directory(tmp_path, monkeypatch):
    import shared.systemd_portable as portable
    import shared.systemd_user as adapter
    import shared.systemd_user_load as loader
    directory = tmp_path / 'user-units'
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(portable, '_unit_directory', lambda path: Path(path))
    monkeypatch.setattr(loader, '_unit_directory', lambda path: Path(path))
    def trusted(path, expected):
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise pub.SystemdUserError('synthetic untrusted file')
    monkeypatch.setattr(pub, '_trusted_file', trusted)
    monkeypatch.setattr(loader, '_trusted_file', trusted)
    monkeypatch.setattr(adapter, '_trusted_file', trusted)
    return directory


def portable_publish(data, directory, **kwargs):
    package, envelope, options, roots = data
    return publish_portable_signed_user_units(package, envelope, str(roots[0]), **options,
                                              modes=('autonomous',), unit_dir=str(directory), **kwargs)


def portable_load(data, directory, **kwargs):
    package, envelope, options, roots = data
    return load_portable_signed_user_units(package, envelope, str(roots[0]), **options,
                                            modes=('autonomous',), unit_dir=str(directory), **kwargs)


def portable_api_request(data, directory, operation, key=None):
    package, envelope, options, roots = data
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
              'expected_product_id': options['expected_product_id'], 'expected_version': '1',
              'install_root': str(roots[0]), 'modes': ['autonomous']}
    if key is not None:
        params.update(unit_dir=str(directory), idempotency_key=key)
    return {'api_major': 1, 'request_id': 'test', 'operation': operation, 'params': params}


class FakeReloadManager:
    def __init__(self, directory, *, lose_response=False):
        self.directory, self.lose_response, self.reloaded, self.calls = directory, lose_response, False, []

    def __call__(self, argv, *, timeout, env):
        self.calls.append(argv[4:])
        if argv[4:] == ['daemon-reload']:
            self.reloaded = True
            if self.lose_response:
                raise TimeoutError('synthetic lost response')
            return ''
        if '--value' in argv:
            return str(self.directory) + '\n'
        name = argv[-1]
        return (f'Id={name}\nLoadState=loaded\nActiveState=inactive\nSubState=dead\n'
                f'FragmentPath={self.directory / name}\nDropInPaths=\n'
                f'NeedDaemonReload={"no" if self.reloaded else "yes"}\n')


def test_portable_api_plan_publish_load_and_retries(portable_release, portable_directory, monkeypatch):
    import shared.systemd_portable as portable
    import shared.systemd_user_load as loader
    from shared.api import handle
    publisher, calls = fake_manager(portable_directory)
    monkeypatch.setattr(portable, '_run', publisher)
    query = portable_api_request(portable_release, portable_directory, 'plan-portable-systemd-user-units')
    validator = pytest.importorskip('jsonschema').Draft202012Validator(
        json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text()))
    assert validator.is_valid(query)
    caps = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 17 and query['operation'] in caps['operations']
    assert query['operation'] not in caps['mutating_operations']
    result = handle(query)
    assert result['ok'] and result['result']['state'] == 'planned' and 'unit_texts' not in result['result']
    assert not calls and not list(portable_directory.iterdir())
    query = portable_api_request(portable_release, portable_directory, 'publish-portable-systemd-user-units', 'a' * 32)
    assert validator.is_valid(query) and query['operation'] in caps['mutating_operations']
    published = handle(query)
    assert published['ok'] and published['result']['state'] == 'published'
    assert handle(query) == published
    query['params']['verify_generated_units'] = True
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'
    manager = FakeReloadManager(portable_directory)
    monkeypatch.setattr(loader, '_run', manager)
    query = portable_api_request(portable_release, portable_directory, 'load-portable-systemd-user-units', 'b' * 32)
    assert validator.is_valid(query) and query['operation'] in caps['mutating_operations']
    loaded = handle(query)
    assert loaded['ok'] and loaded['result']['state'] == 'loaded'
    assert handle(query) == loaded and manager.calls.count(['daemon-reload']) == 1
    query['params']['idempotency_key'] = 'c' * 32
    assert handle(query)['error']['code'] == 'systemd_load_failed'
    assert manager.calls.count(['daemon-reload']) == 1
    monkeypatch.setattr('shared.api._systemd_plan_available', lambda: False)
    caps = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations'] and query['operation'] not in caps['mutating_operations']


def test_portable_loading_requires_bound_publication(portable_release, portable_directory):
    manager = FakeReloadManager(portable_directory)
    with pytest.raises(UnitLoadError):
        portable_load(portable_release, portable_directory, execute=True, operation_id='b' * 32, runner=manager)
    assert not manager.calls
    publisher, _ = fake_manager(portable_directory)
    portable_publish(portable_release, portable_directory, execute=True, operation_id='a' * 32, runner=publisher)
    receipt = portable_release[3][0] / '.homensai-control/user-systemd-publication.json'
    record = json.loads(receipt.read_text())
    record['binding'] = '0' * 64
    receipt.write_text(json.dumps(record))
    with pytest.raises(UnitLoadError):
        portable_load(portable_release, portable_directory, execute=True, operation_id='b' * 32, runner=manager)
    assert not manager.calls


@pytest.mark.parametrize('lost_response', [False, True])
def test_portable_loading_never_replays_reload(portable_release, portable_directory, lost_response):
    publisher, _ = fake_manager(portable_directory)
    portable_publish(portable_release, portable_directory, execute=True, operation_id='a' * 32, runner=publisher)
    manager = FakeReloadManager(portable_directory, lose_response=lost_response)
    args = dict(execute=True, operation_id='b' * 32, runner=manager)
    if lost_response:
        with pytest.raises(UnitLoadError):
            portable_load(portable_release, portable_directory, **args)
        receipt = portable_release[3][0] / '.homensai-control/user-systemd-reload.json'
        before = receipt.read_bytes()
        with pytest.raises(UnitLoadError):
            portable_load(portable_release, portable_directory, **args)
        package, envelope, options, roots = portable_release
        observed = inspect_pending_portable_user_units(package, envelope, str(roots[0]), **options,
                       modes=('autonomous',), unit_dir=str(portable_directory), operation_id='b' * 32, runner=manager)
        assert observed['state'] == 'pending_observed' and observed['reload_completion_verified'] is False
        assert receipt.read_bytes() == before
    else:
        loaded = portable_load(portable_release, portable_directory, **args)
        assert loaded['state'] == 'loaded' and loaded['services_started'] is False
        assert portable_load(portable_release, portable_directory, **args) == loaded
        package, envelope, options, roots = portable_release
        group = portable_signed_user_group(package, envelope, str(roots[0]), **options,
                    modes=('autonomous',), unit_dir=str(portable_directory), operation_id='b' * 32, runner=manager)
        assert tuple(group.order) == ('web', 'bot', 'scheduler')
    assert manager.calls.count(['daemon-reload']) == 1
    with pytest.raises(UnitLoadError):
        portable_load(portable_release, portable_directory, execute=True, operation_id='c' * 32, runner=manager)
    assert manager.calls.count(['daemon-reload']) == 1


def test_portable_publication_repeats_without_rewrite_and_rejects_conflict(portable_release, portable_directory):
    manager, calls = fake_manager(portable_directory)
    plan = portable_publish(portable_release, portable_directory, runner=manager)
    assert plan['state'] == 'planned' and not calls and not list(portable_directory.iterdir())
    first = portable_publish(portable_release, portable_directory, execute=True, operation_id='a' * 32, runner=manager)
    receipt = portable_release[3][0] / '.homensai-control/user-systemd-publication.json'
    assert json.loads(receipt.read_text())['status'] == 'done'
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in portable_directory.iterdir()}
    assert portable_publish(portable_release, portable_directory, execute=True,
                            operation_id='a' * 32, runner=manager) == first
    assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in portable_directory.iterdir()}
    with pytest.raises(PortableUnitError):
        portable_publish(portable_release, portable_directory, execute=True, operation_id='b' * 32, runner=manager)
    assert all(call[0] == 'show' for call in calls)
    (portable_directory / first['units'][0]['name']).write_text('tampered')
    with pytest.raises(PortableUnitError):
        portable_publish(portable_release, portable_directory, execute=True, operation_id='a' * 32, runner=manager)
    assert json.loads(receipt.read_text())['status'] == 'done'


def test_portable_publication_interruption_preserves_pending(portable_release, portable_directory, monkeypatch):
    manager, _ = fake_manager(portable_directory)
    original = pub._write_exclusive
    writes = []
    def fail_during_write(fd, name, data):
        writes.append(name)
        if len(writes) == 2:
            raise OSError('synthetic interruption')
        original(fd, name, data)
    monkeypatch.setattr(pub, '_write_exclusive', fail_during_write)
    with pytest.raises(PortableUnitError):
        portable_publish(portable_release, portable_directory, execute=True, operation_id='a' * 32, runner=manager)
    receipt = portable_release[3][0] / '.homensai-control/user-systemd-publication.json'
    before = receipt.read_bytes()
    assert json.loads(before)['status'] == 'pending'
    monkeypatch.setattr(pub, '_write_exclusive', original)
    with pytest.raises(PortableUnitError):
        portable_publish(portable_release, portable_directory, execute=True, operation_id='a' * 32, runner=manager)
    assert receipt.read_bytes() == before


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен отдельный тестовый user-systemd')
def test_portable_publication_with_isolated_real_user_manager(portable_release, monkeypatch):
    import pwd
    import subprocess
    import sys
    import shared.systemd_portable as portable
    import shared.systemd_user_load as loader
    from shared.api import handle
    identity = pwd.getpwuid(os.getuid())
    if (os.getuid() == 0 or identity.pw_name != 'homenstest'
            or identity.pw_dir != '/home/homenstest' or not Path('/run/.containerenv').is_file()):
        pytest.skip('Защитные условия изолированной среды не выполнены')
    package, envelope, options, roots = portable_release
    directory = Path(identity.pw_dir) / '.config/systemd/user'
    plan = plan_portable_signed_user_units(package, envelope, str(roots[0]), **options, modes=('autonomous',))
    expected = {directory / name: text.encode('utf-8') for name, text in plan['unit_texts'].items()}
    marker = directory / ('.' + options['expected_product_id'] + '.homensai-units.pending')
    assert directory.is_dir() and not marker.exists() and all(not p.exists() for p in expected)
    calls = []
    def actual(argv, *, timeout, env):
        calls.append(argv[4:])
        return pub._run(argv, timeout=timeout, env=env)
    monkeypatch.setattr(portable, '_run', actual)
    monkeypatch.setattr(loader, '_run', actual)
    try:
        query = portable_api_request(portable_release, directory, 'publish-portable-systemd-user-units', 'a' * 32)
        response = handle(query)
        assert response['ok'] and response['result']['installation_complete'] is False
        result = response['result']
        before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in expected}
        cli = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                             input=json.dumps(query).encode(), capture_output=True, timeout=90, check=True)
        assert json.loads(cli.stdout) == response
        assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in expected}
        assert result['state'] == 'published' and result['services_started'] is False
        assert all(call[0] == 'show' for call in calls)
        assert all(p.read_bytes() == body for p, body in expected.items())
        query = portable_api_request(portable_release, directory, 'load-portable-systemd-user-units', 'b' * 32)
        loaded = handle(query)
        assert loaded['ok'] and loaded['result']['state'] == 'loaded' and loaded['result']['services_started'] is False
        cli = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                             input=json.dumps(query).encode(), capture_output=True, timeout=90, check=True)
        assert json.loads(cli.stdout) == loaded
        assert calls.count(['daemon-reload']) == 1
        group = portable_signed_user_group(package, envelope, str(roots[0]), **options,
                    modes=('autonomous',), unit_dir=str(directory), operation_id='b' * 32, runner=actual)
        assert tuple(group.order) == ('web', 'bot', 'scheduler') and calls.count(['daemon-reload']) == 1
    finally:
        for path, body in expected.items():
            if path.exists():
                info = path.lstat()
                assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                assert path.read_bytes() == body
                path.unlink()
        if marker.exists():
            info = marker.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
            body = json.loads(marker.read_text())
            assert body['product_id'] == options['expected_product_id'] and body['version'] == '1'
            assert {item['name']: item['sha256'] for item in body['units']} == {
                path.name: hashlib.sha256(data).hexdigest() for path, data in expected.items()}
            marker.unlink()
        env = pub._manager_environment()
        subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
