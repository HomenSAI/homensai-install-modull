"""Подписанная публикация unit-файлов; менеджер в тестах подменён."""
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import pytest

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
        data = ('test runtime ' + str(index)).encode()
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
