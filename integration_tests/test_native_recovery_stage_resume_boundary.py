"""Independent stage-ACK boundaries; real DEV signatures and public operations.

The published whole-root protocol deliberately refuses unknown stage outcomes.
Successful adoption here requires the next explicit typed stage authority; no
old refusal is described as a destructive implementation defect.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from homensai_install.recovery_replacement import RootReplacement
from test_native_adapter_uninstall_boundary import effect_rows, interrupted_operation, uninstall
from test_native_recovery_root_resume_boundary import (
    case, migration, killed_child, managed_state, tree_state, identity,
    review_restore, refuse_original_resume, sha,
)


def stage_cut(case, review):
    """Ports, its effect receipt and metadata have really returned, ACK has not."""
    token = digest([review['plan_id'], review['plan_digest']])[:32]

    def child():
        def before_native_ack(protocol):
            assert protocol.state['recovery_roots_complete'] is True
            assert protocol.state['recovery_target_stage_started'] is True
            assert not protocol.state.get('recovery_target_stage_complete')
            os.kill(os.getpid(), signal.SIGKILL)
        RootReplacement.stage_complete = before_native_ack
        case.submit(review, 'stage-ack-crash')

    killed_child(child)
    journal = case.fixture.root / '.hinstall/operations' / (token + '.json')
    native = json.loads(journal.read_text())
    assert native['recovery_roots_complete'] is True
    assert native['recovery_target_stage_started'] is True
    assert not native.get('recovery_target_stage_complete') and 'restore' not in native['completed']
    assert case.document.read_text() == 'snapshot document'
    target = next(p for p in (case.fixture.root / 'releases').iterdir() if p.name.endswith('release-1'))
    return token, journal, native, target


@pytest.mark.parametrize('source', ['live_preexisting_target', 'retained_absent_target'])
def test_public_original_resume_adopts_verified_stage_without_recopy_or_source_hooks(case, monkeypatch, source):
    if source == 'retained_absent_target':
        uninstall(case)
        assert not list((case.fixture.root / 'releases').iterdir())
        for bundle in (case.first, case.second):
            shutil.rmtree(bundle['options']['payload_dir'])
    review = review_restore(case)
    actual = ExecutableProductAdapter._execute
    trace = case.fixture.base / 'stage-resume-hooks.jsonl'
    resuming = [False]
    roots_at_cut = {}

    def observed(adapter, hook, request, request_digest):
        release = adapter.options['release']
        if resuming[0] and release == 'release-2':
            pytest.fail('Stage adoption cannot invoke source hooks against restored business data')
        if resuming[0] and hook == 'restore':
            assert managed_state(case.fixture.root) == roots_at_cut, 'Stage adoption recopied or prepared persistent roots'
        with trace.open('a') as stream:
            stream.write(json.dumps({'release': release, 'hook': hook, 'key': request['idempotency_key']}) + '\n')
            stream.flush(); os.fsync(stream.fileno())
        return actual(adapter, hook, request, request_digest)

    monkeypatch.setattr(ExecutableProductAdapter, '_execute', observed)
    token, journal, before_native, target = stage_cut(case, review)
    roots_at_cut.update(managed_state(case.fixture.root))
    target_at_cut = tree_state(target)
    archives = {str(p): sha(p) for p in case.fixture.base.rglob('*.hsac')}
    ledger = case.fixture.root / '.hinstall/product-adapter/release-2/adapter-invocations.sqlite3'
    ledger_identity = ledger.stat().st_dev, ledger.stat().st_ino
    api = case.new_api()
    operation = interrupted_operation(api, review)
    resuming[0] = True
    result = api.resume(operation['operation_id'], background=False)
    assert result['operation_id'] == operation['operation_id'] and result['status'] == 'SUCCEEDED', result.get('error')
    case.fixture.current = case.first
    assert tree_state(target) == target_at_cut, 'Verified target code was rewritten during journal-only adoption'
    after_native = json.loads(journal.read_text())
    assert after_native['recovery_target_stage_receipt_id'] == before_native['recovery_target_stage_receipt_id']
    intent = json.loads((case.fixture.root / '.hinstall/recovery-stage-receipts' / (token + '.json')).read_text())
    assert intent['receipt_id'] == digest(intent['body']) == after_native['recovery_target_stage_receipt_id']
    assert intent['body']['root_receipt_id'] == before_native['recovery_root_receipt_id']
    assert intent['body']['operation_id'] == 'op_' + token
    assert after_native['step_results']['before_restore'] == before_native['step_results']['before_restore']
    assert {str(p): sha(p) for p in case.fixture.base.rglob('*.hsac')} == archives
    assert (ledger.stat().st_dev, ledger.stat().st_ino) == ledger_identity
    assert case.document.read_text() == 'snapshot document' and case.credential.read_text() == 'synthetic snapshot credential'
    assert len(effect_rows(case.database, 'restore')) == len(effect_rows(case.database, 'post_restore')) == 1
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert sum(v['release'] == 'release-2' and v['hook'] == 'quiesce' for v in calls) == 1
    assert not any(v['release'] == 'release-2' and v['hook'] == 'operation_status' for v in calls)
    for hook in ('restore', 'post_restore'):
        assert sum(v['release'] == 'release-1' and v['hook'] == hook for v in calls) == 1


def test_inactive_corrupt_target_is_refused_before_hooks_or_business_root_replacement(case, monkeypatch):
    target = next(p for p in (case.fixture.root / 'releases').iterdir() if p.name.endswith('release-1'))
    damaged = target / 'adapter.py'
    damaged.write_text(damaged.read_text() + '\nraise AssertionError("SYNTHETIC_DAMAGED_INACTIVE_TARGET_EXECUTED")\n')
    damaged_before = damaged.read_bytes(), identity(damaged.lstat())
    processes = case.fixture.root / '.hinstall/processes.json'
    process_records_before = json.loads(processes.read_text())
    assert process_records_before
    latest_document, latest_credential = case.document.read_bytes(), case.credential.read_bytes()
    with sqlite3.connect(case.database.as_uri() + '?mode=ro', uri=True) as db:
        latest_schema = db.execute('PRAGMA user_version').fetchone()
        latest_user_state = db.execute('SELECT value FROM user_state').fetchall()
    calls = []
    actual = ExecutableProductAdapter._execute

    def observed(adapter, hook, request, request_digest):
        assert Path(adapter.options['payload_dir']) != target, 'Corrupt inactive target code executed'
        calls.append(hook)
        return actual(adapter, hook, request, request_digest)

    monkeypatch.setattr(ExecutableProductAdapter, '_execute', observed)
    review = review_restore(case)
    result = case.submit(review, 'corrupt-inactive-target')
    # A known stage conflict is rejected before intentionally restoring any
    # business snapshot. Source quiesce cannot excuse a changed schema/document.
    assert case.document.read_bytes() == latest_document and case.credential.read_bytes() == latest_credential
    with sqlite3.connect(case.database.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone() == latest_schema
        assert db.execute('SELECT value FROM user_state').fetchall() == latest_user_state
    assert result['status'] == 'RECOVERY_REQUIRED' and result['error']['code'] == 'INTEGRITY_FAILED'
    assert calls == []
    assert json.loads(processes.read_text()) == process_records_before
    for record in process_records_before.values():
        os.kill(record['pid'], 0)
    assert (damaged.read_bytes(), identity(damaged.lstat())) == damaged_before


@pytest.mark.parametrize('fault', [
    'missing_intent', 'corrupt_intent', 'missing_ports_receipt',
    'metadata_missing', 'metadata_corrupt', 'inventory_missing', 'signature_corrupt',
    'runtime_mode', 'payload_drift', 'preexisting_target_inode',
])
def test_pending_stage_drift_or_lost_authority_never_reconstructs_or_mutates(case, monkeypatch, fault):
    review = review_restore(case)
    token, journal, native, target = stage_cut(case, review)
    intent_path = case.fixture.root / '.hinstall/recovery-stage-receipts' / (token + '.json')
    assert intent_path.is_file(), 'Typed stage authority is a prerequisite for this corruption case'
    intent = json.loads(intent_path.read_text())
    assert native['recovery_target_stage_receipt_id'] == intent['receipt_id'] == digest(intent['body'])
    ports = Path(intent['body']['ports_receipt']['path'])
    metadata = case.fixture.root / '.hinstall/releases/release-1.json'
    if fault == 'missing_intent': intent_path.unlink()
    elif fault == 'corrupt_intent':
        intent['body']['target_binding_sha256'] = '0' * 64
        intent_path.write_text(json.dumps(intent))
    elif fault == 'missing_ports_receipt': ports.unlink()
    elif fault == 'metadata_missing': metadata.unlink()
    elif fault == 'metadata_corrupt':
        value = json.loads(metadata.read_text()); value['runtime_executable'] = 'runtime/synthetic-unreviewed'
        metadata.write_text(json.dumps(value))
    elif fault == 'inventory_missing': metadata.with_name('release-1.inventory.json').unlink()
    elif fault == 'signature_corrupt': metadata.with_name('release-1.signature.json').write_text('{}')
    elif fault == 'runtime_mode': (target / case.first['options']['runtime_executable']).chmod(0o600)
    elif fault == 'payload_drift': (target / 'adapter.py').write_text('# synthetic offline changed signed code\n')
    else:
        preserved = case.fixture.base / 'synthetic-preserved-original-target'
        target.rename(preserved); shutil.copytree(preserved, target)
    api = case.new_api()
    operation = interrupted_operation(api, review)
    before_roots = managed_state(case.fixture.root)
    before_target = tree_state(target)
    before_meta = {p.name: tree_state(p) for p in metadata.parent.iterdir()}
    metadata_directory_before = identity(metadata.parent.lstat())
    before_intent, before_ports = tree_state(intent_path), tree_state(ports)
    archives = {str(p): sha(p) for p in case.fixture.base.rglob('*.hsac')}
    monkeypatch.setattr(ExecutableProductAdapter, '_execute',
        lambda *a, **k: pytest.fail('Unverified stage authority must refuse before any source or target hook'))
    refuse_original_resume(api, operation)
    assert managed_state(case.fixture.root) == before_roots
    assert tree_state(target) == before_target
    assert {p.name: tree_state(p) for p in metadata.parent.iterdir()} == before_meta
    assert identity(metadata.parent.lstat()) == metadata_directory_before
    assert tree_state(intent_path) == before_intent and tree_state(ports) == before_ports
    assert {str(p): sha(p) for p in case.fixture.base.rglob('*.hsac')} == archives


def test_surviving_stage_intent_fences_new_legacy_token_after_other_authority_is_lost(case, monkeypatch, tmp_path):
    import sys
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.engine import NativeEngine
    from test_native_recovery_root_resume_boundary import refused

    review = review_restore(case)
    token, journal, native, target = stage_cut(case, review)
    root = case.fixture.root
    stage_intent = root / '.hinstall/recovery-stage-receipts' / (token + '.json')
    intent = json.loads(stage_intent.read_text())
    assert intent['receipt_id'] == digest(intent['body']) == native['recovery_target_stage_receipt_id']
    root_receipt = root / '.hinstall/recovery-root-receipts' / (token + '.json')
    assert intent['body']['root_receipt_id'] == json.loads(root_receipt.read_text())['receipt_id']
    owner_path = root / '.hinstall/installation.json'
    owner = json.loads(owner_path.read_text())
    assert owner.pop('pending_recovery')['receipt_id'] == native['recovery_root_receipt_id']
    owner_path.write_text(json.dumps(owner))
    journal.unlink(); root_receipt.unlink()
    (root / 'config').chmod(0o755)
    protected = {p: (p.read_bytes(), identity(p.lstat())) for p in (owner_path, stage_intent)}
    roots_before, target_before = managed_state(root), tree_state(target)
    api = InstallerAPI.from_bootstrap(tmp_path / 'stage-hint-legacy-state', trust_config=tmp_path / 'trust.json',
        allow_development=True, development_runtime=sys.executable, secret_provider=case.fixture.engine.secret_provider)
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', lambda *a, **k: pytest.fail('Orphan stage intent must fence hooks'))
    monkeypatch.setattr(NativeEngine, '_hook', lambda *a, **k: pytest.fail('Legacy constructor must not bypass the surviving stage hint'))

    def new_operation():
        plan = api.plan('restore', case.second['manifest'], case.fixture.target,
                       options={**case.second['options'], 'archive_path': case.archive['path']})
        return api.submit(plan['plan_id'], plan['plan_digest'],
            [plan['review_consent_ref'], *plan['required_consents']],
            idempotency_key='new-token-with-stage-hint', background=False)

    refused(new_operation)
    assert managed_state(root) == roots_before and tree_state(target) == target_before
    assert {p: (p.read_bytes(), identity(p.lstat())) for p in protected} == protected
    assert not journal.exists() and not root_receipt.exists()
