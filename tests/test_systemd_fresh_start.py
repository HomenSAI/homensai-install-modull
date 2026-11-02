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
    barrier = threading.Barrier(2)
    def build(*a, **kw):
        barrier.wait(timeout=10)
        return group
    monkeypatch.setattr(start, 'portable_signed_runtime_user_group', build)
    def invoke():
        try:
            return start.start_portable_fresh('package', b'envelope', str(root), **args)['state']
        except start.FreshStartError:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: invoke(), range(2)))
    assert sorted(result) == ['refused', 'started']
    assert calls == [('start', True)]


def test_duplicate_operation_keys_are_refused(scenario):
    root, args, calls, _ = scenario
    for key in ('a', 'b', 'c'):
        with pytest.raises(start.FreshStartError):
            start.start_portable_fresh('package', b'envelope', str(root), **{**args, 'operation_id': key * 32})
    assert not calls
