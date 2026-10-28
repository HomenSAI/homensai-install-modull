"""Actual legacy lifecycle removal authority; no ProductAdapter capability claim."""
import json
from pathlib import Path
import sys

import pytest

import integration_tests.test_native_engine as fixtures
from homensai_install.engine import EngineError, _digest
import homensai_install.uninstall_receipts as protocol


@pytest.fixture
def installed():
    if not sys.platform.startswith('linux'): pytest.skip('Linux user-scope engine')
    fixture = fixtures.NativeLifecycleTests('test_real_fresh_start_backup_uninstall_restore')
    fixture.setUp()
    try:
        first = fixture.bundle('v1', runtime_tag='old')
        current = fixture.bundle('v2', runtime_tag='new')
        fixture.run_action('install', first)
        fixture.run_action('update', current)
        document = fixture.root / 'data' / 'user.txt'
        document.write_text('preserve owned user data')
        yield fixture, current, document
    finally:
        fixture.tearDown()


def code_bytes(fixture):
    return {p.relative_to(fixture.root / 'releases').as_posix(): p.read_bytes()
            for p in (fixture.root / 'releases').rglob('*') if p.is_file()}


def test_reviewed_inactive_bytes_are_not_removed_after_drift(installed):
    fixture, current, document = installed
    plan = fixture.reviewed_plan('uninstall', current)
    inactive = fixture.root / 'releases/app-v1/server.py'
    inactive.write_bytes(inactive.read_bytes() + b'\n# synthetic drift\n')
    before = code_bytes(fixture)
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'INTEGRITY_FAILED'
    assert code_bytes(fixture) == before and document.read_text() == 'preserve owned user data'
    assert any(fixture.engine._running(row) for row in fixture.engine._records(fixture.engine._context(current['manifest'], fixture.target, current['options'])).values())


def test_boundary_rechecks_all_files_before_first_unlink(installed):
    fixture, current, _ = installed
    plan = fixture.reviewed_plan('uninstall', current)
    snapshots = []
    def event(value):
        if value.get('phase') == 'preserve_recovery' and value.get('status') == 'SUCCEEDED':
            last = fixture.root / 'releases/app-v2/vendor/test_fixture-1.0-py3-none-any.whl'
            last.write_bytes(last.read_bytes() + b'late drift')
            snapshots.append(code_bytes(fixture))
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan, event)
    assert caught.value.code == 'INTEGRITY_FAILED'
    assert code_bytes(fixture) == snapshots[0]


def test_unknown_absence_without_deletion_intent_is_not_resumable(installed):
    fixture, current, document = installed
    plan = fixture.reviewed_plan('uninstall', current)
    def event(value):
        if value.get('phase') == 'preserve_recovery' and value.get('status') == 'SUCCEEDED':
            raise fixtures.SimulatedCrash()
    with pytest.raises(fixtures.SimulatedCrash): fixture.engine.execute(plan, event)
    (fixture.root / 'releases/app-v1/server.py').unlink()
    before = code_bytes(fixture)
    with pytest.raises(EngineError) as caught: fixture.new_engine().resume(plan, {})
    assert caught.value.code == 'INTEGRITY_FAILED'
    assert code_bytes(fixture) == before and document.exists()


def test_original_durable_intent_resumes_after_real_unlink_and_new_operation_is_fenced(installed, monkeypatch):
    fixture, current, document = installed
    plan = fixture.reviewed_plan('uninstall', current)
    real = protocol.unlink_at
    cut = []
    def power_cut(parent, name):
        real(parent, name)
        if name == 'server.py' and not cut:
            cut.append(True)
            raise fixtures.SimulatedCrash()
    monkeypatch.setattr(protocol, 'unlink_at', power_cut)
    with pytest.raises(fixtures.SimulatedCrash): fixture.engine.execute(plan)
    before = code_bytes(fixture)
    different = {**plan, 'plan_id': plan['plan_id'] + '-different'}
    with pytest.raises(EngineError) as caught: fixture.new_engine().execute(different)
    assert caught.value.code == 'RECOVERY_REQUIRED' and code_bytes(fixture) == before
    monkeypatch.setattr(protocol, 'unlink_at', real)
    result = fixture.engine.resume(plan, {})
    assert result['status'] == 'SUCCEEDED' and result['data_preserved']
    assert not code_bytes(fixture) and document.read_text() == 'preserve owned user data'
    receipt = result['retained_recovery']
    assert receipt['kind'] == 'retained_uninstall_recovery'
    assert receipt['operation_id'].startswith('op_')
    assert receipt['receipt_id'] == _digest({key: value for key, value in receipt.items() if key != 'receipt_id'})
    assert receipt['review_sha256'] == _digest(plan['engine_plan']['uninstall_removal'])
    assert not any('path' in key for key in receipt)
    assert fixture.engine.execute(plan) == result


def test_tampered_retained_receipt_refuses_before_any_unlink(installed):
    fixture, current, _ = installed
    plan = fixture.reviewed_plan('uninstall', current)
    before = code_bytes(fixture)
    def event(value):
        if value.get('phase') == 'preserve_recovery' and value.get('status') == 'SUCCEEDED':
            path, = (fixture.root / '.hinstall/uninstall-receipts').glob('*.json')
            body = json.loads(path.read_text()); body['receipt_id'] = '0' * 64
            path.write_text(json.dumps(body))
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan, event)
    assert caught.value.code == 'INTEGRITY_FAILED' and code_bytes(fixture) == before


def test_failed_unlink_original_explicit_resume_preserves_user_data(installed, monkeypatch):
    fixture, current, document = installed
    plan = fixture.reviewed_plan('uninstall', current)
    real = protocol.unlink_at
    def denied(parent, name): raise PermissionError('synthetic owned unlink refusal')
    monkeypatch.setattr(protocol, 'unlink_at', denied)
    before = code_bytes(fixture)
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert code_bytes(fixture) == before
    monkeypatch.setattr(protocol, 'unlink_at', real)
    result = fixture.new_engine().resume(plan, {})
    assert result['status'] == 'SUCCEEDED' and document.exists()


def test_missing_file_after_full_preflight_still_requires_its_own_intent(installed, monkeypatch):
    fixture, current, _ = installed
    plan = fixture.reviewed_plan('uninstall', current)
    first = plan['engine_plan']['uninstall_removal']['releases'][0]
    victim = fixture.root / 'releases' / ('app-' + first['release']) / sorted(first['files'])[0]
    real = protocol.UninstallProtocol.preflight
    before = []
    def drift(self):
        real(self)
        victim.unlink()
        before.append(code_bytes(fixture))
    monkeypatch.setattr(protocol.UninstallProtocol, 'preflight', drift)
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'INTEGRITY_FAILED' and code_bytes(fixture) == before[0]


@pytest.mark.parametrize('field,value', [('product_id', 'different-product'), ('mode', 'docker'), ('schema_version', 999)])
def test_retained_identity_stable_fields_are_all_required(installed, field, value):
    fixture, current, _ = installed
    plan = fixture.reviewed_plan('uninstall', current)
    before = code_bytes(fixture)
    def drift(event):
        if event.get('phase') == 'preserve_recovery' and event.get('status') == 'SUCCEEDED':
            path = fixture.root / 'runtime/recovery/v2/installation.json'
            body = json.loads(path.read_text()); body[field] = value
            path.write_text(json.dumps(body))
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan, drift)
    assert caught.value.code == 'INTEGRITY_FAILED' and code_bytes(fixture) == before


def test_original_explicit_resume_rechecks_full_set_after_stop_failure_before_preserve(installed, monkeypatch):
    fixture, current, document = installed
    plan = fixture.reviewed_plan('uninstall', current)
    def refused(ctx): raise EngineError('RECOVERY_REQUIRED', 'synthetic stop failure')
    monkeypatch.setattr(fixture.engine, '_stop', refused)
    before = code_bytes(fixture)
    with pytest.raises(EngineError): fixture.engine.execute(plan)
    assert code_bytes(fixture) == before
    result = fixture.new_engine().resume(plan, {})
    assert result['status'] == 'SUCCEEDED' and document.exists()


@pytest.mark.parametrize('fault,code', [('missing', 'RECOVERY_REQUIRED'), ('public_mode', 'INTEGRITY_FAILED'), ('hardlink', 'INTEGRITY_FAILED')])
def test_retained_receipt_requires_private_single_owned_inode_before_delete(installed, fault, code):
    import os
    fixture, current, _ = installed
    plan = fixture.reviewed_plan('uninstall', current)
    before = code_bytes(fixture)
    def drift(event):
        if event.get('phase') == 'preserve_recovery' and event.get('status') == 'SUCCEEDED':
            path, = (fixture.root / '.hinstall/uninstall-receipts').glob('*.json')
            if fault == 'missing': path.unlink()
            elif fault == 'public_mode': path.chmod(0o644)
            else: os.link(path, fixture.base / 'synthetic-receipt-hardlink')
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan, drift)
    assert caught.value.code == code and code_bytes(fixture) == before


def test_directory_swap_to_unrelated_fixture_never_unlinks_foreign_bytes(installed, monkeypatch):
    from contextlib import contextmanager
    import stat
    fixture, current, _ = installed
    plan = fixture.reviewed_plan('uninstall', current)
    first = plan['engine_plan']['uninstall_removal']['releases'][0]
    relative = sorted(first['files'])[0]
    parent = (fixture.root / 'releases' / ('app-' + first['release']) / relative).parent
    unrelated = fixture.base / 'unrelated-directory'; unrelated.mkdir(mode=0o750)
    foreign = unrelated / Path(relative).name; foreign.write_bytes(b'unrelated synthetic file'); foreign.chmod(0o640)
    original_mode = stat.S_IMODE(unrelated.stat().st_mode)
    real = protocol.directory_handle
    fired = []
    @contextmanager
    def swapped(path):
        if Path(path) == parent and not fired:
            fired.append(True)
            parent.rename(fixture.base / 'preserved-original-directory')
            parent.symlink_to(unrelated, target_is_directory=True)
        with real(path) as descriptor: yield descriptor
    monkeypatch.setattr(protocol, 'directory_handle', swapped)
    with pytest.raises(EngineError): fixture.engine.execute(plan)
    assert fired and foreign.read_bytes() == b'unrelated synthetic file'
    assert stat.S_IMODE(unrelated.stat().st_mode) == original_mode
    assert (fixture.base / 'preserved-original-directory' / Path(relative).name).is_file()
