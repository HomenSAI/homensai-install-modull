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
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from homensai_install.adapters import HOOKS
from homensai_install.engine import NativeEngine, EngineError
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest


SCRIPT = r'''
import hashlib,json,os,pathlib,sqlite3,sys
request=json.load(sys.stdin);data=request['data'];hook=request['hook']
home=pathlib.Path(os.environ['HOMENSAI_DATA_HOME']);database=home/'schema.sqlite'
if hook!='migration_plan': raise AssertionError('only explicit read-only preparation implemented')
profile=json.loads(pathlib.Path('profile.json').read_text());target=profile['target_schema']
current='empty'
if database.exists():
 with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
  version=db.execute('PRAGMA user_version').fetchone()[0]
 if version not in (1,2,3): raise AssertionError('unknown schema is not empty')
 current='schema_'+str(version)
steps=[] if current==target else [{'id':'to_'+target,'backup_required':current!='empty','reversible':False}]
result={'from_version':current,'to_version':target,'steps':steps}
result['plan_digest']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
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
    def release(name, target_schema, *, config=False):
        value=fixture.bundle(name);payload=Path(value['options']['payload_dir'])
        shutil.copy2(Path('/usr/bin/python3').resolve(),payload/'runtime/python');(payload/'runtime/python').chmod(0o700)
        (payload/'adapter.py').write_text(SCRIPT)
        (payload/'profile.json').write_text(json.dumps({'target_schema':target_schema}))
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
    assert migration.engine.capabilities(migration.target)['actions']['install']['supported'] is False


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
