"""Логика установщика комплекта с фиктивным клиентом API (без systemd)."""
import base64
import hashlib
import json
from pathlib import Path
import stat

import pytest

from shared import kit_setup
from shared.kit_setup import Kit, Setup, SetupError


def last(client, operation):
    return [call for call in client.calls if call[0] == operation][-1]


class FakeClient:
    def __init__(self):
        self.calls, self.fail = [], set()
        self.results = {'uninstall-portable-linux': {'state': 'uninstalled'},
                        'inspect-started-pointer-update': {'state': {'outcome': 'running', 'direction': 'forward',
                                                                     'transaction': 't'}}}

    def call(self, operation, params, *, min_minor=0, language=None, secret_env_name=None):
        from homensai_installer_client import InstallerClientError
        self.calls.append((operation, json.loads(json.dumps(params)), secret_env_name))
        if operation in self.fail:
            self.fail.discard(operation)
            raise InstallerClientError('transport_error', 'synthetic')
        if operation == 'check-local-health':
            return {'health_response_matches': True}
        return self.results.get(operation, {'state': 'recorded_complete'})


MIGRATIONS = {}
SIGNED = {}


def make_kit(base: Path, version='1', migrations=('m-1',), key=b'k' * 32) -> Path:
    MIGRATIONS[version] = list(migrations)
    kit = base / ('kit-' + version)
    (kit / 'package').mkdir(parents=True)
    (kit / 'installer/app').mkdir(parents=True)
    (kit / 'installer/linux-x86_64/python/bin').mkdir(parents=True)
    package = kit / 'package' / f'demo-{version}.zip'
    package.write_bytes(b'package ' + version.encode())
    (kit / 'package' / f'demo-{version}.sig').write_bytes(b'{"signature": "synthetic"}')
    (kit / 'package/publisher-ed25519.pub').write_bytes(key)
    for name in ('setup.sh', 'README.txt'):
        (kit / name).write_text('synthetic')
    meta = {'format': 1, 'product_id': 'demo', 'product_name': 'Demo', 'version': version,
            'package': f'package/demo-{version}.zip', 'envelope': f'package/demo-{version}.sig',
            'public_key': 'package/publisher-ed25519.pub', 'public_key_sha256': hashlib.sha256(key).hexdigest(),
            'package_sha256': hashlib.sha256(package.read_bytes()).hexdigest(), 'modes': ['default'],
            'all_modes': ['default'], 'migrations': list(migrations),
            'required_bytes': {name: 0 for name in ('app', 'releases', 'data', 'backup', 'services', 'config', 'runtime')},
            'reserve_bytes': 4096, 'paths': {'runtime': 'runtime'},
            'web': {'host': '127.0.0.1', 'port': 18431, 'healthz': '/healthz'}}
    (kit / 'kit.json').write_text(json.dumps(meta))
    SIGNED[version] = signed(kit, meta, version)
    return kit


def signed(kit: Path, meta: dict, version: str) -> dict:
    """Подписанные сведения синтетического выпуска: kit.json, файлы установщика, порядок."""
    from shared.kit import UNSIGNED_META, installer_files
    return {'kit': {name: value for name, value in meta.items() if name not in UNSIGNED_META},
            'installer': installer_files(kit), 'sequence': [int(version)]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    monkeypatch.delenv(kit_setup.PASSWORD_ENV, raising=False)
    (tmp_path / 'home').mkdir()
    monkeypatch.setattr(kit_setup, 'signed_release_info',
                        lambda package, envelope, key, product, version: {'migrations': MIGRATIONS[version],
                                                                          **SIGNED[version]})
    client = FakeClient()

    def setup(kit, *, yes=True, answers=(), secrets=()):
        answers, secrets = list(answers), list(secrets)
        instance = Setup(Kit(kit), language='en', state_dir=None, yes=yes, out=lambda *_: None,
                         ask=lambda prompt: answers.pop(0), ask_secret=lambda prompt: secrets.pop(0), client=client)
        files = {'1': [], '2': [], '3': []}

        def manifest(package, envelope, version, key=None):
            files[version] = files[version] or [{'path': 'runtime-linux/bin/python', 'sha256': 'a' * 64}]
            return {'manifest': {}, 'files': files[version]}
        instance.manifest = manifest
        instance.units = lambda manifest, modes: ['demo-web.service']
        return instance
    return tmp_path, client, setup


def install_root(base):
    return base / 'home/.local/share/homensai/demo'


def test_install_persists_keys_before_call_and_resumes(env):
    base, client, setup = env
    kit = make_kit(base)
    client.fail.add('install-portable-linux')
    with pytest.raises(SetupError) as failure:
        setup(kit).install()
    assert failure.value.key == 'kit.api_failed'
    state_file = base / 'home/.local/state/homensai/demo/state.json'
    pending = json.loads(state_file.read_text())['install']
    first = client.calls[-1][1]
    assert first['workflow_key'] == pending['workflow_key'] and first['keys'] == pending['keys']
    assert first['install_root'] == str(install_root(base)) and first['modes'] == ['default']
    assert Path(first['package_path']).is_relative_to(base / 'home/.local/state/homensai/demo/packages/1')
    assert (state_file.stat().st_mode & 0o777) == 0o600
    assert setup(kit).install() == 0
    second = [call for call in client.calls if call[0] == 'install-portable-linux'][-1][1]
    assert second['workflow_key'] == first['workflow_key'] and second['keys'] == first['keys']
    state = json.loads(state_file.read_text())
    assert state['status'] == 'installed' and state['version'] == '1' and 'install' not in state
    assert state['applied_migrations'] == ['m-1'] and state['group']['load_key'] == first['keys']['load']
    stored = base / 'home/.local/state/homensai/demo/installer'
    assert (stored / 'setup.sh').is_file()
    assert Kit(stored).package.read_bytes() == b'package 1'
    with pytest.raises(SetupError) as again:
        setup(kit).install()
    assert again.value.key == 'kit.already_installed'


def test_install_requires_confirmation_and_fresh_root(env):
    base, client, setup = env
    kit = make_kit(base)
    with pytest.raises(SetupError) as cancelled:
        setup(kit, yes=False, answers=['no']).install()
    assert cancelled.value.key == 'kit.cancelled' and not client.calls
    existing = base / 'existing'
    existing.mkdir()
    with pytest.raises(SetupError) as exists:
        setup(kit).install(root=str(existing))
    assert exists.value.key == 'kit.root_exists'
    with pytest.raises(SetupError) as mismatch:
        setup(kit).install(expect_key='0' * 64)
    assert mismatch.value.key == 'kit.key_mismatch'
    with pytest.raises(SetupError) as mode:
        setup(kit).install(modes=['other'])
    assert mode.value.key == 'kit.unknown_mode'


def test_update_flow_password_keys_and_rollback(env, monkeypatch):
    base, client, setup = env
    setup(make_kit(base)).install()
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    with pytest.raises(SetupError) as missing:
        setup(kit2).update()
    assert missing.value.key == 'kit.password_required'
    with pytest.raises(SetupError) as short:
        setup(kit2, yes=False, answers=['yes'], secrets=['short', 'short']).update()
    assert short.value.key == 'kit.password_short'
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    assert setup(kit2).update() == 0
    operation, params, secret = last(client, 'run-pointer-linux-update')
    assert secret == 'HOMENSAI_SECRET_ARCHIVE_PASSWORD'
    assert params['migration_ids'] == ['m-2'] and params['action'] == 'start'
    assert params['old_version'] == '1' and params['new_version'] == '2'
    install = [call for call in client.calls if call[0] == 'install-portable-linux'][0][1]
    assert params['runtime_reserve_bytes'] == install['reserve_bytes']
    assert params['interpreter'].endswith('/runtime/bin/python') and params['interpreter_sha256'] == 'a' * 64
    assert 'synthetic-password' not in json.dumps(params)
    assert kit_setup._SECRET_ENV not in __import__('os').environ
    state = json.loads((base / 'home/.local/state/homensai/demo/state.json').read_text())
    assert state['version'] == '2' and state['applied_migrations'] == ['m-1', 'm-2']
    # Консолидация: удаление пакетом базовой версии 1 и переустановка версии 2 на тех же данных.
    removal = last(client, 'uninstall-portable-linux')[1]
    assert removal['expected_version'] == '1' and removal['data_policy'] == 'keep'
    reinstall = last(client, 'install-portable-linux')[1]
    assert reinstall['expected_version'] == '2' and reinstall['reuse_preserved_data'] is True
    assert state['base'] == '2' and state['status'] == 'installed' and 'consolidate' not in state
    assert state['group']['load_key'] == reinstall['keys']['load']
    with pytest.raises(SetupError) as same:
        setup(kit2).update()
    assert same.value.key == 'kit.same_version'
    kit3 = make_kit(base, '3', ('m-1', 'm-2', 'm-3'))
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'rolled_back', 'transaction': 'u'}}
    assert setup(kit3).update() == 4
    state = json.loads((base / 'home/.local/state/homensai/demo/state.json').read_text())
    assert state['version'] == '2' and state['status'] == 'installed' and 'update' not in state
    other_key = make_kit(base / 'other', '3', ('m-1', 'm-2', 'm-3'), key=b'x' * 32)
    with pytest.raises(SetupError) as changed:
        setup(other_key).update()
    assert changed.value.key == 'kit.key_changed'
    diverged = make_kit(base / 'div', '4', ('m-9',))
    with pytest.raises(SetupError) as history:
        setup(diverged).update()
    assert history.value.key == 'kit.migrations_diverged'


def test_interrupted_update_reopens_with_same_key(env, monkeypatch):
    base, client, setup = env
    setup(make_kit(base)).install()
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    client.fail.add('run-pointer-linux-update')
    with pytest.raises(SetupError):
        setup(kit2).update()
    first = last(client, 'run-pointer-linux-update')[1]
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    assert setup(kit2).update() == 0
    second = last(client, 'run-pointer-linux-update')[1]
    assert second['idempotency_key'] == first['idempotency_key'] and second['action'] == 'reopen'


def test_runtime_change_is_rejected_before_any_effect(env, monkeypatch):
    base, client, setup = env
    setup(make_kit(base)).install()
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    instance = setup(make_kit(base, '2', ('m-1',)))
    original = instance.manifest
    instance.manifest = lambda package, envelope, version, key=None: (
        {'manifest': {}, 'files': [{'path': 'runtime-linux/bin/python', 'sha256': 'b' * 64}]}
        if version == '2' else original(package, envelope, version, key))
    calls = len(client.calls)
    with pytest.raises(SetupError) as failure:
        instance.update()
    assert failure.value.key == 'kit.runtime_changed' and len(client.calls) == calls


def test_uninstall_keep_reinstall_and_purge(env):
    base, client, setup = env
    kit = make_kit(base)
    setup(kit).install()
    assert setup(kit).uninstall() == 0
    removal = last(client, 'uninstall-portable-linux')[1]
    assert removal['data_policy'] == 'keep' and 'confirm_product_id' not in removal
    state = json.loads((base / 'home/.local/state/homensai/demo/state.json').read_text())
    assert state['status'] == 'uninstalled'
    install_root(base).mkdir(parents=True)  # данные сохранены удалением
    with pytest.raises(SetupError) as wrong:
        setup(make_kit(base / 'v2', '2')).install()
    assert wrong.value.key == 'kit.reinstall_version'
    assert setup(kit).install() == 0
    install = [call for call in client.calls if call[0] == 'install-portable-linux'][-1][1]
    assert install['reuse_preserved_data'] is True
    with pytest.raises(SetupError) as cancelled:
        setup(kit, yes=False, answers=['wrong']).uninstall(purge=True)
    assert cancelled.value.key == 'kit.cancelled'
    assert setup(kit, yes=False, answers=['demo']).uninstall(purge=True) == 0
    purge = last(client, 'uninstall-portable-linux')[1]
    assert purge['data_policy'] == 'purge' and purge['confirm_product_id'] == 'demo'


def test_main_reports_localized_errors(env, capsys, monkeypatch):
    base, client, setup = env
    kit = make_kit(base)
    monkeypatch.setattr(kit_setup, 'Setup', lambda *a, **k: (_ for _ in ()).throw(SetupError('kit.not_installed')))
    assert kit_setup.main(['--kit', str(kit), '--language', 'ru', 'status']) == 1
    assert 'не установлен' in capsys.readouterr().err
    assert kit_setup.main(['--kit', str(kit), 'bogus']) == 2
    assert kit_setup.main(['--kit', str(base / 'missing'), '--language', 'de', 'status']) == 1


def test_interrupted_consolidation_resumes_without_password(env, monkeypatch):
    base, client, setup = env
    setup(make_kit(base)).install()
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    client.fail.add('install-portable-linux')
    with pytest.raises(SetupError):
        setup(kit2).update()
    state = json.loads((base / 'home/.local/state/homensai/demo/state.json').read_text())
    assert state['status'] == 'consolidating' and state['consolidate']['uninstalled'] is True
    with pytest.raises(SetupError) as blocked:
        setup(kit2).uninstall()
    assert blocked.value.key == 'kit.consolidation_pending'
    monkeypatch.delenv(kit_setup.PASSWORD_ENV)
    updates = len([call for call in client.calls if call[0] == 'run-pointer-linux-update'])
    removals = len([call for call in client.calls if call[0] == 'uninstall-portable-linux'])
    assert setup(kit2).update() == 0
    assert len([call for call in client.calls if call[0] == 'run-pointer-linux-update']) == updates
    assert len([call for call in client.calls if call[0] == 'uninstall-portable-linux']) == removals
    state = json.loads((base / 'home/.local/state/homensai/demo/state.json').read_text())
    assert state['status'] == 'installed' and state['base'] == '2'


def test_update_restarts_when_no_transaction_was_recorded(env, monkeypatch):
    base, client, setup = env
    setup(make_kit(base)).install()
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    client.fail.add('run-pointer-linux-update')
    with pytest.raises(SetupError):
        setup(kit2).update()
    first = last(client, 'run-pointer-linux-update')[1]
    client.fail.add('inspect-started-pointer-update')  # прежний вызов ничего не записал
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    assert setup(kit2).update() == 0
    second = last(client, 'run-pointer-linux-update')[1]
    assert second['action'] == 'start' and second['idempotency_key'] == first['idempotency_key']


def test_finished_transaction_is_taken_from_inspection(env, monkeypatch):
    base, client, setup = env
    setup(make_kit(base)).install()
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    client.fail.add('run-pointer-linux-update')
    with pytest.raises(SetupError):
        setup(kit2).update()
    runs = len([call for call in client.calls if call[0] == 'run-pointer-linux-update'])
    client.results['inspect-started-pointer-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    assert setup(kit2).update() == 0
    assert len([call for call in client.calls if call[0] == 'run-pointer-linux-update']) == runs
    assert json.loads((base / 'home/.local/state/homensai/demo/state.json').read_text())['version'] == '2'


def test_accepted_new_publisher_key_is_pinned_after_update(env, monkeypatch):
    base, client, setup = env
    setup(make_kit(base)).install()
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    rotated = make_kit(base / 'rotated', '2', ('m-1',), key=b'n' * 32)
    digest = hashlib.sha256(b'n' * 32).hexdigest()
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    assert setup(rotated).update(accept_key=digest) == 0
    params = last(client, 'run-pointer-linux-update')[1]
    assert params['old_key'] == base64.b64encode(b'k' * 32).decode()
    assert params['new_key'] == base64.b64encode(b'n' * 32).decode()
    reinstall = last(client, 'install-portable-linux')[1]
    assert reinstall['trusted_public_key'] == base64.b64encode(b'n' * 32).decode()
    removal = last(client, 'uninstall-portable-linux')[1]
    assert removal['trusted_public_key'] == base64.b64encode(b'k' * 32).decode()
    state = json.loads((base / 'home/.local/state/homensai/demo/state.json').read_text())
    assert state['public_key'] == base64.b64encode(b'n' * 32).decode()


def test_installer_publication_failure_is_finished_by_same_command(env, monkeypatch):
    base, client, setup = env
    kit = make_kit(base)
    original = kit_setup.Setup.store_installer
    monkeypatch.setattr(kit_setup.Setup, 'store_installer', lambda self, state: (_ for _ in ()).throw(OSError('ENOSPC')))
    with pytest.raises(OSError):
        with setup(kit).locked():
            setup(kit).install()
    state_dir = base / 'home/.local/state/homensai/demo'
    state = json.loads((state_dir / 'state.json').read_text())
    assert state['status'] == 'installed' and state['pending_installer'] == '1'
    assert not (state_dir / 'installer').exists()
    monkeypatch.setattr(kit_setup.Setup, 'store_installer', original)
    instance = setup(kit)
    with instance.locked():
        assert instance.install() == 0
    assert (state_dir / 'installer/setup.sh').is_file()
    assert 'pending_installer' not in json.loads((state_dir / 'state.json').read_text())
    with pytest.raises(SetupError) as again:
        with setup(kit).locked():
            setup(kit).install()
    assert again.value.key == 'kit.already_installed'


def test_interrupted_installer_swap_is_restored(env):
    base, client, setup = env
    kit = make_kit(base)
    setup(kit).install()
    state_dir = base / 'home/.local/state/homensai/demo'
    (state_dir / 'installer').rename(state_dir / '.installer.old')  # сбой между переносом и публикацией
    with setup(kit).locked():
        pass
    assert (state_dir / 'installer/setup.sh').is_file() and not (state_dir / '.installer.old').exists()


class ParentCheckingClient(FakeClient):
    """Фиксирует, существовал ли родитель корня в момент вызова установки."""

    def __init__(self):
        super().__init__()
        self.parents = []

    def call(self, operation, params, **kwargs):
        if operation == 'install-portable-linux':
            parent = Path(params['install_root']).parent
            self.parents.append((parent, parent.is_dir() and not parent.is_symlink()))
        return super().call(operation, params, **kwargs)


def test_default_root_parents_are_created_before_api_call(env):
    base, _, setup = env
    kit = make_kit(base)
    client = ParentCheckingClient()
    instance = setup(kit)
    instance._client = client
    assert not (base / 'home/.local/share').exists()
    assert instance.install() == 0
    parent = install_root(base).parent
    assert client.parents == [(parent, True)]
    for path in (base / 'home/.local/share', parent):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
    assert not install_root(base).exists()  # корень создаёт подготовка выпуска, а не установщик


def test_explicit_root_parents_are_created_and_existing_kept(env):
    base, _, setup = env
    kit = make_kit(base)
    existing = base / 'home/apps'
    existing.mkdir(mode=0o750)
    client = ParentCheckingClient()
    instance = setup(kit)
    instance._client = client
    root = existing / 'nested/deeper/demo'
    assert instance.install(root=str(root)) == 0
    assert client.parents == [(root.parent, True)]
    assert stat.S_IMODE(existing.stat().st_mode) == 0o750
    assert stat.S_IMODE((existing / 'nested').stat().st_mode) == 0o700
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize('problem', ['symlink', 'group-writable', 'file'])
def test_unsafe_root_ancestor_is_refused_before_api_call(env, problem):
    base, client, setup = env
    kit = make_kit(base)
    share = base / 'home/.local/share'
    share.parent.mkdir(parents=True, exist_ok=True)
    if problem == 'symlink':
        target = base / 'elsewhere'
        target.mkdir(mode=0o700)
        share.symlink_to(target)
    elif problem == 'group-writable':
        share.mkdir(mode=0o700)
        share.chmod(0o770)
    else:
        share.write_text('not a directory')
    with pytest.raises(SetupError) as refused:
        setup(kit).install()
    assert refused.value.key == 'kit.unsafe_root_parent'
    assert not [call for call in client.calls if call[0] == 'install-portable-linux']
    if problem == 'symlink':
        assert not list((base / 'elsewhere').iterdir())


def test_resume_of_saved_install_creates_missing_parent(env):
    """Состояние, сохранённое прежней версией до создания родителя, продолжается."""
    base, client, setup = env
    kit = make_kit(base)
    client.fail.add('install-portable-linux')
    with pytest.raises(SetupError):
        setup(kit).install()
    parent = install_root(base).parent
    parent.rmdir()
    assert setup(kit).install() == 0
    assert parent.is_dir()
