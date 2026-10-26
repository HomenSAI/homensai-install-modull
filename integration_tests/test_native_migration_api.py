"""Public SDK preparation against a real signed process/SQLite product probe."""
import hashlib
import json

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.backends import BackendRouter
from homensai_install.errors import InstallerError
from test_native_migration import migration


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
