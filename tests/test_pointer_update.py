"""Pointer update assembly and transaction with synthetic services and real archives."""
import json
from pathlib import Path
import stat

import pytest

from test_api_signed_preflight import signed_request
from test_systemd_pointer_signed import signed_pointer
from test_systemd_portable import FakeReloadManager, fake_manager
from shared.pointer_update import assemble_pointer_linux_update, inspect_started_pointer_runtime
from shared.pointer_controller import run_pointer_linux_update
from shared.pointer_recovery import inspect_started_pointer_group
from shared.runtime_native import prepare_signed_linux_runtime
from shared.systemd_pointer import publish_pointer_signed_user_units
from shared.systemd_user_load import load_pointer_signed_user_units
from shared.update_plan import EffectIndeterminate, UpdateError, UpdateIndeterminate


class StatefulManager(FakeReloadManager):
    def __init__(self, directory):
        super().__init__(directory)
        self.active = set()

    def __call__(self, argv, *, timeout, env):
        action, name = argv[4], argv[-1]
        if action in ('start', 'stop'):
            self.calls.append(argv[4:])
            self.active.add(name) if action == 'start' else self.active.discard(name)
            return ''
        output = super().__call__(argv, timeout=timeout, env=env)
        if action == 'show' and name in self.active:
            output = output.replace('ActiveState=inactive\nSubState=dead', 'ActiveState=active\nSubState=running')
        return output


@pytest.fixture
def installation(signed_pointer, tmp_path):
    _, trusted, build = signed_pointer
    for name in ('pre_start', 'health_checks', 'post_update'):
        build.manifest['hooks'][name] = []
    old, old_envelope, root = build('old-pointer')
    new, new_envelope, _ = build('new-pointer', version='2')
    directory, work = tmp_path / 'units', tmp_path / 'update-work'
    directory.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    prepare_signed_linux_runtime(old, old_envelope, str(root), **trusted,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publisher, _ = fake_manager(directory)
    published = publish_pointer_signed_user_units(old, old_envelope, str(root), **trusted,
        modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
        operation_id='b' * 32, reserve_bytes=0, execute=True, runner=publisher)
    manager = StatefulManager(directory)
    load_pointer_signed_user_units(old, old_envelope, str(root), **trusted,
        modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
        operation_id='c' * 32, reserve_bytes=0, runner=manager)
    manager.active = {item['name'] for item in published['units']}
    options = dict(new_package=str(new), new_envelope=new_envelope, new_key=trusted['trusted_public_key'],
        old_package=str(old), old_envelope=old_envelope, old_key=trusted['trusted_public_key'],
        product_id=trusted['expected_product_id'], old_version='1', new_version='2', install_root=str(root),
        work_dir=str(work), modes=('autonomous',), migration_ids=(), operation_id='d' * 32,
        unit_dir=str(directory), load_key='c' * 32, runtime_key='a' * 32, runtime_reserve_bytes=0,
        password_provider=lambda: 'synthetic password', interpreter='/test/python',
        interpreter_sha256='a' * 64, timeout=5, runner=manager)
    return root, work, options, manager


@pytest.mark.parametrize('fail_health', [False, True])
def test_pointer_update_and_rollback_with_verified_runtime(installation, monkeypatch, fail_health):
    root, _, options, manager = installation
    marker = root / 'data/synthetic.txt'
    marker.write_text('original')
    plan = assemble_pointer_linux_update(**options)
    assert plan.status() is None and plan.ports.current_release() == '1'
    calls = []
    def health(key, *_):
        calls.append(key.split('/')[-1])
        if fail_health and key.endswith('/health'):
            raise RuntimeError('synthetic failure')
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', health)
    result = plan.run()
    assert result['outcome'] == ('rolled_back' if fail_health else 'updated')
    assert plan.ports.current_release() == ('1' if fail_health else '2')
    assert marker.read_text() == 'original' and len(manager.active) == 3
    assert stat.S_IMODE((root / 'runtime/bin/python').stat().st_mode) == 0o700
    assert stat.S_IMODE((root / 'runtime/lib/homensai/portable_launcher.py').stat().st_mode) == 0o600
    assert calls == (['health', 'rollback_health'] if fail_health else ['health'])
    assert not any('synthetic password' in json.dumps(call) for call in manager.calls)


def test_pointer_update_refuses_damaged_runtime_before_preparation(installation):
    root, work, options, manager = installation
    (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
    before = list(manager.calls)
    with pytest.raises(UpdateError):
        assemble_pointer_linux_update(**options)
    assert not list(work.iterdir()) and manager.calls == before
    assert not (root / 'services/update.json').exists()


def test_generic_assembly_refuses_pointer_update_without_runtime_guard(installation):
    from shared.systemd_user_load import _pointer_signed_user_group
    from shared.update_assembly import assemble_prepared_linux_update
    root, work, options, manager = installation
    group = _pointer_signed_user_group(options['old_package'], options['old_envelope'], str(root),
        trusted_public_key=options['old_key'], expected_product_id=options['product_id'],
        expected_version='1', modes=options['modes'], unit_dir=options['unit_dir'],
        operation_id=options['load_key'], runtime_operation_id=options['runtime_key'], reserve_bytes=0,
        runner=manager, _require_active=True)
    excluded = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner'}
    generic = {name: value for name, value in options.items() if name not in excluded}
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(group=group, **generic)
    assert not list(work.iterdir()) and not (root / 'services/update.json').exists()


def test_damaged_runtime_blocks_restore_before_live_data_changes(installation, monkeypatch):
    root, _, options, _ = installation
    marker = root / 'data/synthetic.txt'
    marker.write_text('original')
    plan = assemble_pointer_linux_update(**options)
    def fail_and_damage(*_):
        marker.write_text('modified during update')
        (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
        raise RuntimeError('synthetic health failure')
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', fail_and_damage)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    state = plan.status()
    assert state['direction'] == 'rollback' and plan.rollback[state['index']] == 'restore'
    assert state['indeterminate'] and marker.read_text() == 'modified during update'
    assert plan.ports.current_release() == '2'
    assert plan.ports.archive_path(state['transaction'] + '/before_rollback').is_file()


def test_restore_preserves_runtime_when_effects_wrapper_hides_guard(installation, monkeypatch):
    root, _, options, _ = installation
    plan = assemble_pointer_linux_update(**options)
    effects = plan.ports.effects

    class Wrapper:
        def __getattr__(self, name):
            if name == 'runtime_guard':
                raise AttributeError(name)
            return getattr(effects, name)

    plan.ports.effects = Wrapper()
    def health(key, *_):
        if key.endswith('/health'):
            raise RuntimeError('synthetic failure')
    monkeypatch.setattr(effects.health_port, 'health', health)
    assert plan.run()['outcome'] == 'rolled_back'
    assert stat.S_IMODE((root / 'runtime/bin/python').stat().st_mode) == 0o700


def test_inspect_interrupted_runtime_after_pointer_switch(installation, monkeypatch):
    root, _, options, _ = installation
    plan = assemble_pointer_linux_update(**options)
    def interrupt_start(*_):
        raise EffectIndeterminate('synthetic interruption')
    monkeypatch.setattr(plan.ports.effects, 'start', interrupt_start)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    assert plan.ports.current_release() == '2'
    excluded = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner',
                'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    args = {name: value for name, value in options.items() if name not in excluded}
    args.update(runtime_key=options['runtime_key'], runtime_reserve_bytes=0)
    before = plan.status()
    result = inspect_started_pointer_runtime(**args)
    assert result['state'] == before and result['runtime_verified']
    assert not result['services_verified'] and not result['resume_authorized']
    with pytest.raises(UpdateError):
        inspect_started_pointer_runtime(**{**args, 'runtime_reserve_bytes': 1})
    with pytest.raises(UpdateError):
        inspect_started_pointer_runtime(**{**args, 'runtime_key': 'f' * 32})
    launcher = root / 'runtime/lib/homensai/portable_launcher.py'
    original = launcher.read_bytes()
    launcher.write_text('damaged')
    with pytest.raises(UpdateError):
        inspect_started_pointer_runtime(**args)
    launcher.write_bytes(original)
    (root / 'releases/app-1/runtime-linux/runtime-v1.json').write_text('damaged')
    with pytest.raises(UpdateError):
        inspect_started_pointer_runtime(**args)
    assert plan.status() == before and plan.ports.current_release() == '2'


def test_inspect_interrupted_group_after_pointer_switch(installation, monkeypatch):
    root, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    def interrupt_start(*_):
        raise EffectIndeterminate('synthetic interruption')
    monkeypatch.setattr(plan.ports.effects, 'start', interrupt_start)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    excluded = {'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    args = {name: value for name, value in options.items() if name not in excluded}
    before = plan.status()
    calls = len(manager.calls)
    observed = inspect_started_pointer_group(**args)
    assert observed['state'] == before and len(observed['units']) == 3
    assert all(unit['active_state'] == 'inactive' for unit in observed['units'])
    assert not observed['process_identity_verified'] and not observed['resume_authorized']
    assert all(call[0] == 'show' for call in manager.calls[calls:])
    with pytest.raises(UpdateError):
        inspect_started_pointer_group(**{**args, 'load_key': 'f' * 32})
    unit = next((root.parent / 'units').glob('*.service'))
    dropin = unit.with_name(unit.name + '.d')
    dropin.mkdir()
    with pytest.raises(UpdateError):
        inspect_started_pointer_group(**args)
    dropin.rmdir()
    unit.write_text('tampered')
    with pytest.raises(UpdateError):
        inspect_started_pointer_group(**args)
    assert plan.status() == before and plan.ports.current_release() == '2'


def test_reopen_interrupted_pointer_update_does_not_replay_effects(installation, monkeypatch):
    root, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    def interrupt_start(*_):
        raise EffectIndeterminate('synthetic interruption')
    monkeypatch.setattr(plan.ports.effects, 'start', interrupt_start)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    before = plan.status()
    assert plan.ports.current_release() == '2'
    calls = len(manager.calls)
    reopened = assemble_pointer_linux_update(**options, reopen=True)
    assert reopened.status() == before
    assert all(call[0] == 'show' for call in manager.calls[calls:])
    with pytest.raises(UpdateIndeterminate):
        reopened.run()
    assert reopened.status() == before
    launcher = root / 'runtime/lib/homensai/portable_launcher.py'
    launcher.write_text('damaged')
    with pytest.raises(UpdateError):
        assemble_pointer_linux_update(**options, reopen=True)
    assert reopened.status() == before


def test_reopen_reconciles_stopped_group_without_repeating_stop(installation, monkeypatch):
    import shared.systemd_lifecycle_port as lifecycle
    _, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    original_write = lifecycle._atomic_write
    def lose_stop_ack(path, value):
        if value.get('action') == 'stop' and value.get('status') == 'done':
            raise OSError('synthetic lost acknowledgement')
        return original_write(path, value)
    monkeypatch.setattr(lifecycle, '_atomic_write', lose_stop_ack)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    state = plan.status()
    assert state['indeterminate'] and plan.forward[state['index']] == 'stop'
    assert not manager.active
    monkeypatch.setattr(lifecycle, '_atomic_write', original_write)
    key = state['transaction'] + '/stop'
    reopened = assemble_pointer_linux_update(**options, reopen=True, reconcile_stop_key=key)
    monkeypatch.setattr(reopened.ports.effects.health_port, 'health', lambda *_: None)
    before = len([call for call in manager.calls if call[0] == 'stop'])
    assert reopened.run(resume_indeterminate=True)['outcome'] == 'updated'
    assert len([call for call in manager.calls if call[0] == 'stop']) == before


def test_api_inspects_interrupted_pointer_group(installation, monkeypatch):
    from shared import api, pointer_recovery
    from test_update_prepare import api_query
    root, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    def interrupt_start(*_):
        raise EffectIndeterminate('synthetic interruption')
    monkeypatch.setattr(plan.ports.effects, 'start', interrupt_start)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    state = plan.status()
    omitted = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner',
               'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    request = api_query({name: value for name, value in options.items() if name not in omitted})
    request['operation'] = 'inspect-started-pointer-update'
    request['params'].update({name: options[name] for name in
                              ('unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes')})
    monkeypatch.setattr(pointer_recovery, '_run', manager)
    validator = pytest.importorskip('jsonschema').Draft202012Validator(json.loads(
        (Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text()))
    assert validator.is_valid(request)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 59 and request['operation'] in caps['mutating_operations']
    before = len(manager.calls)
    result = api.handle(request)
    assert result['ok'] and result['result']['state'] == state
    assert result['result']['runtime_verified'] and not result['result']['resume_authorized']
    assert all(call[0] == 'show' for call in manager.calls[before:])
    assert str(root) not in json.dumps(result)
    request['params']['load_key'] = request['params']['runtime_key']
    assert api.handle(request)['error']['code'] == 'invalid_request'
    request['params']['load_key'] = options['load_key']
    unit = next(Path(options['unit_dir']).glob('*.service'))
    unit.write_text('tampered')
    from shared.i18n import translate
    for language in ('ru', 'en', 'de'):
        request['language'] = language
        failed = api.handle(request)
        assert failed['error']['code'] == 'update_inspect_failed'
        assert failed['error']['message'] == translate(language, 'api.update_inspect_failed')
        assert str(root) not in json.dumps(failed)
    assert plan.status() == state


def test_controller_starts_only_with_password_and_does_not_reveal_it(installation, monkeypatch):
    from shared.update_health_port import LocalUpdateHealthPort
    root, work, options, manager = installation
    args = {name: value for name, value in options.items() if name != 'password_provider'}
    before = list(manager.calls)
    with pytest.raises(UpdateError):
        run_pointer_linux_update(**args, action='start', password_env='HOMENSAI_TEST_ABSENT_SECRET')
    assert list(work.iterdir()) == [] and manager.calls == before
    monkeypatch.setenv('HOMENSAI_TEST_PASSWORD', 'synthetic password')
    monkeypatch.setattr(LocalUpdateHealthPort, 'health', lambda *_: None)
    result = run_pointer_linux_update(**args, action='start', password_env='HOMENSAI_TEST_PASSWORD')
    assert result['state']['outcome'] == 'updated'
    assert not result['process_identity_verified'] and not result['all_writers_verified']
    assert 'synthetic password' not in json.dumps(result)
    assert 'synthetic password' not in json.dumps(manager.calls)


def test_controller_reopens_and_reconciles_stop_without_repeating(installation, monkeypatch):
    import shared.systemd_lifecycle_port as lifecycle
    from shared.update_health_port import LocalUpdateHealthPort
    _, _, options, manager = installation
    args = {name: value for name, value in options.items() if name != 'password_provider'}
    monkeypatch.setenv('HOMENSAI_TEST_PASSWORD', 'synthetic password')
    original = lifecycle._atomic_write
    def lost_ack(path, value):
        if value.get('action') == 'stop' and value.get('status') == 'done':
            raise OSError('synthetic lost acknowledgement')
        return original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, '_atomic_write', lost_ack)
        with pytest.raises(UpdateError):
            run_pointer_linux_update(**args, action='start', password_env='HOMENSAI_TEST_PASSWORD')
    observed = inspect_started_pointer_group(**{name: value for name, value in args.items()
        if name not in ('interpreter', 'interpreter_sha256', 'timeout')})
    state = observed['state']
    calls = len([call for call in manager.calls if call[0] == 'stop'])
    with pytest.raises(UpdateError):
        run_pointer_linux_update(**args, action='reopen', password_env='HOMENSAI_TEST_PASSWORD')
    assert len([call for call in manager.calls if call[0] == 'stop']) == calls
    monkeypatch.setattr(LocalUpdateHealthPort, 'health', lambda *_: None)
    result = run_pointer_linux_update(**args, action='reconcile-stop', password_env='HOMENSAI_TEST_PASSWORD',
                                      reconcile_stop_key=state['transaction'] + '/stop')
    assert result['state']['outcome'] == 'updated'
    assert len([call for call in manager.calls if call[0] == 'stop']) == calls


def test_api_runs_pointer_update_with_secret_env(installation, monkeypatch):
    from shared import api, systemd_user, systemd_user_load
    from shared.update_health_port import LocalUpdateHealthPort
    from test_update_prepare import api_query
    root, work, options, manager = installation
    omitted = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner',
               'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    request = api_query({name: value for name, value in options.items() if name not in omitted})
    request['operation'] = 'run-pointer-linux-update'
    request['params'].update({name: options[name] for name in
                              ('unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes',
                               'interpreter', 'interpreter_sha256')})
    request['params'].update(password_env='HOMENSAI_TEST_PASSWORD', action='start',
                             reconcile_stop_key=None, timeout_seconds=5)
    validator = pytest.importorskip('jsonschema').Draft202012Validator(json.loads(
        (Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text()))
    assert validator.is_valid(request)
    monkeypatch.setattr(systemd_user, '_run', manager)
    monkeypatch.setattr(systemd_user_load, '_run', manager)
    monkeypatch.setattr(LocalUpdateHealthPort, 'health', lambda *_: None)
    before = list(manager.calls)
    from shared.i18n import translate
    for language in ('ru', 'en', 'de'):
        request['language'] = language
        failed = api.handle(request)
        assert failed['error']['code'] == 'update_run_failed'
        assert failed['error']['message'] == translate(language, 'api.update_run_failed')
    assert list(work.iterdir()) == []
    assert manager.calls == before
    monkeypatch.setenv('HOMENSAI_TEST_PASSWORD', 'synthetic password')
    result = api.handle(request)
    assert result['ok'] and result['result']['state']['outcome'] == 'updated'
    assert 'synthetic password' not in json.dumps(result) and str(root) not in json.dumps(result)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 60 and request['operation'] in caps['mutating_operations']


def test_controller_reconcile_never_stops_without_service_receipt(installation, monkeypatch):
    _, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    state = {'version': 1, 'transaction': 'f' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('stop'),
             'failed_phase': None, 'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    args = {name: value for name, value in options.items() if name != 'password_provider'}
    monkeypatch.setenv('HOMENSAI_TEST_PASSWORD', 'synthetic password')
    before = list(manager.calls)
    with pytest.raises(UpdateError):
        run_pointer_linux_update(**args, action='reconcile-stop', password_env='HOMENSAI_TEST_PASSWORD',
                                 reconcile_stop_key=state['transaction'] + '/stop')
    assert all(call[0] == 'show' for call in manager.calls[len(before):])
    assert plan.status() == state
    reopened = assemble_pointer_linux_update(**options, reopen=True,
                                             reconcile_stop_key=state['transaction'] + '/stop')
    before = list(manager.calls)
    with pytest.raises(UpdateIndeterminate):
        reopened.run(resume_indeterminate=True)
    assert all(call[0] == 'show' for call in manager.calls[len(before):])
    assert reopened.status() == state


@pytest.mark.parametrize('rollback', [False, True])
def test_inspection_can_report_completed_update_without_replaying(installation, monkeypatch, rollback):
    from shared.update_prepare import UpdatePrepareError, verify_started_linux_update
    root, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    def health(key, *_):
        if rollback and key.endswith('/health'):
            raise RuntimeError('synthetic failure')
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', health)
    state = plan.run()
    assert state['outcome'] == ('rolled_back' if rollback else 'updated')
    omitted = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner',
               'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    common = {name: value for name, value in options.items() if name not in omitted}
    with pytest.raises(UpdatePrepareError):
        verify_started_linux_update(**common)
    args = {**common, 'unit_dir': options['unit_dir'], 'load_key': options['load_key'],
            'runtime_key': options['runtime_key'], 'runtime_reserve_bytes': 0, 'runner': manager}
    before = len(manager.calls)
    observed = inspect_started_pointer_group(**args)
    assert observed['state'] == state and not observed['resume_authorized']
    assert all(call[0] == 'show' for call in manager.calls[before:])
    if not rollback:
        from shared import api, pointer_recovery
        from test_update_prepare import api_query
        request = api_query(common)
        request['operation'] = 'inspect-started-pointer-update'
        request['params'].update({name: args[name] for name in
                                  ('unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes')})
        monkeypatch.setattr(pointer_recovery, '_run', manager)
        result = api.handle(request)
        assert result['ok'] and result['result']['state'] == state
        assert not result['result']['resume_authorized']
    pointer = plan.ports.pointer
    pointer.write_text(json.dumps({'release': '2' if rollback else '1'}))
    with pytest.raises(UpdateError):
        inspect_started_pointer_group(**args)


def test_terminal_group_holds_locks_and_requires_bound_scope(installation, monkeypatch):
    from contextvars import copy_context
    from shared.pointer_terminal import _verified_terminal_group
    from shared.update_plan import LockBusy, operation_lock
    from shared.update_prepare import UpdatePrepareError, verify_started_linux_update
    root, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', lambda *_: None)
    state = plan.run()
    excluded = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner',
                'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    common = {name: value for name, value in options.items() if name not in excluded}
    with pytest.raises(UpdatePrepareError):
        verify_started_linux_update(**common, _allow_terminal=True, _locks_held=True)
    group_args = {name: value for name, value in options.items() if name not in
                  ('password_provider', 'interpreter', 'interpreter_sha256', 'timeout')}
    before = len(manager.calls)
    with _verified_terminal_group(**group_args) as (group, observed):
        assert observed['state'] == state and len(group.order) == 3
        assert verify_started_linux_update(**common, _allow_terminal=True, _locks_held=True)['state'] == state
        saved_context = copy_context()
        with pytest.raises(LockBusy):
            with operation_lock(plan.lock_path):
                pass
    assert all(call[0] == 'show' for call in manager.calls[before:])
    assert plan.status() == state
    with pytest.raises(UpdatePrepareError):
        verify_started_linux_update(**common, _allow_terminal=True, _locks_held=True)
    with pytest.raises(UpdatePrepareError):
        saved_context.run(lambda: verify_started_linux_update(**common, _allow_terminal=True, _locks_held=True))
    with pytest.raises(UpdateError):
        with _verified_terminal_group(**group_args):
            (root / 'runtime/lib/homensai/portable_launcher.py').write_text('tampered')
    with operation_lock(plan.lock_path):
        pass
    assert plan.status() == state
