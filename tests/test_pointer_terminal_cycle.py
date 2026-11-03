"""Durable post-update service cycle with synthetic manager and signed packages."""
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
