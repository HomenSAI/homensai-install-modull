"""Удаление Linux user-systemd установки с синтетическим менеджером; службы не запускаются."""
import json
from pathlib import Path
import sys

import pytest

from test_api_signed_preflight import signed_request  # noqa: F401
from test_systemd_pointer_signed import signed_pointer  # noqa: F401
from shared.systemd_pointer import plan_pointer_signed_user_units
from shared.update_plan import operation_lock
import shared.uninstall_linux as uninstall
from shared.uninstall_linux import RECEIPT_NAME, UninstallError, preserved_installation, uninstall_portable_linux

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Требуется Linux')
MODES = ('autonomous',)


class FakeManager:
    """Состояние units: загрузка после daemon-reload, активность, отказ на выбранном шаге."""

    def __init__(self, directory, names, *, fail_on=None):
        self.directory, self.fail_on, self.calls = directory, fail_on, []
        self.loaded = set(names)
        self.active = {name: 'active' for name in names}

    def __call__(self, argv, *, timeout, env):
        args = argv[4:]
        self.calls.append(args)
        if self.fail_on is not None and args[0] == self.fail_on:
            self.fail_on = None
            raise TimeoutError('synthetic interruption')
        if '--value' in argv:
            return '/etc/systemd/user ' + str(self.directory) + '\n'
        if args == ['daemon-reload']:
            self.loaded = {name for name in self.loaded if (self.directory / name).exists()}
            return ''
        name = args[-1]
        if args[0] == 'stop':
            self.active[name] = 'inactive'
            return ''
        if args[0] == 'reset-failed':
            self.active[name] = 'inactive'
            return ''
        loaded = name in self.loaded
        state = self.active.get(name, 'inactive') if loaded else 'inactive'
        return (f'Id={name}\nLoadState={"loaded" if loaded else "not-found"}\nActiveState={state}\n'
                f'SubState={"running" if state == "active" else "dead"}\n'
                f'FragmentPath={self.directory / name if loaded else ""}\nDropInPaths=\nNeedDaemonReload=no\n')


@pytest.fixture
def installed(signed_pointer, tmp_path, monkeypatch):
    _, options, build = signed_pointer
    package, envelope, root = build('installed')
    directory = tmp_path / 'user-units'
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(uninstall, '_unit_directory', lambda path: Path(path))
    texts = plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=MODES)['unit_texts']
    for name, text in texts.items():
        path = directory / name
        path.write_text(text, encoding='utf-8')
        path.chmod(0o600)
    for name in ('data', 'backup', 'config'):
        (root / name / 'keep.txt').write_text('synthetic ' + name, encoding='utf-8')
    args = dict(trusted_public_key=options['trusted_public_key'],
                expected_product_id=options['expected_product_id'], expected_version='1',
                modes=MODES, unit_dir=str(directory), operation_id='a' * 32)
    return package, envelope, root, directory, list(texts), args


def run(installed, **overrides):
    package, envelope, root, _, _, args = installed
    return uninstall_portable_linux(package, envelope, str(root), **{**args, **overrides})


def test_plan_lists_paths_without_effects(installed):
    _, _, root, directory, names, _ = installed
    manager = FakeManager(directory, names)
    result = run(installed, runner=manager)
    assert result['state'] == 'planned' and result['units'] == names
    assert set(result['preserved_paths']) == {'data', 'backup', 'config'}
    assert {'app', 'releases', 'runtime', 'services', '.homensai-control'} <= set(result['removed_paths'])
    assert not result['services_stopped'] and not result['secure_erase']
    assert not manager.calls and not (root / RECEIPT_NAME).exists()
    assert all((directory / name).exists() for name in names) and (root / 'releases').is_dir()


def test_keep_stops_services_removes_program_and_preserves_data(installed):
    _, _, root, directory, names, args = installed
    manager = FakeManager(directory, names)
    result = run(installed, execute=True, runner=manager)
    assert result['state'] == 'uninstalled' and result['services_stopped']
    assert all(manager.active[name] == 'inactive' for name in names)
    assert [call[0] for call in manager.calls].count('stop') == len(names)
    assert ['daemon-reload'] in manager.calls and not manager.loaded
    assert not any((directory / name).exists() for name in names)
    for name in ('app', 'releases', 'runtime', 'services', '.homensai-control', '.homensai-fresh.json'):
        assert not (root / name).exists()
    for name in ('data', 'backup', 'config'):
        assert (root / name / 'keep.txt').read_text(encoding='utf-8') == 'synthetic ' + name
    receipt = json.loads((root / RECEIPT_NAME).read_text())
    assert receipt['status'] == 'done' and (root / RECEIPT_NAME).stat().st_mode & 0o077 == 0
    assert preserved_installation(str(root), expected_product_id=args['expected_product_id'])['preserved'] == \
        ['data', 'backup', 'config']
    with pytest.raises(UninstallError):
        preserved_installation(str(root), expected_product_id='other-product')
    repeat = FakeManager(directory, [])
    assert run(installed, execute=True, runner=repeat)['state'] == 'uninstalled' and not repeat.calls
    with pytest.raises(UninstallError) as error:
        run(installed, execute=True, runner=repeat, operation_id='b' * 32)
    assert error.value.code == 'uninstall_conflict'


def test_purge_requires_matching_confirmation(installed):
    _, _, root, directory, names, args = installed
    for confirm in (None, 'other-product'):
        with pytest.raises(UninstallError) as error:
            run(installed, execute=True, runner=FakeManager(directory, names), data_policy='purge',
                confirm_product_id=confirm)
        assert error.value.code == 'invalid_request'
    with pytest.raises(UninstallError):
        run(installed, confirm_product_id=args['expected_product_id'])
    assert (root / 'data/keep.txt').exists() and not (root / RECEIPT_NAME).exists()
    result = run(installed, execute=True, runner=FakeManager(directory, names), data_policy='purge',
                 confirm_product_id=args['expected_product_id'])
    assert result['state'] == 'uninstalled' and result['preserved_paths'] == []
    assert sorted(path.name for path in root.iterdir()) == ['.homensai-bootstrap.lock', RECEIPT_NAME]
    with pytest.raises(UninstallError):
        preserved_installation(str(root), expected_product_id=args['expected_product_id'])


@pytest.mark.parametrize('change', ['modified-unit', 'foreign-fragment', 'drop-in', 'wrong-key', 'other-root'])
def test_foreign_or_unproven_installation_is_refused_without_changes(installed, tmp_path, change):
    package, envelope, root, directory, names, args = installed
    manager = FakeManager(directory, names)
    overrides = {}
    if change == 'modified-unit':
        (directory / names[0]).write_text('[Service]\nExecStart=/bin/true\n')
    elif change == 'foreign-fragment':
        original = manager.__call__
        overrides['runner'] = lambda argv, **kw: original(argv, **kw).replace(
            'FragmentPath=' + str(directory), 'FragmentPath=/etc/systemd/user')
    elif change == 'drop-in':
        original = manager.__call__
        overrides['runner'] = lambda argv, **kw: original(argv, **kw).replace('DropInPaths=\n', 'DropInPaths=/x.conf\n')
    elif change == 'wrong-key':
        overrides['trusted_public_key'] = b'\0' * 32
    else:
        other = tmp_path / 'other-root'
        other.mkdir(mode=0o700)
        for name in ('data', 'releases'):
            (other / name).mkdir()
        overrides['install_root'] = str(other)
    call_args = {**args, 'runner': manager, 'execute': True, **overrides}
    install_root = call_args.pop('install_root', str(root))
    with pytest.raises(UninstallError):
        uninstall_portable_linux(package, envelope, install_root, **call_args)
    assert (root / 'releases').is_dir() and (root / 'data/keep.txt').exists()
    assert all(manager.active[name] == 'active' for name in names)
    assert not any(call[0] in ('stop', 'daemon-reload') for call in manager.calls)
    if change != 'modified-unit':
        assert all((directory / name).exists() for name in names)


def test_unfinished_update_or_busy_lock_blocks_uninstall(installed):
    _, _, root, directory, names, _ = installed
    state = root / 'services/update.json'
    state.write_text(json.dumps({'outcome': 'running'}))
    with pytest.raises(UninstallError) as error:
        run(installed, execute=True, runner=FakeManager(directory, names))
    assert error.value.code == 'update_in_progress' and not (root / RECEIPT_NAME).exists()
    state.write_text(json.dumps({'outcome': 'running', 'indeterminate': True}))
    with pytest.raises(UninstallError):
        run(installed, execute=True, runner=FakeManager(directory, names))
    state.write_text(json.dumps({'outcome': 'updated'}))
    with operation_lock(root / 'services/operation.lock'):
        with pytest.raises(UninstallError):
            run(installed, execute=True, runner=FakeManager(directory, names))
    assert not (root / RECEIPT_NAME).exists() and (root / 'releases').is_dir()
    assert run(installed, execute=True, runner=FakeManager(directory, names))['state'] == 'uninstalled'


@pytest.mark.parametrize('step', ['stop', 'daemon-reload'])
def test_interrupted_uninstall_resumes_with_same_operation(installed, step):
    _, _, root, directory, names, _ = installed
    manager = FakeManager(directory, names, fail_on=step)
    with pytest.raises(UninstallError):
        run(installed, execute=True, runner=manager)
    assert json.loads((root / RECEIPT_NAME).read_text())['status'] == 'pending'
    assert (root / 'data/keep.txt').exists()
    with pytest.raises(UninstallError) as error:
        run(installed, execute=True, runner=manager, operation_id='c' * 32)
    assert error.value.code == 'uninstall_conflict'
    assert run(installed, execute=True, runner=manager)['state'] == 'uninstalled'
    assert not (root / 'releases').exists() and (root / 'data/keep.txt').exists()
    assert json.loads((root / RECEIPT_NAME).read_text())['status'] == 'done'


def test_failed_component_is_reset_and_invalid_parameters_refused(installed):
    _, _, root, directory, names, _ = installed
    manager = FakeManager(directory, names)
    manager.active[names[0]] = 'failed'
    assert run(installed, execute=True, runner=manager)['state'] == 'uninstalled'
    assert ['reset-failed', '--', names[0]] in manager.calls
    for overrides in ({'operation_id': 'A' * 32}, {'modes': ['autonomous']}, {'data_policy': 'erase'},
                      {'timeout': 0}, {'execute': 1}):
        with pytest.raises(UninstallError) as error:
            run(installed, **overrides)
        assert error.value.code == 'invalid_request'


def test_public_api_and_client_plan_and_uninstall(installed, monkeypatch):
    import base64
    from homensai_installer_client import InstallerClient, contract_schema
    from shared.api import handle
    package, envelope, root, directory, names, args = installed
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(args['trusted_public_key']).decode(),
              'expected_product_id': args['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'unit_dir': str(directory), 'modes': list(MODES),
              'operation_id': 'd' * 32, 'data_policy': 'keep'}
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 67
    assert 'plan-portable-linux-uninstall' in caps['operations']
    assert 'plan-portable-linux-uninstall' not in caps['mutating_operations']
    assert 'uninstall-portable-linux' in caps['mutating_operations']
    plan = {'api_major': 1, 'request_id': 'plan', 'operation': 'plan-portable-linux-uninstall', 'params': params}
    assert validator.is_valid(plan)
    planned = handle(plan)
    assert planned['ok'] and planned['result']['state'] == 'planned'
    # The client runs the API in a child process without test substitutions:
    # a group/world-writable unit directory must be refused there.
    unsafe = directory.parent / 'unsafe-units'
    unsafe.mkdir()
    unsafe.chmod(0o777)
    with pytest.raises(Exception) as refused:
        InstallerClient(timeout=90).call(plan['operation'], {**params, 'unit_dir': str(unsafe)}, min_minor=67)
    assert getattr(refused.value, 'code', None) == 'uninstall_failed'
    for bad in ({'data_policy': 'purge'}, {'confirm_product_id': args['expected_product_id']},
                {'data_policy': 'purge', 'confirm_product_id': 'other'}, {'operation_id': 'x'}):
        request = {**plan, 'params': {**params, **bad}}
        assert handle(request)['error']['code'] == 'invalid_request'
        if 'operation_id' not in bad and bad.get('confirm_product_id') != 'other':
            assert not validator.is_valid(request)
    manager = FakeManager(directory, names)
    monkeypatch.setattr(uninstall, '_run', manager)
    request = {'api_major': 1, 'request_id': 'remove', 'operation': 'uninstall-portable-linux',
               'params': params, 'language': 'ru'}
    assert validator.is_valid(request)
    result = handle(request)
    assert result['ok'] and result['result']['state'] == 'uninstalled' and (root / 'data/keep.txt').exists()
    request['params'] = {**params, 'operation_id': 'e' * 32}
    error = handle(request)['error']
    assert error['code'] == 'uninstall_conflict' and 'удален' in error['message']


def reinstall(installed, **overrides):
    from test_api_signed_preflight import NAMES
    from shared.fresh_prepare import prepare_idempotent_native
    package, envelope, root, _, _, args = installed
    options = {name: args[name] for name in ('trusted_public_key', 'expected_product_id', 'expected_version')}
    return prepare_idempotent_native(package, envelope, str(root), **{**options, **overrides},
        required_bytes={name: 0 for name in NAMES}, reserve_bytes=0, operation_id='f' * 32, reuse_preserved=True)


def test_reinstall_reuses_preserved_data_of_same_version(installed):
    from shared.fresh_prepare import FreshPrepareError, verify_fresh_release
    package, envelope, root, directory, names, args = installed
    with pytest.raises(FreshPrepareError):
        reinstall(installed)
    run(installed, execute=True, runner=FakeManager(directory, names))
    assert json.loads((root / RECEIPT_NAME).read_text())['release'] == '1'
    state = reinstall(installed)
    assert state['status'] == 'prepared' and not (root / RECEIPT_NAME).exists()
    assert json.loads((root / '.homensai-control/previous-uninstall.json').read_text())['status'] == 'done'
    for name in ('data', 'backup', 'config'):
        assert (root / name / 'keep.txt').read_text(encoding='utf-8') == 'synthetic ' + name
    options = {name: args[name] for name in ('trusted_public_key', 'expected_product_id', 'expected_version')}
    assert verify_fresh_release(package, envelope, str(root), **options)['release_files_match']
    assert reinstall(installed) == state
    # The new installation is owned by the fresh journal again: a later removal is a new operation.
    texts = plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=MODES)['unit_texts']
    for name, text in texts.items():
        (directory / name).write_text(text, encoding='utf-8')
        (directory / name).chmod(0o600)
    assert run(installed, execute=True, runner=FakeManager(directory, names),
               operation_id='9' * 32)['state'] == 'uninstalled'


@pytest.mark.parametrize('change', ['no-flag', 'foreign-file', 'purged', 'other-version', 'pending', 'tampered'])
def test_reinstall_refuses_unproven_or_changed_preserved_root(installed, change):
    from shared.fresh_prepare import FreshPrepareError, prepare_idempotent_native
    from test_api_signed_preflight import NAMES
    package, envelope, root, directory, names, args = installed
    overrides = {}
    if change == 'purged':
        run(installed, execute=True, runner=FakeManager(directory, names), data_policy='purge',
            confirm_product_id=args['expected_product_id'])
    elif change == 'pending':
        with pytest.raises(UninstallError):
            run(installed, execute=True, runner=FakeManager(directory, names, fail_on='daemon-reload'))
    else:
        run(installed, execute=True, runner=FakeManager(directory, names))
    receipt = root / RECEIPT_NAME
    before = receipt.read_bytes()
    if change == 'foreign-file':
        (root / 'unexpected.txt').write_text('foreign')
    elif change == 'other-version':
        value = json.loads(before)
        value['release'] = '2'
        receipt.write_text(json.dumps(value))
    elif change == 'tampered':
        receipt.write_text('{"version": 1}')
    if change == 'no-flag':
        options = {name: args[name] for name in ('trusted_public_key', 'expected_product_id', 'expected_version')}
        with pytest.raises(FreshPrepareError):
            prepare_idempotent_native(package, envelope, str(root), **options,
                required_bytes={name: 0 for name in NAMES}, reserve_bytes=0, operation_id='f' * 32)
    else:
        with pytest.raises(FreshPrepareError):
            reinstall(installed, **overrides)
    if change == 'pending':
        assert json.loads(receipt.read_text())['status'] == 'pending' and (root / 'releases').is_dir()
    else:
        assert not (root / '.homensai-fresh.json').exists() and not (root / 'releases').exists()
    if change not in ('purged', 'pending', 'tampered'):
        assert (root / 'data/keep.txt').exists()


def test_interrupted_reinstall_resumes_and_api_accepts_explicit_flag(installed, monkeypatch):
    import base64
    import shared.fresh_prepare as fresh
    from homensai_installer_client import contract_schema
    from shared.api import handle
    from test_api_signed_preflight import NAMES
    package, envelope, root, directory, names, args = installed
    run(installed, execute=True, runner=FakeManager(directory, names))
    original = fresh._move_uninstall_receipt
    def crash(*values):
        raise OSError('synthetic crash after journal')
    monkeypatch.setattr(fresh, '_move_uninstall_receipt', crash)
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(args['trusted_public_key']).decode(),
              'expected_product_id': args['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'required_bytes': {name: 0 for name in NAMES}, 'reserve_bytes': 0,
              'idempotency_key': '8' * 32, 'reuse_preserved_data': True}
    request = {'api_major': 1, 'request_id': 'reinstall', 'operation': 'prepare-native-release', 'params': params}
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    assert validator.is_valid(request)
    assert not validator.is_valid({**request, 'params': {**params, 'reuse_preserved_data': False}})
    assert handle({**request, 'params': {**params, 'reuse_preserved_data': False}})['error']['code'] == 'invalid_request'
    assert handle(request)['error']['code'] == 'fresh_prepare_failed'
    assert json.loads((root / '.homensai-fresh.json').read_text())['status'] == 'preparing'
    assert (root / RECEIPT_NAME).exists()
    monkeypatch.setattr(fresh, '_move_uninstall_receipt', original)
    without_flag = {key: value for key, value in params.items() if key != 'reuse_preserved_data'}
    assert handle({**request, 'params': without_flag})['error']['code'] == 'conflict'
    result = handle(request)
    assert result['ok'] and result['result']['status'] == 'prepared'
    assert not (root / RECEIPT_NAME).exists() and (root / 'data/keep.txt').exists()
    assert handle(request) == result
