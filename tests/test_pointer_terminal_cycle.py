"""Durable post-update service cycle with synthetic manager and signed packages."""
import json
from pathlib import Path

import pytest

from shared.pointer_terminal_cycle import cycle_terminal_pointer
from shared.pointer_update import assemble_pointer_linux_update
from shared.update_plan import UpdateError
from test_api_signed_preflight import signed_request
from test_pointer_update import installation
from test_systemd_pointer_signed import signed_pointer


@pytest.fixture
def completed(installation, monkeypatch):
    root, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', lambda *_: None)
    assert plan.run()['outcome'] == 'updated'
    args = {name: value for name, value in options.items() if name not in
            ('password_provider', 'interpreter', 'interpreter_sha256', 'timeout')}
    return root, plan, args, manager


def test_cycle_stops_and_starts_without_replaying(completed):
    _, plan, args, manager = completed
    before = len(manager.calls)
    assert cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)['state'] == 'stopped'
    stops = len([x for x in manager.calls[before:] if x[0] == 'stop'])
    assert stops == 3 and not manager.active
    assert cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)['state'] == 'stopped'
    assert len([x for x in manager.calls[before:] if x[0] == 'stop']) == stops
    assert cycle_terminal_pointer(**args, action='start', cycle_key='f' * 32)['state'] == 'started'
    starts = len([x for x in manager.calls[before:] if x[0] == 'start'])
    assert starts == 3 and len(manager.active) == 3
    assert cycle_terminal_pointer(**args, action='start', cycle_key='f' * 32)['state'] == 'started'
    assert len([x for x in manager.calls[before:] if x[0] == 'start']) == starts
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)
    assert plan.status()['outcome'] == 'updated'


def test_cycle_reconciles_lost_stop_without_repeating(completed, monkeypatch):
    import shared.pointer_terminal_cycle as cycle
    _, _, args, manager = completed
    original = cycle._write
    def lose_ack(path, state):
        if state['events'][-1]['status'] == 'done':
            raise OSError('synthetic lost acknowledgement')
        return original(path, state)
    before = len([x for x in manager.calls if x[0] == 'stop'])
    with monkeypatch.context() as patch:
        patch.setattr(cycle, '_write', lose_ack)
        with pytest.raises(UpdateError):
            cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)
    count = len([x for x in manager.calls if x[0] == 'stop'])
    assert count - before == 3 and not manager.active
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)
    result = cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32, reconcile=True)
    assert result['state'] == 'stopped' and not result['prior_command_confirmed']
    assert len([x for x in manager.calls if x[0] == 'stop']) == count


def test_cycle_refuses_tampered_unit_before_effect(completed):
    root, _, args, manager = completed
    unit = next((root.parent / 'units').glob('*.service'))
    unit.write_text('tampered')
    before = len(manager.calls)
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)
    assert all(call[0] == 'show' for call in manager.calls[before:])


def test_cycle_unknown_start_can_be_stopped_without_start_replay(completed, monkeypatch):
    import shared.pointer_terminal_cycle as cycle
    _, _, args, manager = completed
    cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)
    original = cycle._write
    def lose_start_ack(path, state):
        if state['events'][-1]['action'] == 'start' and state['events'][-1]['status'] == 'done':
            raise OSError('synthetic lost acknowledgement')
        return original(path, state)
    with monkeypatch.context() as patch:
        patch.setattr(cycle, '_write', lose_start_ack)
        with pytest.raises(UpdateError):
            cycle_terminal_pointer(**args, action='start', cycle_key='f' * 32)
    count = len([call for call in manager.calls if call[0] == 'start'])
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**args, action='start', cycle_key='f' * 32)
    assert len([call for call in manager.calls if call[0] == 'start']) == count
    assert cycle_terminal_pointer(**args, action='stop', cycle_key='9' * 32)['state'] == 'stopped'
    assert not manager.active


def test_cycle_after_rollback_uses_original_release(installation, monkeypatch):
    _, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    def fail_forward(key, *_):
        if key.endswith('/health'):
            raise RuntimeError('synthetic health failure')
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', fail_forward)
    assert plan.run()['outcome'] == 'rolled_back' and plan.ports.current_release() == '1'
    args = {name: value for name, value in options.items() if name not in
            ('password_provider', 'interpreter', 'interpreter_sha256', 'timeout')}
    assert cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)['state'] == 'stopped'
    assert cycle_terminal_pointer(**args, action='start', cycle_key='f' * 32)['state'] == 'started'
    assert plan.ports.current_release() == '1' and len(manager.active) == 3


def test_cycle_refuses_running_update_without_writing_journal(installation):
    root, _, options, manager = installation
    plan = assemble_pointer_linux_update(**options)
    state = {'version': 1, 'transaction': '8' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': 0, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    args = {name: value for name, value in options.items() if name not in
            ('password_provider', 'interpreter', 'interpreter_sha256', 'timeout')}
    before = len(manager.calls)
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)
    assert all(call[0] == 'show' for call in manager.calls[before:])
    assert not (root / '.homensai-control/pointer-terminal-cycle.json').exists()
    assert plan.status() == state


def test_api_cycles_updated_services_without_duplicate_commands(completed, monkeypatch):
    from shared import api, pointer_recovery
    from test_update_prepare import api_query
    root, plan, args, manager = completed
    excluded = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner'}
    request = api_query({name: value for name, value in args.items() if name not in excluded})
    request['operation'] = 'cycle-updated-pointer-linux'
    request['params'].update({name: args[name] for name in
                              ('unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes')})
    request['params'].update(action='stop', cycle_key='e' * 32, reconcile=False)
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert pytest.importorskip('jsonschema').Draft202012Validator(schema).is_valid(request)
    monkeypatch.setattr(pointer_recovery, '_run', manager)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 61 and request['operation'] in caps['mutating_operations']
    before = len(manager.calls)
    stopped = api.handle(request)
    assert stopped['ok'] and stopped['result']['state'] == 'stopped'
    assert str(root) not in json.dumps(stopped)
    assert api.handle(request)['result'] == stopped['result']
    assert len([call for call in manager.calls[before:] if call[0] == 'stop']) == 3
    request['params'].update(action='start', cycle_key='f' * 32)
    started = api.handle(request)
    assert started['ok'] and started['result']['state'] == 'started'
    assert not started['result']['health_verified'] and not started['result']['process_identity_verified']
    assert api.handle(request)['result'] == started['result']
    assert len([call for call in manager.calls[before:] if call[0] == 'start']) == 3
    request['params']['reconcile'] = True
    assert api.handle(request)['error']['code'] == 'invalid_request'
    assert plan.status()['outcome'] == 'updated'


def test_damaged_runtime_allows_stop_but_blocks_start(completed):
    from shared.pointer_recovery import inspect_started_pointer_group
    root, plan, args, manager = completed
    launcher = root / 'runtime/lib/homensai/portable_launcher.py'
    original = launcher.read_bytes()
    launcher.write_bytes(b'tampered')
    with pytest.raises(UpdateError):
        inspect_started_pointer_group(**args, _verify_runtime=False)
    assert cycle_terminal_pointer(**args, action='stop', cycle_key='e' * 32)['state'] == 'stopped'
    before = len([call for call in manager.calls if call[0] == 'start'])
    with pytest.raises(UpdateError):
        cycle_terminal_pointer(**args, action='start', cycle_key='f' * 32)
    assert len([call for call in manager.calls if call[0] == 'start']) == before
    launcher.write_bytes(original)
    assert cycle_terminal_pointer(**args, action='start', cycle_key='f' * 32)['state'] == 'started'
    assert plan.status()['outcome'] == 'updated'


def test_api_stops_damaged_runtime_and_refuses_restart(completed, monkeypatch):
    from shared import api, pointer_recovery
    from test_update_prepare import api_query
    root, _, args, manager = completed
    excluded = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner'}
    request = api_query({name: value for name, value in args.items() if name not in excluded})
    request['operation'] = 'cycle-updated-pointer-linux'
    request['params'].update({name: args[name] for name in
                              ('unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes')})
    request['params'].update(action='stop', cycle_key='e' * 32, reconcile=False)
    monkeypatch.setattr(pointer_recovery, '_run', manager)
    launcher = root / 'runtime/lib/homensai/portable_launcher.py'
    original = launcher.read_bytes()
    launcher.write_bytes(b'tampered')
    stopped = api.handle(request)
    assert stopped['ok'] and stopped['result']['state'] == 'stopped'
    request['params'].update(action='start', cycle_key='f' * 32)
    assert api.handle(request)['error']['code'] == 'linux_cycle_failed'
    assert not manager.active
    launcher.write_bytes(original)
    assert api.handle(request)['result']['state'] == 'started'
