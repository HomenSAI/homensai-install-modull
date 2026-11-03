"""Журнал первого запуска: повтор и неопределённый результат."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shared import systemd_fresh_start as start


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    root = tmp_path / 'installation'
    root.mkdir(mode=0o700)
    control = root / '.homensai-control'
    control.mkdir(mode=0o700)
    units = tmp_path / 'units'
    units.mkdir(mode=0o700)
    publication = {'version': 1, 'operation_id': 'b' * 32, 'binding': '0' * 64, 'status': 'done'}
    (control / 'user-systemd-publication.json').write_text(json.dumps(publication))
    calls = []
    class Group:
        def command(self, action, *, execute=False):
            calls.append((action, execute))
    group = Group()
    monkeypatch.setattr(start, 'verify_signed_linux_runtime', lambda *a, **kw: None)
    monkeypatch.setattr(start, 'audit_signed_wheels', lambda *a, **kw: None)
    monkeypatch.setattr(start, 'portable_signed_runtime_user_group', lambda *a, **kw: group)
    monkeypatch.setattr(start, 'signed_user_group', lambda *a, **kw: group)
    args = dict(trusted_public_key=b'x' * 32, expected_product_id='example', expected_version='1',
                modes=('autonomous',), unit_dir=str(units), load_key='c' * 32, runtime_key='a' * 32,
                reserve_bytes=0, operation_id='d' * 32)
    return root, args, calls, group


def test_unsafe_wheel_rejected_before_start_receipt(scenario, monkeypatch):
    from shared.wheel_audit import WheelAuditError
    root, args, calls, _ = scenario
    def reject(*a, **kw):
        raise WheelAuditError('invalid wheel')
    monkeypatch.setattr(start, 'audit_signed_wheels', reject)
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **args)
    assert not calls
    assert not (root / '.homensai-control/user-systemd-start.json').exists()


@pytest.mark.parametrize('entry,allowed', [('demo/__init__.py', True), ('../escape.py', False)])
def test_api_start_audits_real_signed_wheel(scenario, tmp_path, monkeypatch, entry, allowed):
    from shared.api import handle
    from shared.wheel_audit import audit_signed_wheels
    from test_wheel_audit import make_signed

    root, args, calls, _ = scenario
    params, package, marker = make_signed(tmp_path, entry=entry)
    monkeypatch.setattr(start, 'audit_signed_wheels', audit_signed_wheels)
    request = {'api_major': 1, 'request_id': 'signed-wheel-start', 'operation': 'start-portable-linux',
               'params': {**params, 'install_root': str(root), 'modes': list(args['modes']),
                          'unit_dir': args['unit_dir'], 'load_key': args['load_key'],
                          'runtime_key': args['runtime_key'], 'reserve_bytes': args['reserve_bytes'],
                          'idempotency_key': args['operation_id']}}
    response = handle(request)
    receipt = root / '.homensai-control/user-systemd-start.json'
    if allowed:
        assert response['ok'] and calls == [('start', True)]
        assert json.loads(receipt.read_text())['status'] == 'done'
    else:
        assert not response['ok'] and response['error']['code'] == 'linux_start_failed'
        assert not calls and not receipt.exists()
    assert package.is_file() and not marker.exists() and not (tmp_path / 'escape.py').exists()


def test_repeat_confirmed_start_does_not_send_command(scenario):
    root, args, calls, _ = scenario
    first = start.start_portable_fresh('package', b'envelope', str(root), **args)
    assert first['state'] == 'started' and first['health_verified'] is False
    assert start.start_portable_fresh('package', b'envelope', str(root), **args) == first
    assert calls == [('start', True)]
    assert json.loads((root / '.homensai-control/user-systemd-start.json').read_text())['status'] == 'done'
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **{**args, 'reserve_bytes': 1})
    assert calls == [('start', True)]


def test_failed_start_leaves_pending_and_same_key_retry_resumes(scenario, monkeypatch):
    root, args, calls, group = scenario
    def fail(action, *, execute=False):
        calls.append((action, execute))
        raise RuntimeError('SECRET-SENTINEL')
    monkeypatch.setattr(group, 'command', fail)
    with pytest.raises(start.FreshStartError) as error:
        start.start_portable_fresh('package', b'envelope', str(root), **args)
    assert 'SECRET-SENTINEL' not in str(error.value)
    receipt = root / '.homensai-control/user-systemd-start.json'
    before = receipt.read_bytes()
    assert json.loads(before)['status'] == 'pending'
    # Другая привязка не продолжает чужой pending.
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **{**args, 'reserve_bytes': 1})
    assert calls == [('start', True)] and receipt.read_bytes() == before
    # Неустойчивая группа (failed/activating) — отказ без команды.
    def unstable(*a, **kw):
        raise RuntimeError('failed unit')
    monkeypatch.setattr(start, 'portable_signed_runtime_user_group', unstable)
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **args)
    assert calls == [('start', True)] and receipt.read_bytes() == before
    requested = []
    def stable(*a, **kw):
        requested.append(kw.get('_require_stable'))
        return group
    monkeypatch.setattr(start, 'portable_signed_runtime_user_group', stable)
    monkeypatch.setattr(group, 'command', lambda action, execute=False: calls.append((action, execute)))
    result = start.start_portable_fresh('package', b'envelope', str(root), **args)
    assert result['state'] == 'started' and requested == [True]
    assert calls == [('start', True), ('start', True)]
    assert json.loads(receipt.read_text())['status'] == 'done'
    assert start.start_portable_fresh('package', b'envelope', str(root), **args) == result
    assert calls == [('start', True), ('start', True)]


def test_parallel_requests_issue_at_most_one_start(scenario, monkeypatch):
    root, args, calls, group = scenario
    entered, release = threading.Event(), threading.Event()
    def execute(action, *, execute=False):
        calls.append((action, execute))
        entered.set()
        assert release.wait(timeout=10)
    monkeypatch.setattr(group, 'command', execute)
    def invoke():
        try:
            return start.start_portable_fresh('package', b'envelope', str(root), **args)['state']
        except start.FreshStartError:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke)
        assert entered.wait(timeout=10)
        second = pool.submit(invoke)
        assert second.result(timeout=10) == 'refused'
        release.set()
        result = [first.result(timeout=10), 'refused']
    assert sorted(result) == ['refused', 'started']
    assert calls == [('start', True)]


def test_duplicate_operation_keys_are_refused(scenario):
    root, args, calls, _ = scenario
    for key in ('a', 'b', 'c'):
        with pytest.raises(start.FreshStartError):
            start.start_portable_fresh('package', b'envelope', str(root), **{**args, 'operation_id': key * 32})
    assert not calls


def test_stop_after_unknown_start_and_repeat_without_commands(scenario, monkeypatch):
    root, args, calls, group = scenario
    def fail(action, *, execute=False):
        calls.append((action, execute))
        raise RuntimeError('unknown')
    monkeypatch.setattr(group, 'command', fail)
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **args)
    monkeypatch.setattr(group, 'command', lambda action, execute=False: calls.append((action, execute)))
    stop_args = {**args, 'start_key': args['operation_id'], 'operation_id': 'e' * 32}
    stopped = start.stop_portable_fresh('package', b'envelope', str(root), **stop_args)
    assert stopped['state'] == 'stopped' and stopped['all_writers_stopped'] is False
    assert start.stop_portable_fresh('package', b'envelope', str(root), **stop_args) == stopped
    assert calls == [('start', True), ('stop', True)]


def test_stop_rejected_while_start_is_executing(scenario, monkeypatch):
    root, args, calls, group = scenario
    entered, release = threading.Event(), threading.Event()
    def execute(action, *, execute=False):
        calls.append((action, execute))
        entered.set()
        assert release.wait(timeout=10)
    monkeypatch.setattr(group, 'command', execute)
    stop_args = {**args, 'start_key': args['operation_id'], 'operation_id': 'e' * 32}
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(start.start_portable_fresh, 'package', b'envelope', str(root), **args)
        assert entered.wait(timeout=10)
        with pytest.raises(start.FreshStartError):
            start.stop_portable_fresh('package', b'envelope', str(root), **stop_args)
        release.set()
        assert running.result(timeout=10)['state'] == 'started'
    assert calls == [('start', True)]


def test_uncertain_stop_is_not_replayed(scenario, monkeypatch):
    root, args, calls, group = scenario
    start.start_portable_fresh('package', b'envelope', str(root), **args)
    def fail(action, *, execute=False):
        calls.append((action, execute))
        raise RuntimeError('response lost')
    monkeypatch.setattr(group, 'command', fail)
    stop_args = {**args, 'start_key': args['operation_id'], 'operation_id': 'e' * 32}
    for _ in range(2):
        with pytest.raises(start.FreshStartError):
            start.stop_portable_fresh('package', b'envelope', str(root), **stop_args)
    assert calls == [('start', True), ('stop', True)]
    record = json.loads((root / '.homensai-control/user-systemd-stop.json').read_text())
    assert record['status'] == 'pending'
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **args)


def test_reconcile_pending_stop_observes_without_repeating(scenario, monkeypatch):
    root, args, calls, group = scenario
    start.start_portable_fresh('package', b'envelope', str(root), **args)
    def fail(action, *, execute=False):
        calls.append((action, execute))
        raise RuntimeError('response lost')
    monkeypatch.setattr(group, 'command', fail)
    stop_args = {**args, 'start_key': args['operation_id'], 'operation_id': 'e' * 32}
    with pytest.raises(start.FreshStartError):
        start.stop_portable_fresh('package', b'envelope', str(root), **stop_args)
    receipt = root / '.homensai-control/user-systemd-stop.json'
    before = receipt.read_bytes()
    original = start.signed_user_group
    def active(*a, **kw):
        raise RuntimeError('service remains active')
    monkeypatch.setattr(start, 'signed_user_group', active)
    with pytest.raises(start.FreshStartError):
        start.stop_portable_fresh('package', b'envelope', str(root), **stop_args, reconcile=True)
    assert receipt.read_bytes() == before
    monkeypatch.setattr(start, 'signed_user_group', original)
    result = start.stop_portable_fresh('package', b'envelope', str(root), **stop_args, reconcile=True)
    assert result['services_stopped'] and result['prior_stop_command_confirmed'] is False
    assert start.stop_portable_fresh('package', b'envelope', str(root), **stop_args, reconcile=True) == result
    assert json.loads(receipt.read_text())['status'] == 'done'
    assert calls == [('start', True), ('stop', True)]


def test_reconcile_pending_start_observes_without_repeating(scenario, monkeypatch):
    root, args, calls, group = scenario
    def fail(action, *, execute=False):
        calls.append((action, execute))
        raise RuntimeError('response lost')
    monkeypatch.setattr(group, 'command', fail)
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **args)
    receipt = root / '.homensai-control/user-systemd-start.json'
    before = receipt.read_bytes()
    original = start.signed_user_group
    monkeypatch.setattr(start, 'signed_user_group', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('inactive')))
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **args, reconcile=True)
    assert receipt.read_bytes() == before
    monkeypatch.setattr(start, 'signed_user_group', original)
    result = start.start_portable_fresh('package', b'envelope', str(root), **args, reconcile=True)
    assert result['services_started'] and result['prior_start_command_confirmed'] is False
    assert start.start_portable_fresh('package', b'envelope', str(root), **args, reconcile=True) == result
    assert json.loads(receipt.read_text())['status'] == 'done'
    assert calls == [('start', True)]


@pytest.fixture
def cycle_scenario(scenario, monkeypatch):
    from shared import systemd_cycle as cycle
    root, args, calls, group = scenario
    start.start_portable_fresh('package', b'envelope', str(root), **args)
    stop_args = {**args, 'start_key': args['operation_id'], 'operation_id': 'e' * 32}
    start.stop_portable_fresh('package', b'envelope', str(root), **stop_args)
    monkeypatch.setattr(cycle, 'verify_signed_linux_runtime', lambda *a, **kw: None)
    monkeypatch.setattr(cycle, 'audit_signed_wheels', lambda *a, **kw: None)
    monkeypatch.setattr(cycle, 'portable_signed_runtime_user_group', lambda *a, **kw: group)
    monkeypatch.setattr(cycle, 'signed_user_group', lambda *a, **kw: group)
    cycle_args = {**args, 'initial_start_key': 'd' * 32, 'initial_stop_key': 'e' * 32}
    calls.clear()
    return cycle, root, cycle_args, calls, group


def test_cycle_rejects_unsafe_wheel_before_writing(cycle_scenario, monkeypatch):
    from shared.wheel_audit import WheelAuditError
    cycle, root, args, calls, _ = cycle_scenario
    def reject(*a, **kw):
        raise WheelAuditError('invalid wheel')
    monkeypatch.setattr(cycle, 'audit_signed_wheels', reject)
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root),
                             **{**args, 'operation_id': '1' * 32, 'action': 'start'})
    assert not calls
    assert not (root / '.homensai-control/user-systemd-cycle-v2.json').exists()


def test_multiple_cycles_and_stale_keys(cycle_scenario):
    cycle, root, args, calls, _ = cycle_scenario
    for action, key in [('start', '1'), ('stop', '2'), ('start', '3'), ('stop', '4')]:
        options = {**args, 'operation_id': key * 32, 'action': action}
        first = cycle.cycle_portable('package', b'envelope', str(root), **options)
        assert cycle.cycle_portable('package', b'envelope', str(root), **options) == first
    assert calls == [('start', True), ('stop', True), ('start', True), ('stop', True)]
    state = json.loads((root / '.homensai-control/user-systemd-cycle-v2.json').read_text())
    assert [e['status'] for e in state['events']] == ['done'] * 4
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root), **{**args, 'operation_id': '1' * 32, 'action': 'start'})
    with pytest.raises(start.FreshStartError):
        start.stop_portable_fresh('package', b'envelope', str(root), **{
            **{k: v for k, v in args.items() if not k.startswith('initial_')},
            'start_key': 'd' * 32, 'operation_id': 'e' * 32})


def test_cycle_recovers_pending_and_can_stop_partial_start(cycle_scenario, monkeypatch):
    cycle, root, args, calls, group = cycle_scenario
    def fail(action, *, execute=False):
        calls.append((action, execute))
        raise RuntimeError('lost')
    monkeypatch.setattr(group, 'command', fail)
    opts = {**args, 'action': 'start', 'operation_id': '1' * 32}
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root), **opts)
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root), **opts)
    result = cycle.cycle_portable('package', b'envelope', str(root), **opts, reconcile=True)
    assert result['prior_command_confirmed'] is False and calls == [('start', True)]
    monkeypatch.setattr(group, 'command', lambda action, execute=False: calls.append((action, execute)))
    cycle.cycle_portable('package', b'envelope', str(root), **{**args, 'action': 'stop', 'operation_id': '2' * 32})
    monkeypatch.setattr(group, 'command', fail)
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root), **{**args, 'action': 'start', 'operation_id': '3' * 32})
    monkeypatch.setattr(group, 'command', lambda action, execute=False: calls.append((action, execute)))
    cycle.cycle_portable('package', b'envelope', str(root), **{**args, 'action': 'stop', 'operation_id': '4' * 32})
    state = json.loads((root / '.homensai-control/user-systemd-cycle-v2.json').read_text())
    assert [e['status'] for e in state['events']] == ['done', 'done', 'interrupted', 'done']


def test_cycle_rejects_unconfirmed_recovery_and_modified_history(cycle_scenario, monkeypatch):
    cycle, root, args, calls, group = cycle_scenario
    def fail(action, *, execute=False):
        calls.append((action, execute))
        raise RuntimeError('lost')
    monkeypatch.setattr(group, 'command', fail)
    opts = {**args, 'action': 'start', 'operation_id': '1' * 32}
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root), **opts)
    path = root / '.homensai-control/user-systemd-cycle-v2.json'
    before = path.read_bytes()
    monkeypatch.setattr(cycle, 'signed_user_group', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('inactive')))
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root), **opts, reconcile=True)
    assert path.read_bytes() == before and calls == [('start', True)]
    state = json.loads(before)
    state['events'].append({'action': 'start', 'key': '2' * 32, 'status': 'done'})
    path.write_text(json.dumps(state))
    with pytest.raises(start.FreshStartError):
        cycle.cycle_portable('package', b'envelope', str(root), **opts, reconcile=True)
    assert calls == [('start', True)]
