"""Подписанная публикация unit-файлов; менеджер в тестах подменён."""
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import pytest
from synthetic_wheel import structural_wheel

from shared import systemd_user_publish as pub
from shared.fresh_prepare import prepare_fresh_native
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.preflight import NAMES

ROOT = Path(__file__).resolve().parents[1]
TARGET = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}


@pytest.fixture
def signed_units(tmp_path, monkeypatch):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    if os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1':
        monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: dict(TARGET))
        monkeypatch.setattr('shared.package_prepare.native_target', lambda: dict(TARGET))
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [dict(TARGET)]}
    payload = tmp_path / 'payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        data = (structural_wheel() if artifact['file'].endswith('.whl')
                else ('test runtime ' + str(index)).encode())
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        artifact['sha256'] = hashlib.sha256(data).hexdigest()
    units = {}
    for component in ('web', 'bot', 'scheduler'):
        name = manifest['product_id'] + '-' + component + '.service'
        content = ('[Unit]\nDescription=Synthetic test\n[Service]\nExecStart=/usr/bin/sleep 90\n').encode()
        path = payload / 'systemd-user' / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        units[name] = content
    package = tmp_path / 'signed.zip'
    build_package(manifest, '1', payload, package)
    key = ed.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=key, expected_product_id=manifest['product_id'], expected_version='1')
    root = tmp_path / 'install'
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    prepare_fresh_native(package, envelope, root, **options,
                         required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    destination = tmp_path / 'user-units'
    destination.mkdir(mode=0o700)
    return package, envelope, root, options, destination, units


def runner_for(destination, *, occupied=None):
    def runner(argv, *, timeout, env):
        assert argv[:4] == ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
        assert set(env) == {'PATH', 'LC_ALL', 'SYSTEMD_PAGER', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'}
        if '--value' in argv:
            return str(destination) + '\n'
        name = argv[-1]
        load = 'loaded' if name == occupied else 'not-found'
        return (f'Id={name}\nLoadState={load}\nActiveState=inactive\nSubState=dead\n'
                'FragmentPath=\nDropInPaths=\nNeedDaemonReload=no\n')
    return runner


def call(data, **kwargs):
    package, envelope, root, options, destination, _ = data
    return pub.publish_signed_user_units(package, envelope, str(root), **options,
                                         modes=('autonomous',), unit_dir=str(destination), **kwargs)


@pytest.fixture
def local_directory(monkeypatch):
    # Tests run in a root container with tmp_path under /tmp; this substitutes
    # only the host-owned-directory policy, not signing, hashes or file writes.
    monkeypatch.setattr(pub, '_unit_directory', lambda path: Path(path))
    def trusted(path, expected):
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise pub.SystemdUserError('synthetic untrusted file')
    monkeypatch.setattr(pub, '_trusted_file', trusted)


def test_signed_units_plan_and_publish_without_start(signed_units, local_directory):
    destination, units = signed_units[-2:]
    def forbidden(*args, **kwargs):
        pytest.fail('planning must not call the manager')
    plan = call(signed_units, runner=forbidden)
    assert plan['state'] == 'planned' and plan['services_started'] is False
    assert not list(destination.iterdir())
    result = call(signed_units, execute=True, runner=runner_for(destination))
    assert result['state'] == 'published' and result['services_started'] is False
    assert {p.name for p in destination.iterdir()} == set(units)
    for name, data in units.items():
        assert (destination / name).read_bytes() == data
        assert (destination / name).stat().st_mode & 0o777 == 0o600


def test_existing_file_is_preserved_and_no_other_unit_is_written(signed_units, local_directory):
    destination, units = signed_units[-2:]
    existing = destination / next(iter(units))
    existing.write_text('user file')
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert existing.read_text() == 'user file'
    assert list(destination.iterdir()) == [existing]


def test_existing_manager_unit_prevents_any_write(signed_units, local_directory):
    destination, units = signed_units[-2:]
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination, occupied=next(iter(units))))
    assert not list(destination.iterdir())


def test_modified_signed_release_refuses_publication(signed_units, local_directory):
    destination, units = signed_units[-2:]
    root = signed_units[2]
    changed = root / 'releases/app-1/systemd-user' / next(iter(units))
    changed.write_text('tampered')
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert not list(destination.iterdir()) and changed.read_text() == 'tampered'


def test_interrupted_publication_leaves_marker_and_refuses_retry(signed_units, local_directory, monkeypatch):
    destination, _ = signed_units[-2:]
    original = pub._write_exclusive
    calls = []
    def interrupt(fd, name, data):
        calls.append(name)
        if len(calls) == 3:
            raise OSError('synthetic interruption')
        original(fd, name, data)
    monkeypatch.setattr(pub, '_write_exclusive', interrupt)
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    marker = destination / ('.' + signed_units[3]['expected_product_id'] + '.homensai-units.pending')
    assert marker.is_file()
    assert len(list(destination.glob('*.service'))) == 1
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    monkeypatch.setattr(pub, '_write_exclusive', original)
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert before == {p.name: p.read_bytes() for p in destination.iterdir()}


def test_nonzero_root_user_is_required_for_real_directory_guard(tmp_path):
    if os.getuid() != 0:
        pytest.skip('Root-container boundary test')
    with pytest.raises(pub.UnitPublicationError):
        pub._unit_directory(tmp_path)


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Реальный user systemd проверяется только явно')
def test_real_user_manager_accepts_signed_unit_publication(signed_units):
    import pwd

    if os.getuid() == 0:
        pytest.skip('Тест требует непривилегированного пользователя')
    package, envelope, root, options, _, units = signed_units
    destination = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.config/systemd/user'
    if not destination.is_dir():
        pytest.skip('Каталог пользовательских unit отсутствует')
    marker = destination / ('.' + options['expected_product_id'] + '.homensai-units.pending')
    expected = [destination / name for name in units]
    assert not marker.exists() and all(not path.exists() for path in expected)
    try:
        result = pub.publish_signed_user_units(package, envelope, str(root), **options,
                    modes=('autonomous',), unit_dir=str(destination), execute=True)
        assert result['state'] == 'published' and result['services_started'] is False
        for path in expected:
            assert path.read_bytes() == units[path.name]
            assert path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o777 == 0o600
        assert not marker.exists()
    finally:
        # Удаляем только собственные случайно названные файлы при полном
        # совпадении байтов и владельца. Чужие/изменённые файлы не трогаем.
        for path in expected:
            if path.exists():
                info = path.lstat()
                assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                assert path.read_bytes() == units[path.name]
                path.unlink()
        if marker.exists():
            marker_info = marker.lstat()
            assert stat.S_ISREG(marker_info.st_mode) and marker_info.st_uid == os.getuid() and marker_info.st_nlink == 1
            body = json.loads(marker.read_text())
            assert body['product_id'] == options['expected_product_id'] and body['version'] == '1'
            assert {item['name']: item['sha256'] for item in body['units']} == {
                name: hashlib.sha256(value).hexdigest() for name, value in units.items()}
            marker.unlink()


def test_unknown_manager_directory_and_runner_errors_never_write(signed_units, local_directory):
    destination = signed_units[-2]
    def wrong_manager(*args, **kwargs):
        return '/different/unit/path\n'
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=wrong_manager)
    def unavailable(*args, **kwargs):
        raise RuntimeError('SECRET-SENTINEL')
    with pytest.raises(pub.UnitPublicationError) as error:
        call(signed_units, execute=True, runner=unavailable)
    assert 'SECRET-SENTINEL' not in str(error.value)
    assert not list(destination.iterdir())


def test_wrong_publisher_key_fails_before_any_write(signed_units, local_directory):
    package, envelope, root, options, destination, _ = signed_units
    bad = {**options, 'trusted_public_key': b'x' * 32}
    with pytest.raises(pub.UnitPublicationError):
        pub.publish_signed_user_units(package, envelope, str(root), **bad,
                                      modes=('autonomous',), unit_dir=str(destination),
                                      execute=True, runner=runner_for(destination))
    assert not list(destination.iterdir())


def test_existing_symlink_is_never_followed(signed_units, local_directory, tmp_path):
    destination, units = signed_units[-2:]
    target = tmp_path / 'unrelated'
    target.write_text('preserve')
    link = destination / next(iter(units))
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('Символические ссылки недоступны')
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert link.is_symlink() and target.read_text() == 'preserve'
    assert list(destination.iterdir()) == [link]


def test_existing_unit_dropin_prevents_publication(signed_units, local_directory):
    destination, units = signed_units[-2:]
    dropin = destination / (next(iter(units)) + '.d')
    dropin.mkdir()
    (dropin / 'keep.conf').write_text('preserve')
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert (dropin / 'keep.conf').read_text() == 'preserve'
    assert not list(destination.glob('*.service'))


@pytest.mark.parametrize('lock_name', ['bootstrap', 'services'])
def test_existing_operation_lock_prevents_any_publication(signed_units, local_directory, lock_name):
    from shared.update_plan import operation_lock
    destination = signed_units[-2]
    root = signed_units[2]
    lock = root / ('.homensai-bootstrap.lock' if lock_name == 'bootstrap' else 'services/operation.lock')
    with operation_lock(lock):
        with pytest.raises(pub.UnitPublicationError):
            call(signed_units, execute=True, runner=runner_for(destination))
        assert not list(destination.iterdir())


@pytest.mark.parametrize('lock_name', ['bootstrap', 'services'])
def test_stale_lock_prevents_publication_without_erasing_evidence(signed_units, local_directory, lock_name):
    destination = signed_units[-2]
    root = signed_units[2]
    lock = root / ('.homensai-bootstrap.lock' if lock_name == 'bootstrap' else 'services/operation.lock')
    before = b'\0' + json.dumps({'version': 1, 'status': 'held', 'pid': 12345}).encode()
    lock.write_bytes(before)
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert lock.read_bytes() == before
    assert not list(destination.iterdir())


def test_unsafe_lock_permissions_refuse_publication(signed_units, local_directory):
    root, destination = signed_units[2], signed_units[-2]
    lock = root / 'services/operation.lock'
    lock.chmod(0o666)
    before = lock.read_bytes()
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert lock.stat().st_mode & 0o777 == 0o666 and lock.read_bytes() == before
    assert not list(destination.iterdir())


def test_existing_update_state_prevents_publication(signed_units, local_directory):
    root, destination = signed_units[2], signed_units[-2]
    state = root / 'services/update.json'
    state.write_text('preserve unknown update state')
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    assert state.read_text() == 'preserve unknown update state'
    assert not list(destination.iterdir())


def test_dangling_update_state_link_prevents_publication(signed_units, local_directory):
    root, destination = signed_units[2], signed_units[-2]
    state = root / 'services/update.json'
    state.symlink_to(root / 'missing')
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination), operation_id='a' * 32)
    assert state.is_symlink() and not list(destination.iterdir())


def test_idempotent_publication_rechecks_without_writing(signed_units, local_directory):
    destination, units = signed_units[-2:]
    result = call(signed_units, execute=True, runner=runner_for(destination), operation_id='a' * 32)
    receipt = signed_units[2] / '.homensai-control/user-systemd-publication.json'
    assert json.loads(receipt.read_text())['status'] == 'done'
    before = {name: ((destination / name).stat().st_ino, (destination / name).stat().st_mtime_ns)
              for name in units}
    assert call(signed_units, execute=True, runner=runner_for(destination), operation_id='a' * 32) == result
    assert before == {name: ((destination / name).stat().st_ino, (destination / name).stat().st_mtime_ns)
                      for name in units}
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination), operation_id='b' * 32)
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination))
    (destination / next(iter(units))).write_text('tampered')
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination), operation_id='a' * 32)
    assert json.loads(receipt.read_text())['status'] == 'done'


def test_interrupted_idempotent_publication_blocks_all_retries_and_load(signed_units, local_directory, monkeypatch):
    import shared.systemd_user_load as loader
    from shared.systemd_user_load import UnitLoadError
    destination = signed_units[-2]
    original = pub._write_exclusive
    writes = []
    def interrupted(fd, name, data):
        writes.append(name)
        if len(writes) == 2:
            raise OSError('synthetic interruption')
        original(fd, name, data)
    monkeypatch.setattr(pub, '_write_exclusive', interrupted)
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination), operation_id='a' * 32)
    receipt = signed_units[2] / '.homensai-control/user-systemd-publication.json'
    before = receipt.read_bytes()
    assert json.loads(before)['status'] == 'pending'
    monkeypatch.setattr(pub, '_write_exclusive', original)
    for operation in ('a' * 32, 'b' * 32, None):
        with pytest.raises(pub.UnitPublicationError):
            call(signed_units, execute=True, runner=runner_for(destination), operation_id=operation)
    assert receipt.read_bytes() == before
    monkeypatch.setattr(loader, '_unit_directory', lambda path: Path(path))
    monkeypatch.setattr(loader, '_trusted_file', pub._trusted_file)
    def forbidden(*args, **kwargs):
        pytest.fail('Pending publication must not call the manager')
    with pytest.raises(UnitLoadError):
        load_call(signed_units, execute=True, runner=forbidden)
    assert receipt.read_bytes() == before


def test_lost_completion_record_is_not_assumed_success(signed_units, local_directory, monkeypatch):
    destination, units = signed_units[-2:]
    def lost_completion(*args, **kwargs):
        raise OSError('synthetic crash after file publication')
    monkeypatch.setattr(pub, '_atomic_write', lost_completion)
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination), operation_id='c' * 32)
    receipt = signed_units[2] / '.homensai-control/user-systemd-publication.json'
    assert json.loads(receipt.read_text())['status'] == 'pending'
    assert {p.name for p in destination.iterdir()} == set(units)
    with pytest.raises(pub.UnitPublicationError):
        call(signed_units, execute=True, runner=runner_for(destination), operation_id='c' * 32)
    assert json.loads(receipt.read_text())['status'] == 'pending'


class FakeReloadManager:
    def __init__(self, directory, *, active=False, dropin=False, lose_response=False):
        self.directory = directory
        self.active = active
        self.dropin = dropin
        self.lose_response = lose_response
        self.reloaded = False
        self.calls = []

    def __call__(self, argv, *, timeout, env):
        assert argv[:4] == ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
        self.calls.append(argv[4:])
        if argv[4:] == ['daemon-reload']:
            self.reloaded = True
            if self.lose_response:
                raise TimeoutError('synthetic lost response')
            return ''
        if '--value' in argv:
            return str(self.directory) + '\n'
        name = argv[-1]
        active = 'active' if self.active else 'inactive'
        sub = 'running' if self.active else 'dead'
        dropins = '/untrusted.conf' if self.dropin else ''
        dirty = 'no' if self.reloaded else 'yes'
        return (f'Id={name}\nLoadState=loaded\nActiveState={active}\nSubState={sub}\n'
                f'FragmentPath={self.directory / name}\nDropInPaths={dropins}\nNeedDaemonReload={dirty}\n')


@pytest.fixture
def published_units(signed_units, local_directory, monkeypatch):
    import shared.systemd_user_load as loader
    import shared.systemd_user as adapter
    destination = signed_units[-2]
    call(signed_units, execute=True, runner=runner_for(destination))
    monkeypatch.setattr(loader, '_unit_directory', lambda path: Path(path))
    monkeypatch.setattr(loader, '_trusted_file', pub._trusted_file)
    monkeypatch.setattr(adapter, '_trusted_file', pub._trusted_file)
    return signed_units


def load_call(data, **kwargs):
    from shared.systemd_user_load import load_signed_user_units
    package, envelope, root, options, destination, _ = data
    return load_signed_user_units(package, envelope, str(root), **options,
                                  modes=('autonomous',), unit_dir=str(destination), **kwargs)


def test_signed_user_units_load_once_and_repeat_only_observes(published_units):
    destination = published_units[-2]
    manager = FakeReloadManager(destination)
    plan = load_call(published_units, runner=manager)
    assert plan['state'] == 'planned' and not manager.calls
    receipt = published_units[2] / '.homensai-control/user-systemd-reload.json'
    assert not receipt.exists()
    result = load_call(published_units, execute=True, runner=manager)
    assert result['state'] == 'loaded' and result['services_started'] is False
    assert json.loads(receipt.read_text())['status'] == 'done'
    assert manager.calls.count(['daemon-reload']) == 1
    assert load_call(published_units, execute=True, runner=manager) == result
    assert manager.calls.count(['daemon-reload']) == 1
    assert all(call[0] in ('show', 'daemon-reload') for call in manager.calls)


@pytest.mark.parametrize('condition', ['active', 'dropin', 'marker', 'modified_unit'])
def test_user_units_load_rejects_unsafe_state_before_reload(published_units, condition):
    from shared.systemd_user_load import UnitLoadError
    destination, units = published_units[-2:]
    manager = FakeReloadManager(destination, active=condition == 'active', dropin=condition == 'dropin')
    if condition == 'marker':
        (destination / ('.' + published_units[3]['expected_product_id'] + '.homensai-units.pending')).write_text('pending')
    elif condition == 'modified_unit':
        (destination / next(iter(units))).write_text('tampered')
    with pytest.raises(UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    assert ['daemon-reload'] not in manager.calls
    assert not (published_units[2] / '.homensai-control/user-systemd-reload.json').exists()


def test_lost_reload_response_preserves_pending_and_never_replays(published_units):
    from shared.systemd_user_load import UnitLoadError
    manager = FakeReloadManager(published_units[-2], lose_response=True)
    with pytest.raises(UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    receipt = published_units[2] / '.homensai-control/user-systemd-reload.json'
    before = receipt.read_bytes()
    assert json.loads(before)['status'] == 'pending'
    assert manager.calls.count(['daemon-reload']) == 1
    with pytest.raises(UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    assert receipt.read_bytes() == before and manager.calls.count(['daemon-reload']) == 1


def group_call(data, **kwargs):
    from shared.systemd_user_load import signed_user_group
    package, envelope, root, options, destination, _ = data
    return signed_user_group(package, envelope, str(root), **options,
                             modes=('autonomous',), unit_dir=str(destination), **kwargs)


def test_api_plans_signed_units_without_manager_or_writes(signed_units, monkeypatch):
    import base64
    import subprocess
    import sys
    from shared.api import handle

    package, envelope, root, options, destination, units = signed_units
    query = {'api_major': 1, 'request_id': 'test', 'operation': 'plan-systemd-user-units',
             'params': {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
                        'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
                        'expected_product_id': options['expected_product_id'], 'expected_version': '1',
                        'install_root': str(root), 'modes': ['autonomous']}}
    monkeypatch.setattr(pub, '_run', lambda *args, **kwargs: pytest.fail('Must not invoke systemd'))
    capabilities = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
    assert capabilities['api_minor'] >= 13 and query['operation'] in capabilities['operations']
    assert query['operation'] not in capabilities['mutating_operations']
    result = handle(query)
    assert result['ok'] and result['result']['state'] == 'planned'
    assert result['result']['manager_checked'] is False and result['result']['publication_readiness_verified'] is False
    assert {item['name']: item['sha256'] for item in result['result']['units']} == {
        name: hashlib.sha256(body).hexdigest() for name, body in units.items()}
    assert not list(destination.iterdir())
    validator = pytest.importorskip('jsonschema').Draft202012Validator(
        json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text()))
    assert not list(validator.iter_errors(query))
    cli = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], input=json.dumps(query).encode(),
                         cwd=ROOT, capture_output=True, timeout=30, check=True)
    assert json.loads(cli.stdout) == result
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    query['language'] = 'de'
    failure = handle(query)
    assert failure['error']['code'] == 'systemd_plan_failed'
    assert failure['error']['message'] == 'Die Dienstdateien konnten nicht geprüft werden'
    assert not list(destination.iterdir())
    monkeypatch.setattr('shared.api._systemd_plan_available', lambda: False)
    assert query['operation'] not in handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']['operations']
    assert handle(query)['error']['code'] == 'unsupported_operation'


def api_publish_request(data, directory):
    import base64
    package, envelope, root, options, _, _ = data
    return {'api_major': 1, 'request_id': 'test', 'operation': 'publish-systemd-user-units',
            'params': {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
                       'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
                       'expected_product_id': options['expected_product_id'], 'expected_version': '1',
                       'install_root': str(root), 'modes': ['autonomous'], 'unit_dir': str(directory),
                       'idempotency_key': 'a' * 32}}


def test_api_publish_has_key_bound_retry_and_schema(signed_units, local_directory, monkeypatch):
    from shared.api import handle
    destination = signed_units[-2]
    monkeypatch.setattr(pub, '_run', runner_for(destination))
    query = api_publish_request(signed_units, destination)
    caps = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 16 and query['operation'] in caps['mutating_operations']
    validator = pytest.importorskip('jsonschema').Draft202012Validator(
        json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text()))
    assert validator.is_valid(query)
    query['params']['verify_generated_units'] = True
    assert validator.is_valid(query)
    assert handle(query)['error']['code'] == 'systemd_publish_failed'
    assert not list(destination.iterdir())
    assert not (signed_units[2] / '.homensai-control/user-systemd-publication.json').exists()
    query['params']['verify_generated_units'] = 'true'
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'
    del query['params']['verify_generated_units']
    first = handle(query)
    assert first['ok'] and first['result']['state'] == 'published'
    assert first['result']['services_started'] is False and first['result']['installation_complete'] is False
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in destination.iterdir()}
    assert handle(query) == first
    assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in destination.iterdir()}
    query['params']['idempotency_key'] = 'b' * 32
    query['language'] = 'de'
    failure = handle(query)
    assert failure['error']['code'] == 'systemd_publish_failed'
    assert failure['error']['message'] == 'Die Veröffentlichung der Dienste konnte nicht bestätigt werden'
    assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in destination.iterdir()}
    query['params']['idempotency_key'] = 'INVALID-KEY'
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'
    monkeypatch.setattr('shared.api._systemd_plan_available', lambda: False)
    caps = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations'] and query['operation'] not in caps['mutating_operations']


@pytest.mark.parametrize('lost_response', [False, True])
def test_api_load_does_not_replay_reload(published_units, monkeypatch, lost_response):
    import shared.systemd_user_load as loader
    from shared.api import handle
    manager = FakeReloadManager(published_units[-2], lose_response=lost_response)
    monkeypatch.setattr(loader, '_run', manager)
    query = api_publish_request(published_units, published_units[-2])
    query['operation'] = 'load-systemd-user-units'
    query['params']['idempotency_key'] = 'd' * 32
    validator = pytest.importorskip('jsonschema').Draft202012Validator(
        json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text()))
    assert validator.is_valid(query)
    caps = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 15 and query['operation'] in caps['mutating_operations']
    result = handle(query)
    assert result['ok'] is not lost_response
    if lost_response:
        assert result['error']['code'] == 'systemd_load_failed'
    else:
        assert result['result']['state'] == 'loaded' and result['result']['installation_complete'] is False
    assert manager.calls.count(['daemon-reload']) == 1
    repeated = handle(query)
    assert repeated == result
    assert manager.calls.count(['daemon-reload']) == 1
    query['params']['idempotency_key'] = 'e' * 32
    query['language'] = 'de'
    failure = handle(query)
    assert failure['error']['code'] == 'systemd_load_failed'
    assert failure['error']['message'] == 'Das Laden der Dienste konnte nicht bestätigt werden'
    assert manager.calls.count(['daemon-reload']) == 1
    with pytest.raises(loader.UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    assert manager.calls.count(['daemon-reload']) == 1


@pytest.mark.parametrize('state', ['missing', 'pending'])
def test_signed_group_requires_completed_reload_without_reloading(published_units, state):
    from shared.systemd_user_load import UnitLoadError
    manager = FakeReloadManager(published_units[-2], lose_response=state == 'pending')
    if state == 'pending':
        with pytest.raises(UnitLoadError):
            load_call(published_units, execute=True, runner=manager)
    before = list(manager.calls)
    with pytest.raises(UnitLoadError):
        group_call(published_units, runner=manager)
    assert manager.calls == before


def test_signed_group_binds_published_units_and_refuses_tampering(published_units):
    from shared.systemd_user_load import UnitLoadError
    manager = FakeReloadManager(published_units[-2])
    load_call(published_units, execute=True, runner=manager)
    group = group_call(published_units, runner=manager)
    destination, units = published_units[-2:]
    assert tuple(group.order) == ('web', 'bot', 'scheduler')
    assert {adapter.path.name: adapter.digest for adapter in group.adapters.values()} == {
        name: hashlib.sha256(body).hexdigest() for name, body in units.items()}
    assert manager.calls.count(['daemon-reload']) == 1
    (destination / next(iter(units))).write_text('tampered')
    with pytest.raises(UnitLoadError):
        group_call(published_units, runner=manager)
    assert manager.calls.count(['daemon-reload']) == 1


def test_changed_receipt_fails_without_reload(published_units):
    from shared.systemd_user_load import UnitLoadError
    receipt = published_units[2] / '.homensai-control/user-systemd-reload.json'
    receipt.write_text('{"version":true,"binding":"' + '0' * 64 + '","status":"done"}')
    manager = FakeReloadManager(published_units[-2])
    with pytest.raises(UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    assert ['daemon-reload'] not in manager.calls


def test_user_units_load_refuses_stale_lock_without_changing_it(published_units):
    from shared.systemd_user_load import UnitLoadError
    root, destination = published_units[2], published_units[-2]
    lock = root / 'services/operation.lock'
    before = b'\0' + json.dumps({'version': 1, 'status': 'held', 'pid': 12345}).encode()
    lock.write_bytes(before)
    manager = FakeReloadManager(destination)
    with pytest.raises(UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    assert lock.read_bytes() == before and not manager.calls
    assert not (root / '.homensai-control/user-systemd-reload.json').exists()


def test_reload_followed_by_unexpected_active_unit_leaves_pending(published_units):
    from shared.systemd_user_load import UnitLoadError
    manager = FakeReloadManager(published_units[-2])
    original = manager.__call__
    def turns_active(argv, *, timeout, env):
        result = original(argv, timeout=timeout, env=env)
        if argv[4:] == ['daemon-reload']:
            manager.active = True
        return result
    with pytest.raises(UnitLoadError):
        load_call(published_units, execute=True, runner=turns_active)
    receipt = published_units[2] / '.homensai-control/user-systemd-reload.json'
    assert json.loads(receipt.read_text())['status'] == 'pending'
    assert manager.calls.count(['daemon-reload']) == 1


def inspect_pending_call(data, **kwargs):
    from shared.systemd_user_load import inspect_pending_user_units
    package, envelope, root, options, destination, _ = data
    return inspect_pending_user_units(package, envelope, str(root), **options,
                                      modes=('autonomous',), unit_dir=str(destination), **kwargs)


def test_inspect_pending_does_not_write_or_claim_reload_completion(published_units, monkeypatch):
    import shared.systemd_user_load as loader
    manager = FakeReloadManager(published_units[-2], lose_response=True)
    with pytest.raises(loader.UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    receipt = published_units[2] / '.homensai-control/user-systemd-reload.json'
    before = receipt.read_bytes()
    calls_before = manager.calls.count(['daemon-reload'])
    def forbidden(*args, **kwargs):
        pytest.fail('read-only inspection must not write a receipt')
    monkeypatch.setattr(loader, '_atomic_write', forbidden)
    monkeypatch.setattr(loader, '_new_receipt', forbidden)
    result = inspect_pending_call(published_units, runner=manager)
    assert result['state'] == 'pending_observed' and result['reload_completion_verified'] is False
    assert result['services_started'] is False
    assert receipt.read_bytes() == before and json.loads(before)['status'] == 'pending'
    assert manager.calls.count(['daemon-reload']) == calls_before == 1
    with pytest.raises(loader.UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    assert receipt.read_bytes() == before and manager.calls.count(['daemon-reload']) == 1


@pytest.mark.parametrize('change', ['not_loaded', 'active', 'dropin', 'modified_unit'])
def test_inspect_pending_refuses_inconsistent_state_and_preserves_receipt(published_units, change):
    from shared.systemd_user_load import UnitLoadError
    manager = FakeReloadManager(published_units[-2], lose_response=True)
    with pytest.raises(UnitLoadError):
        load_call(published_units, execute=True, runner=manager)
    receipt = published_units[2] / '.homensai-control/user-systemd-reload.json'
    before = receipt.read_bytes()
    if change == 'not_loaded':
        manager.reloaded = False
    elif change == 'active':
        manager.active = True
    elif change == 'dropin':
        manager.dropin = True
    else:
        name = next(iter(published_units[-1]))
        (published_units[-2] / name).write_text('tampered')
    with pytest.raises(UnitLoadError):
        inspect_pending_call(published_units, runner=manager)
    assert receipt.read_bytes() == before and manager.calls.count(['daemon-reload']) == 1


def test_inspect_pending_requires_matching_pending_receipt(published_units):
    from shared.systemd_user_load import UnitLoadError
    manager = FakeReloadManager(published_units[-2])
    with pytest.raises(UnitLoadError):
        inspect_pending_call(published_units, runner=manager)
    assert not manager.calls
    assert load_call(published_units, execute=True, runner=manager)['state'] == 'loaded'
    previous = list(manager.calls)
    with pytest.raises(UnitLoadError):
        inspect_pending_call(published_units, runner=manager)
    assert manager.calls == previous


def test_inspect_pending_rejects_unbound_receipt_without_manager_call(published_units):
    from shared.systemd_user_load import UnitLoadError
    receipt = published_units[2] / '.homensai-control/user-systemd-reload.json'
    before = json.dumps({'version': 1, 'binding': '0' * 64, 'status': 'pending'}).encode()
    receipt.write_bytes(before)
    manager = FakeReloadManager(published_units[-2])
    with pytest.raises(UnitLoadError):
        inspect_pending_call(published_units, runner=manager)
    assert receipt.read_bytes() == before and not manager.calls


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Требуется явно созданный изолированный systemd-контейнер')
@pytest.mark.parametrize('exercise_lifecycle', [False, True], ids=['reload-only', 'group-lifecycle'])
def test_isolated_manager_reloads_signed_units_without_start(signed_units, exercise_lifecycle, monkeypatch):
    import pwd
    import subprocess
    import sys
    from shared.api import handle
    import shared.systemd_user_load as loader
    from shared.systemd_user_load import load_signed_user_units, signed_user_group

    identity = pwd.getpwuid(os.getuid())
    if (os.getuid() == 0 or identity.pw_name != 'homenstest'
            or identity.pw_dir != '/home/homenstest' or not Path('/run/.containerenv').is_file()):
        pytest.skip('Защитные условия изолированной тестовой среды не выполнены')
    package, envelope, root, options, _, units = signed_units
    destination = Path(identity.pw_dir) / '.config/systemd/user'
    assert destination.is_dir()
    marker = destination / ('.' + options['expected_product_id'] + '.homensai-units.pending')
    expected = [destination / name for name in units]
    assert not marker.exists() and all(not path.exists() for path in expected)
    calls = []

    def actual_runner(argv, *, timeout, env):
        calls.append(argv[4:])
        return pub._run(argv, timeout=timeout, env=env)

    try:
        if exercise_lifecycle:
            query = api_publish_request(signed_units, destination)
            response = handle(query)
            assert response['ok'] and response['result']['installation_complete'] is False
            published = {key: value for key, value in response['result'].items() if key != 'installation_complete'}
        else:
            published = pub.publish_signed_user_units(package, envelope, str(root), **options,
                          modes=('autonomous',), unit_dir=str(destination), execute=True,
                          operation_id='a' * 32, runner=actual_runner)
        assert published['state'] == 'published' and published['services_started'] is False
        before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in expected}
        if exercise_lifecycle:
            cli = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                                 input=json.dumps(query).encode(), capture_output=True, timeout=60, check=True)
            assert json.loads(cli.stdout) == response
            repeated = published
        else:
            repeated = pub.publish_signed_user_units(package, envelope, str(root), **options,
                         modes=('autonomous',), unit_dir=str(destination), execute=True,
                         operation_id='a' * 32, runner=actual_runner)
        assert repeated == published
        assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in expected}
        if exercise_lifecycle:
            monkeypatch.setattr(loader, '_run', actual_runner)
            query['operation'] = 'load-systemd-user-units'
            conflict = handle(query)
            assert conflict['error']['code'] == 'systemd_load_failed'
            assert ['daemon-reload'] not in calls
            query['params']['idempotency_key'] = 'd' * 32
            load_response = handle(query)
            assert load_response['ok'] and load_response['result']['installation_complete'] is False
            loaded = {key: value for key, value in load_response['result'].items() if key != 'installation_complete'}
        else:
            loaded = load_signed_user_units(package, envelope, str(root), **options,
                         modes=('autonomous',), unit_dir=str(destination), execute=True, runner=actual_runner)
        assert loaded['state'] == 'loaded' and loaded['services_started'] is False
        assert calls.count(['daemon-reload']) == 1
        assert json.loads((root / '.homensai-control/user-systemd-reload.json').read_text())['status'] == 'done'
        if exercise_lifecycle:
            cli = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                                 input=json.dumps(query).encode(), capture_output=True, timeout=60, check=True)
            assert json.loads(cli.stdout) == load_response
        else:
            assert load_signed_user_units(package, envelope, str(root), **options,
                       modes=('autonomous',), unit_dir=str(destination), execute=True, runner=actual_runner) == loaded
        assert calls.count(['daemon-reload']) == 1
        assert all(path.read_bytes() == units[path.name] for path in expected)
        if exercise_lifecycle:
            group = signed_user_group(package, envelope, str(root), **options,
                                      modes=('autonomous',), unit_dir=str(destination),
                                      operation_id='d' * 32, runner=actual_runner)
            assert calls.count(['daemon-reload']) == 1
            assert [status['active_state'] for status in group.command('start', execute=True)] == ['active'] * 3
            assert [status['active_state'] for status in group.command('stop', execute=True)] == ['inactive'] * 3
    finally:
        if exercise_lifecycle:
            # Only random unit names created by this test, inside the gated manager.
            subprocess.run(['/usr/bin/systemctl', '--user', 'stop', '--', *[p.name for p in expected]],
                           env=pub._manager_environment(), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30, check=False)
        for path in expected:
            if path.exists():
                info = path.lstat()
                assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                assert path.read_bytes() == units[path.name]
                path.unlink()
        if marker.exists():
            info = marker.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
            body = json.loads(marker.read_text())
            assert body['product_id'] == options['expected_product_id'] and body['version'] == '1'
            assert {item['name']: item['sha256'] for item in body['units']} == {
                name: hashlib.sha256(value).hexdigest() for name, value in units.items()}
            marker.unlink()
        # This reload is confined to the disposable, explicitly gated manager.
        env = pub._manager_environment()
        subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
