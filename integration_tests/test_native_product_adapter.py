"""Narrow NativeEngine → signed ProductAdapter maintenance integration.

Real synthetic Linux processes/HTTP/crypto. No OS installer/platform acceptance.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from homensai_install.adapters import HOOKS
from homensai_install.engine import NativeEngine, EngineError
from homensai_install.errors import InstallerError
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest
from shared import archive_crypto
from shared.datahome import DataHome
from shared.update_plan import operation_lock


SCRIPT = r'''
import json,os,sys,urllib.request
from pathlib import Path
request=json.load(sys.stdin);hook=request['hook'];data=request['data']
home=Path(os.environ['HOMENSAI_DATA_HOME']);statefile=home/'adapter-effects.json'
state=json.loads(statefile.read_text()) if statefile.exists() else {'quiesce':0,'ready':0,'applied':{}}
modefile=home/'adapter-mode.txt';mode=modefile.read_text() if modefile.exists() else ''
def save():statefile.write_text(json.dumps(state))
if hook=='quiesce' and mode=='fail_before_quiesce':sys.exit(18)
if hook=='readiness':
 state['ready']+=1;save()
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 with opener.open('http://127.0.0.1:PORT/healthz',timeout=1) as reply:observed=json.load(reply)
 identity={key:observed[key] for key in ('product_id','release','installation_id')}
 identity['probe_id']=data['probe_id']
 if mode=='wrong_release':identity['release']='other-version'
 if mode=='wrong_instance':identity['installation_id']='other-instance'
 if mode=='stale_probe':identity['probe_id']='older-probe'
 result={'ready':mode!='not_ready','checks':[{'id':'live-observation','status':'failed' if mode=='not_ready' else 'ok'}], 'observed_identity':identity}
 if mode=='missing_identity':result.pop('observed_identity')
elif hook=='quiesce':
 state['quiesce']+=1
 result={'quiesced':True,'active_writers':0}
 state['applied'][request['idempotency_key']]={'result':result,'request_digest':request['request_digest']};save()
 if mode in ('fail_after_quiesce','unknown_quiesce'):sys.exit(19)
elif hook=='operation_status':
 item=state['applied'].get(request['idempotency_key'])
 outcome='unknown' if mode=='unknown_quiesce' else 'applied' if item else 'not_applied'
 result={'outcome':outcome,'request_digest':request['request_digest']}
 if outcome=='applied':result['result']=item['result']
else:raise AssertionError('unexpected hook')
response={key:request[key] for key in ('protocol_version','product_id','request_id','request_digest','hook')}
response.update(status='ok',result=result);print(json.dumps(response))
'''


@pytest.fixture
def installed(tmp_path):
    if not sys.platform.startswith('linux'):
        pytest.skip('Linux user-session executable adapter backend')
    spec=importlib.util.spec_from_file_location('adapter_native_fixture',Path(__file__).with_name('test_native_engine.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    fixture=module.NativeLifecycleTests(methodName='runTest');fixture.setUp()
    try:
        bundle=fixture.bundle('v1');payload=Path(bundle['options']['payload_dir'])
        shutil.copy2(Path('/usr/bin/python3').resolve(),payload/'runtime/python');(payload/'runtime/python').chmod(0o700)
        (payload/'adapter.py').write_text(SCRIPT.replace('PORT',str(fixture.port)))
        (payload/'installer').mkdir()
        descriptor={'schema_version':1,'protocol_version':'1.0','product_id':bundle['manifest']['product_id'],
            'adapter_id':'native-reference','adapter_version':'0.1.0','command':['runtime/python','-I','-B','adapter.py'],
            'environment_allowlist':['HOMENSAI_DATA_HOME'],
            'capabilities':{name:{'mutating':HOOKS[name],'idempotency':'application' if HOOKS[name] else 'read_only'}
                            for name in ('describe','readiness','quiesce','operation_status')}}
        (payload/'installer/adapter.json').write_bytes(canonical_bytes(descriptor))
        alternate={**descriptor,'adapter_id':'alternate-signed-adapter'}
        (payload/'installer/adapter-other.json').write_bytes(canonical_bytes(alternate))
        private=Ed25519PrivateKey.generate();public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        def resign():
            inventory={'schema_version':1,'product_id':bundle['manifest']['product_id'],'release':'v1',
                'target':{key:fixture.target[key] for key in ('os','arch','mode')},
                'files':{p.relative_to(payload).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in payload.rglob('*') if p.is_file()},
                'development':True,'manifest_sha256':hash_manifest(bundle['manifest']),'runtime_executable':'runtime/python'}
            signature={'algorithm':'ed25519','key_id':'synthetic','signature':base64.b64encode(private.sign(canonical_bytes(inventory))).decode()}
            Path(bundle['options']['inventory_file']).write_bytes(canonical_bytes(inventory))
            Path(bundle['options']['signature_file']).write_bytes(canonical_bytes(signature))
        resign();verifier=SignedPayloadVerifier({'synthetic':public},allow_test_keys=True)
        # The legacy install remains explicitly separate. CP021 supports maintenance
        # only; no new adapter migration contract is asserted by this fixture.
        fixture.engine=NativeEngine(verifier,lambda ref:'synthetic-archive-passphrase',development_runtime=sys.executable)
        fixture.run_action('install',bundle);fixture.current=bundle
        fixture.engine=NativeEngine(verifier,lambda ref:'synthetic-archive-passphrase',development_runtime=sys.executable,
                                   product_adapter_descriptor='installer/adapter.json')
        yield fixture,bundle,resign
    finally:
        # Cleanup uses the legacy trusted engine so an injected adapter UNKNOWN
        # does not prevent this fixture from stopping only its own test process.
        if fixture.current:
            cleanup=NativeEngine(fixture.engine.verifier,development_runtime=sys.executable)
            ctx=cleanup._context(fixture.current['manifest'],fixture.target,fixture.current['options'])
            cleanup._stop(ctx)
            fixture.current=None
        fixture.tearDown()


def state(fixture):
    path=fixture.root/'data/adapter-effects.json'
    return json.loads(path.read_text()) if path.exists() else {'ready':0,'quiesce':0}


def test_real_start_readiness_is_fresh_and_completed_replay_has_no_effect(installed):
    fixture,bundle,_=installed
    plan=fixture.reviewed_plan('start',bundle)
    assert 'product_adapter_binding' in plan['engine_plan']
    assert state(fixture)['ready']==0
    first=fixture.engine.execute(plan)
    assert first['health']['product_adapter']['ready']
    assert state(fixture)['ready']==1
    assert fixture.engine.execute(plan)==first
    assert state(fixture)['ready']==1
    fixture.engine.execute(fixture.reviewed_plan('start',bundle))
    assert state(fixture)['ready']==2


@pytest.mark.parametrize('mode',['wrong_release','wrong_instance','stale_probe','missing_identity','not_ready'])
def test_adapter_readiness_cannot_accept_wrong_version_instance_or_stale_observation(installed,mode):
    fixture,bundle,_=installed
    (fixture.root/'data/adapter-mode.txt').write_text(mode)
    plan=fixture.reviewed_plan('start',bundle)
    with pytest.raises(EngineError) as caught:fixture.engine.execute(plan)
    assert caught.value.code=='HEALTH_CHECK_FAILED'
    assert state(fixture)['ready']==1 and state(fixture)['quiesce']==0


def test_quiesce_and_real_encrypted_backup_preserve_data_and_restart(installed):
    fixture,bundle,_=installed
    user=fixture.root/'data/user.txt';user.write_text('synthetic user data')
    result=fixture.engine.execute(fixture.reviewed_plan('backup',bundle))
    assert result['status']=='SUCCEEDED'
    assert archive_crypto.is_encrypted(result['archive']['path'])
    assert result['archive']['sha256']==hashlib.sha256(Path(result['archive']['path']).read_bytes()).hexdigest()
    assert result['archive']['includes_recovery_payload'] and user.read_text()=='synthetic user data'
    receipt = result['archive']['backup_receipt']
    assert receipt['migration_eligible'] is False and receipt['verification_scope'] == 'recovery_only'
    assert receipt['from_schema_version'] is None and receipt['plan_binding_sha256'] is None
    assert receipt['archive_sha256'] == result['archive']['sha256']
    assert receipt['installation_id'] == fixture.engine._owner(fixture.engine._context(
        bundle['manifest'], fixture.target, bundle['options']), required=True)['installation_id']
    assert state(fixture)['quiesce']==1 and state(fixture)['ready']==1


@pytest.mark.parametrize('change', ['installation', 'release', 'payload', 'inventory'])
def test_receipt_rejects_decryptable_same_product_archive_with_foreign_or_tampered_recovery(installed, change):
    fixture, bundle, _ = installed
    result = fixture.engine.execute(fixture.reviewed_plan('backup', bundle))['archive']
    ctx = fixture.engine._context(bundle['manifest'], fixture.target, bundle['options'])
    store = fixture.engine._backup_receipt_store(ctx, lambda: 'synthetic-archive-passphrase')
    keys = ('product_id', 'installation_id', 'operation_id', 'from_release',
            'from_manifest_sha256', 'from_inventory_sha256', 'from_schema_version', 'plan_binding_sha256')
    expected = {key: result['backup_receipt'][key] for key in keys}
    expected.update(from_schema_version='v1', plan_binding_sha256='e' * 64)
    valid = store.issue(result['path'], expected, consistency_scope='owned_components_stopped_and_adapter_quiesced')
    assert store.verify(valid['receipt_id'], expected_binding=expected)['migration_eligible']
    candidate = fixture.base / ('receipt-foreign-' + change); candidate.mkdir(mode=0o700)
    archive_crypto.restore_archive(result['path'], candidate,
        password_provider=lambda: 'synthetic-archive-passphrase', expected_product_id=bundle['manifest']['product_id'])
    recovery = candidate / 'roots/runtime/recovery/v1'
    if change == 'installation':
        marker = recovery / 'installation.json'; value = json.loads(marker.read_text())
        value['installation_id'] = '0' * 32; marker.write_text(json.dumps(value))
    elif change == 'release':
        marker = recovery / 'release.json'; value = json.loads(marker.read_text())
        value['release'] = 'other-release'; marker.write_text(json.dumps(value))
    elif change == 'payload': (recovery / 'payload/adapter.py').write_text('modified signed payload')
    else:
        marker = recovery / 'release.inventory.json'; value = json.loads(marker.read_text())
        value['files']['adapter.py'] = '0' * 64; marker.write_text(json.dumps(value))
    foreign = fixture.base / ('foreign-' + change + '.hsac')
    archived_home = DataHome(ctx['home'].product_id, candidate / 'app', candidate / 'releases',
        {key: candidate / 'roots' / key for key in ctx['home'].roots})
    archived_home.app.mkdir(); archived_home.releases.mkdir()
    archive_crypto.build_encrypted_archive(archived_home, foreign,
        password_provider=lambda: 'synthetic-archive-passphrase')
    with pytest.raises(InstallerError) as caught:
        store.issue(foreign, expected, consistency_scope='owned_components_stopped_and_adapter_quiesced')
    assert caught.value.code == ('INTEGRITY_FAILED' if change in {'payload', 'inventory'} else 'RECOVERY_REQUIRED')


def test_verified_historical_recovery_gets_additive_identity_marker_and_foreign_marker_is_preserved(installed):
    fixture, bundle, _ = installed
    ctx = fixture.engine._context(bundle['manifest'], fixture.target, bundle['options'])
    with operation_lock(fixture.base / 'recovery-test.lock'):
        destination = fixture.engine._recovery_copy(ctx, 'v1')
        marker = destination / 'installation.json'; original = json.loads(marker.read_text()); marker.unlink()
        assert fixture.engine._recovery_copy(ctx, 'v1') == destination
        assert json.loads(marker.read_text()) == original
        foreign = {**original, 'installation_id': '0' * 32}; marker.write_text(json.dumps(foreign))
        with pytest.raises(EngineError): fixture.engine._recovery_copy(ctx, 'v1')
        assert json.loads(marker.read_text()) == foreign


def test_backup_receipt_requires_durable_quiescence_before_archive_creation(installed):
    fixture, bundle, _ = installed
    ctx = fixture.engine._context(bundle['manifest'], fixture.target, bundle['options'])
    path = fixture.base / 'must-not-exist.hsac'
    with pytest.raises(EngineError) as caught:
        fixture.engine._backup(ctx, path, lambda: 'synthetic-archive-passphrase')
    assert caught.value.code == 'RECOVERY_REQUIRED' and not path.exists()


def test_unknown_quiesce_blocks_new_plans_and_explicit_resume_reconciles_without_repeating(installed):
    fixture,bundle,_=installed;mode=fixture.root/'data/adapter-mode.txt';mode.write_text('fail_after_quiesce')
    plan=fixture.reviewed_plan('stop',bundle)
    with pytest.raises(EngineError) as caught:fixture.engine.execute(plan)
    assert caught.value.code=='RECOVERY_REQUIRED' and state(fixture)['quiesce']==1
    with pytest.raises(EngineError):fixture.engine.execute(fixture.reviewed_plan('start',bundle))
    assert state(fixture)['ready']==0
    mode.write_text('')
    result=fixture.engine.resume(plan,{})
    assert result['stopped'] and state(fixture)['quiesce']==1


def test_unknown_reconciliation_never_replays_or_rolls_back(installed):
    fixture,bundle,_=installed;(fixture.root/'data/adapter-mode.txt').write_text('unknown_quiesce')
    plan=fixture.reviewed_plan('stop',bundle)
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    for _ in range(2):
        with pytest.raises(EngineError) as caught:fixture.engine.resume(plan,{})
        assert caught.value.code=='RECOVERY_REQUIRED'
    assert state(fixture)['quiesce']==1


def test_direct_unreviewed_opt_in_execution_fails_before_hooks(installed):
    fixture,bundle,_=installed
    plan=fixture.operation_plan('start',bundle)
    with pytest.raises(EngineError) as caught:fixture.engine.execute(plan)
    assert caught.value.code=='PLAN_STALE' and state(fixture)['ready']==0


@pytest.mark.parametrize('action',['restore','rollback','repair','reinstall','uninstall'])
def test_unimplemented_adapter_lifecycle_rejected_before_effects(installed,action):
    fixture,bundle,_=installed
    assert fixture.engine.capabilities(fixture.target)['actions'][action]['supported'] is False
    with pytest.raises(EngineError) as caught:fixture.reviewed_plan(action,bundle)
    assert caught.value.code=='CAPABILITY_UNSUPPORTED'
    with pytest.raises(EngineError) as caught:fixture.engine.execute(fixture.operation_plan(action,bundle))
    assert caught.value.code=='CAPABILITY_UNSUPPORTED'
    assert state(fixture)['ready']==0 and state(fixture)['quiesce']==0


def test_update_requires_signed_migration_hooks_and_review_before_effects(installed):
    fixture,bundle,_=installed
    assert fixture.engine.capabilities(fixture.target)['actions']['update']['supported'] is True
    with pytest.raises(EngineError) as caught:fixture.reviewed_plan('update',bundle)
    assert caught.value.code=='CAPABILITY_UNSUPPORTED'  # Maintenance-only descriptor.
    with pytest.raises(EngineError) as caught:fixture.engine.execute(fixture.operation_plan('update',bundle))
    assert caught.value.code=='PLAN_STALE'
    assert state(fixture)['ready']==0 and state(fixture)['quiesce']==0


def test_signed_descriptor_registration_substitution_invalidates_review_before_effects(installed):
    fixture,bundle,_=installed
    plan=fixture.reviewed_plan('start',bundle)
    fixture.engine=NativeEngine(fixture.engine.verifier,development_runtime=sys.executable,
                               product_adapter_descriptor='installer/adapter-other.json')
    with pytest.raises(EngineError) as caught:fixture.engine.execute(plan)
    assert caught.value.code=='PLAN_STALE' and state(fixture)['ready']==0


def test_not_applied_reconciliation_allows_only_explicit_resume(installed):
    fixture,bundle,_=installed;mode=fixture.root/'data/adapter-mode.txt';mode.write_text('fail_before_quiesce')
    plan=fixture.reviewed_plan('stop',bundle)
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    assert state(fixture)['quiesce']==0
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    assert state(fixture)['quiesce']==0
    mode.write_text('')
    assert fixture.engine.resume(plan,{})['stopped']
    assert state(fixture)['quiesce']==1


def test_new_legacy_controller_cannot_bypass_known_adapter_unknown_journal(installed):
    fixture,bundle,_=installed;(fixture.root/'data/adapter-mode.txt').write_text('unknown_quiesce')
    plan=fixture.reviewed_plan('stop',bundle)
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    legacy=NativeEngine(fixture.engine.verifier,development_runtime=sys.executable)
    with pytest.raises(EngineError) as caught:legacy.execute(fixture.operation_plan('start',bundle))
    assert caught.value.code=='RECOVERY_REQUIRED'
    assert state(fixture)['ready']==0 and state(fixture)['quiesce']==1


def test_backup_unknown_quiesce_reconciles_then_builds_verified_archive(installed):
    fixture,bundle,_=installed;mode=fixture.root/'data/adapter-mode.txt';mode.write_text('fail_after_quiesce')
    user=fixture.root/'data/user.txt';user.write_text('keep backup after reconciliation')
    plan=fixture.reviewed_plan('backup',bundle)
    with pytest.raises(EngineError) as caught:fixture.engine.execute(plan)
    assert caught.value.code=='RECOVERY_REQUIRED' and state(fixture)['quiesce']==1
    assert not list((fixture.root.parent/'.installed-recovery').glob('*.hsac'))
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    mode.write_text('')
    result=fixture.engine.resume(plan,{})
    assert result['status']=='SUCCEEDED' and state(fixture)['quiesce']==1
    assert archive_crypto.is_encrypted(result['archive']['path'])
    archive_crypto.verify_archive(result['archive']['path'],password_provider=lambda:'synthetic-archive-passphrase',
                                  expected_product_id=bundle['manifest']['product_id'])
    assert user.read_text()=='keep backup after reconciliation' and state(fixture)['ready']==1


def test_backup_unknown_reconciliation_still_blocks_archive_and_replay(installed):
    fixture,bundle,_=installed;(fixture.root/'data/adapter-mode.txt').write_text('unknown_quiesce')
    plan=fixture.reviewed_plan('backup',bundle)
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    with pytest.raises(EngineError) as caught:fixture.engine.resume(plan,{})
    assert caught.value.code=='RECOVERY_REQUIRED' and state(fixture)['quiesce']==1
    assert not list((fixture.root.parent/'.installed-recovery').glob('*.hsac'))


def test_resolved_quiesce_does_not_authorize_replay_of_later_backup_failure(installed,monkeypatch):
    fixture,bundle,_=installed;mode=fixture.root/'data/adapter-mode.txt';mode.write_text('fail_after_quiesce')
    plan=fixture.reviewed_plan('backup',bundle)
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    mode.write_text('')
    def fail_archive(*args,**kwargs):raise EngineError('STATE_STORAGE_UNAVAILABLE')
    monkeypatch.setattr(fixture.engine,'_backup',fail_archive)
    with pytest.raises(EngineError):fixture.engine.resume(plan,{})
    assert state(fixture)['quiesce']==1
    # The quiesce fence was cleared on verified reconciliation. An unrelated
    # archive failure retains the pre-existing conservative backup resume rule.
    with pytest.raises(EngineError) as caught:fixture.engine.resume(plan,{})
    assert caught.value.code=='RECOVERY_REQUIRED' and state(fixture)['quiesce']==1


@pytest.mark.parametrize('kind',['missing','blank','empty_schema'])
def test_known_unknown_never_recreates_or_trusts_lost_authoritative_adapter_ledger(installed,kind):
    import sqlite3
    fixture,bundle,_=installed;(fixture.root/'data/adapter-mode.txt').write_text('fail_after_quiesce')
    plan=fixture.reviewed_plan('stop',bundle)
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    ledger=fixture.root/'.hinstall/product-adapter/v1/adapter-invocations.sqlite3'
    ledger.unlink()
    if kind=='blank':ledger.touch(mode=0o600)
    elif kind=='empty_schema':
        connection=sqlite3.connect(ledger)
        connection.execute('CREATE TABLE calls(call_id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,operation_id TEXT NOT NULL,hook TEXT NOT NULL,state TEXT NOT NULL,result TEXT,owner_pid INTEGER NOT NULL,updated_at TEXT NOT NULL)')
        connection.commit();connection.close();ledger.chmod(0o600)
    before=ledger.read_bytes() if ledger.exists() else None
    with pytest.raises(EngineError):fixture.engine.execute(plan)
    assert state(fixture)['quiesce']==1
    with pytest.raises(EngineError) as caught:fixture.engine.resume(plan,{})
    assert caught.value.code=='RECOVERY_REQUIRED' and state(fixture)['quiesce']==1
    assert (ledger.read_bytes() if ledger.exists() else None)==before


def test_fault_before_adapter_ledger_creation_keeps_native_intent_fenced(installed,monkeypatch):
    from homensai_install.adapters import ExecutableProductAdapter
    fixture,bundle,_=installed
    class FaultBeforeLedger(BaseException):pass
    original=ExecutableProductAdapter.invoke
    def interrupted(self,hook,request,**kwargs):
        if hook=='quiesce':raise FaultBeforeLedger()
        return original(self,hook,request,**kwargs)
    plan=fixture.reviewed_plan('stop',bundle)
    with monkeypatch.context() as patch:
        patch.setattr(ExecutableProductAdapter,'invoke',interrupted)
        with pytest.raises(FaultBeforeLedger):fixture.engine.execute(plan)
    with pytest.raises(EngineError):fixture.engine.execute(fixture.reviewed_plan('start',bundle))
    with pytest.raises(EngineError) as caught:fixture.engine.resume(plan,{})
    assert caught.value.code=='RECOVERY_REQUIRED' and state(fixture)['quiesce']==0
    assert not (fixture.root/'.hinstall/product-adapter/v1/adapter-invocations.sqlite3').exists()


def test_explicit_resume_after_cancel_before_hook_can_create_initial_ledger(installed):
    fixture,bundle,_=installed;plan=fixture.reviewed_plan('stop',bundle)
    calls=[]
    def cancel():
        calls.append(True)
        return len(calls)>1
    result=fixture.engine.execute(plan,cancel_requested=cancel)
    assert result['status']=='CANCELED' and state(fixture)['quiesce']==0
    assert fixture.engine.resume(plan,{})['stopped'] and state(fixture)['quiesce']==1


def test_fault_with_existing_readiness_ledger_cannot_register_missing_quiesce_on_duplicate(installed,monkeypatch):
    from homensai_install.adapters import ExecutableProductAdapter
    fixture,bundle,_=installed
    fixture.engine.execute(fixture.reviewed_plan('start',bundle))
    assert state(fixture)['ready']==1 and state(fixture)['quiesce']==0
    class FaultBeforeInvocation(BaseException):pass
    original=ExecutableProductAdapter.invoke
    def interrupted(self,hook,request,**kwargs):
        if hook=='quiesce':raise FaultBeforeInvocation()
        return original(self,hook,request,**kwargs)
    plan=fixture.reviewed_plan('stop',bundle)
    with monkeypatch.context() as patch:
        patch.setattr(ExecutableProductAdapter,'invoke',interrupted)
        with pytest.raises(FaultBeforeInvocation):fixture.engine.execute(plan)
    ledger=fixture.root/'.hinstall/product-adapter/v1/adapter-invocations.sqlite3'
    assert ledger.is_file()
    with pytest.raises(EngineError) as caught:fixture.engine.execute(plan)
    assert caught.value.code=='RECOVERY_REQUIRED'
    assert state(fixture)['quiesce']==0
    with pytest.raises(EngineError) as caught:fixture.engine.resume(plan,{})
    assert caught.value.code=='RECOVERY_REQUIRED' and state(fixture)['quiesce']==0


@pytest.mark.parametrize('recovery_failure',[None,'start','health'])
def test_backup_primary_failure_survives_recovery_failure_without_leaking_messages(installed,monkeypatch,recovery_failure):
    fixture,bundle,_=installed
    plan=fixture.reviewed_plan('backup',bundle)
    primary=EngineError('STATE_STORAGE_UNAVAILABLE','synthetic-primary-private-message')
    def fail_backup(*args,**kwargs):raise primary
    monkeypatch.setattr(fixture.engine,'_backup',fail_backup)
    if recovery_failure=='start':
        def fail_start(*args,**kwargs):raise EngineError('HEALTH_CHECK_FAILED','synthetic-secondary-private-message')
        monkeypatch.setattr(fixture.engine,'_start',fail_start)
    elif recovery_failure=='health':
        (fixture.root/'data/adapter-mode.txt').write_text('wrong_release')
    with pytest.raises(EngineError) as caught:
        fixture.engine.execute(plan)
    assert caught.value is primary and caught.value.code=='STATE_STORAGE_UNAVAILABLE'
    journal=next(json.loads(path.read_text()) for path in (fixture.root/'.hinstall/operations').glob('*.json')
                 if json.loads(path.read_text()).get('action')=='backup')
    assert journal['error_code']=='STATE_STORAGE_UNAVAILABLE'
    if recovery_failure is None:
        assert primary.details=={} and 'recovery_error' not in journal
    else:
        expected={'phase':'recovery_'+recovery_failure,'code':'HEALTH_CHECK_FAILED'}
        assert primary.details['primary_error_code']=='STATE_STORAGE_UNAVAILABLE'
        assert primary.details['recovery_error']==expected==journal['recovery_error']
        assert journal['primary_error_code']=='STATE_STORAGE_UNAVAILABLE'
        # Public facade transports these safe details without changing primary.
        from homensai_install.api.v1 import InstallerAPI
        from homensai_install.errors import InstallerError
        class FailedEngine:
            def execute(self,*args):raise primary
        api=InstallerAPI(fixture.base/'diagnostic-transport',engine=FailedEngine())
        with pytest.raises(InstallerError) as public:api._engine_call('execute',{})
        assert public.value.code=='STATE_STORAGE_UNAVAILABLE'
        assert public.value.context['details']['recovery_error']==expected
    output=json.dumps({'journal':journal,'details':primary.details})
    assert 'synthetic-primary-private-message' not in output and 'synthetic-secondary-private-message' not in output
