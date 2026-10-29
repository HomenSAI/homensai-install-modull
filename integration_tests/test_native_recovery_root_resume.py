"""Registered recovery resumes only proven complete-root boundaries.

Real DEV signatures, public factory, SQLite effects and controller SIGKILL.
Not hardware power-loss, another OS, or arbitrary mid-file recovery acceptance.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from shared.adapters_fs import FilesystemPorts
from test_native_adapter_uninstall_boundary import case, interrupted_operation, effect_rows
from test_native_migration import migration


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cut_after_first_root(case, review):
    pid = os.fork()
    if pid == 0:
        actual = FilesystemPorts._reconcile
        def reconcile(source, destination, preserved):
            actual(source, destination, preserved)
            source = Path(source)
            if (source.name == 'data' and source.parent.name == 'roots'
                    and source.parent.parent.name.endswith('-restore-candidate')):
                os.kill(os.getpid(), signal.SIGKILL)
        FilesystemPorts._reconcile = reconcile
        try:
            operation = case.submit(review, 'root-boundary-cut')
            os.write(2, json.dumps({'pre_cut_status': operation['status'], 'error': operation.get('error')}).encode() + b'\n')
        except BaseException: os._exit(86)
        os._exit(87)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL


@pytest.mark.parametrize('action,legacy_lock', [('restore', False), ('rollback', False), ('restore', True)])
def test_original_public_operation_resumes_after_complete_first_root_without_recopy(case, monkeypatch, action, legacy_lock):
    case.run('stop', case.second, 'stop-before-controller-fork')
    root = case.fixture.root
    service_lock = root / 'services/operation.lock'
    if legacy_lock:
        from shared.update_plan import operation_lock
        with operation_lock(service_lock): pass
        lock_before = service_lock.read_bytes(), service_lock.stat().st_ino, service_lock.stat().st_ctime_ns
    review = case.plan(action, case.second, archive_path=case.archive['path'], acknowledge_data_loss=True)
    hook_log = case.fixture.base / 'root-recovery-hook-effects.jsonl'
    actual = ExecutableProductAdapter._execute
    def execute(adapter, hook, request, request_digest):
        result = actual(adapter, hook, request, request_digest)
        record = {'hook': hook, 'release': adapter.options['release'], 'key': request['idempotency_key']}
        with hook_log.open('a') as stream:
            stream.write(json.dumps(record) + '\n'); stream.flush(); os.fsync(stream.fileno())
        return result
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', execute)
    ledger = root / '.hinstall/product-adapter/release-2/adapter-invocations.sqlite3'
    ledger_identity = ledger.stat().st_dev, ledger.stat().st_ino
    cut_after_first_root(case, review)
    assert case.document.read_text() == 'snapshot document'
    first_root_file_identity = case.document.stat().st_dev, case.document.stat().st_ino
    # Empty backup is already the desired root. An old==target root is a
    # verified no-op, not a reason to reconcile or change its directory ctime.
    backup = root / 'backup'
    assert list(backup.iterdir()) == []
    backup_identity = backup.stat().st_ino, backup.stat().st_ctime_ns
    snapshots = {str(p): sha(p) for p in case.fixture.base.rglob('*.hsac')}
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    resumed = reopened.resume(operation['operation_id'], background=False)
    assert resumed['status'] == 'SUCCEEDED', resumed.get('error')
    assert resumed['operation_id'] == operation['operation_id']
    assert (case.document.stat().st_dev, case.document.stat().st_ino) == first_root_file_identity
    assert (backup.stat().st_ino, backup.stat().st_ctime_ns) == backup_identity
    if legacy_lock:
        assert (service_lock.read_bytes(), service_lock.stat().st_ino, service_lock.stat().st_ctime_ns) == lock_before
    assert {str(p): sha(p) for p in case.fixture.base.rglob('*.hsac')} == snapshots
    assert (ledger.stat().st_dev, ledger.stat().st_ino) == ledger_identity
    hooks = [json.loads(line) for line in hook_log.read_text().splitlines()]
    assert len([row for row in hooks if row['hook'] == 'quiesce' and row['release'] == 'release-2']) == 1
    assert len(effect_rows(case.database, 'restore')) == len(effect_rows(case.database, 'post_restore')) == 1
    with sqlite3.connect(case.database.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert db.execute('SELECT value FROM user_state').fetchall() == [('snapshot user state',)]
    assert case.credential.read_text() == 'synthetic snapshot credential'
    assert resumed['result']['health']['release'] == 'release-1'
    case.fixture.current = case.first
    case.run('stop', case.first, 'stop-after-recovery')


def test_new_public_instance_retires_marker_after_native_terminal_before_return_without_hooks(case, monkeypatch):
    from homensai_install import engine as engine_module
    case.run('stop', case.second, 'stop-before-terminal-fork')
    review = case.plan('restore', case.second, archive_path=case.archive['path'])
    token = digest([review['plan_id'], review['plan_digest']])[:32]
    journal = case.fixture.root / '.hinstall/operations' / (token + '.json')
    owner = case.fixture.root / '.hinstall/installation.json'
    child = os.fork()
    if child == 0:
        actual = engine_module._write
        def write(path, value, **kwargs):
            actual(path, value, **kwargs)
            if Path(path) == journal and value.get('status') == 'SUCCEEDED' and value.get('recovery_root_terminal_id'):
                os.kill(os.getpid(), signal.SIGKILL)
        engine_module._write = write
        try: case.submit(review, 'native-terminal-cut')
        except BaseException: os._exit(86)
        os._exit(87)
    assert os.waitstatus_to_exitcode(os.waitpid(child, 0)[1]) == -signal.SIGKILL
    assert json.loads(journal.read_text())['status'] == 'SUCCEEDED'
    assert json.loads(owner.read_text())['pending_recovery']['operation_id'] == 'op_' + token
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    with monkeypatch.context() as readonly:
        readonly.setattr(ExecutableProductAdapter, '_execute', lambda *a, **k: pytest.fail('terminal retirement cannot run hooks'))
        resumed = reopened.resume(operation['operation_id'], background=False)
    assert resumed['status'] == 'SUCCEEDED' and resumed['operation_id'] == operation['operation_id']
    assert 'pending_recovery' not in json.loads(owner.read_text())
    case.fixture.current = case.first
    case.run('stop', case.first, 'stop-after-terminal-retirement')


@pytest.mark.parametrize('status', ['SUCCEEDED', 'FAILED_ROLLED_BACK'])
def test_native_success_flag_without_terminal_authority_never_bypasses_pending_fence(case, monkeypatch, status):
    case.run('stop', case.second, 'stop-before-forged-terminal')
    review = case.plan('restore', case.second, archive_path=case.archive['path'])
    cut_after_first_root(case, review)
    token = digest([review['plan_id'], review['plan_digest']])[:32]
    journal = case.fixture.root / '.hinstall/operations' / (token + '.json')
    value = json.loads(journal.read_text()); value['status'] = status
    value['result'] = {'status': 'SUCCEEDED', 'synthetic_forged_flag': True}
    journal.write_text(json.dumps(value)); journal.chmod(0o600)
    before = case.document.read_bytes(), case.document.stat().st_ino
    reopened = case.new_api(); operation = interrupted_operation(reopened, review)
    with monkeypatch.context() as readonly:
        readonly.setattr(ExecutableProductAdapter, '_execute', lambda *a, **k: pytest.fail('forged terminal cannot run hooks'))
        resumed = reopened.resume(operation['operation_id'], background=False)
    assert resumed['status'] == 'RECOVERY_REQUIRED'
    assert resumed['error']['code'] in {'RECOVERY_REQUIRED', 'INTEGRITY_FAILED'}
    assert (case.document.read_bytes(), case.document.stat().st_ino) == before


def test_numeric_complete_flag_cannot_authorize_a_pending_root(case, monkeypatch):
    case.run('stop', case.second, 'stop-before-numeric-progress')
    review = case.plan('restore', case.second, archive_path=case.archive['path'])
    cut_after_first_root(case, review)
    token = digest([review['plan_id'], review['plan_digest']])[:32]
    journal = case.fixture.root / '.hinstall/operations' / (token + '.json')
    value = json.loads(journal.read_text())
    value['recovery_root_progress']['data']['complete'] = 1
    journal.write_text(json.dumps(value)); journal.chmod(0o600)
    before = case.document.read_bytes(), case.document.stat().st_ino, case.document.stat().st_ctime_ns
    api = case.new_api(); operation = interrupted_operation(api, review)
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', lambda *a, **k: pytest.fail('invalid typed progress cannot invoke hooks'))
    resumed = api.resume(operation['operation_id'], background=False)
    assert resumed['status'] == 'RECOVERY_REQUIRED'
    assert resumed['error']['code'] == 'RECOVERY_REQUIRED'
    assert (case.document.read_bytes(), case.document.stat().st_ino, case.document.stat().st_ctime_ns) == before
