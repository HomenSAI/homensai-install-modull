"""Public update backup-space boundaries with real crypto and signed DEV code.

Only the actual backup receipt issue/verify call is resource-constrained. No
trusted callback/authorizer is replaced, and no product success is fabricated.
Linux source integration is separate from installed/clean-host acceptance.
"""
import base64
import errno
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from types import SimpleNamespace

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI
from homensai_install.migration_receipts import BackupReceiptStore
import homensai_install.migration_receipts as receipt_module
from shared import archive, archive_crypto
from test_native_migration import migration
from test_native_recovery_space_boundary import NoSpaceWriter, image, roots_image, sha


def read_owned_journal(path, *, private_ancestor=None):
    """Read one owned regular checkpoint through its private parent fd.

    Existing UpdatePlan files may be 0644 under a private 0700 parent. Require
    no shared writes and preserve their observed mode; never chmod the fixture.
    """
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    parent_before = path.parent.lstat()
    assert stat.S_ISDIR(parent_before.st_mode) and parent_before.st_uid == os.getuid()
    if private_ancestor is None:
        assert stat.S_IMODE(parent_before.st_mode) == 0o700
    else:
        # Fresh authenticated extraction normalizes file permissions but its
        # nested mkdir uses the host umask. Its explicitly private oracle root
        # supplies the privacy boundary; this exception never applies to live.
        assert private_ancestor in path.parent.parents
        current = path.parent
        while True:
            info = current.lstat()
            assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            assert info.st_mode & 0o022 == 0
            if current == private_ancestor:
                assert stat.S_IMODE(info.st_mode) == 0o700
                break
            current = current.parent
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert identity(os.fstat(parent_fd)) == identity(parent_before)
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        assert stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.getuid()
        assert before.st_mode & 0o022 == 0
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, 'rb') as stream:
            assert identity(os.fstat(stream.fileno())) == identity(before)
            raw = stream.read(65537)
            assert len(raw) <= 65536
            assert identity(os.fstat(stream.fileno())) == identity(before)
        assert identity(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)) == identity(before)
        assert identity(os.fstat(parent_fd)) == identity(parent_before)
        assert identity(path.parent.lstat()) == identity(parent_before)
    finally:
        os.close(parent_fd)
    return json.loads(raw), stat.S_IMODE(before.st_mode)


def journal_binding(value):
    assert value['direction'] == 'forward' and value['outcome'] == 'running'
    assert value['failed_phase'] is None
    assert type(value['index']) is int and value['index'] >= 0
    return {key: value[key] for key in ('version', 'transaction', 'plan', 'old_release', 'release', 'direction')}


def business_image(value, own_journal):
    result = {name: {relative: dict(item) for relative, item in entries.items()}
              for name, entries in value.items()}
    result['services'].pop(own_journal, None)
    # Atomic publication of this single owned checkpoint can change its parent
    # size/timestamps. Keep parent dev/inode/type/mode/uid/gid/nlink and every
    # other file/dir identity; unrelated entries cannot hide behind this rule.
    result['services']['.']['identity'] = result['services']['.']['identity'][:6]
    return result


@pytest.fixture
def update_case(migration, tmp_path):
    trust = tmp_path / 'synthetic-trust.json'
    trust.write_text(json.dumps({'schema_version': 1, 'keys': {
        'synthetic': base64.b64encode(migration.public_key).decode()}}))
    def new_api():
        return InstallerAPI.from_bootstrap(tmp_path / 'public-state', trust_config=trust,
            allow_development=True, development_runtime=sys.executable,
            product_adapter_descriptor='installer/adapter.json', secret_provider=migration.engine.secret_provider)
    api = new_api()
    def submit(plan, key):
        return api.submit(plan['plan_id'], plan['plan_digest'],
            [plan['review_consent_ref'], *plan['required_consents']], idempotency_key=key, background=False)
    first = migration.release('release-1', 'schema_1', config=True)
    empty = api.prepare_migration('install', first['manifest'], migration.target, options=first['options'])
    plan = api.plan('install', first['manifest'], migration.target,
        options={**first['options'], 'migration_review_id': empty['migration_review_id']})
    installed = submit(plan, 'fresh-v1')
    assert installed['status'] == 'SUCCEEDED', installed.get('error')
    migration.current = first
    document = migration.root / 'data/document.txt'
    document.write_text('latest pre-update document')
    credential = migration.root / 'data/synthetic-credential.txt'
    credential.write_text('synthetic pre-update credential'); credential.chmod(0o600)
    database = migration.root / 'data/schema.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE user_state(value TEXT)')
        db.execute('INSERT INTO user_state VALUES (?)', ('latest pre-update user state',))
    second = migration.release('release-2', 'schema_2', config=True)
    review = api.prepare_migration('update', second['manifest'], migration.target, options=second['options'])
    second = {**second, 'options': {**second['options'], 'migration_review_id': review['migration_review_id']}}
    plan = api.plan('update', second['manifest'], migration.target, options=second['options'])
    return SimpleNamespace(fixture=migration, api=api, new_api=new_api, submit=submit,
        first=first, second=second, plan=plan, document=document, credential=credential, database=database)


def fault_update(case, monkeypatch, receipt_phase, fault):
    root = case.fixture.root
    receipts = root / '.hinstall/backup-receipts'
    native_token = digest([case.plan['plan_id'], case.plan['plan_digest']])[:32]
    own_journal = 'update-' + native_token + '.json'
    captured, fired, hooks = {}, [], []
    actual_method = getattr(BackupReceiptStore, receipt_phase)
    actual_fdopen, actual_atomic = os.fdopen, receipt_module._atomic_write
    actual_hook = ExecutableProductAdapter._execute

    def observe_hook(adapter, hook, request, request_digest):
        hooks.append((adapter.options['release'], hook, request['idempotency_key']))
        response = actual_hook(adapter, hook, request, request_digest)
        if adapter.options['release'] == 'release-2' and hook == 'readiness':
            captured.setdefault('readiness', []).append(response)
        return response

    def constrained_call(store, *args, **kwargs):
        if store.directory != receipts or captured:
            return actual_method(store, *args, **kwargs)
        if receipt_phase == 'issue':
            cipher = Path(args[0])
        else:
            assert re.fullmatch('[0-9a-f]{64}', args[0])
            cipher = Path(json.loads((receipts / (args[0] + '.json')).read_text())['archive_path'])
        assert case.fixture.base in cipher.parents
        update_journal, journal_mode = read_owned_journal(root / 'services' / own_journal)
        captured.update(roots=roots_image(root), cipher=cipher, cipher_image=image(cipher),
            receipts={p.name: image(p) for p in sorted(receipts.glob('*.json'))}, own_journal=own_journal,
            update_journal=update_journal, update_journal_mode=journal_mode)
        with monkeypatch.context() as constrained:
            if fault == 'space':
                def no_space(parent):
                    assert receipts in Path(parent).parents or Path(parent) == receipts
                    fired.append('space')
                    return SimpleNamespace(free=0)
                constrained.setattr(archive.shutil, 'disk_usage', no_space)
            elif fault == 'snapshot_write':
                def fdopen(fd, mode='r', *other, **options):
                    endpoint = Path(os.readlink('/proc/self/fd/' + str(fd)))
                    stream = actual_fdopen(fd, mode, *other, **options)
                    if (endpoint.name == 'archive.hsac' and endpoint.parent.name.startswith('verify-')
                            and endpoint.parent.parent == receipts and mode == 'wb'):
                        return NoSpaceWriter(stream, fired)
                    return stream
                constrained.setattr(os, 'fdopen', fdopen)
            else:
                def atomic(path, value):
                    if Path(path).parent == receipts:
                        assert value['body']['kind'] == 'encrypted_backup'
                        fired.append('receipt_atomic')
                        raise OSError(errno.ENOSPC, 'SYNTHETIC_PRIVATE_SPACE_ERROR')
                    return actual_atomic(path, value)
                constrained.setattr(receipt_module, '_atomic_write', atomic)
            return actual_method(store, *args, **kwargs)

    monkeypatch.setattr(ExecutableProductAdapter, '_execute', observe_hook)
    monkeypatch.setattr(BackupReceiptStore, receipt_phase, constrained_call)
    operation = case.submit(case.plan, 'update-space-' + receipt_phase + '-' + fault)
    # Preservation assertions precede the new error-code assertion in baseline.
    assert captured and fired == [fault]
    assert business_image(roots_image(root), own_journal) == business_image(captured['roots'], own_journal)
    after_journal, after_mode = read_owned_journal(root / 'services' / own_journal)
    assert after_mode == captured['update_journal_mode']
    assert journal_binding(after_journal) == journal_binding(captured['update_journal'])
    assert after_journal['index'] >= captured['update_journal']['index']
    assert image(captured['cipher']) == captured['cipher_image']
    assert {p.name: image(p) for p in sorted(receipts.glob('*.json'))} == captured['receipts']
    assert not any(release == 'release-2' and hook == 'migration_apply' for release, hook, _ in hooks)
    with sqlite3.connect(case.database.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM adapter_effects WHERE hook=?', ('migration_apply',)).fetchone() == (1,)
        assert db.execute('SELECT value FROM user_state').fetchall() == [('latest pre-update user state',)]
    assert case.document.read_text() == 'latest pre-update document'
    assert case.credential.read_text() == 'synthetic pre-update credential'
    assert stat.S_IMODE(case.credential.stat().st_mode) == 0o600
    if receipt_phase == 'issue':
        assert captured['receipts'] == {}
    else:
        assert captured['receipts']
    assert operation['status'] == 'RECOVERY_REQUIRED'
    assert operation['error']['code'] == 'DISK_SPACE_LOW', operation.get('error')
    # Migration authorization deliberately wraps the public verify error. Its
    # original safe reason/phase is tested at the base store boundary separately.
    if receipt_phase == 'issue':
        assert operation['error']['phase'] == 'product_adapter'
        assert operation['error']['details'] == {'reason': 'backup_receipt_insufficient_space'}
    else:
        assert operation['error']['phase'] is None and operation['error']['details'] == {}
    assert 'SYNTHETIC_PRIVATE_SPACE_ERROR' not in json.dumps(operation['error'])
    assert str(root) not in json.dumps(operation['error'])
    return operation, captured, hooks


@pytest.mark.parametrize('receipt_phase,fault', [
    ('issue', 'space'), ('issue', 'snapshot_write'), ('issue', 'receipt_atomic'),
    ('verify', 'space'), ('verify', 'snapshot_write')],
    ids=['issue-space', 'issue-snapshot', 'issue-atomic', 'verify-space', 'verify-snapshot'])
def test_public_update_backup_space_refuses_before_migration(update_case, monkeypatch, receipt_phase, fault):
    fault_update(update_case, monkeypatch, receipt_phase, fault)


@pytest.mark.parametrize('receipt_phase', ['issue', 'verify'])
def test_new_public_factory_resumes_same_update_after_backup_space_refusal(update_case, monkeypatch, receipt_phase):
    case = update_case
    root = case.fixture.root
    ledger = root / '.hinstall/product-adapter/release-1/adapter-invocations.sqlite3'
    ledger_identity = (ledger.stat().st_dev, ledger.stat().st_ino)
    operation, captured, hooks = fault_update(case, monkeypatch, receipt_phase, 'space')
    source_calls = [call for call in hooks if call[0] == 'release-1']
    assert len([call for call in source_calls if call[1] == 'quiesce']) == 1
    with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
        quiesce = db.execute("SELECT call_id,request_digest,operation_id,state,result FROM calls WHERE hook='quiesce' ORDER BY call_id").fetchall()
    resumed = case.new_api().resume(operation['operation_id'], background=False)
    assert resumed['status'] == 'SUCCEEDED', resumed.get('error')
    assert resumed['operation_id'] == operation['operation_id']
    case.fixture.current = case.second
    assert image(captured['cipher']) == captured['cipher_image']
    assert (ledger.stat().st_dev, ledger.stat().st_ino) == ledger_identity
    assert [call for call in hooks if call[0] == 'release-1'] == source_calls
    with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute("SELECT call_id,request_digest,operation_id,state,result FROM calls WHERE hook='quiesce' ORDER BY call_id").fetchall() == quiesce
    assert len([call for call in hooks if call[0] == 'release-2' and call[1] == 'migration_apply']) == 1
    with sqlite3.connect(case.database.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (2,)
        assert db.execute('SELECT count(*) FROM adapter_effects WHERE hook=?', ('migration_apply',)).fetchone() == (2,)
        assert db.execute('SELECT value FROM user_state').fetchall() == [('latest pre-update user state',)]
    assert case.document.read_text() == 'latest pre-update document'
    assert case.credential.read_text() == 'synthetic pre-update credential'
    assert stat.S_IMODE(case.credential.stat().st_mode) == 0o600
    assert resumed['result']['release'] == 'release-2'
    assert captured['readiness'][-1]['ready'] is True
    assert captured['readiness'][-1]['observed_identity']['release'] == 'release-2'
    oracle = case.fixture.base / ('backup-space-' + receipt_phase + '-oracle')
    oracle.mkdir(mode=0o700)
    archive_crypto.restore_archive(captured['cipher'], oracle, password_provider=lambda: 'synthetic-archive-passphrase',
        expected_product_id=case.first['manifest']['product_id'])
    for name, before in captured['roots'].items():
        expected = {relative: item.get('sha256') for relative, item in before.items()}
        actual = {relative: item.get('sha256') for relative, item in image(oracle / 'roots' / name).items()}
        if name == 'services':
            expected.pop(captured['own_journal'])
            actual.pop(captured['own_journal'])
        assert actual == expected
    archived_journal, _ = read_owned_journal(oracle / 'roots/services' / captured['own_journal'],
                                           private_ancestor=oracle)
    assert journal_binding(archived_journal) == journal_binding(captured['update_journal'])
    assert archived_journal['index'] <= captured['update_journal']['index']
    with sqlite3.connect((oracle / 'roots/data/schema.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (1,)
        assert db.execute('SELECT value FROM user_state').fetchall() == [('latest pre-update user state',)]
    assert case.new_api().status(operation['operation_id'])['result'] == resumed['result']
    stop = case.new_api().plan('stop', case.second['manifest'], case.fixture.target, options=case.second['options'])
    stopped = case.submit(stop, 'stop-after-backup-space-resume')
    assert stopped['status'] == 'SUCCEEDED', stopped.get('error')
    assert json.loads((root / '.hinstall/processes.json').read_text()) == {}
