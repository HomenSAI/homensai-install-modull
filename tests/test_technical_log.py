"""The optional event log has a narrow schema and fails closed."""
import base64
import json
import os
import sys

import pytest

from shared.interactive_install import run
from shared.technical_log import TechnicalLogError, record

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux-only event log')


@pytest.fixture
def private(tmp_path):
    path = tmp_path / 'private'
    path.mkdir(mode=0o700)
    return path


def test_log_contains_only_allowlisted_events_and_time(private):
    path = private / 'events.jsonl'
    record(path, 'inspection_started')
    record(path, 'install_recorded')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row['event'] for row in rows] == ['inspection_started', 'install_recorded']
    assert all(set(row) == {'time_utc', 'event'} and row['time_utc'].endswith('+00:00') for row in rows)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(TechnicalLogError):
        record(path, 'password=synthetic-secret')
    assert 'synthetic-secret' not in path.read_text()


def test_log_rejects_symlink_unsafe_directory_and_partial_tail(private, tmp_path):
    target = tmp_path / 'outside'
    target.write_text('original')
    link = private / 'events'
    link.symlink_to(target)
    with pytest.raises(TechnicalLogError):
        record(link, 'inspection_started')
    assert target.read_text() == 'original'
    unsafe = tmp_path / 'unsafe'
    unsafe.mkdir(mode=0o755)
    with pytest.raises(TechnicalLogError):
        record(unsafe / 'events', 'inspection_started')
    partial = private / 'partial'
    partial.write_text('{"unfinished"')
    partial.chmod(0o600)
    with pytest.raises(TechnicalLogError):
        record(partial, 'inspection_started')
    assert partial.read_text() == '{"unfinished"'


def test_failed_log_before_effect_prevents_api_call():
    calls = []
    def fail(event):
        raise TechnicalLogError('synthetic')
    with pytest.raises(TechnicalLogError):
        run({}, 'en', ask=lambda _: 'INSTALL', write=lambda _: None,
            handle=lambda request: calls.append(request), emit=fail)
    assert calls == []


def test_busy_log_does_not_append_or_wait(private):
    import fcntl
    path = private / 'events'
    record(path, 'inspection_started')
    before = path.read_bytes()
    with path.open('rb') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TechnicalLogError):
            record(path, 'install_started')
    assert path.read_bytes() == before


def test_confirmed_install_emits_only_fixed_events(private):
    params = {'expected_product_id': 'demo', 'expected_version': '1', 'install_root': '/secret-location',
              'package_path': '/secret-package', 'unit_dir': '/units', 'work_dir': '/work',
              'modes': ['autonomous'], 'trusted_public_key': base64.b64encode(b'x' * 32).decode('ascii')}
    path = private / 'events.jsonl'
    calls = []
    def handle(request):
        calls.append(request['operation'])
        return {'ok': True, 'result': {'state': 'no_checkpoint' if len(calls) == 1 else 'recorded_complete'}}
    assert run(params, 'en', ask=lambda _: 'INSTALL', write=lambda _: None,
               handle=handle, emit=lambda event: record(path, event)) == 0
    assert [json.loads(line)['event'] for line in path.read_text().splitlines()] == [
        'inspection_started', 'inspection_succeeded', 'install_started', 'install_recorded']
    assert 'secret' not in path.read_text()


def test_log_failure_after_effect_cannot_report_confirmed_install():
    params = {'expected_product_id': 'demo', 'expected_version': '1', 'install_root': '/install',
              'package_path': '/package', 'unit_dir': '/units', 'work_dir': '/work',
              'modes': ['autonomous'], 'trusted_public_key': base64.b64encode(b'x' * 32).decode('ascii')}
    calls, lines = [], []
    def handle(request):
        calls.append(request['operation'])
        return {'ok': True, 'result': {'state': 'no_checkpoint' if len(calls) == 1 else 'recorded_complete'}}
    def emit(event):
        if event == 'install_recorded':
            raise TechnicalLogError('synthetic failure')
    with pytest.raises(TechnicalLogError):
        run(params, 'en', ask=lambda _: 'INSTALL', write=lines.append, handle=handle, emit=emit)
    assert calls == ['inspect-portable-linux-install', 'install-portable-linux']
    assert not any('workflow was recorded complete' in line for line in lines)
