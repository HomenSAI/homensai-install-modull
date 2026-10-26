"""Независимые проверки сохранности данных при отказах нового adapter update."""
import json
import sqlite3

import pytest

from homensai_install.engine import EngineError
from homensai_install.migration_receipts import BackupReceiptStore
from test_native_migration import migration, prepare, fresh_plan, migration_effect_count


def reviewed_update(fixture):
    if not fixture.engine.capabilities(fixture.target)['actions']['update']['supported']:
        pytest.skip('Adapter update execution ещё не реализован; это не PASS lifecycle')
    first = fixture.release('release-1', 'schema_1', config=True)
    plan, prepared = fresh_plan(fixture, first)
    assert fixture.engine.execute(plan)['status'] == 'SUCCEEDED'
    fixture.current = prepared
    second = fixture.release('release-2', 'schema_2', config=True)
    review = prepare(fixture, 'update', second)
    prepared = {**second, 'options': {**second['options'],
                                   'migration_review_id': review['migration_review_id']}}
    return fixture.reviewed_plan('update', prepared), prepared


def test_live_data_changed_after_backup_never_reaches_migration_effect(migration, monkeypatch):
    plan, prepared = reviewed_update(migration)
    document = migration.root / 'data/document.txt'
    original = BackupReceiptStore.verify
    calls = []

    def drift_then_verify(store, receipt_id, *, expected_binding):
        calls.append(receipt_id)
        document.write_text('Изменение данных после snapshot', encoding='utf-8')
        return original(store, receipt_id, expected_binding=expected_binding)

    monkeypatch.setattr(BackupReceiptStore, 'verify', drift_then_verify)
    with pytest.raises(EngineError):
        migration.engine.execute(plan)
    assert calls, 'Проверка должна достичь настоящего backup receipt authorizer'
    assert migration_effect_count(migration) == 1
    with sqlite3.connect(migration.root / 'data/schema.sqlite') as database:
        assert database.execute('PRAGMA user_version').fetchone() == (1,)
    assert document.read_text(encoding='utf-8') == 'Изменение данных после snapshot'
    ctx = migration.engine._context(prepared['manifest'], migration.target, prepared['options'])
    assert migration.engine._pointer(ctx) == 'release-1'


def test_failed_candidate_health_preserves_new_data_without_legacy_restore(migration, monkeypatch):
    plan, prepared = reviewed_update(migration)
    document = migration.root / 'data/document.txt'
    original = migration.engine._health
    observed = []

    def fail_after_candidate_write(ctx, *args, **kwargs):
        if migration.engine._pointer(ctx) == 'release-2':
            document.write_text('Новые данные candidate до ошибки health', encoding='utf-8')
            observed.append(True)
            raise EngineError('INTEGRITY_FAILED', 'Синтетический отказ candidate readiness')
        return original(ctx, *args, **kwargs)

    monkeypatch.setattr(migration.engine, '_health', fail_after_candidate_write)
    with pytest.raises(EngineError):
        migration.engine.execute(plan)
    assert observed, 'Fault должен сработать после активации нового выпуска'
    assert document.read_text(encoding='utf-8') == 'Новые данные candidate до ошибки health'
    assert migration_effect_count(migration) == 2
    with sqlite3.connect(migration.root / 'data/schema.sqlite') as database:
        assert database.execute('PRAGMA user_version').fetchone() == (2,)
    ctx = migration.engine._context(prepared['manifest'], migration.target, prepared['options'])
    assert migration.engine._pointer(ctx) == 'release-2'
    journals = [json.loads(path.read_text(encoding='utf-8'))
                for path in (migration.root / 'services').glob('update-*.json')]
    assert len(journals) == 1 and journals[0]['direction'] == 'forward'
    assert all(not migration.engine._running(record)
               for record in migration.engine._records(ctx).values()), 'Owned candidate должен быть остановлен'
    migration.current = prepared
