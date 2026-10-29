"""Actual local Native archive roundtrip through the public API boundary."""
from pathlib import Path
import sys

import pytest

from homensai_install.api.v1 import InstallerAPI
from shared import archive_crypto
import test_native_engine as native_fixture


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux native integration')
@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_public_backup_restore_preserves_functional_home_locator(action):
    fixture = native_fixture.NativeLifecycleTests('test_real_fresh_start_backup_uninstall_restore')
    fixture.setUp()
    try:
        fixture.base = fixture.base / 'home' / 'synthetic-user' / 'synthetic@example.test'
        fixture.base.mkdir(parents=True)
        fixture.root = fixture.base / 'installed'
        bundle = fixture.bundle('v1')
        fixture.run_action('install', bundle)
        document = fixture.root / 'data' / 'user-document.txt'
        document.write_text('snapshot bytes')
        api = InstallerAPI(fixture.base / 'api-state', engine=fixture.engine)

        def run(action, **extra):
            plan = api.plan(action, bundle['manifest'], target=fixture.target,
                            options={**bundle['options'], **extra})
            operation = api.submit(plan['plan_id'], plan['plan_digest'],
                [plan['review_consent_ref'], *plan['required_consents']],
                idempotency_key=action, background=False)
            assert operation['status'] == 'SUCCEEDED', operation.get('error')
            return plan, operation

        plan, backup = run('backup')
        archive = backup['result']['archive']
        assert '/home/synthetic-user/' in archive['path']
        assert 'synthetic@example.test' in archive['path']
        assert Path(archive['path']).is_file()
        archive_crypto.verify_archive(archive['path'],
            password_provider=lambda: 'test-only-strong-archive-password',
            expected_product_id=bundle['manifest']['product_id'])
        fresh = InstallerAPI(api.state_dir, engine=fixture.engine)
        assert fresh.status(backup['operation_id'])['result']['archive']['path'] == archive['path']
        duplicate = fresh.submit(plan['plan_id'], plan['plan_digest'],
            [plan['review_consent_ref'], *plan['required_consents']],
            idempotency_key='backup', background=False)
        assert duplicate['result']['archive']['path'] == archive['path']
        document.write_text('latest bytes')
        _, restored = run(action, archive_path=archive['path'], acknowledge_data_loss=True)
        assert document.read_text() == 'snapshot bytes'
        safety = restored['result']['before_recovery_archive']
        assert Path(safety['path']).is_file()
        assert '/home/synthetic-user/' in safety['path']
        assert safety['sha256'] == restored['result']['before_recovery']['archive_sha256']
    finally:
        fixture.tearDown()
