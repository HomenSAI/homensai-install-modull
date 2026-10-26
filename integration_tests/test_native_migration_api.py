"""Public SDK preparation against a real signed process/SQLite product probe."""
import hashlib
import json
import base64
import sys

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.backends import BackendRouter
from homensai_install.errors import InstallerError
from test_native_migration import migration
from test_native_migration import migration_effect_count


def product_files(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file() and '.hinstall' not in path.parts}


def test_public_sdk_prepares_fresh_signed_probe_without_installing_product(migration, tmp_path):
    value = migration.release('release-1', 'schema_1')
    api = InstallerAPI(tmp_path / 'api-state', engine=BackendRouter(native=migration.engine))
    result = api.prepare_migration('install', value['manifest'], migration.target, value['options'])
    assert result['action'] == 'install' and result['from_release'] is None
    assert result['product_plan']['from_version'] == 'empty'
    assert result['product_plan']['to_version'] == 'schema_1'
    assert not api.state_dir.exists()
    ctx = migration.engine._context(value['manifest'], migration.target, value['options'])
    assert migration.engine._pointer(ctx) is None
    assert not any(ctx['home'].roots['data'].iterdir())
    before = product_files(migration.root)
    with pytest.raises(InstallerError) as error:
        api.prepare_migration('install', value['manifest'], migration.target,
            {**value['options'], 'migration_review_id': result['migration_review_id']})
    assert error.value.code == 'INVALID_REQUEST'
    assert product_files(migration.root) == before


def test_public_sdk_update_preparation_observes_schema_without_mutating_data(migration, tmp_path):
    old = migration.release('release-1', 'schema_1')
    migration.legacy_install(old)
    new = migration.release('release-2', 'schema_2')
    api = InstallerAPI(tmp_path / 'api-state', engine=BackendRouter(native=migration.engine))
    before = product_files(migration.root)
    result = api.prepare_migration('update', new['manifest'], migration.target, new['options'])
    assert result['from_release'] == 'release-1' and result['to_release'] == 'release-2'
    assert result['product_plan']['from_version'] == 'schema_1'
    assert result['product_plan']['to_version'] == 'schema_2'
    assert product_files(migration.root) == before
    assert not api.state_dir.exists()


@pytest.mark.parametrize('status', ['FAILED', 'QUEUED', 'UNKNOWN', None])
def test_preparation_does_not_accept_unresolved_or_unknown_native_journal(migration, tmp_path, status):
    old = migration.release('release-1', 'schema_1')
    migration.legacy_install(old)
    new = migration.release('release-2', 'schema_2')
    journal = migration.root / '.hinstall/operations/unresolved.json'
    journal.write_text(json.dumps({'action': 'update', 'status': status}), encoding='utf-8')
    journal.chmod(0o600)
    api = InstallerAPI(tmp_path / 'api-state', engine=BackendRouter(native=migration.engine))
    before = product_files(migration.root)
    with pytest.raises(InstallerError) as error:
        api.prepare_migration('update', new['manifest'], migration.target, new['options'])
    assert error.value.code == 'RECOVERY_REQUIRED'
    assert product_files(migration.root) == before


def public_fresh(migration, tmp_path, *, fault=None):
    value = migration.release('release-1', 'schema_1', config=True, **({'fault': fault} if fault else {}))
    trust = tmp_path / 'trusted-public-keys.json'
    trust.write_text(json.dumps({'schema_version': 1, 'keys': {
        name: base64.b64encode(key).decode() for name, key in migration.engine.verifier._keys.items()}}), encoding='utf-8')
    api = InstallerAPI.from_bootstrap(tmp_path / 'public-fresh-state', trust_config=trust,
        allow_development=True, development_runtime=sys.executable,
        product_adapter_descriptor='installer/adapter.json', secret_provider=migration.engine.secret_provider)
    prepared = api.prepare_migration('install', value['manifest'], migration.target, value['options'])
    options = {**value['options'], 'migration_review_id': prepared['migration_review_id']}
    plan = api.plan('install', value['manifest'], migration.target, options=options)
    return api, plan, {**value, 'options': options}


def test_public_factory_fresh_lost_ack_resumes_original_operation_without_second_migration(migration, tmp_path):
    api, plan, prepared = public_fresh(migration, tmp_path, fault='after_apply')
    operation = api.submit(plan['plan_id'], plan['plan_digest'],
        [plan['review_consent_ref'], *plan['required_consents']], idempotency_key='reviewed-fresh', background=False)
    assert operation['status'] == 'RECOVERY_REQUIRED'
    assert migration_effect_count(migration) == 1
    resumed = api.resume(operation['operation_id'], background=False)
    migration.current = prepared
    assert resumed['operation_id'] == operation['operation_id'] and resumed['status'] == 'SUCCEEDED'
    assert migration_effect_count(migration) == 1
    assert api.submit(plan['plan_id'], plan['plan_digest'],
        [plan['review_consent_ref'], *plan['required_consents']], idempotency_key='reviewed-fresh', background=False)['operation_id'] == operation['operation_id']
    assert migration_effect_count(migration) == 1


def test_public_sdk_resume_after_known_no_effect_fresh_authorization_refusal(migration, tmp_path, monkeypatch):
    from homensai_install.migration_receipts import FreshEmptyReceiptStore
    api, plan, prepared = public_fresh(migration, tmp_path)
    verify = FreshEmptyReceiptStore.verify
    def refuse(*args, **kwargs):
        raise InstallerError('PLAN_STALE')
    monkeypatch.setattr(FreshEmptyReceiptStore, 'verify', refuse)
    operation = api.submit(plan['plan_id'], plan['plan_digest'],
        [plan['review_consent_ref'], *plan['required_consents']], idempotency_key='refused-before-effect', background=False)
    assert operation['status'] == 'RECOVERY_REQUIRED' and operation['error']['code'] == 'PLAN_STALE'
    assert migration_effect_count(migration) == 0
    monkeypatch.setattr(FreshEmptyReceiptStore, 'verify', verify)
    resumed = api.resume(operation['operation_id'], background=False)
    migration.current = prepared
    assert resumed['status'] == 'SUCCEEDED' and migration_effect_count(migration) == 1
