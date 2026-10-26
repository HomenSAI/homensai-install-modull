"""Explicit signed migration preparation; реальная SQLite, без accepted install/update."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from homensai_install.adapters import HOOKS
from homensai_install.engine import NativeEngine, EngineError
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest


SCRIPT = r'''
import hashlib,json,os,pathlib,sqlite3,sys,urllib.request
request=json.load(sys.stdin);data=request['data'];hook=request['hook']
home=pathlib.Path(os.environ['HOMENSAI_DATA_HOME']);database=home/'schema.sqlite'
profile=json.loads(pathlib.Path('profile.json').read_text());target=profile['target_schema']
current='empty'
if database.exists():
 with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
  version=db.execute('PRAGMA user_version').fetchone()[0]
 if version not in (1,2,3): raise AssertionError('unknown schema is not empty')
 current='schema_'+str(version)
if hook=='migration_plan':
 steps=[] if current==target else [{'id':'to_'+target,'backup_required':current!='empty','reversible':False}]
 if profile.get('multiple_steps'): steps.append({'id':'extra','backup_required':current!='empty','reversible':False})
 result={'from_version':current,'to_version':target,'steps':steps}
 result['plan_digest']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
elif hook=='migration_apply':
 binding=data['migration_binding'];fault=profile.get('fault')
 if fault=='before_apply': sys.exit(21)
 assert binding['to_version']==target and binding['to_release']==profile['release']
 with sqlite3.connect(database) as db:
  db.execute('CREATE TABLE IF NOT EXISTS adapter_effects(call_id TEXT PRIMARY KEY,request_digest TEXT,hook TEXT,result TEXT)')
  old=db.execute('SELECT request_digest,result FROM adapter_effects WHERE call_id=?',(request['idempotency_key'],)).fetchone()
  if old:
   assert old[0]==request['request_digest'];result=json.loads(old[1])
  else:
   assert current==binding['from_version']
   db.execute('PRAGMA user_version='+target.split('_')[-1])
   result={'applied':True,'receipt_id':'effect_'+request['idempotency_key'][-32:],'state_version':target}
   db.execute('INSERT INTO adapter_effects VALUES(?,?,?,?)',(request['idempotency_key'],request['request_digest'],hook,json.dumps(result)))
 config=pathlib.Path(os.environ['HOMENSAI_CONFIG_HOME'])
 for path,content in ((home/'document.txt','Синтетический документ'),(config/'settings.json','{"test":true}')):
  try:
   with path.open('x') as output: output.write(content)
  except FileExistsError: pass
 if fault in ('after_apply','unknown'): sys.exit(22)
elif hook=='operation_status':
 old=None
 if database.exists():
  with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
   try: old=db.execute('SELECT request_digest,result FROM adapter_effects WHERE call_id=?',(request['idempotency_key'],)).fetchone()
   except sqlite3.OperationalError: pass
 result={'outcome':'unknown','request_digest':request['request_digest']}
 if old and profile.get('fault')!='unknown':
  assert old[0]==request['request_digest'];result.update(outcome='applied',result=json.loads(old[1]))
elif hook=='readiness':
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 with opener.open('http://127.0.0.1:'+str(profile['port'])+'/healthz',timeout=1) as response: observed=json.load(response)
 identity={key:observed[key] for key in ('product_id','installation_id','release')};identity['probe_id']=data['probe_id']
 result={'ready':current==target,'checks':[{'id':'schema','status':'ok' if current==target else 'failed'}],'observed_identity':identity}
elif hook=='quiesce': result={'quiesced':True,'active_writers':0}
else: raise AssertionError('unexpected hook')
response={key:request[key] for key in ('protocol_version','product_id','request_id','request_digest','hook')}
response.update(status='ok',result=result);print(json.dumps(response))
'''


@pytest.fixture
def migration(tmp_path):
    if sys.platform != 'linux': pytest.skip('Linux/native/user executable adapter')
    spec=importlib.util.spec_from_file_location('migration_native_fixture',Path(__file__).with_name('test_native_engine.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    fixture=module.NativeLifecycleTests(methodName='runTest');fixture.setUp()
    private=Ed25519PrivateKey.generate()
    public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    verifier=SignedPayloadVerifier({'synthetic':public},allow_test_keys=True)
    fixture.engine=NativeEngine(verifier,lambda ref:'synthetic-archive-passphrase',development_runtime=sys.executable,
                               product_adapter_descriptor='installer/adapter.json')
    def release(name, target_schema, *, config=False, fault=None, multiple_steps=False):
        value=fixture.bundle(name);payload=Path(value['options']['payload_dir'])
        shutil.copy2(Path('/usr/bin/python3').resolve(),payload/'runtime/python');(payload/'runtime/python').chmod(0o700)
        (payload/'adapter.py').write_text(SCRIPT)
        (payload/'profile.json').write_text(json.dumps({'target_schema':target_schema,'release':name,'port':fixture.port,
                                                      'fault':fault,'multiple_steps':multiple_steps}))
        (payload/'installer').mkdir()
        descriptor={'schema_version':1,'protocol_version':'1.0','product_id':value['manifest']['product_id'],
            'adapter_id':'migration-reference','adapter_version':'0.1.0','command':['runtime/python','-I','-B','adapter.py'],
            'environment_allowlist':['HOMENSAI_DATA_HOME', *(['HOMENSAI_CONFIG_HOME'] if config else [])],
            'capabilities':{key:{'mutating':mutating,'idempotency':'application' if mutating else 'read_only'}
                            for key,mutating in HOOKS.items()}}
        (payload/'installer/adapter.json').write_bytes(canonical_bytes(descriptor))
        inventory={'schema_version':1,'product_id':value['manifest']['product_id'],'release':name,
            'target':{key:fixture.target[key] for key in ('os','arch','mode')},
            'files':{p.relative_to(payload).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in payload.rglob('*') if p.is_file()},
            'development':True,'manifest_sha256':hash_manifest(value['manifest']),'runtime_executable':'runtime/python'}
        signature={'algorithm':'ed25519','key_id':'synthetic','signature':base64.b64encode(private.sign(canonical_bytes(inventory))).decode()}
        Path(value['options']['inventory_file']).write_bytes(canonical_bytes(inventory))
        Path(value['options']['signature_file']).write_bytes(canonical_bytes(signature))
        return value
    def legacy_install(value):
        engine=fixture.engine
        fixture.engine=NativeEngine(verifier,lambda ref:'synthetic-archive-passphrase',development_runtime=sys.executable)
        fixture.run_action('install',value)
        fixture.engine=engine
        with sqlite3.connect(fixture.root/'data/schema.sqlite') as db: db.execute('PRAGMA user_version=1')
    fixture.release=release;fixture.legacy_install=legacy_install
    fixture.public_key=public
    try: yield fixture
    finally:
        if fixture.current:
            cleanup=NativeEngine(verifier,development_runtime=sys.executable)
            cleanup._stop(cleanup._context(fixture.current['manifest'],fixture.target,fixture.current['options']))
            fixture.current=None
        fixture.tearDown()


def prepare(fixture, action, value):
    return fixture.engine.prepare_migration(action,value['manifest'],fixture.target,value['options'])


def test_fresh_preparation_has_no_fake_release_or_product_data_and_binds_immutable_review(migration):
    value=migration.release('release-1','schema_1')
    result=prepare(migration,'install',value)
    assert set(result)=={'migration_review_id','action','product_plan','from_release','to_release','installation_id','binding_sha256'}
    assert len(result['migration_review_id'])==64 and len(result['binding_sha256'])==64
    assert result['from_release'] is None and result['to_release']=='release-1'
    assert result['product_plan']['from_version']=='empty' and result['product_plan']['to_version']=='schema_1'
    ctx=migration.engine._context(value['manifest'],migration.target,value['options'])
    assert migration.engine._pointer(ctx) is None
    assert migration.engine._owner(ctx,required=True)['installed'] is False
    assert not any(ctx['home'].roots['data'].iterdir())
    assert migration.engine.capabilities(migration.target)['actions']['update']['supported'] is False


@pytest.mark.parametrize('content',['sqlite','document','unknown_directory'])
def test_unknown_nonempty_root_never_becomes_fresh(migration,content):
    value=migration.release('release-1','schema_1')
    migration.root.mkdir()
    if content=='unknown_directory': (migration.root/'foreign').mkdir()
    else: (migration.root/('existing.sqlite' if content=='sqlite' else 'document.txt')).write_bytes(b'not empty')
    with pytest.raises(EngineError): prepare(migration,'install',value)
    assert not (migration.root/'.hinstall/installation.json').exists()


def test_data_added_after_fresh_review_is_stale_before_hook_or_mutation(migration,monkeypatch):
    value=migration.release('release-1','schema_1');result=prepare(migration,'install',value)
    (migration.root/'data/unknown.sqlite').write_bytes(b'existing data')
    from homensai_install.adapters import ExecutableProductAdapter
    monkeypatch.setattr(ExecutableProductAdapter,'invoke',lambda *args,**kwargs:pytest.fail('stale empty proof must fail before hook'))
    ctx=migration.engine._context(value['manifest'],migration.target,value['options'])
    with pytest.raises(EngineError) as caught:
        migration.engine._checked_migration_review(ctx,'install',result['migration_review_id'])
    assert caught.value.code=='PLAN_STALE'


def test_update_preparation_observes_schema_separately_from_release_and_detects_drift(migration):
    old=migration.release('release-1','schema_1');migration.legacy_install(old)
    value=migration.release('release-2','schema_2');result=prepare(migration,'update',value)
    assert result['from_release']=='release-1' and result['to_release']=='release-2'
    assert result['product_plan']['from_version']=='schema_1' and result['product_plan']['to_version']=='schema_2'
    ctx=migration.engine._context(value['manifest'],migration.target,value['options'])
    migration.engine._checked_migration_review(ctx,'update',result['migration_review_id'],recheck_hook=True)
    with sqlite3.connect(migration.root/'data/schema.sqlite') as db: db.execute('PRAGMA user_version=3')
    with pytest.raises(EngineError) as caught:
        migration.engine._checked_migration_review(ctx,'update',result['migration_review_id'],recheck_hook=True)
    assert caught.value.code=='PLAN_STALE'


def test_reading_prepared_review_during_plan_does_not_invoke_product_hooks(migration,monkeypatch):
    value=migration.release('release-1','schema_1');result=prepare(migration,'install',value)
    from homensai_install.adapters import ExecutableProductAdapter
    monkeypatch.setattr(ExecutableProductAdapter,'invoke',lambda *args,**kwargs:pytest.fail('plan cannot invoke hooks'))
    ctx=migration.engine._context(value['manifest'],migration.target,value['options'])
    assert migration.engine._checked_migration_review(ctx,'install',result['migration_review_id'])['product_plan']==result['product_plan']


@pytest.mark.parametrize('change',['missing_root','replace_root','owner_hardlink','owner_permission','unknown_control','review_tamper'])
def test_lost_or_tampered_fresh_witness_never_authorizes_a_plan(migration,change):
    value=migration.release('release-1','schema_1');result=prepare(migration,'install',value)
    ctx=migration.engine._context(value['manifest'],migration.target,value['options'])
    if change=='missing_root': (migration.root/'data').rmdir()
    elif change=='replace_root':
        (migration.root/'data').rename(migration.root/'old-data')
        (migration.root/'data').mkdir(mode=0o700)
    elif change=='owner_hardlink': os.link(ctx['control']/'installation.json',migration.base/'owner-linked.json')
    elif change=='owner_permission': (ctx['control']/'installation.json').chmod(0o644)
    elif change=='unknown_control': (ctx['control']/'unknown.json').write_text('{}')
    else:
        path=ctx['control']/'migration-reviews'/(result['migration_review_id']+'.json')
        body=json.loads(path.read_text());body['product_plan']['from_version']='invented';path.write_text(json.dumps(body));path.chmod(0o600)
    with pytest.raises(EngineError) as caught:
        migration.engine._checked_migration_review(ctx,'install',result['migration_review_id'])
    assert caught.value.code=='PLAN_STALE'


def test_unknown_update_schema_is_rejected_without_creating_or_repairing_product_data(migration):
    old=migration.release('release-1','schema_1');migration.legacy_install(old)
    value=migration.release('release-2','schema_2')
    with sqlite3.connect(migration.root/'data/schema.sqlite') as db: db.execute('PRAGMA user_version=99')
    before=(migration.root/'data/schema.sqlite').read_bytes()
    with pytest.raises(EngineError): prepare(migration,'update',value)
    assert (migration.root/'data/schema.sqlite').read_bytes()==before


def test_prepare_rejects_missing_registration_wrong_action_or_existing_review_option(migration):
    value=migration.release('release-1','schema_1')
    legacy=NativeEngine(migration.engine.verifier,development_runtime=sys.executable)
    with pytest.raises(EngineError) as caught: legacy.prepare_migration('install',value['manifest'],migration.target,value['options'])
    assert caught.value.code=='CAPABILITY_UNSUPPORTED' and not migration.root.exists()
    with pytest.raises(EngineError): prepare(migration,'backup',value)
    with pytest.raises(EngineError): migration.engine.prepare_migration('install',value['manifest'],migration.target,
        {**value['options'],'migration_review_id':'a'*64})
    assert not migration.root.exists()


@pytest.mark.parametrize('config',[False,True])
def test_config_environment_is_trusted_and_only_enabled_by_signed_allowlist(migration,config):
    value=migration.release('release-1','schema_1',config=config)
    ctx=migration.engine._context(value['manifest'],migration.target,value['options'])
    adapter,_=migration.engine._product_adapter(ctx)
    assert ('HOMENSAI_CONFIG_HOME' in adapter.environment) is config
    if config: assert adapter.environment['HOMENSAI_CONFIG_HOME']==str(migration.root/'config')


def fresh_plan(fixture,value):
    review=prepare(fixture,'install',value)
    prepared={**value,'options':{**value['options'],'migration_review_id':review['migration_review_id']}}
    return fixture.reviewed_plan('install',prepared),prepared


def migration_effect_count(fixture):
    database=fixture.root/'data/schema.sqlite'
    if not database.exists(): return 0
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        return db.execute("SELECT count(*) FROM adapter_effects WHERE hook='migration_apply'").fetchone()[0]


def test_true_adapter_fresh_install_applies_reviewed_plan_once_without_legacy_migration(migration):
    value=migration.release('release-1','schema_1',config=True);plan,prepared=fresh_plan(migration,value)
    result=migration.engine.execute(plan);migration.current=prepared
    assert result['status']=='SUCCEEDED' and result['health']['product_adapter']['ready']
    assert (migration.root/'data/document.txt').read_text()=='Синтетический документ'
    assert json.loads((migration.root/'config/settings.json').read_text())=={'test':True}
    assert not (migration.root/'data/product.sqlite').exists() and not (migration.root/'data/migration-runs.txt').exists()
    assert migration.engine.execute(plan)==result and migration_effect_count(migration)==1


@pytest.mark.parametrize('fault',['after_apply','unknown'])
def test_fresh_lost_ack_reconciles_or_fences_without_repeating_mutation(migration,fault):
    value=migration.release('release-1','schema_1',config=True,fault=fault);plan,prepared=fresh_plan(migration,value)
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code=='RECOVERY_REQUIRED' and migration_effect_count(migration)==1
    if fault=='unknown':
        with pytest.raises(EngineError): migration.engine.resume(plan,{})
    else:
        result=migration.engine.resume(plan,{});migration.current=prepared
        assert result['status']=='SUCCEEDED'
    assert migration_effect_count(migration)==1


def test_fresh_cancel_after_stage_resumes_original_review_once(migration):
    value=migration.release('release-1','schema_1',config=True);plan,prepared=fresh_plan(migration,value)
    canceled=[False]
    def emit(event):
        if event['phase']=='stage' and event['status']=='SUCCEEDED': canceled[0]=True
    result=migration.engine.execute(plan,emit,lambda:canceled[0])
    assert result['status']=='CANCELED' and migration_effect_count(migration)==0
    result=migration.engine.resume(plan,{});migration.current=prepared
    assert result['status']=='SUCCEEDED' and migration_effect_count(migration)==1


def test_multi_step_migration_is_explicitly_unsupported_in_first_engine_slice(migration):
    value=migration.release('release-1','schema_1',config=True,multiple_steps=True)
    review=prepare(migration,'install',value)
    prepared={**value,'options':{**value['options'],'migration_review_id':review['migration_review_id']}}
    with pytest.raises(EngineError) as caught: migration.reviewed_plan('install',prepared)
    assert caught.value.code=='CAPABILITY_UNSUPPORTED' and migration_effect_count(migration)==0


def test_authorization_refusal_is_known_no_effect_and_explicit_resume_can_retry(migration,monkeypatch):
    from homensai_install.migration_receipts import FreshEmptyReceiptStore
    from homensai_install.errors import InstallerError
    value=migration.release('release-1','schema_1',config=True);plan,prepared=fresh_plan(migration,value)
    verify=FreshEmptyReceiptStore.verify
    def refuse(*args,**kwargs): raise InstallerError('PLAN_STALE')
    monkeypatch.setattr(FreshEmptyReceiptStore,'verify',refuse)
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code=='PLAN_STALE' and migration_effect_count(migration)==0
    journal=next((migration.root/'.hinstall/operations').glob('*.json'));state=json.loads(journal.read_text())
    assert state['product_adapter_migration_started'] is False and not state.get('product_adapter_unknown')
    monkeypatch.setattr(FreshEmptyReceiptStore,'verify',verify)
    assert migration.engine.resume(plan,{})['status']=='SUCCEEDED';migration.current=prepared
    assert migration_effect_count(migration)==1


def test_foreign_data_arriving_after_stage_is_never_initialized_as_fresh(migration):
    value=migration.release('release-1','schema_1',config=True);plan,_=fresh_plan(migration,value)
    def emit(event):
        if event['phase']=='migrate' and event['status']=='RUNNING':
            (migration.root/'data/foreign.sqlite').write_bytes(b'never overwrite this unknown database')
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan,emit)
    assert caught.value.code in {'RECOVERY_REQUIRED','PLAN_STALE'} and migration_effect_count(migration)==0
    assert (migration.root/'data/foreign.sqlite').read_bytes()==b'never overwrite this unknown database'


KILL_CONTROLLER = r'''
import base64,json,os,signal,sys
from pathlib import Path
from homensai_install.engine import NativeEngine
from homensai_install.trust import SignedPayloadVerifier
from homensai_install.adapters import ExecutableProductAdapter,_Ledger
value=json.loads(Path(sys.argv[1]).read_text());phase=value['kill_phase']
class Controller(NativeEngine):
 def _adapter_migration_completed(self,ctx,result):
  if phase=='after_apply': os.kill(os.getpid(),signal.SIGKILL)
  return super()._adapter_migration_completed(ctx,result)
original_begin=_Ledger.begin
def begin(self,key,request_digest,operation_id,hook,**kwargs):
 if hook=='migration_apply' and phase=='before_ledger': os.kill(os.getpid(),signal.SIGKILL)
 return original_begin(self,key,request_digest,operation_id,hook,**kwargs)
_Ledger.begin=begin
original_execute=ExecutableProductAdapter._execute
def execute(self,hook,*args,**kwargs):
 if hook=='migration_apply' and phase=='after_ledger': os.kill(os.getpid(),signal.SIGKILL)
 return original_execute(self,hook,*args,**kwargs)
ExecutableProductAdapter._execute=execute
def emit(event):
 if phase=='after_stage' and event['phase']=='stage' and event['status']=='SUCCEEDED': os.kill(os.getpid(),signal.SIGKILL)
engine=Controller(SignedPayloadVerifier({'synthetic':base64.b64decode(value['public_key'])},allow_test_keys=True),
 lambda ref:'synthetic-archive-passphrase',development_runtime=sys.executable,product_adapter_descriptor='installer/adapter.json')
engine.execute(value['plan'],emit)
raise AssertionError('expected owned controller SIGKILL')
'''


def kill_controller(fixture,plan,phase):
    source=fixture.base/'kill-controller.json'
    source.write_text(json.dumps({'plan':plan,'kill_phase':phase,'public_key':base64.b64encode(fixture.public_key).decode()}))
    result=subprocess.run([sys.executable,'-c',KILL_CONTROLLER,str(source)],cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25)
    assert result.returncode == -9, result.stderr.decode(errors='replace')


@pytest.mark.parametrize('phase',['after_stage','before_ledger','after_ledger','after_apply'])
def test_actual_controller_sigkill_boundaries_resume_without_duplicate_fresh_migration(migration,phase):
    value=migration.release('release-1','schema_1',config=True);plan,prepared=fresh_plan(migration,value)
    kill_controller(migration,plan,phase)
    assert migration_effect_count(migration)==(1 if phase=='after_apply' else 0)
    if phase in {'before_ledger','after_ledger'}:
        with pytest.raises(EngineError) as caught: migration.engine.resume(plan,{})
        assert caught.value.code=='RECOVERY_REQUIRED' and migration_effect_count(migration)==0
    else:
        assert migration.engine.resume(plan,{})['status']=='SUCCEEDED';migration.current=prepared
        assert migration_effect_count(migration)==1


@pytest.mark.parametrize('missing',['receipt','ledger'])
def test_lost_ack_with_lost_receipt_or_ledger_never_repeats_actual_effect(migration,missing):
    value=migration.release('release-1','schema_1',config=True);plan,prepared=fresh_plan(migration,value)
    kill_controller(migration,plan,'after_apply')
    if missing=='ledger':
        (migration.root/'.hinstall/product-adapter/release-1/adapter-invocations.sqlite3').unlink()
        with pytest.raises(EngineError): migration.engine.resume(plan,{})
    else:
        for path in (migration.root/'.hinstall/fresh-receipts').glob('*.json'): path.unlink()
        assert migration.engine.resume(plan,{})['status']=='SUCCEEDED';migration.current=prepared
    assert migration_effect_count(migration)==1


@pytest.mark.parametrize('change',['missing','changed'])
def test_expected_native_intent_is_required_at_authorization_boundary(migration,monkeypatch,change):
    from homensai_install.adapters import _Ledger
    value=migration.release('release-1','schema_1',config=True);plan,_=fresh_plan(migration,value)
    original=_Ledger.begin
    def tamper(self,key,request_digest,operation_id,hook,**kwargs):
        if hook=='migration_apply':
            journal=next((migration.root/'.hinstall/operations').glob('*.json'))
            if change=='missing': journal.unlink()
            else:
                state=json.loads(journal.read_text());state['migration_review_id']='0'*64;journal.write_text(json.dumps(state));journal.chmod(0o600)
        return original(self,key,request_digest,operation_id,hook,**kwargs)
    monkeypatch.setattr(_Ledger,'begin',tamper)
    with pytest.raises(EngineError) as caught: migration.engine.execute(plan)
    assert caught.value.code=='RECOVERY_REQUIRED' and migration_effect_count(migration)==0
