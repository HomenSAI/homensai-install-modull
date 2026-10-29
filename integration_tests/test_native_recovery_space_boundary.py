"""Public recovery space failures with real crypto and signed DEV adapters.

Faults affect only the selected recovery receipt call. Trusted verification,
product hooks and filesystem effects otherwise execute unchanged. These are
Linux source boundaries, not installed/frozen or clean-host acceptance.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from homensai_install.recovery_receipts import RecoveryBackupReceiptStore
import homensai_install.recovery_receipts as receipt_module
from shared import archive
from shared.adapters_fs import FilesystemPorts
from test_native_adapter_uninstall_boundary import case
from test_native_migration import migration


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image(root):
    """Include root/empty-directory identity and private attrs, excluding atime."""
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        item = {'identity': (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)}
        if stat.S_ISREG(info.st_mode):
            item['sha256'] = sha(path)
        elif stat.S_ISLNK(info.st_mode):
            item['link'] = os.readlink(path)
        result[path.relative_to(root).as_posix()] = item
    return result


def roots_image(root):
    return {name: image(root / name) for name in ('data', 'backup', 'services', 'config', 'runtime')}


class NoSpaceWriter:
    """The actual exclusive snapshot fd exists; its first write fails ENOSPC."""
    def __init__(self, stream, fired):
        self.stream, self.fired = stream, fired

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, data):
        self.fired.append('snapshot_write')
        raise OSError(errno.ENOSPC, 'SYNTHETIC_PRIVATE_SPACE_ERROR')


CASES = [
    ('issue', 'space'), ('issue', 'snapshot_write'), ('issue', 'receipt_atomic'),
    ('verify', 'space'), ('verify', 'snapshot_write'),
]


@pytest.mark.parametrize('action', ['restore', 'rollback'])
@pytest.mark.parametrize('receipt_phase,fault', CASES,
    ids=['issue-space', 'issue-snapshot', 'issue-atomic', 'verify-space', 'verify-snapshot'])
def test_public_recovery_space_refusal_precedes_first_root_write(case, monkeypatch, action, receipt_phase, fault):
    root = case.fixture.root
    review = case.plan(action, case.second, archive_path=case.archive['path'], acknowledge_data_loss=True)
    token = digest([review['plan_id'], review['plan_digest']])[:32]
    work = Path(case.second['options'].get('work_dir', str(root.parent / ('.' + root.name + '-recovery'))))
    safety = work / (token + '-before-' + action + '.hsac')
    receipts = root / '.hinstall/recovery-receipts'
    native = root / '.hinstall/operations' / (token + '.json')
    original_source = sha(Path(case.archive['path']))
    hooks, root_writes, fired, captured = [], [], [], {}
    actual_hook = ExecutableProductAdapter._execute
    actual_reconcile = FilesystemPorts._reconcile
    actual_method = getattr(RecoveryBackupReceiptStore, receipt_phase)
    actual_fdopen = os.fdopen
    actual_atomic = receipt_module._atomic_write
    destinations = {root / name for name in ('data', 'backup', 'services', 'config', 'runtime')}

    def observe_hook(adapter, hook, request, request_digest):
        hooks.append((adapter.options['release'], hook))
        return actual_hook(adapter, hook, request, request_digest)

    def observe_reconcile(source, destination, preserved):
        if Path(destination) in destinations and Path(source).parent.name == 'roots':
            root_writes.append(str(destination))
        return actual_reconcile(source, destination, preserved)

    def receipt_call(store, *args, **kwargs):
        if store.directory != receipts:
            return actual_method(store, *args, **kwargs)
        assert not captured, 'Fault must be reached on the first selected real receipt call'
        assert safety.is_file()
        captured.update(roots=roots_image(root), safety_sha256=sha(safety),
            receipts={p.name: image(p) for p in sorted(receipts.glob('*.json'))},
            source_sha256=sha(Path(case.archive['path'])))
        with monkeypatch.context() as constrained:
            if fault == 'space':
                def no_space(parent):
                    assert receipts in Path(parent).parents or Path(parent) == receipts
                    fired.append('space')
                    return SimpleNamespace(free=0)
                constrained.setattr(archive.shutil, 'disk_usage', no_space)
            elif fault == 'snapshot_write':
                def snapshot_fdopen(fd, mode='r', *other, **options):
                    endpoint = Path(os.readlink('/proc/self/fd/' + str(fd)))
                    stream = actual_fdopen(fd, mode, *other, **options)
                    if (endpoint.name == 'archive.hsac' and endpoint.parent.name.startswith('verify-')
                            and endpoint.parent.parent == receipts and mode == 'wb'):
                        return NoSpaceWriter(stream, fired)
                    return stream
                constrained.setattr(os, 'fdopen', snapshot_fdopen)
            else:
                def no_space_publication(path, value):
                    if Path(path).parent == receipts:
                        assert value['body']['kind'] == 'before_recovery_backup'
                        assert value['body']['recovery_action'] == action
                        fired.append('receipt_atomic')
                        raise OSError(errno.ENOSPC, 'SYNTHETIC_PRIVATE_SPACE_ERROR')
                    return actual_atomic(path, value)
                constrained.setattr(receipt_module, '_atomic_write', no_space_publication)
            return actual_method(store, *args, **kwargs)

    monkeypatch.setattr(ExecutableProductAdapter, '_execute', observe_hook)
    monkeypatch.setattr(FilesystemPorts, '_reconcile', observe_reconcile)
    monkeypatch.setattr(RecoveryBackupReceiptStore, receipt_phase, receipt_call)
    operation = case.submit(review, 'recovery-' + action + '-' + receipt_phase + '-' + fault)

    # Check preservation before the new error-code expectation: baseline RED
    # must not obscure whether an incorrect error accompanies destructive work.
    assert captured and fired == [fault]
    assert roots_image(root) == captured['roots']
    assert sha(safety) == captured['safety_sha256']
    assert sha(Path(case.archive['path'])) == original_source == captured['source_sha256']
    assert {p.name: image(p) for p in sorted(receipts.glob('*.json'))} == captured['receipts']
    assert root_writes == []
    assert not any(hook in ('restore', 'post_restore') for _, hook in hooks)
    assert case.document.read_text() == 'latest document after snapshot'
    assert case.credential.read_text() == 'synthetic latest credential'
    assert stat.S_IMODE(case.credential.stat().st_mode) == 0o600
    state = json.loads(native.read_text())
    assert 'restore' not in state['completed']
    assert not state.get('recovery_root_receipt_id') and not state.get('recovery_replace_started')
    if receipt_phase == 'issue':
        assert 'before_' + action not in state['completed']
        assert captured['receipts'] == {}
    else:
        assert 'before_' + action in state['completed']
        assert len(captured['receipts']) == 1
    assert json.loads((root / '.hinstall/processes.json').read_text()) == {}
    assert operation['status'] == 'RECOVERY_REQUIRED'
    assert operation['error']['code'] == 'DISK_SPACE_LOW', operation.get('error')
    assert operation['error']['phase'] == 'product_adapter'
    assert operation['error']['details'] == {'reason': 'recovery_receipt_insufficient_space'}
    assert 'SYNTHETIC_PRIVATE_SPACE_ERROR' not in json.dumps(operation['error'])
    assert str(root) not in json.dumps(operation['error'])


@pytest.mark.parametrize('action,receipt_phase', [('restore', 'issue'), ('rollback', 'verify')])
def test_new_public_factory_resumes_original_space_refusal_with_same_cipher_and_effects(case, monkeypatch, action, receipt_phase):
    from shared import archive_crypto
    from test_native_adapter_uninstall_boundary import effect_rows
    import sqlite3

    root = case.fixture.root
    review = case.plan(action, case.second, archive_path=case.archive['path'], acknowledge_data_loss=True)
    token = digest([review['plan_id'], review['plan_digest']])[:32]
    work = Path(case.second['options'].get('work_dir', str(root.parent / ('.' + root.name + '-recovery'))))
    safety = work / (token + '-before-' + action + '.hsac')
    receipts = root / '.hinstall/recovery-receipts'
    ledger = root / '.hinstall/product-adapter/release-2/adapter-invocations.sqlite3'
    ledger_identity = (ledger.stat().st_dev, ledger.stat().st_ino)
    input_sha = sha(Path(case.archive['path']))
    hooks, captured, fault_reached = [], {}, []
    actual_method = getattr(RecoveryBackupReceiptStore, receipt_phase)
    actual_hook = ExecutableProductAdapter._execute

    def observe_hook(adapter, hook, request, request_digest):
        hooks.append((adapter.options['release'], hook, request['idempotency_key']))
        return actual_hook(adapter, hook, request, request_digest)

    def once_constrained(store, *args, **kwargs):
        if store.directory != receipts or fault_reached:
            return actual_method(store, *args, **kwargs)
        captured.update(roots=roots_image(root), safety_sha=sha(safety),
            safety_identity=image(safety), sidecar=image(safety.with_name(safety.name + '.receipt.json')))
        def no_space(parent):
            assert receipts in Path(parent).parents or Path(parent) == receipts
            fault_reached.append(True)
            return SimpleNamespace(free=0)
        with monkeypatch.context() as constrained:
            constrained.setattr(archive.shutil, 'disk_usage', no_space)
            return actual_method(store, *args, **kwargs)

    monkeypatch.setattr(ExecutableProductAdapter, '_execute', observe_hook)
    monkeypatch.setattr(RecoveryBackupReceiptStore, receipt_phase, once_constrained)
    operation = case.submit(review, 'resume-space-' + action)
    assert fault_reached == [True]
    assert operation['status'] == 'RECOVERY_REQUIRED' and operation['error']['code'] == 'DISK_SPACE_LOW'
    assert roots_image(root) == captured['roots']
    assert sha(Path(case.archive['path'])) == input_sha
    assert not any(hook in ('restore', 'post_restore') for _, hook, _ in hooks)
    original_id = operation['operation_id']
    source_calls = [call for call in hooks if call[0] == 'release-2']
    assert len([call for call in source_calls if call[1] == 'quiesce']) == 1
    with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
        quiesce_rows = db.execute("SELECT call_id,request_digest,operation_id,state,result FROM calls WHERE hook='quiesce' ORDER BY call_id").fetchall()

    # Remove only the injected resource constraint. A new public client must
    # recover the original reviewed operation, not mint a new backup or key.
    resumed = case.new_api().resume(original_id, background=False)
    assert resumed['status'] == 'SUCCEEDED', resumed.get('error')
    assert resumed['operation_id'] == original_id
    case.fixture.current = case.first
    assert image(safety) == captured['safety_identity'] and sha(safety) == captured['safety_sha']
    assert image(safety.with_name(safety.name + '.receipt.json')) == captured['sidecar']
    assert sha(Path(case.archive['path'])) == input_sha
    assert (ledger.stat().st_dev, ledger.stat().st_ino) == ledger_identity
    assert [call for call in hooks if call[0] == 'release-2'] == source_calls
    with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute("SELECT call_id,request_digest,operation_id,state,result FROM calls WHERE hook='quiesce' ORDER BY call_id").fetchall() == quiesce_rows
    for hook in ('restore', 'post_restore'):
        assert len(effect_rows(case.database, hook)) == 1
        assert len([call for call in hooks if call[0] == 'release-1' and call[1] == hook]) == 1
    assert case.document.read_text() == 'snapshot document'
    assert case.credential.read_text() == 'synthetic snapshot credential'
    assert stat.S_IMODE(case.credential.stat().st_mode) == 0o600
    with sqlite3.connect(case.database.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (1,)
        assert db.execute('SELECT value FROM user_state').fetchall() == [('snapshot user state',)]
    result = resumed['result']
    assert result['before_recovery_archive']['path'] == str(safety)
    assert result['before_recovery_archive']['sha256'] == captured['safety_sha']
    assert result['before_recovery']['source_archive_sha256'] == input_sha
    assert result['health']['release'] == 'release-1' and result['health']['product_adapter']['ready']

    oracle = case.fixture.base / ('space-resume-' + action + '-latest-safety-oracle')
    oracle.mkdir(mode=0o700)
    archive_crypto.restore_archive(safety, oracle, password_provider=lambda: 'synthetic-archive-passphrase',
        expected_product_id=case.second['manifest']['product_id'])
    for name, before in captured['roots'].items():
        expected_content = {relative: entry.get('sha256') for relative, entry in before.items()}
        actual_content = {relative: entry.get('sha256') for relative, entry in image(oracle / 'roots' / name).items()}
        assert actual_content == expected_content
    assert (oracle / 'roots/data/document.txt').read_text() == 'latest document after snapshot'
    assert (oracle / 'roots/data/synthetic-credential.txt').read_text() == 'synthetic latest credential'
    with sqlite3.connect((oracle / 'roots/data/schema.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (2,)
        assert db.execute('SELECT value FROM user_state').fetchall() == [('latest user state',)]
    assert case.new_api().status(original_id)['result'] == result
    case.run('stop', case.first, 'stop-after-space-resume-' + action)
    assert json.loads((root / '.hinstall/processes.json').read_text()) == {}
