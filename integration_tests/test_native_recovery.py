"""Настоящие recovery archives и запрет mutation без safety snapshot."""
import hashlib
import json
from pathlib import Path
import sys
import shutil

import pytest

from homensai_install.engine import EngineError
from shared import archive_crypto


@pytest.fixture
def recovery():
    if sys.platform != 'linux':
        pytest.skip('Linux/native/user recovery')
    from test_native_engine import NativeLifecycleTests
    fixture = NativeLifecycleTests(methodName='runTest')
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def setup_recovery(fixture, action):
    first, second = fixture.bundle('v1'), fixture.bundle('v2')
    fixture.run_action('install', first)
    document = fixture.root / 'data/user.txt'
    document.write_text('snapshot')
    archive = fixture.run_action('backup', first)['archive']['path']
    fixture.run_action('update', second)
    document.write_text('new data before recovery')
    options = {'archive_path': archive}
    if action == 'rollback':
        options['acknowledge_data_loss'] = True
    return first, second, document, archive, fixture.operation_plan(action, second, **options)


@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_before_recovery_is_independent_verified_full_archive(recovery, action):
    first, second, document, archive, plan = setup_recovery(recovery, action)
    result = recovery.engine.execute(plan)
    recovery.current = first
    proof = result['before_recovery']
    assert proof['recovery_action'] == action
    assert proof['source_archive_sha256'] == hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    assert proof['verification_scope'] == 'full_snapshot_and_signed_recovery_payload_restored'
    assert proof['migration_eligible'] is False
    assert proof['from_release'] == 'v2'
    assert document.read_text() == 'snapshot'
    safety = Path(result['before_recovery_archive']['path'])
    assert safety.name.endswith('-before-' + action + '.hsac')
    assert result['before_recovery_archive']['sha256'] == proof['archive_sha256']
    restored = recovery.base / 'safety-oracle'
    restored.mkdir(mode=0o700)
    archive_crypto.restore_archive(safety, restored, password_provider=lambda: 'test-only-strong-archive-password',
                                   expected_product_id=second['manifest']['product_id'])
    assert (restored / 'roots/data/user.txt').read_text() == 'new data before recovery'
    assert json.loads((restored / 'roots/services/active.json').read_text()) == {'release': 'v2'}
    assert (restored / 'roots/runtime/recovery/v2/payload/server.py').is_file()


@pytest.mark.parametrize('action', ['restore', 'rollback'])
@pytest.mark.parametrize('fault', ['missing_archive', 'tampered_archive', 'missing_receipt', 'live_drift'])
def test_safety_proof_is_rechecked_at_actual_replacement_boundary(recovery, action, fault):
    _, _, document, _, plan = setup_recovery(recovery, action)
    def corrupt(event):
        if event['phase'] == 'restore' and event['status'] == 'RUNNING':
            safety = next((recovery.base / '.installed-recovery').glob('*-before-*.hsac'))
            if fault == 'missing_archive':
                safety.unlink()
            elif fault == 'tampered_archive':
                raw = bytearray(safety.read_bytes()); raw[-1] ^= 1; safety.write_bytes(raw)
            elif fault == 'missing_receipt':
                for path in (recovery.root / '.hinstall/recovery-receipts').glob('*.json'):
                    path.unlink()
            else:
                document.write_text('new concurrent user data')
    with pytest.raises(Exception) as caught:
        recovery.engine.execute(plan, emit=corrupt)
    assert getattr(caught.value, 'code', None) == 'RECOVERY_REQUIRED'
    assert document.read_text() == ('new concurrent user data' if fault == 'live_drift' else 'new data before recovery')
    assert json.loads((recovery.root / 'services/active.json').read_text()) == {'release': 'v2'}


@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_same_product_foreign_installation_archive_is_refused_before_stop(recovery, action):
    _, second, document, archive, plan = setup_recovery(recovery, action)
    candidate = recovery.base / 'foreign-source'
    candidate.mkdir(mode=0o700)
    password = lambda: 'test-only-strong-archive-password'
    archive_crypto.restore_archive(archive, candidate, password_provider=password,
                                   expected_product_id=second['manifest']['product_id'])
    owner_path = candidate / 'roots/runtime/recovery/v1/installation.json'
    owner = json.loads(owner_path.read_text()); owner['installation_id'] = 'f' * 32
    owner_path.write_text(json.dumps(owner))
    from shared.datahome import resolve_datahome
    home = resolve_datahome(second['manifest'], candidate / 'roots')
    foreign = recovery.base / 'foreign.hsac'
    archive_crypto.build_encrypted_archive(home, foreign, password_provider=password)
    plan['options']['archive_path'] = str(foreign)
    processes = (recovery.root / '.hinstall/processes.json').read_bytes()
    with pytest.raises(EngineError) as caught:
        recovery.engine.execute(plan)
    assert caught.value.code == 'OPERATION_CONFLICT'
    assert (recovery.root / '.hinstall/processes.json').read_bytes() == processes
    assert document.read_text() == 'new data before recovery'


@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_partial_replacement_fences_original_resume_and_new_plan(recovery, action, monkeypatch):
    from shared.adapters_fs import FilesystemPorts
    _, second, document, _, plan = setup_recovery(recovery, action)
    actual = FilesystemPorts._reconcile
    calls = []
    def interrupted(source, destination, preserved):
        if Path(source).parent.name == 'roots' and Path(source).parent.parent.name.endswith('-restore-candidate'):
            calls.append(Path(source).name)
            actual(source, destination, preserved)
            raise RuntimeError('synthetic failure after first reconciled root')
        return actual(source, destination, preserved)
    monkeypatch.setattr(FilesystemPorts, '_reconcile', interrupted)
    with pytest.raises(RuntimeError):
        recovery.engine.execute(plan)
    assert len(calls) == 1
    monkeypatch.setattr(FilesystemPorts, '_reconcile', actual)
    after = document.read_text()
    for request in (plan, recovery.operation_plan(action, second, **{
            'archive_path': plan['options']['archive_path'], 'acknowledge_data_loss': True})):
        with pytest.raises(EngineError) as caught:
            recovery.engine.execute(request)
        assert caught.value.code == 'RECOVERY_REQUIRED'
        assert document.read_text() == after


@pytest.mark.parametrize('root', ['data', 'backup'])
def test_imported_source_inside_replaced_root_is_refused_before_stop(recovery, root):
    _, second, document, archive, _ = setup_recovery(recovery, 'rollback')
    imported = recovery.root / root / 'imported.hsac'
    shutil.copyfile(archive, imported)
    processes = (recovery.root / '.hinstall/processes.json').read_bytes()
    for reviewed in (False, True):
        with pytest.raises(EngineError) as caught:
            plan = recovery.reviewed_plan('rollback', second, archive_path=str(imported), acknowledge_data_loss=True) if reviewed else (
                recovery.operation_plan('rollback', second, archive_path=str(imported), acknowledge_data_loss=True))
            recovery.engine.execute(plan)
        assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
        assert caught.value.details['reason'] == 'recovery_source_overlaps_managed_roots'
        assert imported.read_bytes() == Path(archive).read_bytes()
        assert document.read_text() == 'new data before recovery'
        assert (recovery.root / '.hinstall/processes.json').read_bytes() == processes


def test_archive_with_different_product_mode_is_refused_before_stop(recovery):
    recovery.manifest['modes'].append({'name': 'alternate', 'components': ['web']})
    first, second = recovery.bundle('v1'), recovery.bundle('v2')
    recovery.run_action('install', first)
    snapshot = recovery.run_action('backup', first)['archive']['path']
    second['options']['modes'] = ['alternate']
    recovery.run_action('update', second)
    processes = (recovery.root / '.hinstall/processes.json').read_bytes()
    with pytest.raises(EngineError) as caught:
        recovery.engine.execute(recovery.operation_plan('rollback', second, archive_path=snapshot, acknowledge_data_loss=True))
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert (recovery.root / '.hinstall/processes.json').read_bytes() == processes
    assert json.loads((recovery.root / 'services/active.json').read_text())['release'] == 'v2'
