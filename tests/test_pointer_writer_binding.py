"""The cooperative writer lock is part of a signed update checkpoint."""
import pytest

from shared.pointer_recovery import inspect_started_pointer_group
from shared.pointer_update import assemble_pointer_linux_update
from shared.pointer_terminal_cycle import cycle_terminal_pointer
from shared.update_plan import EffectIndeterminate, UpdateError, UpdateIndeterminate
from shared.writer_guard import CooperativeWriterLock
from test_api_signed_preflight import signed_request
from test_pointer_update import installation
from test_systemd_pointer_signed import signed_pointer


def test_started_update_requires_original_writer_lock_to_reopen(installation, monkeypatch):
    root, _, options, manager = installation
    directory = root.parent / 'writer-locks'
    directory.mkdir(mode=0o700)
    original = CooperativeWriterLock(str(directory / 'original.lock'))
    different = CooperativeWriterLock(str(directory / 'different.lock'))
    plan = assemble_pointer_linux_update(**options, writer_lock=original)
    def interrupt_start(*_):
        raise EffectIndeterminate('synthetic interruption')
    monkeypatch.setattr(plan.ports.effects, 'start', interrupt_start)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    before = plan.status()
    assert before['indeterminate'] and plan.ports.current_release() == '2'
    with pytest.raises(UpdateError):
        assemble_pointer_linux_update(**options, reopen=True)
    with pytest.raises(UpdateError):
        assemble_pointer_linux_update(**options, writer_lock=different, reopen=True)
    reopened = assemble_pointer_linux_update(**options, writer_lock=original, reopen=True)
    assert reopened.status() == before
    excluded = {'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    inspect_args = {name: value for name, value in options.items() if name not in excluded}
    with pytest.raises(UpdateError):
        inspect_started_pointer_group(**inspect_args)
    assert inspect_started_pointer_group(**inspect_args, writer_lock_path=str(original.path))['state'] == before
    assert len(manager.active) == 0
    monkeypatch.setattr(reopened.ports.effects.health_port, 'health', lambda *_: None)
    assert reopened.run(resume_indeterminate=True)['outcome'] == 'updated'
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**inspect_args, action='stop', cycle_key='e' * 32)
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**inspect_args, action='stop', cycle_key='e' * 32,
                               writer_lock_path=str(different.path))
    assert cycle_terminal_pointer(**inspect_args, action='stop', cycle_key='e' * 32,
                                  writer_lock_path=str(original.path))['state'] == 'stopped'
    assert cycle_terminal_pointer(**inspect_args, action='start', cycle_key='f' * 32,
                                  writer_lock_path=str(original.path))['state'] == 'started'


def test_api_keeps_writer_binding_and_rejects_live_writer(installation, monkeypatch):
    import json
    from pathlib import Path
    from shared import api, pointer_recovery, systemd_user, systemd_user_load
    from shared.update_health_port import LocalUpdateHealthPort
    from test_update_prepare import api_query

    root, work, options, manager = installation
    directory = root.parent / 'writer-locks'
    directory.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(directory / 'original.lock'))
    excluded = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner',
                'password_provider', 'interpreter', 'interpreter_sha256', 'timeout'}
    request = api_query({name: value for name, value in options.items() if name not in excluded})
    request['operation'] = 'run-pointer-linux-update'
    request['params'].update({name: options[name] for name in
        ('unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'interpreter', 'interpreter_sha256')})
    request['params'].update(password_env='HOMENSAI_TEST_PASSWORD', action='start', reconcile_stop_key=None,
                             timeout_seconds=5, writer_lock_path=str(guard.path))
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert pytest.importorskip('jsonschema').Draft202012Validator(schema).is_valid(request)
    monkeypatch.setenv('HOMENSAI_TEST_PASSWORD', 'synthetic password')
    monkeypatch.setattr(systemd_user, '_run', manager)
    monkeypatch.setattr(systemd_user_load, '_run', manager)
    monkeypatch.setattr(pointer_recovery, '_run', manager)
    monkeypatch.setattr(LocalUpdateHealthPort, 'health', lambda *_: None)
    with guard.hold(exclusive=False):
        failure = api.handle(request)
    assert failure['error']['code'] == 'update_run_failed'
    inspect = {**request, 'operation': 'inspect-started-pointer-update', 'params': {
        name: value for name, value in request['params'].items() if name not in
        ('password_env', 'action', 'reconcile_stop_key', 'interpreter', 'interpreter_sha256', 'timeout_seconds')}}
    result = api.handle(inspect)
    assert result['ok'] and result['result']['state']['indeterminate']
    assert result['result']['state']['index'] == 1 and not list(work.glob('*.hsac'))
    lock_path = inspect['params'].pop('writer_lock_path')
    assert api.handle(inspect)['error']['code'] == 'update_inspect_failed'
    inspect['params']['writer_lock_path'] = str(directory / 'different.lock')
    assert api.handle(inspect)['error']['code'] == 'update_inspect_failed'
    inspect['params']['writer_lock_path'] = lock_path
    assert api.handle(inspect)['result']['state'] == result['result']['state']
