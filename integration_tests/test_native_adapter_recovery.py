"""Signed ProductAdapter recovery: реальная SQLite, процессы и encrypted roots."""
import json
import base64
from pathlib import Path
import sqlite3
import subprocess
import sys
import stat

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.engine import EngineError, NativeEngine
from homensai_install.errors import InstallerError
from test_native_migration import migration, prepare, fresh_plan


RECOVERY_HOOK = r'''
elif hook in ('restore','post_restore'):
 assert data['release']==profile['release'] and len(data['before_recovery_receipt_id'])==64
 assert len(data['source_archive_sha256'])==64 and len(data['installation_id'])==32
 if (home/'private').exists():
  config=pathlib.Path(os.environ['HOMENSAI_CONFIG_HOME'])
  for directory in (home/'private',config/'private'):
   assert directory.stat().st_uid==os.getuid() and directory.stat().st_mode&0o777==0o700
  for path in (home/'private/adapter.sqlite',config/'private/credential'):
   assert path.stat().st_uid==os.getuid() and path.stat().st_mode&0o777==0o600
  with sqlite3.connect((home/'private/adapter.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as db:
   assert db.execute('SELECT value FROM private_state').fetchall()==[('snapshot private record',)]
  assert (config/'private/credential').read_text()=='synthetic snapshot credential'
 with sqlite3.connect(database) as db:
  old=db.execute('SELECT request_digest,result FROM adapter_effects WHERE call_id=?',(request['idempotency_key'],)).fetchone()
  if old:
   assert old[0]==request['request_digest'];result=json.loads(old[1])
  else:
   result={'applied':True,'receipt_id':'effect_'+request['idempotency_key'][-32:],'state_version':current}
   db.execute('INSERT INTO adapter_effects VALUES(?,?,?,?)',(request['idempotency_key'],request['request_digest'],hook,json.dumps(result)))
'''


@pytest.fixture
def recovery_adapter(migration, monkeypatch, request):
    import test_native_migration as fixture_module
    monkeypatch.setattr(fixture_module, 'SCRIPT', fixture_module.SCRIPT.replace("elif hook=='quiesce':", RECOVERY_HOOK + "\nelif hook=='quiesce':"))
    first = migration.release('release-1', 'schema_1', config=True)
    plan, first = fresh_plan(migration, first)
    migration.engine.execute(plan); migration.current = first
    document = migration.root / 'data/document.txt'
    document.write_text('snapshot data')
    if getattr(request, 'param', False):
        for folder in (migration.root / 'data/private', migration.root / 'config/private'):
            folder.mkdir(mode=0o700)
        with sqlite3.connect(migration.root / 'data/private/adapter.sqlite') as db:
            db.execute('CREATE TABLE private_state(value TEXT)')
            db.execute('INSERT INTO private_state VALUES (?)', ('snapshot private record',))
        (migration.root / 'data/private/adapter.sqlite').chmod(0o600)
        credential = migration.root / 'config/private/credential'
        credential.write_text('synthetic snapshot credential'); credential.chmod(0o600)
    snapshot = migration.engine.execute(migration.reviewed_plan('backup', first))['archive']['path']
    second = migration.release('release-2', 'schema_2', config=True)
    review = prepare(migration, 'update', second)
    second = {**second, 'options': {**second['options'], 'migration_review_id': review['migration_review_id']}}
    migration.engine.execute(migration.reviewed_plan('update', second)); migration.current = second
    document.write_text('latest data')
    if getattr(request, 'param', False):
        with sqlite3.connect(migration.root / 'data/private/adapter.sqlite') as db:
            db.execute('UPDATE private_state SET value=?', ('latest private record',))
        (migration.root / 'config/private/credential').write_text('synthetic latest credential')
        for folder in (migration.root / 'data/private', migration.root / 'config/private'):
            folder.chmod(0o755)
    return migration, first, second, snapshot, document


def recovery_plan(fixture, second, snapshot, action):
    return fixture.reviewed_plan(action, second, archive_path=snapshot, acknowledge_data_loss=True)


def effects(fixture, hook):
    with sqlite3.connect(fixture.root / 'data/schema.sqlite') as db:
        return db.execute('SELECT count(*) FROM adapter_effects WHERE hook=?', (hook,)).fetchone()[0]


@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_public_adapter_recovery_restores_schema_release_data_and_calls_target_hooks(recovery_adapter, action):
    fixture, first, second, snapshot, document = recovery_adapter
    plan = recovery_plan(fixture, second, snapshot, action)
    assert plan['engine_plan']['product_adapter_binding']['release'] == 'release-2'
    result = fixture.engine.execute(plan); fixture.current = first
    assert result['status'] == 'SUCCEEDED'
    assert result['health']['release'] == 'release-1'
    assert result['health']['product_adapter']['ready'] is True
    assert result['before_recovery']['from_release'] == 'release-2'
    assert result['external_actions_on_hold'] is True
    assert document.read_text() == 'snapshot data'
    assert effects(fixture, 'migration_apply') == 1
    assert effects(fixture, 'restore') == effects(fixture, 'post_restore') == 1
    assert fixture.engine.execute(plan) == result


@pytest.mark.parametrize('action', ['restore', 'rollback'])
@pytest.mark.parametrize('hook', ['restore', 'post_restore'])
def test_lost_ack_reconciles_original_recovery_hook_once(recovery_adapter, monkeypatch, action, hook):
    fixture, first, second, snapshot, document = recovery_adapter
    plan = recovery_plan(fixture, second, snapshot, action)
    actual = ExecutableProductAdapter._execute
    fired = []
    def lose_response(self, invoked_hook, *args, **kwargs):
        result = actual(self, invoked_hook, *args, **kwargs)
        if invoked_hook == hook and not fired:
            fired.append(True)
            raise InstallerError('ADAPTER_EXECUTION_FAILED')
        return result
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', lose_response)
    with pytest.raises(EngineError) as caught:
        fixture.engine.execute(plan)
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert effects(fixture, hook) == 1 and document.read_text() == 'snapshot data'
    with pytest.raises(EngineError):
        fixture.engine.execute(recovery_plan(fixture, first, snapshot, action))
    result = fixture.engine.resume(plan, {}); fixture.current = first
    assert result['health']['release'] == 'release-1'
    assert effects(fixture, hook) == 1
    assert effects(fixture, 'restore') == effects(fixture, 'post_restore') == 1


@pytest.mark.parametrize('fault', ['missing_ledger', 'unknown'])
def test_uncertain_recovery_hook_cannot_be_repeated_when_reconciliation_is_unavailable(recovery_adapter, monkeypatch, fault):
    fixture, first, second, snapshot, document = recovery_adapter
    plan = recovery_plan(fixture, second, snapshot, 'rollback')
    actual = ExecutableProductAdapter._execute
    def lose_response(self, hook, *args, **kwargs):
        result = actual(self, hook, *args, **kwargs)
        if hook == 'restore':
            raise InstallerError('ADAPTER_EXECUTION_FAILED')
        return result
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', lose_response)
    with pytest.raises(EngineError): fixture.engine.execute(plan)
    assert effects(fixture, 'restore') == 1
    if fault == 'missing_ledger':
        (fixture.root / '.hinstall/product-adapter/release-1/adapter-invocations.sqlite3').unlink()
    else:
        actual_reconcile = ExecutableProductAdapter.reconcile
        def unknown(self, key):
            if key.startswith('restore_'):
                return {'outcome': 'unknown'}
            return actual_reconcile(self, key)
        monkeypatch.setattr(ExecutableProductAdapter, 'reconcile', unknown)
    with pytest.raises(EngineError) as caught: fixture.engine.resume(plan, {})
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert effects(fixture, 'restore') == 1 and effects(fixture, 'post_restore') == 0
    assert document.read_text() == 'snapshot data'
    if fault == 'missing_ledger':
        assert not (fixture.root / '.hinstall/product-adapter/release-1/adapter-invocations.sqlite3').exists()


def test_failed_restored_readiness_preserves_new_writes_and_safety_snapshot(recovery_adapter, monkeypatch):
    fixture, first, second, snapshot, document = recovery_adapter
    plan = recovery_plan(fixture, second, snapshot, 'rollback')
    def failed_health(ctx):
        document.write_text('new writes in restored candidate')
        raise EngineError('HEALTH_CHECK_FAILED')
    monkeypatch.setattr(fixture.engine, '_health', failed_health)
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'HEALTH_CHECK_FAILED'
    assert document.read_text() == 'new writes in restored candidate'
    assert json.loads((fixture.root / 'services/active.json').read_text())['release'] == 'release-1'
    ctx = fixture.engine._context(first['manifest'], fixture.target, first['options'])
    assert not fixture.engine._records(ctx)
    state = [json.loads(p.read_text()) for p in (fixture.root / '.hinstall/operations').glob('*.json')
             if json.loads(p.read_text()).get('action') == 'rollback'][0]
    assert state['recovery_primary_error_code'] == 'HEALTH_CHECK_FAILED'
    assert state['product_adapter_recovery_required'] is True
    assert 'product_adapter_recovery_cleanup_pending' not in state
    from shared import archive_crypto
    safety = next((fixture.base / '.installed-recovery').glob('*-before-rollback.hsac'))
    restored = fixture.base / 'safety-oracle'; restored.mkdir(mode=0o700)
    archive_crypto.restore_archive(safety, restored, password_provider=lambda: 'synthetic-archive-passphrase',
                                   expected_product_id=first['manifest']['product_id'])
    assert (restored / 'roots/data/document.txt').read_text() == 'latest data'


def test_health_cleanup_unknown_preserves_both_codes_and_fences_retry(recovery_adapter, monkeypatch):
    fixture, first, second, snapshot, document = recovery_adapter
    plan = recovery_plan(fixture, second, snapshot, 'rollback')
    health_calls = []
    def failed_health(ctx):
        health_calls.append(True)
        raise EngineError('HEALTH_CHECK_FAILED')
    actual = ExecutableProductAdapter.invoke
    def cleanup_unknown(self, hook, request, **kwargs):
        if hook == 'quiesce' and request['idempotency_key'].startswith('cleanup_quiesce_'):
            raise InstallerError('ADAPTER_TIMEOUT')
        return actual(self, hook, request, **kwargs)
    monkeypatch.setattr(fixture.engine, '_health', failed_health)
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', cleanup_unknown)
    with pytest.raises(EngineError): fixture.engine.execute(plan)
    state = [json.loads(p.read_text()) for p in (fixture.root / '.hinstall/operations').glob('*.json')
             if json.loads(p.read_text()).get('action') == 'rollback'][0]
    assert state['recovery_primary_error_code'] == 'HEALTH_CHECK_FAILED'
    assert state['recovery_cleanup_error_code'] == 'RECOVERY_REQUIRED'
    assert state['product_adapter_unknown'] is True
    with pytest.raises(EngineError): fixture.engine.resume(plan, {})
    assert len(health_calls) == 1
    assert effects(fixture, 'restore') == effects(fixture, 'post_restore') == 1


@pytest.mark.parametrize('lost_ack', [False, True])
def test_original_public_factory_operation_restores_and_resumes_with_reviewed_consents(recovery_adapter, tmp_path, monkeypatch, lost_ack):
    from homensai_install.api.v1 import InstallerAPI
    fixture, first, second, snapshot, document = recovery_adapter
    trust = tmp_path / 'synthetic-trust.json'
    trust.write_text(json.dumps({'schema_version': 1, 'keys': {'synthetic': base64.b64encode(fixture.public_key).decode()}}))
    api = InstallerAPI.from_bootstrap(tmp_path / 'public-api-state', trust_config=trust,
        allow_development=True, development_runtime=sys.executable, product_adapter_descriptor='installer/adapter.json',
        secret_provider=fixture.engine.secret_provider)
    with monkeypatch.context() as frozen:
        frozen.setattr(ExecutableProductAdapter, 'invoke', lambda *a, **k: pytest.fail('plan must not run hooks'))
        plan = api.plan('rollback', second['manifest'], fixture.target,
            options={**second['options'], 'archive_path': snapshot, 'acknowledge_data_loss': True})
    fired = []
    if lost_ack:
        execute = ExecutableProductAdapter._execute
        def lose_response(self, hook, *args, **kwargs):
            result = execute(self, hook, *args, **kwargs)
            if hook == 'restore' and not fired:
                fired.append(True); raise InstallerError('ADAPTER_TIMEOUT')
            return result
        monkeypatch.setattr(ExecutableProductAdapter, '_execute', lose_response)
    operation = api.submit(plan['plan_id'], plan['plan_digest'],
        [plan['review_consent_ref'], *plan['required_consents']], idempotency_key='public-reviewed-recovery', background=False)
    if lost_ack:
        assert operation['status'] == 'RECOVERY_REQUIRED'
        original_id = operation['operation_id']
        operation = api.resume(original_id, background=False)
        assert operation['operation_id'] == original_id
    fixture.current = first
    assert operation['status'] == 'SUCCEEDED'
    assert operation['result']['health']['release'] == 'release-1'
    assert document.read_text() == 'snapshot data'
    assert effects(fixture, 'restore') == effects(fixture, 'post_restore') == 1


RECOVERY_KILL_CONTROLLER = r'''
import base64,json,os,signal,sys
from pathlib import Path
from homensai_install.engine import NativeEngine
from homensai_install.adapters import ExecutableProductAdapter,_Ledger
from homensai_install.trust import SignedPayloadVerifier
from shared.adapters_fs import FilesystemPorts
value=json.loads(Path(sys.argv[1]).read_text());phase=value['phase']
actual_begin=_Ledger.begin
def begin(self,key,request_digest,operation_id,hook,**kwargs):
 if hook=='restore' and phase=='before_ledger': os.kill(os.getpid(),signal.SIGKILL)
 return actual_begin(self,key,request_digest,operation_id,hook,**kwargs)
_Ledger.begin=begin
actual_execute=ExecutableProductAdapter._execute
def execute(self,hook,*args,**kwargs):
 result=actual_execute(self,hook,*args,**kwargs)
 if hook=='restore' and phase=='after_effect': os.kill(os.getpid(),signal.SIGKILL)
 return result
ExecutableProductAdapter._execute=execute
actual_reconcile=FilesystemPorts._reconcile
def reconcile(source,destination,preserved):
 actual_reconcile(source,destination,preserved)
 if phase=='partial_replace' and Path(source).parent.name=='roots' and Path(source).parent.parent.name.endswith('-restore-candidate'):
  os.kill(os.getpid(),signal.SIGKILL)
FilesystemPorts._reconcile=reconcile
def emit(event):
 if event['status']=='SUCCEEDED' and ((phase=='after_safety' and event['phase']=='before_rollback') or (phase=='after_pointer' and event['phase']=='activate')):
  os.kill(os.getpid(),signal.SIGKILL)
engine=NativeEngine(SignedPayloadVerifier({'synthetic':base64.b64decode(value['public_key'])},allow_test_keys=True),
 lambda ref:'synthetic-archive-passphrase',development_runtime=sys.executable,product_adapter_descriptor='installer/adapter.json')
engine.execute(value['plan'],emit)
raise AssertionError('expected owned controller SIGKILL')
'''


@pytest.mark.parametrize('phase', ['after_safety', 'before_ledger', 'after_effect', 'after_pointer', 'partial_replace'])
def test_actual_controller_sigkill_keeps_original_recovery_proofs_and_fences_uncertainty(recovery_adapter, phase):
    fixture, first, second, snapshot, document = recovery_adapter
    plan = recovery_plan(fixture, second, snapshot, 'rollback')
    source = fixture.base / 'recovery-controller.json'
    source.write_text(json.dumps({'plan': plan, 'phase': phase, 'public_key': base64.b64encode(fixture.public_key).decode()}))
    result = subprocess.run([sys.executable, '-c', RECOVERY_KILL_CONTROLLER, str(source)],
        cwd=Path(__file__).parents[1], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
    assert result.returncode == -9, result.stderr.decode(errors='replace')
    if phase in {'before_ledger', 'partial_replace'}:
        before = document.read_bytes()
        with pytest.raises(EngineError) as caught: fixture.engine.resume(plan, {})
        assert caught.value.code == 'RECOVERY_REQUIRED'
        assert document.read_bytes() == before and effects(fixture, 'restore') == 0
    else:
        result = fixture.engine.resume(plan, {}); fixture.current = first
        assert result['health']['release'] == 'release-1'
        assert document.read_text() == 'snapshot data'
        assert effects(fixture, 'restore') == effects(fixture, 'post_restore') == 1


def test_ciphertext_changed_after_review_is_stale_before_source_quiesce(recovery_adapter, monkeypatch):
    fixture, first, second, snapshot, document = recovery_adapter
    plan = recovery_plan(fixture, second, snapshot, 'rollback')
    processes = (fixture.root / '.hinstall/processes.json').read_bytes()
    path = Path(snapshot); raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', lambda *a, **k: pytest.fail('stale source cannot invoke hooks'))
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'PLAN_STALE'
    assert (fixture.root / '.hinstall/processes.json').read_bytes() == processes
    assert document.read_text() == 'latest data'


def test_archive_target_missing_post_restore_is_refused_before_live_source_stop(migration, monkeypatch):
    first = migration.release('release-1', 'schema_1', config=True, omit_hooks=('post_restore',))
    plan, first = fresh_plan(migration, first)
    migration.engine.execute(plan); migration.current = first
    snapshot = migration.engine.execute(migration.reviewed_plan('backup', first))['archive']['path']
    second = migration.release('release-2', 'schema_2', config=True)
    review = prepare(migration, 'update', second)
    second = {**second, 'options': {**second['options'], 'migration_review_id': review['migration_review_id']}}
    migration.engine.execute(migration.reviewed_plan('update', second)); migration.current = second
    plan = recovery_plan(migration, second, snapshot, 'rollback')
    processes = (migration.root / '.hinstall/processes.json').read_bytes()
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', lambda *a, **k: pytest.fail('unsupported target cannot invoke hooks'))
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert (migration.root / '.hinstall/processes.json').read_bytes() == processes


def test_public_factory_preserves_primary_and_cleanup_codes_when_cleanup_is_unknown(recovery_adapter, tmp_path, monkeypatch):
    from homensai_install.api.v1 import InstallerAPI
    fixture, first, second, snapshot, document = recovery_adapter
    trust = tmp_path / 'synthetic-trust.json'
    trust.write_text(json.dumps({'schema_version': 1, 'keys': {'synthetic': base64.b64encode(fixture.public_key).decode()}}))
    api = InstallerAPI.from_bootstrap(tmp_path / 'public-api-state', trust_config=trust,
        allow_development=True, development_runtime=sys.executable, product_adapter_descriptor='installer/adapter.json',
        secret_provider=fixture.engine.secret_provider)
    plan = api.plan('rollback', second['manifest'], fixture.target,
        options={**second['options'], 'archive_path': snapshot, 'acknowledge_data_loss': True})
    def failed_health(self, ctx): raise EngineError('HEALTH_CHECK_FAILED')
    actual = ExecutableProductAdapter.invoke
    def cleanup_unknown(self, hook, request, **kwargs):
        if hook == 'quiesce' and request['idempotency_key'].startswith('cleanup_quiesce_'):
            raise InstallerError('ADAPTER_TIMEOUT')
        return actual(self, hook, request, **kwargs)
    monkeypatch.setattr(NativeEngine, '_health', failed_health)
    monkeypatch.setattr(ExecutableProductAdapter, 'invoke', cleanup_unknown)
    operation = api.submit(plan['plan_id'], plan['plan_digest'],
        [plan['review_consent_ref'], *plan['required_consents']], idempotency_key='public-cleanup-unknown', background=False)
    assert operation['status'] == 'RECOVERY_REQUIRED'
    assert operation['error']['code'] == 'RECOVERY_REQUIRED'
    assert operation['error']['details'] == {'primary_error_code': 'HEALTH_CHECK_FAILED', 'cleanup_error_code': 'RECOVERY_REQUIRED'}
    resumed = api.resume(operation['operation_id'], background=False)
    assert resumed['status'] == 'RECOVERY_REQUIRED'
    assert effects(fixture, 'restore') == effects(fixture, 'post_restore') == 1


def test_wrong_schema_in_authenticated_snapshot_is_refused_before_live_source_stop(recovery_adapter):
    from shared import archive_crypto
    from shared.datahome import resolve_datahome
    fixture, first, second, snapshot, document = recovery_adapter
    candidate = fixture.base / 'wrong-schema-source'; candidate.mkdir(mode=0o700)
    archive_crypto.restore_archive(snapshot, candidate, password_provider=lambda: 'synthetic-archive-passphrase',
                                   expected_product_id=first['manifest']['product_id'])
    with sqlite3.connect(candidate / 'roots/data/schema.sqlite') as db:
        db.execute('PRAGMA user_version=3')
    wrong = fixture.base / 'authenticated-wrong-schema.hsac'
    archive_crypto.build_encrypted_archive(resolve_datahome(first['manifest'], candidate / 'roots'), wrong,
                                           password_provider=lambda: 'synthetic-archive-passphrase')
    plan = recovery_plan(fixture, second, str(wrong), 'rollback')
    processes = (fixture.root / '.hinstall/processes.json').read_bytes()
    with pytest.raises(EngineError) as caught: fixture.engine.execute(plan)
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert (fixture.root / '.hinstall/processes.json').read_bytes() == processes
    assert document.read_text() == 'latest data'
    assert json.loads((fixture.root / 'services/active.json').read_text())['release'] == 'release-2'
    assert effects(fixture, 'migration_apply') == 2 and effects(fixture, 'restore') == 0


@pytest.mark.parametrize('recovery_adapter', [True], indirect=True)
@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_recovery_restores_private_database_credential_and_directory_permissions(recovery_adapter, action):
    fixture, first, second, snapshot, document = recovery_adapter
    result = fixture.engine.execute(recovery_plan(fixture, second, snapshot, action)); fixture.current = first
    assert result['status'] == 'SUCCEEDED'
    for folder in (fixture.root / 'data/private', fixture.root / 'config/private'):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    database = fixture.root / 'data/private/adapter.sqlite'
    credential = fixture.root / 'config/private/credential'
    assert stat.S_IMODE(database.stat().st_mode) == stat.S_IMODE(credential.stat().st_mode) == 0o600
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT value FROM private_state').fetchall() == [('snapshot private record',)]
    assert credential.read_text() == 'synthetic snapshot credential'
    assert effects(fixture, 'restore') == effects(fixture, 'post_restore') == 1
