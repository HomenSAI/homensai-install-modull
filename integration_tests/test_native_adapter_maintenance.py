"""Same-release registered maintenance: trusted candidate, preserved business data."""
import json
import os
from pathlib import Path
import sqlite3

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.engine import EngineError, _digest
from homensai_install.errors import InstallerError
from test_native_adapter_recovery import recovery_adapter, migration
from test_native_migration import fresh_plan


@pytest.fixture
def installed(migration):
    value = migration.release('release-1', 'schema_1', config=True)
    plan, value = fresh_plan(migration, value)
    migration.engine.execute(plan); migration.current = value
    document = migration.root / 'data/document.txt'
    document.write_text('latest synthetic user document')
    return migration, value, document


def operation(fixture, action):
    records = [json.loads(p.read_text()) for p in (fixture.root / '.hinstall/operations').glob('*.json')]
    return next(record for record in records if record['action'] == action)


def maintenance_paths(fixture, plan):
    token = _digest([plan['plan_id'], plan.get('plan_digest', _digest(plan))])[:32]
    return token, fixture.root / '.hinstall/code-maintenance' / token


@pytest.mark.parametrize('action', ['repair', 'reinstall'])
def test_registered_code_maintenance_preserves_data_and_never_runs_damaged_source(recovery_adapter, monkeypatch, tmp_path, action):
    fixture, first, current, old_archive, document = recovery_adapter
    installed = fixture.root / 'releases/app-release-2'
    (installed / 'adapter.py').write_text("raise RuntimeError('damaged installed adapter must never execute')")
    (installed / 'server.py').write_text("raise RuntimeError('damaged installed server must never execute')")
    owner_before = json.loads((fixture.root / '.hinstall/installation.json').read_text())
    database = (fixture.root / 'data/schema.sqlite').read_bytes()
    credential = fixture.root / 'config/synthetic-credential'
    credential.write_text('synthetic maintenance credential'); credential.chmod(0o600)
    actual = ExecutableProductAdapter._execute
    observed = []
    def watch(adapter, hook, request, request_digest):
        observed.append((hook, Path(adapter.options['payload_dir'])))
        assert hook not in {'migration_apply', 'restore', 'post_restore'}
        return actual(adapter, hook, request, request_digest)
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', watch)
    plan = fixture.reviewed_plan(action, current)
    assert not observed, 'plan must be read-only'
    result = fixture.engine.execute(plan)
    assert result['status'] == 'SUCCEEDED' and result['health']['release'] == 'release-2'
    assert result['before_maintenance']['kind'] == 'before_code_maintenance_backup'
    assert result['before_maintenance']['migration_eligible'] is False
    assert document.read_text() == 'latest data' and credential.read_text() == 'synthetic maintenance credential'
    assert (fixture.root / 'data/schema.sqlite').read_bytes() == database
    assert json.loads((fixture.root / '.hinstall/installation.json').read_text()) == owner_before
    assert (installed / 'server.py').read_bytes() == (Path(current['options']['payload_dir']) / 'server.py').read_bytes()
    assert [path for hook, path in observed if hook == 'quiesce'] and all(
        path != installed for hook, path in observed if hook in {'quiesce', 'migration_plan'})
    assert [path for hook, path in observed if hook == 'readiness'] == [installed]
    assert fixture.engine.execute(plan) == result


def test_first_repair_without_prior_backup_produces_existing_format_full_recovery_archive(installed, tmp_path):
    from shared import archive_crypto
    fixture, value, document = installed
    (fixture.root / 'releases/app-release-1/server.py').unlink()
    key = fixture.root / 'config/private-key'; key.write_bytes(b'synthetic private key'); key.chmod(0o600)
    plan = fixture.reviewed_plan('repair', value)
    result = fixture.engine.execute(plan)
    candidate = tmp_path / 'oracle'; candidate.mkdir(mode=0o700)
    archive_crypto.restore_archive(result['before_maintenance_archive']['path'], candidate,
        password_provider=lambda: 'synthetic-archive-passphrase', expected_product_id=value['manifest']['product_id'])
    assert {p.name for p in (candidate / 'roots').iterdir()} == {'data', 'backup', 'services', 'config', 'runtime'}
    assert (candidate / 'roots/data/document.txt').read_bytes() == document.read_bytes()
    assert (candidate / 'roots/config/private-key').read_bytes() == key.read_bytes()
    assert (candidate / 'roots/runtime/recovery/release-1/payload/server.py').is_file()
    assert fixture.engine._verify(value['manifest'], fixture.target, {
        **value['options'], 'payload_dir': str(candidate / 'roots/runtime/recovery/release-1/payload')})


def test_same_release_reinstall_after_registered_uninstall_preserves_identity_keys_and_schema(installed):
    fixture, value, document = installed
    fixture.engine.execute(fixture.reviewed_plan('uninstall', value))
    assert not any((fixture.root / 'releases').iterdir())
    document.write_text('changes after uninstall')
    marker = json.loads((fixture.root / '.hinstall/installation.json').read_text())
    database = (fixture.root / 'data/schema.sqlite').read_bytes()
    result = fixture.engine.execute(fixture.reviewed_plan('reinstall', value))
    after = json.loads((fixture.root / '.hinstall/installation.json').read_text())
    assert result['status'] == 'SUCCEEDED' and after['installed'] is True and 'retained_uninstall' not in after
    assert after['installation_id'] == marker['installation_id']
    assert (fixture.root / 'data/schema.sqlite').read_bytes() == database
    assert document.read_text() == 'changes after uninstall'


@pytest.mark.parametrize('fault', ['schema', 'stale_code', 'stale_candidate'])
def test_unreviewed_or_schema_incompatible_repair_refuses_before_code_replacement(installed, monkeypatch, fault):
    fixture, value, document = installed
    live = fixture.root / 'releases/app-release-1/server.py'
    live.write_text('synthetic damaged source')
    if fault == 'schema':
        with sqlite3.connect(fixture.root / 'data/schema.sqlite') as db: db.execute('PRAGMA user_version=3')
    plan = fixture.reviewed_plan('repair', value)
    if fault == 'stale_code': live.write_text('different corruption after review')
    if fault == 'stale_candidate': (Path(value['options']['payload_dir']) / 'server.py').write_text('changed candidate')
    before = live.read_bytes(); data = document.read_bytes()
    from homensai_install import code_maintenance
    monkeypatch.setattr(code_maintenance, 'rename_at', lambda *a: pytest.fail('refusal before any rename'))
    with pytest.raises((EngineError, InstallerError)):
        fixture.engine.execute(plan)
    assert live.read_bytes() == before and document.read_bytes() == data
    assert not list((fixture.base / '.installed-recovery').glob('*-before-repair.hsac'))


@pytest.mark.parametrize('fault', ['missing', 'replacement'])
def test_authoritative_ledger_lost_between_candidate_proof_and_quiesce_is_not_recreated(installed, monkeypatch, fault):
    fixture, value, document = installed
    live = fixture.root / 'releases/app-release-1/server.py'; live.write_text('damaged')
    plan = fixture.reviewed_plan('repair', value)
    actual = ExecutableProductAdapter.invoke
    seen = []
    def lose(adapter, hook, request, **kwargs):
        if hook == 'quiesce' and not seen:
            seen.append(True)
            path = adapter.state_dir / 'adapter-invocations.sqlite3'
            path.rename(path.with_suffix('.preserved'))
            if fault == 'replacement': path.write_bytes(path.with_suffix('.preserved').read_bytes()); path.chmod(0o600)
        return actual(adapter, hook, request, **kwargs)
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', lose)
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'RECOVERY_REQUIRED' and seen
    path = fixture.root / '.hinstall/product-adapter/release-1/adapter-invocations.sqlite3'
    if fault == 'missing': assert not path.exists()
    assert live.read_text() == 'damaged' and document.read_text() == 'latest synthetic user document'


@pytest.mark.parametrize('cut', ['quarantine', 'publish'])
def test_real_process_death_after_each_code_rename_resumes_only_original_operation(installed, monkeypatch, cut):
    fixture, value, document = installed
    # Stop owned processes first: child process death must not leave parent wait() ownership ambiguities.
    fixture.engine.execute(fixture.reviewed_plan('stop', value))
    live = fixture.root / 'releases/app-release-1/server.py'; live.write_text('original damaged bytes')
    plan = fixture.reviewed_plan('repair', value)
    from homensai_install import code_maintenance
    actual = code_maintenance.rename_at
    child = os.fork()
    if child == 0:
        def power_cut(sp, sn, dp, dn):
            actual(sp, sn, dp, dn)
            if dn == ('quarantine' if cut == 'quarantine' else 'app-release-1'): os._exit(89)
        code_maintenance.rename_at = power_cut
        try: fixture.engine.execute(plan)
        except BaseException: os._exit(90)
        os._exit(91)
    assert os.waitpid(child, 0)[1] == 89 << 8
    token, container = maintenance_paths(fixture, plan)
    assert (container / 'quarantine/server.py').read_text() == 'original damaged bytes'
    assert document.read_text() == 'latest synthetic user document'
    with pytest.raises(EngineError): fixture.engine.execute(fixture.reviewed_plan('repair', value))
    result = fixture.engine.resume(plan, {})
    assert result['status'] == 'SUCCEEDED' and document.read_text() == 'latest synthetic user document'
    assert sorted(p.name for p in (fixture.root / 'releases').iterdir()) == ['app-release-1']
    # Namespace remains compatible with the fully reviewed uninstall protocol.
    assert fixture.engine.execute(fixture.reviewed_plan('uninstall', value))['status'] == 'SUCCEEDED'


@pytest.mark.parametrize('cleanup_fault', ['unknown', 'known'])
def test_failed_health_preserves_trusted_code_original_bytes_and_both_cleanup_errors(installed, monkeypatch, cleanup_fault):
    fixture, value, document = installed
    (fixture.root / 'releases/app-release-1/server.py').write_text('original broken code')
    plan = fixture.reviewed_plan('repair', value)
    monkeypatch.setattr(fixture.engine, '_health', lambda ctx: (_ for _ in ()).throw(EngineError('HEALTH_CHECK_FAILED')))
    actual = ExecutableProductAdapter._execute
    def cleanup_unknown(adapter, hook, request, request_digest):
        if hook == 'quiesce' and request['idempotency_key'].startswith('cleanup_'):
            raise InstallerError('ADAPTER_EXECUTION_FAILED')
        return actual(adapter, hook, request, request_digest)
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', cleanup_unknown)
    if cleanup_fault == 'known':
        from homensai_install.code_maintenance import CodeMaintenance
        actual_quiesce = CodeMaintenance.quiesce
        def cleanup_refusal(protocol, cleanup=False):
            if cleanup: raise EngineError('PERMISSION_REQUIRED')
            return actual_quiesce(protocol, cleanup)
        monkeypatch.setattr(CodeMaintenance, 'quiesce', cleanup_refusal)
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == ('RECOVERY_REQUIRED' if cleanup_fault == 'unknown' else 'HEALTH_CHECK_FAILED')
    assert caught.value.details == {'primary_error_code': 'HEALTH_CHECK_FAILED',
        'cleanup_error_code': 'RECOVERY_REQUIRED' if cleanup_fault == 'unknown' else 'PERMISSION_REQUIRED'}
    _, container = maintenance_paths(fixture, plan)
    assert (container / 'quarantine/server.py').read_text() == 'original broken code'
    assert document.read_text() == 'latest synthetic user document'
    assert not fixture.engine._records(fixture.engine._context(value['manifest'], fixture.target, value['options']))


def cut_after_journal_write(fixture, plan, predicate):
    from homensai_install import engine as engine_module
    real = engine_module._write
    child = os.fork()
    if child == 0:
        def write(path, value, **kwargs):
            real(path, value, **kwargs)
            if Path(path).parent.name == 'operations' and predicate(value): os._exit(89)
        engine_module._write = write
        try: fixture.engine.execute(plan)
        except BaseException: os._exit(90)
        os._exit(91)
    assert os.waitpid(child, 0)[1] == 89 << 8


def test_publish_intent_without_actual_publish_does_not_waive_live_snapshot_comparison(installed):
    fixture, value, document = installed
    fixture.engine.execute(fixture.reviewed_plan('stop', value))
    (fixture.root / 'releases/app-release-1/server.py').write_text('original damaged code')
    plan = fixture.reviewed_plan('repair', value)
    cut_after_journal_write(fixture, plan, lambda state: state.get('code_maintenance_renames', {}).get('publish_started'))
    _, container = maintenance_paths(fixture, plan)
    assert (container / 'stage').is_dir() and not (fixture.root / 'releases/app-release-1').exists()
    document.write_text('offline edit after durable publish intent')
    with pytest.raises((EngineError, InstallerError)) as caught: fixture.engine.resume(plan, {})
    assert caught.value.code in {'RECOVERY_REQUIRED', 'INTEGRITY_FAILED'}
    assert (container / 'stage').is_dir() and not (fixture.root / 'releases/app-release-1').exists()
    assert document.read_text() == 'offline edit after durable publish intent'


@pytest.mark.parametrize('artifact', ['archive', 'receipt'])
def test_completed_code_step_still_requires_verified_safety_artifact_before_resumed_start(installed, monkeypatch, artifact):
    fixture, value, document = installed
    fixture.engine.execute(fixture.reviewed_plan('stop', value))
    (fixture.root / 'releases/app-release-1/server.py').write_text('original damaged code')
    plan = fixture.reviewed_plan('repair', value)
    cut_after_journal_write(fixture, plan, lambda state: 'repair' in state.get('completed', []))
    token, _ = maintenance_paths(fixture, plan)
    path = (fixture.base / '.installed-recovery' / (token + '-before-repair.hsac') if artifact == 'archive'
            else fixture.root / '.hinstall/code-maintenance-receipts' / (token + '.json'))
    assert path.is_file(); path.unlink()
    monkeypatch.setattr(fixture.engine, '_start', lambda ctx: pytest.fail('must verify historical backup before any resumed start'))
    with pytest.raises((EngineError, InstallerError)): fixture.engine.resume(plan, {})
    assert not path.exists() and document.read_text() == 'latest synthetic user document'


def test_missing_completed_cleanup_row_does_not_authorize_resumed_start(installed, monkeypatch):
    fixture, value, document = installed
    plan = fixture.reviewed_plan('repair', value)
    actual_health = fixture.engine._health
    monkeypatch.setattr(fixture.engine, '_health', lambda ctx: (_ for _ in ()).throw(EngineError('HEALTH_CHECK_FAILED')))
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'HEALTH_CHECK_FAILED'
    token, _ = maintenance_paths(fixture, plan)
    state = operation(fixture, 'repair')
    assert state['code_maintenance_cleanup']['complete'] is True
    row = state['code_maintenance_cleanup']['witness']['call_id']
    with sqlite3.connect(fixture.root / '.hinstall/product-adapter/release-1/adapter-invocations.sqlite3') as db:
        assert db.execute('DELETE FROM calls WHERE call_id=?', (row,)).rowcount == 1
    monkeypatch.setattr(fixture.engine, '_health', actual_health)
    monkeypatch.setattr(fixture.engine, '_start', lambda ctx: pytest.fail('missing cleanup witness must refuse before resumed start'))
    with pytest.raises((EngineError, InstallerError)) as caught: fixture.engine.resume(plan, {})
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert document.read_text() == 'latest synthetic user document'


@pytest.mark.parametrize('fault', ['late_code_drift', 'receipt_tamper', 'receipt_hardlink', 'receipt_public_mode'])
def test_entire_source_and_private_backup_proof_rechecked_before_first_rename(installed, monkeypatch, fault):
    fixture, value, document = installed
    live = fixture.root / 'releases/app-release-1'
    (live / 'server.py').write_text('reviewed damaged source')
    plan = fixture.reviewed_plan('repair', value)
    token, container = maintenance_paths(fixture, plan)
    fired = []
    def emit(event):
        if event.get('phase') == 'backup' and event.get('status') == 'SUCCEEDED':
            fired.append(True)
            proof = fixture.root / '.hinstall/code-maintenance-receipts' / (token + '.json')
            if fault == 'late_code_drift': (live / 'server.py').write_text('changed after full backup')
            elif fault == 'receipt_tamper':
                value = json.loads(proof.read_text()); value['receipt_id'] = '0' * 64
                proof.write_text(json.dumps(value))
            elif fault == 'receipt_hardlink': os.link(proof, fixture.base / 'preserved-proof-link')
            else: proof.chmod(0o644)
    with pytest.raises((EngineError, InstallerError)) as caught: fixture.engine.execute(plan, emit=emit)
    assert fired and caught.value.code in {'PLAN_STALE', 'INTEGRITY_FAILED', 'RECOVERY_REQUIRED'}
    assert not (container / 'quarantine').exists()
    assert (live / 'server.py').read_text() == ('changed after full backup' if fault == 'late_code_drift' else 'reviewed damaged source')
    assert document.read_text() == 'latest synthetic user document'


def test_corrupt_existing_recovery_cache_is_refused_by_readonly_plan_before_any_hook(installed, monkeypatch):
    fixture, value, document = installed
    ctx = fixture.engine._context(value['manifest'], fixture.target, value['options'])
    recovery = fixture.engine._recovery_copy(ctx, 'release-1')
    (recovery / 'payload/server.py').write_text('untrusted existing recovery payload')
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', lambda *a, **k: pytest.fail('plan must not run any hook'))
    with pytest.raises((EngineError, InstallerError)): fixture.reviewed_plan('repair', value)
    assert document.read_text() == 'latest synthetic user document'
    assert (recovery / 'payload/server.py').read_text() == 'untrusted existing recovery payload'


@pytest.fixture
def required_secret_installation(migration, monkeypatch):
    original_bundle = migration.bundle
    token = 'synthetic original product credential'
    def bundle(name):
        value = original_bundle(name, synthetic_secret=token)
        value['options']['secret_refs'] = {'PRODUCT_TOKEN': 'accepted.product'}
        return value
    monkeypatch.setattr(migration, 'bundle', bundle)
    provider_calls = []
    def provider(reference):
        provider_calls.append(reference)
        if reference == 'test:archive': return 'synthetic-archive-passphrase'
        if reference == 'accepted.product': return token
        # This provider models a real product refusing a bootstrap's foreign alias.
        (migration.base / 'foreign-secret-provider-called').write_text(reference)
        raise AssertionError('maintenance used incoming credential alias')
    migration.engine.secret_provider = provider
    value = migration.release('release-1', 'schema_1', config=True)
    plan, value = fresh_plan(migration, value)
    migration.engine.execute(plan); migration.current = value
    document = migration.root / 'data/document.txt'; document.write_text('preserved credential-bound user data')
    provider_calls.clear()
    return migration, value, document, provider_calls, token


@pytest.mark.parametrize('action', ['repair', 'reinstall'])
@pytest.mark.parametrize('original_resume', [False, True])
def test_required_installed_secret_alias_wins_before_preflight_and_on_original_resume(required_secret_installation, action, original_resume):
    fixture, value, document, provider_calls, expected = required_secret_installation
    fixture.engine.execute(fixture.reviewed_plan('stop', value))
    if action == 'reinstall':
        fixture.engine.execute(fixture.reviewed_plan('uninstall', value))
    else:
        (fixture.root / 'releases/app-release-1/server.py').write_text('damaged source must not run')
    metadata = fixture.root / '.hinstall/releases/release-1.json'
    before = metadata.read_bytes()
    provider_calls.clear()
    plan = fixture.reviewed_plan(action, value, secret_refs={'PRODUCT_TOKEN': 'forbidden.incoming'})
    assert not provider_calls, 'readonly review does not resolve credentials'
    if original_resume:
        cut_after_journal_write(fixture, plan, lambda state: 'repair' in state.get('completed', []))
        result = fixture.engine.resume(plan, {})
    else:
        result = fixture.engine.execute(plan)
    assert result['status'] == 'SUCCEEDED'
    assert not (fixture.base / 'foreign-secret-provider-called').exists()
    assert set(provider_calls) <= {'accepted.product', 'test:archive'} and 'accepted.product' in provider_calls
    assert metadata.read_bytes() == before
    assert document.read_text() == 'preserved credential-bound user data'
    ctx = fixture.engine._context(value['manifest'], fixture.target, value['options'])
    records = fixture.engine._records(ctx)
    assert records
    for record in records.values():
        environment = Path('/proc', str(record['pid']), 'environ').read_bytes().split(b'\0')
        assert b'PRODUCT_TOKEN=' + expected.encode() in environment


@pytest.mark.parametrize('action,phase,failure', [
    ('repair', 'before_verification', 'space'), ('reinstall', 'before_verification', 'space'),
    ('repair', 'after_publish', 'space'), ('reinstall', 'after_publish', 'space'),
    ('repair', 'after_publish', 'invalid_archive')])
def test_public_space_failure_is_typed_and_original_resume_keeps_archive_closure_and_ledger(required_secret_installation, tmp_path, monkeypatch, action, phase, failure):
    import base64
    import hashlib
    import sys
    from types import SimpleNamespace
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.engine import NativeEngine
    from homensai_install.migration_receipts import BackupReceiptStore
    from shared import archive
    fixture, value, document, provider_calls, expected = required_secret_installation
    if action == 'reinstall': fixture.engine.execute(fixture.reviewed_plan('uninstall', value))
    else: (fixture.root / 'releases/app-release-1/adapter.py').write_text('damaged source must never execute')
    trust = tmp_path / 'public-trust.json'
    trust.write_text(json.dumps({'schema_version': 1, 'keys': {'synthetic': base64.b64encode(fixture.public_key).decode()}}))
    def client():
        return InstallerAPI.from_bootstrap(tmp_path / 'public-state', trust_config=trust, allow_development=True,
            development_runtime=sys.executable, product_adapter_descriptor='installer/adapter.json',
            secret_provider=fixture.engine.secret_provider)
    api = client()
    plan = api.plan(action, value['manifest'], fixture.target, options={**value['options'],
        'secret_refs': {'PRODUCT_TOKEN': 'forbidden.incoming'}})
    token = _digest([plan['plan_id'], plan['plan_digest']])[:32]
    native = fixture.root / '.hinstall/operations' / (token + '.json')
    ledger = fixture.root / '.hinstall/product-adapter/release-1/adapter-invocations.sqlite3'
    ledger_identity = (ledger.stat().st_dev, ledger.stat().st_ino)
    archive_path = fixture.base / '.installed-recovery' / (token + '-before-' + action + '.hsac')
    closure = fixture.root / '.hinstall/code-maintenance' / token / 'candidate'
    actual_verify, actual_hook, actual_start = BackupReceiptStore._verified, ExecutableProductAdapter._execute, NativeEngine._start
    fault_active, fired, hooks, starts = [True], [], [], []
    def verified(store, *args, **kwargs):
        if store.directory.name == 'code-maintenance-receipts' and fault_active[0]:
            state = json.loads(native.read_text())
            published = state.get('code_maintenance_renames', {}).get('publish_complete') is True
            if published == (phase == 'after_publish'):
                fired.append(True)
                if failure == 'invalid_archive':
                    original_password = store.password_provider
                    store.password_provider = lambda: 'synthetic wrong archive password'
                    try: return actual_verify(store, *args, **kwargs)
                    finally: store.password_provider = original_password
                with monkeypatch.context() as constrained:
                    constrained.setattr(archive.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
                    return actual_verify(store, *args, **kwargs)
        return actual_verify(store, *args, **kwargs)
    def execute(adapter, hook, request, request_digest):
        hooks.append((hook, request['idempotency_key']))
        return actual_hook(adapter, hook, request, request_digest)
    def start(engine, ctx):
        starts.append(ctx['operation_token'])
        return actual_start(engine, ctx)
    monkeypatch.setattr(BackupReceiptStore, '_verified', verified)
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', execute)
    monkeypatch.setattr(NativeEngine, '_start', start)
    operation = api.submit(plan['plan_id'], plan['plan_digest'], [plan['review_consent_ref'], *plan['required_consents']],
        idempotency_key='space-failure', background=False)
    expected_code = 'DISK_SPACE_LOW' if failure == 'space' else 'INTEGRITY_FAILED'
    assert operation['status'] in {'FAILED', 'RECOVERY_REQUIRED'} and operation['error']['code'] == expected_code, operation.get('error')
    assert fired and not starts
    assert document.read_text() == 'preserved credential-bound user data'
    native_state = json.loads(native.read_text())
    assert ('repair' in native_state['completed']) == (phase == 'after_publish')
    archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    closure_identity = (closure.stat().st_dev, closure.stat().st_ino)
    quiesces = [(hook, key) for hook, key in hooks if hook == 'quiesce']
    assert len(quiesces) == 1
    fault_active[0] = False
    resumed = client().resume(operation['operation_id'], background=False)
    assert resumed['status'] == 'SUCCEEDED', resumed.get('error')
    assert resumed['operation_id'] == operation['operation_id']
    assert resumed['result']['before_maintenance_archive']['path'] == str(archive_path)
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == archive_sha
    assert (closure.stat().st_dev, closure.stat().st_ino) == closure_identity
    assert (ledger.stat().st_dev, ledger.stat().st_ino) == ledger_identity
    assert [(hook, key) for hook, key in hooks if hook == 'quiesce'] == quiesces
    assert document.read_text() == 'preserved credential-bound user data' and starts == [token]
