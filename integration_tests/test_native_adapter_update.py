"""Single-step update: настоящий fresh и отдельные prerequisite contracts."""
from copy import deepcopy
import sqlite3

import pytest

from homensai_install.engine import EngineError
from homensai_install.migration_receipts import MigrationReviewStore
from test_native_migration import migration, prepare, fresh_plan, migration_effect_count


def installed_first(fixture):
    value = fixture.release('release-1', 'schema_1', config=True)
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
        try:
            plan = migration.reviewed_plan('update', prepared)
        except EngineError as error:
            if error.code != 'CAPABILITY_UNSUPPORTED':
                raise
            assert migration.engine.capabilities(migration.target)['actions']['update']['supported'] is False
            assert migration_effect_count(migration) == 1
            assert document.read_text() == 'Данные пользователя до update'
            pytest.xfail('Update выключен: real receipts/forward journal/fault acceptance ещё не реализованы')
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


def test_update_plan_pins_source_and_failure_policy_without_enabling_capability(migration, monkeypatch):
    value, prepared, review, ctx, body = update_review(migration)
    assert migration.engine.capabilities(migration.target)['actions']['update']['supported'] is False
    original = migration.engine.capabilities
    def planning_capabilities(target):
        result = deepcopy(original(target))
        result['actions']['update']['supported'] = True
        return result
    # Только изолированный тест read-only plan: execute не вызывается.
    monkeypatch.setattr(migration.engine, 'capabilities', planning_capabilities)
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
