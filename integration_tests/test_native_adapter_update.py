"""Single-step update: настоящий fresh и отдельные prerequisite contracts."""
from copy import deepcopy
import json
import base64
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from homensai_install.engine import EngineError
from homensai_install.migration_receipts import MigrationReviewStore
from test_native_migration import migration, prepare, fresh_plan, migration_effect_count, KILL_CONTROLLER


def installed_first(fixture, *, fault=None):
    value = fixture.release('release-1', 'schema_1', config=True, fault=fault)
    plan, prepared = fresh_plan(fixture, value)
    assert fixture.engine.execute(plan)['status'] == 'SUCCEEDED'
    fixture.current = prepared
    return prepared


def update_review(fixture):
    installed_first(fixture)
    value = fixture.release('release-2', 'schema_2', config=True)
    review = prepare(fixture, 'update', value)
    prepared = {**value, 'options': {**value['options'], 'migration_review_id': review['migration_review_id']}}
    ctx = fixture.engine._context(value['manifest'], fixture.target, value['options'])
    body = MigrationReviewStore(ctx['control'] / 'migration-reviews').read(review['migration_review_id'])
    return value, prepared, review, ctx, body


def test_adapter_update_release1_to_release2_to_release3(migration):
    installed_first(migration)
    document = migration.root / 'data/document.txt'
    document.write_text('Данные пользователя до update')
    for version in (2, 3):
        value = migration.release('release-' + str(version), 'schema_' + str(version), config=True)
        review = prepare(migration, 'update', value)
        prepared = {**value, 'options': {**value['options'], 'migration_review_id': review['migration_review_id']}}
        plan = migration.reviewed_plan('update', prepared)
        assert migration.engine.execute(plan)['status'] == 'SUCCEEDED'
        migration.current = prepared
        with sqlite3.connect(migration.root / 'data/schema.sqlite') as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert document.read_text() == 'Данные пользователя до update'
        assert migration_effect_count(migration) == version


def test_update_review_uses_schema2_with_distinct_installed_source_binding(migration):
    value, prepared, review, ctx, body = update_review(migration)
    assert body['schema_version'] == 2
    source = body['binding']['from_adapter_binding']
    assert source['release'] == 'release-1'
    assert body['binding']['target_binding']['release'] == 'release-2'
    assert source['manifest_sha256'] == body['binding']['from_manifest_sha256']
    assert source['runtime_executable'] == 'runtime/python'
    assert migration.engine._checked_migration_review(ctx, 'update', review['migration_review_id']) == body
    assert set(migration.engine._migration_public_binding(review['migration_review_id'], body)) == {
        'migration_review_id', 'binding_sha256', 'installation_id', 'from_release', 'to_release',
        'from_version', 'to_version'}


def test_schema1_update_review_is_stale_even_with_current_binding(migration):
    value, prepared, review, ctx, body = update_review(migration)
    body['schema_version'] = 1
    old_id = MigrationReviewStore(ctx['control'] / 'migration-reviews').issue(body)
    with pytest.raises(EngineError) as caught:
        migration.engine._checked_migration_review(ctx, 'update', old_id)
    assert caught.value.code == 'PLAN_STALE'


def test_update_plan_pins_source_and_failure_policy_without_product_hooks(migration, monkeypatch):
    value, prepared, review, ctx, body = update_review(migration)
    assert migration.engine.capabilities(migration.target)['actions']['update']['supported'] is True
    from homensai_install.adapters import ExecutableProductAdapter
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', lambda *args, **kwargs: pytest.fail('plan invokes no hooks'))
    plan = migration.reviewed_plan('update', prepared)['engine_plan']
    assert plan['product_adapter_source_binding'] == body['binding']['from_adapter_binding']
    assert plan['product_adapter_binding'] == body['binding']['target_binding']
    assert plan['update_failure_policy'] == 'preserve_forward_journal_no_automatic_restore'
    assert plan['migration_product_plan'] == review['product_plan']


def test_fresh_review_keeps_schema1_without_update_source_binding(migration):
    value = migration.release('release-1', 'schema_1', config=True)
    review = prepare(migration, 'install', value)
    ctx = migration.engine._context(value['manifest'], migration.target, value['options'])
    body = migration.engine._checked_migration_review(ctx, 'install', review['migration_review_id'])
    assert body['schema_version'] == 1
    assert 'from_adapter_binding' not in body['binding']


def test_source_adapter_tamper_invalidates_update_review_before_hooks(migration, monkeypatch):
    value, prepared, review, ctx, body = update_review(migration)
    from homensai_install.adapters import ExecutableProductAdapter
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', lambda *args, **kwargs: pytest.fail('no hooks'))
    source = migration.engine._release(ctx, 'release-1') / 'installer/adapter.json'
    source.write_text('{}')
    with pytest.raises(EngineError) as caught:
        migration.engine._checked_migration_review(ctx, 'update', review['migration_review_id'])
    assert caught.value.code == 'PLAN_STALE'


@pytest.mark.parametrize('field', ['release', 'runtime_executable', 'inventory_sha256',
                                  'manifest_sha256', 'capabilities_sha256', 'descriptor'])
def test_foreign_source_binding_cannot_reuse_current_update_review(migration, monkeypatch, field):
    value, prepared, review, ctx, body = update_review(migration)
    body['binding']['from_adapter_binding'][field] = 'foreign'
    foreign_id = MigrationReviewStore(ctx['control'] / 'migration-reviews').issue(body)
    from homensai_install.adapters import ExecutableProductAdapter
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', lambda *args, **kwargs: pytest.fail('no hooks'))
    with pytest.raises(EngineError) as caught:
        migration.engine._checked_migration_review(ctx, 'update', foreign_id)
    assert caught.value.code == 'PLAN_STALE'


def test_execution_slice_real_release1_to_release2_to_release3(migration, monkeypatch):
    from homensai_install.adapters import ExecutableProductAdapter
    invoke = ExecutableProductAdapter.invoke
    calls = []
    def observed(self, hook, request, **kwargs):
        if hook == 'migration_apply':
            assert set(request['data']) == {'plan_digest', 'migration_binding',
                'backup_receipt_id' if request['data']['migration_binding']['from_release'] else 'fresh_receipt_id'}
            calls.append(request['data'])
        return invoke(self, hook, request, **kwargs)
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', observed)
    test_adapter_update_release1_to_release2_to_release3(migration)
    assert len(calls) == 3
    for journal in (migration.root / '.hinstall/operations').glob('*.json'):
        state = json.loads(journal.read_text())
        if state['action'] != 'update':
            continue
        assert state['status'] == 'SUCCEEDED'
        assert len(state['product_adapter_backup_refs']) == 2
        for ref in state['product_adapter_backup_refs'].values():
            body = json.loads((migration.root / '.hinstall/backup-receipts' / (ref['receipt_id'] + '.json')).read_text())['body']
            assert body['migration_eligible'] is True
            assert body['from_schema_version'] in {'schema_1', 'schema_2'}


def planned_update(fixture, *, fault=None, source_fault=None):
    installed_first(fixture, fault=source_fault)
    value = fixture.release('release-2', 'schema_2', config=True, fault=fault)
    review = prepare(fixture, 'update', value)
    prepared = {**value, 'options': {**value['options'], 'migration_review_id': review['migration_review_id']}}
    return fixture.reviewed_plan('update', prepared), prepared


def update_state(fixture):
    for path in (fixture.root / '.hinstall/operations').glob('*.json'):
        value = json.loads(path.read_text())
        if value['action'] == 'update':
            return path, value
    pytest.fail('missing native update journal')


@pytest.mark.parametrize('fault', ['missing_archive', 'tampered_archive', 'missing_receipt', 'tampered_receipt', 'live_data', 'live_schema'])
def test_update_backup_or_live_drift_refuses_apply_and_preserves_forward_journal(migration, fault):
    plan, prepared = planned_update(migration)
    def emit(event):
        if event['phase'] != 'backup:0' or event['status'] != 'SUCCEEDED':
            return
        journal, state = update_state(migration)
        ref = next(ref for ref in state['product_adapter_backup_refs'].values() if ref['purpose'] == 'backup:0')
        receipt = migration.root / '.hinstall/backup-receipts' / (ref['receipt_id'] + '.json')
        record = json.loads(receipt.read_text())
        archive = Path(record['archive_path'])
        if fault == 'missing_archive': archive.unlink()
        elif fault == 'tampered_archive': archive.write_bytes(archive.read_bytes() + b'tampered')
        elif fault == 'missing_receipt': receipt.unlink()
        elif fault == 'tampered_receipt':
            record['body']['from_schema_version'] = 'foreign'
            receipt.write_text(json.dumps(record))
        elif fault == 'live_data': (migration.root / 'data/document.txt').write_text('Изменено после backup')
        else:
            with sqlite3.connect(migration.root / 'data/schema.sqlite') as db: db.execute('PRAGMA user_version=3')
    with pytest.raises(EngineError) as caught:
        migration.engine.execute(plan, emit)
    assert caught.value.code in {'PLAN_STALE', 'RECOVERY_REQUIRED'}
    assert migration_effect_count(migration) == 1
    journal, state = update_state(migration)
    txn = json.loads(next((migration.root / 'services').glob('update-*.json')).read_text())
    assert txn['direction'] == 'forward' and txn['outcome'] == 'running'
    assert not (migration.root / 'data/migration-runs.txt').exists()
    assert state.get('product_adapter_migration_complete') is not True


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_update_health_failure_preserves_new_data_and_stops_only_owned_candidate(migration, monkeypatch, cleanup_fails):
    plan, prepared = planned_update(migration)
    health = migration.engine._health
    def fail_health(ctx):
        if ctx.get('action') == 'update':
            (migration.root / 'data/document.txt').write_text('Новые данные после migration')
            raise EngineError('HEALTH_CHECK_FAILED')
        return health(ctx)
    monkeypatch.setattr(migration.engine, '_health', fail_health)
    quiesce = migration.engine._update_adapter_quiesce
    if cleanup_fails:
        def fail_cleanup(ctx, key, release):
            if release == 'release-2': raise EngineError('OPERATION_CONFLICT')
            return quiesce(ctx, key, release)
        monkeypatch.setattr(migration.engine, '_update_adapter_quiesce', fail_cleanup)
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code == 'HEALTH_CHECK_FAILED'
    assert migration_effect_count(migration) == 2
    assert (migration.root / 'data/document.txt').read_text() == 'Новые данные после migration'
    journal, state = update_state(migration)
    assert state['primary_error_code'] == 'HEALTH_CHECK_FAILED'
    if cleanup_fails:
        assert state['cleanup_error_code'] == 'OPERATION_CONFLICT'
        assert caught.value.details['primary_error_code'] == 'HEALTH_CHECK_FAILED'
        assert caught.value.details['cleanup_error'] == {'phase': 'candidate_cleanup', 'code': 'OPERATION_CONFLICT'}
    else:
        assert not json.loads((migration.root / '.hinstall/processes.json').read_text())
        scopes = list(state['product_adapter_quiesce_scopes'].values())
        assert {(x['release'], x['complete']) for x in scopes} == {('release-1', True), ('release-2', True)}
    txn = json.loads(next((migration.root / 'services').glob('update-*.json')).read_text())
    assert txn['direction'] == 'forward' and txn['index'] == 7
    assert json.loads((migration.root / 'services/active.json').read_text()) == {'release': 'release-2'}


@pytest.mark.parametrize('fault', ['after_apply', 'unknown'])
def test_update_lost_ack_reconciles_without_duplicate_or_automatic_restore(migration, fault):
    plan, prepared = planned_update(migration, fault=fault)
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code == 'RECOVERY_REQUIRED' and migration_effect_count(migration) == 2
    if fault == 'unknown':
        with pytest.raises(EngineError): migration.engine.resume(plan, {})
    else:
        assert migration.engine.resume(plan, {})['status'] == 'SUCCEEDED'
        migration.current = prepared
    assert migration_effect_count(migration) == 2


UPDATE_KILL_CONTROLLER = KILL_CONTROLLER
UPDATE_KILL_CONTROLLER = UPDATE_KILL_CONTROLLER.replace(
    'def emit(event):',
    "def emit(event):\n if phase=='before_backup' and event['phase']=='backup:0' and event['status']=='RUNNING': os.kill(os.getpid(),signal.SIGKILL)\n if phase=='after_backup' and event['phase']=='backup:0' and event['status']=='SUCCEEDED': os.kill(os.getpid(),signal.SIGKILL)\n if phase=='after_pointer' and event['phase']=='activate' and event['status']=='SUCCEEDED': os.kill(os.getpid(),signal.SIGKILL)")
UPDATE_KILL_CONTROLLER = UPDATE_KILL_CONTROLLER.replace('def emit(event):',
    "def emit(event):\n if phase=='before_archive' and event['phase']=='archive' and event['status']=='RUNNING': os.kill(os.getpid(),signal.SIGKILL)\n if phase=='after_archive' and event['phase']=='archive' and event['status']=='SUCCEEDED': os.kill(os.getpid(),signal.SIGKILL)")


def kill_update_controller(fixture, plan, phase):
    source = fixture.base / 'update-kill-controller.json'
    source.write_text(json.dumps({'plan': plan, 'kill_phase': phase,
        'public_key': base64.b64encode(fixture.public_key).decode()}))
    result = subprocess.run([sys.executable, '-c', UPDATE_KILL_CONTROLLER, str(source)],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
    assert result.returncode == -9, result.stderr.decode(errors='replace')


@pytest.mark.parametrize('phase', ['before_archive', 'after_archive', 'before_backup', 'after_backup', 'after_stage', 'before_ledger', 'after_ledger', 'after_apply', 'after_pointer'])
def test_update_actual_sigkill_boundaries_keep_original_journal_and_one_apply(migration, phase):
    plan, prepared = planned_update(migration)
    kill_update_controller(migration, plan, phase)
    effects = 2 if phase in {'after_apply', 'after_pointer'} else 1
    assert migration_effect_count(migration) == effects
    if phase in {'before_ledger', 'after_ledger'}:
        with pytest.raises(EngineError) as caught: migration.engine.resume(plan, {})
        assert caught.value.code == 'RECOVERY_REQUIRED'
    else:
        assert migration.engine.resume(plan, {})['status'] == 'SUCCEEDED'
        migration.current = prepared
    assert migration_effect_count(migration) == (effects if phase in {'before_ledger', 'after_ledger'} else 2)


def test_update_lost_ack_with_missing_ledger_never_repeats_product_effect(migration):
    plan, prepared = planned_update(migration)
    kill_update_controller(migration, plan, 'after_apply')
    assert migration_effect_count(migration) == 2
    (migration.root / '.hinstall/product-adapter/release-2/adapter-invocations.sqlite3').unlink()
    with pytest.raises(EngineError): migration.engine.resume(plan, {})
    assert migration_effect_count(migration) == 2


def test_source_quiesce_completion_never_clears_an_unrelated_unknown_fence(migration, monkeypatch):
    plan, prepared = planned_update(migration)
    original = migration.engine._adapter_migrate
    def fenced(ctx):
        ctx['operation_state']['product_adapter_unknown'] = True
        ctx['operation_state']['product_adapter_unknown_scope'] = 'foreign_effect'
        return original(ctx)
    monkeypatch.setattr(migration.engine, '_adapter_migrate', fenced)
    with pytest.raises(EngineError): migration.engine.execute(plan)
    journal, state = update_state(migration)
    assert state['product_adapter_unknown'] is True and state['product_adapter_unknown_scope'] == 'foreign_effect'
    assert migration_effect_count(migration) == 1


def test_source_quiesce_unknown_blocks_backup_and_apply_across_resume(migration):
    plan, prepared = planned_update(migration, source_fault='quiesce_unknown')
    with pytest.raises(EngineError): migration.engine.execute(plan)
    journal, state = update_state(migration)
    assert state['product_adapter_unknown'] is True
    assert not state.get('product_adapter_backup_refs')
    with pytest.raises(EngineError): migration.engine.resume(plan, {})
    assert migration_effect_count(migration) == 1


def test_candidate_cleanup_unknown_keeps_primary_error_and_fences_restart(migration, monkeypatch):
    plan, prepared = planned_update(migration, fault='quiesce_unknown')
    def fail_health(ctx):
        raise EngineError('HEALTH_CHECK_FAILED')
    monkeypatch.setattr(migration.engine, '_health', fail_health)
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code == 'HEALTH_CHECK_FAILED'
    journal, state = update_state(migration)
    assert state['primary_error_code'] == 'HEALTH_CHECK_FAILED'
    assert state['cleanup_error_code'] == 'RECOVERY_REQUIRED' and state['product_adapter_unknown'] is True
    attempts = state['candidate_health_attempt']
    with pytest.raises(EngineError): migration.engine.resume(plan, {})
    assert update_state(migration)[1]['candidate_health_attempt'] == attempts
    assert migration_effect_count(migration) == 2


@pytest.mark.parametrize('identity', ['operation_id', 'plan_binding_sha256'])
def test_valid_encrypted_foreign_receipt_is_rejected_at_engine_authorization(migration, monkeypatch, identity):
    from homensai_install.migration_receipts import BackupReceiptStore
    plan, prepared = planned_update(migration)
    verify = BackupReceiptStore.verify
    foreign_ids = []
    def substitute(self, receipt_id, *, expected_binding):
        record = self._read(receipt_id)
        foreign = {**expected_binding, identity: 'op_foreign' if identity == 'operation_id' else 'a' * 64}
        issued = self.issue(record['archive_path'], foreign,
            consistency_scope='owned_components_stopped_and_adapter_quiesced')
        assert issued['migration_eligible'] is True and issued['receipt_id'] != receipt_id
        foreign_ids.append(issued['receipt_id'])
        return verify(self, issued['receipt_id'], expected_binding=expected_binding)
    monkeypatch.setattr(BackupReceiptStore, 'verify', substitute)
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert foreign_ids and migration_effect_count(migration) == 1


def test_installation_identity_drift_before_apply_refuses_mutation(migration):
    plan, prepared = planned_update(migration)
    def emit(event):
        if event['phase'] == 'migrate' and event['status'] == 'RUNNING':
            path = migration.root / '.hinstall/installation.json'
            value = json.loads(path.read_text()); value['installation_id'] = 'a' * 32
            path.write_text(json.dumps(value))
    with pytest.raises(EngineError): migration.engine.execute(plan, emit)
    assert migration_effect_count(migration) == 1


def test_source_without_quiesce_is_rejected_by_read_only_update_plan(migration):
    value = migration.release('release-1', 'schema_1', config=True, omit_hooks=('quiesce',))
    plan, prepared = fresh_plan(migration, value)
    migration.engine.execute(plan); migration.current = prepared
    target = migration.release('release-2', 'schema_2', config=True)
    review = prepare(migration, 'update', target)
    options = {**target, 'options': {**target['options'], 'migration_review_id': review['migration_review_id']}}
    with pytest.raises(EngineError) as caught: migration.reviewed_plan('update', options)
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert migration_effect_count(migration) == 1
