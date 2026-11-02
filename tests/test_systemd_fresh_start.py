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
    monkeypatch.setattr(start, 'portable_signed_runtime_user_group', lambda *a, **kw: group)
    monkeypatch.setattr(start, 'signed_user_group', lambda *a, **kw: group)
    args = dict(trusted_public_key=b'x' * 32, expected_product_id='example', expected_version='1',
                modes=('autonomous',), unit_dir=str(units), load_key='c' * 32, runtime_key='a' * 32,
                reserve_bytes=0, operation_id='d' * 32)
    return root, args, calls, group


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


def test_failed_start_leaves_pending_and_refuses_retry(scenario, monkeypatch):
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
    with pytest.raises(start.FreshStartError):
        start.start_portable_fresh('package', b'envelope', str(root), **args)
    assert calls == [('start', True)] and receipt.read_bytes() == before


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
