"""Local catalogue update contract: real signatures, data only, isolated owned state."""
import importlib


def test_signed_catalogue_update_public_store_contract_exists():
    module = importlib.import_module('homensai_install.error_catalog_updates')
    for name in ('CatalogUpdatePolicy', 'CatalogUpdateStore', 'load_catalog_update_policy'):
        assert callable(getattr(module, name))

import base64
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from homensai_install.api.model import canonical
from homensai_install.errors import ErrorCatalog, InstallerError

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux owned catalogue store write contract')


@pytest.fixture
def setup_catalog(tmp_path):
    from homensai_install.error_catalog_updates import CatalogUpdatePolicy, CatalogUpdateStore
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    clock = [datetime(2026, 10, 29, 12, tzinfo=timezone.utc)]
    policy = CatalogUpdatePolicy({'catalog-dev': public}, policy_id='dev-policy',
        valid_from='2026-01-01T00:00:00Z', valid_until='2027-01-01T00:00:00Z',
        allow_development=True, clock=lambda: clock[0])
    store = CatalogUpdateStore(tmp_path/'catalogue', policy=policy)
    counter = []
    def artifact(sequence=1, *, text='New offline explanation', patch=None, catalog_patch=None, signing_key=None):
        data = deepcopy(ErrorCatalog()._data)
        data['catalog_version'] = '1.0.'+str(sequence)
        for locale in ('ru', 'en', 'de'):
            data['errors']['PLAN_STALE']['translations'][locale]['description'] = text+' '+locale
        if catalog_patch: catalog_patch(data)
        encoded = canonical(data).encode()
        metadata = dict(schema_version=1,kind='error_catalog_update',purpose='installer-error-catalog',
            module_id=policy.module_id,catalog_id=policy.catalog_id,catalog_schema_version=1,api_major=1,
            baseline_semantics_sha256=policy.baseline_semantics_sha256,catalog_version=data['catalog_version'],
            sequence=sequence,issued_at='2026-10-29T00:00:00Z',expires_at='2026-11-29T00:00:00Z',
            development=True,catalog_sha256=hashlib.sha256(encoded).hexdigest(),catalog_bytes=len(encoded))
        if patch: metadata.update(patch)
        signature = (signing_key or key).sign(canonical(metadata).encode())
        value = dict(metadata=metadata,catalog=data,signature=dict(algorithm='ed25519',key_id='catalog-dev',signature=base64.b64encode(signature).decode()))
        path=tmp_path/('artifact-'+str(len(counter))+'.json');counter.append(path)
        path.write_text(canonical(value));path.chmod(0o600)
        return path
    return store,policy,key,clock,artifact


def files(path):
    return {str(p.relative_to(path)):(p.read_bytes(),p.stat().st_ino,p.stat().st_mode,p.stat().st_mtime_ns,p.stat().st_ctime_ns)
            for p in path.rglob('*') if p.is_file()}


def apply(store, artifact):
    preview=store.preview(artifact)
    return store.apply(artifact,expected_review_digest=preview['review_digest'],confirmed=True)


def test_real_signed_update_preview_is_readonly_and_reopen_has_no_global_leak(setup_catalog,tmp_path):
    from homensai_install.error_catalog_updates import CatalogUpdateStore
    store,policy,_,_,make=setup_catalog
    original=ErrorCatalog().describe('PLAN_STALE','ru')
    artifact=make();before=files(tmp_path)
    assert store.status()['status']=='builtin'
    assert store.resolve_catalog().describe('PLAN_STALE','ru')==original
    preview=store.preview(artifact)
    assert files(tmp_path)==before and not store.directory.exists()
    assert preview['previous_sequence']==0
    assert apply(store,artifact)['status']=='APPLIED'
    protected=files(tmp_path)
    for locale in ('ru','en','de'):
        assert CatalogUpdateStore(store.directory,policy=policy).resolve_catalog().describe('PLAN_STALE',locale)['description']=='New offline explanation '+locale
    assert store.status()['status']=='active' and store.status()['high_water_sequence']==1
    assert CatalogUpdateStore(tmp_path/'other',policy=policy).status()['status']=='builtin'
    assert ErrorCatalog().describe('PLAN_STALE','ru')==original and files(tmp_path)==protected


def test_lost_ack_original_review_and_fresh_review_are_no_write_unchanged(setup_catalog,tmp_path):
    store,_,_,_,make=setup_catalog;artifact=make();review=store.preview(artifact)['review_digest']
    store.apply(artifact,expected_review_digest=review,confirmed=True);before=files(tmp_path)
    assert store.apply(artifact,expected_review_digest=review,confirmed=True)['status']=='UNCHANGED'
    assert store.apply(artifact,expected_review_digest=store.preview(artifact)['review_digest'],confirmed=True)['changed'] is False
    assert files(tmp_path)==before
    with pytest.raises(InstallerError) as caught:store.apply(artifact,expected_review_digest='0'*64,confirmed=True)
    assert caught.value.code=='PLAN_STALE' and files(tmp_path)==before


@pytest.mark.parametrize('patch',[{'sequence':True},{'sequence':0},{'schema_version':True},{'purpose':'product-payload'},
    {'module_id':'foreign'},{'catalog_id':'foreign'},{'catalog_schema_version':2},{'api_major':2},
    {'baseline_semantics_sha256':'f'*64},{'catalog_sha256':'f'*64},{'catalog_bytes':True},
    {'issued_at':'2026-12-01T00:00:00Z'},{'expires_at':'2026-10-29T12:00:00Z'},
    {'issued_at':'2026-10-29T00:00:00+00:00'},{'expires_at':'2028-01-01T00:00:00Z'}])
def test_bad_signed_metadata_never_creates_authority(setup_catalog,tmp_path,patch):
    store,_,_,_,make=setup_catalog;artifact=make(patch=patch);before=files(tmp_path)
    with pytest.raises(InstallerError):store.preview(artifact)
    with pytest.raises(InstallerError):store.apply(artifact,expected_review_digest='0'*64,confirmed=True)
    assert files(tmp_path)==before and not store.directory.exists()


@pytest.mark.parametrize('mutation',[
    lambda d:d['errors'].pop('UNKNOWN_ERROR'),
    lambda d:d['errors'].update(FOREIGN=deepcopy(d['errors']['PLAN_STALE'])),
    lambda d:d['errors']['PLAN_STALE'].update(retryable=True),
    lambda d:d['errors']['PLAN_STALE'].update(permitted_actions=['exec']),
    lambda d:d['errors']['PLAN_STALE'].update(severity='critical'),
    lambda d:d['errors']['PLAN_STALE'].update(alias_of='UNKNOWN_ERROR'),
    lambda d:d['errors']['PLAN_STALE']['translations'].pop('de'),
    lambda d:d['errors']['PLAN_STALE']['translations']['ru'].update(title='x'*257),
    lambda d:d['errors']['PLAN_STALE']['translations']['ru'].update(checks=['x']*33),
    lambda d:d['errors']['PLAN_STALE']['translations']['ru'].update(remediation=['x'*8193]),
    lambda d:d['errors']['PLAN_STALE']['translations']['ru'].update(impact=''),
    lambda d:d['errors']['PLAN_STALE']['translations']['ru'].update(html='<script>bad</script>'),
])
def test_catalogue_exact_codes_behavior_locales_and_all_text_bounds(setup_catalog,tmp_path,mutation):
    store,_,_,_,make=setup_catalog;artifact=make(catalog_patch=mutation);before=files(tmp_path)
    with pytest.raises(InstallerError):store.preview(artifact)
    assert files(tmp_path)==before


@pytest.mark.parametrize('fault',['signature','content','unknownkey','wrongkey','duplicate','nan','oversize'])
def test_untrusted_artifact_has_no_write_and_no_raw_error_leak(setup_catalog,tmp_path,fault):
    store,_,_,_,make=setup_catalog;artifact=make(signing_key=Ed25519PrivateKey.generate() if fault=='wrongkey' else None)
    value=json.loads(artifact.read_text())
    if fault=='signature':value['signature']['signature']='AA=='
    elif fault=='content':value['catalog']['errors']['PLAN_STALE']['translations']['ru']['description']='tampered'
    elif fault=='unknownkey':value['signature']['key_id']='product-key'
    if fault=='duplicate':artifact.write_text('{"metadata":{},"metadata":{}}')
    elif fault=='nan':artifact.write_text('{"metadata":NaN}')
    elif fault=='oversize':artifact.write_bytes(b'x'*(2*1024*1024+1))
    else:artifact.write_text(canonical(value))
    before=files(tmp_path)
    with pytest.raises(InstallerError) as caught:store.preview(artifact)
    assert str(tmp_path) not in json.dumps(caught.value.to_dict()) and files(tmp_path)==before


def test_sequence_floor_conflict_and_review_toctou(setup_catalog,tmp_path):
    store,_,_,_,make=setup_catalog;one=make(1);two=make(2);three=make(3)
    oldreview=store.preview(two)['review_digest'];apply(store,one)
    with pytest.raises(InstallerError) as caught:store.apply(two,expected_review_digest=oldreview,confirmed=True)
    assert caught.value.code=='PLAN_STALE'
    apply(store,two);state_before=files(store.directory)
    for artifact in (one,make(2,text='different same sequence')):
        with pytest.raises(InstallerError):store.preview(artifact)
    assert files(store.directory)==state_before
    statebefore=files(store.directory);review=store.preview(three)['review_digest']
    three.write_text(one.read_text())
    with pytest.raises(InstallerError):store.apply(three,expected_review_digest=review,confirmed=True)
    assert files(store.directory)==statebefore


def test_expired_revoked_policy_and_clock_fallback_keep_highwater(setup_catalog,tmp_path):
    from dataclasses import replace
    from homensai_install.error_catalog_updates import CatalogUpdateStore
    store,policy,_,clock,make=setup_catalog;artifact=make();apply(store,artifact);before=files(tmp_path)
    for change in ({'revoked_key_ids':('catalog-dev',)}, {'revoked_artifact_sha256':(hashlib.sha256(artifact.read_bytes()).hexdigest(),)}, {'valid_until':'2026-10-29T01:00:00Z'}, {'allow_development':False}):
        changed=CatalogUpdateStore(store.directory,policy=replace(policy,keys=dict(policy.keys),**change))
        assert changed.status()['effective_source']=='builtin' and changed.status()['high_water_sequence']==1
        with pytest.raises(InstallerError):changed.apply(artifact,expected_review_digest='0'*64,confirmed=True)
    clock[0]+=timedelta(days=32)
    assert store.status()['status']=='expired' and store.resolve_catalog().version==ErrorCatalog().version
    clock[0]=datetime(2026,10,29,11,tzinfo=timezone.utc)
    assert store.status()['effective_source']=='builtin'
    assert files(tmp_path)==before


@pytest.mark.parametrize('fault',['missingdb','missingmarker','missinglock','dbmode','markerhardlink','dbhardlink','corruptdb','wal','sidecar'])
def test_bad_state_readers_fallback_without_create_chmod_sidecars_or_authority_reset(setup_catalog,tmp_path,fault):
    store,_,_,_,make=setup_catalog;artifact=make();apply(store,artifact)
    if fault=='missingdb':store.path.unlink()
    elif fault=='missingmarker':store.marker.unlink()
    elif fault=='missinglock':(store.directory/'catalog.lock').unlink()
    elif fault=='dbmode':store.path.chmod(0o644)
    elif fault=='markerhardlink':os.link(store.marker,tmp_path/'link')
    elif fault=='dbhardlink':os.link(store.path,tmp_path/'link')
    elif fault=='corruptdb':store.path.write_bytes(b'corrupt database')
    elif fault=='wal':
        with sqlite3.connect(store.path) as connection:connection.execute('PRAGMA journal_mode=WAL')
        # Close explicitly; connection context manager alone does not close.
        connection.close()
    else:(store.directory/'catalog-state.sqlite3-journal').write_bytes(b'unknown')
    before=files(tmp_path)
    assert store.status()['status']=='unavailable' and store.resolve_catalog().version==ErrorCatalog().version
    with pytest.raises(InstallerError):store.preview(artifact)
    with pytest.raises(InstallerError):store.apply(artifact,expected_review_digest='0'*64,confirmed=True)
    assert files(tmp_path)==before


def test_real_transaction_failure_rolls_back_old_signed_bytes_highwater_and_pointer(setup_catalog,tmp_path,monkeypatch):
    from homensai_install import error_catalog_updates as module
    store,_,_,_,make=setup_catalog;one=make();two=make(2);apply(store,one)
    old=store.path.read_bytes();review=store.preview(two)['review_digest']
    def no_commit(connection):raise OSError(28,'UNTRUSTED_PRIVATE_ERROR','secret-path')
    with monkeypatch.context() as m:
        m.setattr(module,'_commit',no_commit)
        with pytest.raises(InstallerError) as caught:store.apply(two,expected_review_digest=review,confirmed=True)
    assert caught.value.code=='DISK_SPACE_LOW' and 'UNTRUSTED' not in json.dumps(caught.value.to_dict())
    assert store.path.read_bytes()==old and store.status()['high_water_sequence']==1
    assert apply(store,two)['sequence']==2


def test_real_sigkill_after_commit_can_ack_original_review_without_rewrite(setup_catalog,tmp_path,monkeypatch):
    from homensai_install import error_catalog_updates as module
    store,policy,_,_,make=setup_catalog;one=make();two=make(2);apply(store,one);review=store.preview(two)['review_digest']
    pid=os.fork()
    if pid==0:
        real=module._commit
        def commit_and_die(connection):real(connection);os.kill(os.getpid(),9)
        module._commit=commit_and_die
        store.apply(two,expected_review_digest=review,confirmed=True)
        os._exit(99)
    _,status=os.waitpid(pid,0);assert os.WIFSIGNALED(status) and os.WTERMSIG(status)==9
    fresh=module.CatalogUpdateStore(store.directory,policy=policy);before=files(tmp_path)
    assert fresh.status()['high_water_sequence']==2
    assert fresh.apply(two,expected_review_digest=review,confirmed=True)['status']=='UNCHANGED'
    assert files(tmp_path)==before


def test_loader_separate_trusted_keys_and_no_file_clock_or_development_override(setup_catalog,tmp_path):
    from homensai_install.error_catalog_updates import load_catalog_update_policy
    _,policy,_,_,_=setup_catalog;path=tmp_path/'policy.json';path.write_text(canonical(policy._body()));path.chmod(0o600)
    loaded=load_catalog_update_policy(path,allow_development=True)
    assert loaded.keys==policy.keys and loaded.clock is None
    for field,value in [('clock','caller-controlled'),('allow_development',True)]:
        data=policy._body();data[field]=value;path.write_text(canonical(data))
        with pytest.raises(InstallerError):load_catalog_update_policy(path)


def test_two_real_writer_processes_one_commit_other_stale_or_busy(setup_catalog,tmp_path):
    store,_,_,_,make=setup_catalog;apply(store,make(1))
    candidates=[make(2),make(3)]
    reviews=[store.preview(p)['review_digest'] for p in candidates]
    start_r,start_w=os.pipe();result_r,result_w=os.pipe();pids=[]
    for artifact,review in zip(candidates,reviews):
        pid=os.fork()
        if pid==0:
            os.close(start_w);os.close(result_r);os.read(start_r,1)
            try:result=store.apply(artifact,expected_review_digest=review,confirmed=True)['status']
            except InstallerError as error:result=error.code
            os.write(result_w,(result+'\n').encode());os._exit(0)
        pids.append(pid)
    os.close(start_r);os.close(result_w);os.write(start_w,b'xx');os.close(start_w)
    results=b''
    while True:
        chunk=os.read(result_r,1024)
        if not chunk:break
        results+=chunk
    os.close(result_r)
    for pid in pids:assert os.waitpid(pid,0)[1]==0
    values=results.decode().splitlines()
    assert values.count('APPLIED')==1 and set(values)<= {'APPLIED','PLAN_STALE','OPERATION_CONFLICT'}
    assert store.status()['high_water_sequence'] in (2,3) and store.status()['status']=='active'


def test_actual_kill_before_commit_keeps_hot_state_failclosed_without_readonly_recovery(setup_catalog,tmp_path):
    from homensai_install import error_catalog_updates as module
    store,_,_,_,make=setup_catalog;apply(store,make(1));nextone=make(2);review=store.preview(nextone)['review_digest']
    pid=os.fork()
    if pid==0:
        def die_without_commit(connection):os.kill(os.getpid(),9)
        module._commit=die_without_commit
        store.apply(nextone,expected_review_digest=review,confirmed=True);os._exit(99)
    _,status=os.waitpid(pid,0);assert os.WIFSIGNALED(status) and os.WTERMSIG(status)==9
    assert (store.directory/'catalog-state.sqlite3-journal').exists()
    before=files(tmp_path)
    assert store.status()['status']=='unavailable' and store.resolve_catalog().version==ErrorCatalog().version
    with pytest.raises(InstallerError):store.apply(nextone,expected_review_digest=review,confirmed=True)
    assert files(tmp_path)==before


@pytest.mark.parametrize('fault',['trigger','table','extra_file'])
def test_unknown_state_schema_or_namespace_refuses_before_update_effect(setup_catalog,tmp_path,fault):
    store,_,_,_,make=setup_catalog;apply(store,make(1));nextone=make(2)
    if fault=='extra_file':(store.directory/'unknown-state').write_text('not ours')
    else:
        connection=sqlite3.connect(store.path)
        try:
            connection.execute('CREATE TABLE foreign_state(x TEXT)')
            if fault=='trigger':connection.execute("CREATE TRIGGER foreign_hook AFTER INSERT ON state BEGIN INSERT INTO foreign_state VALUES ('effect'); END")
            connection.commit()
        finally:connection.close()
    before=files(tmp_path)
    assert store.status()['status']=='unavailable'
    with pytest.raises(InstallerError):store.preview(nextone)
    assert files(tmp_path)==before


@pytest.mark.parametrize('patch',[
    {'min_sequence':True},{'min_sequence':0},{'policy_epoch':False},{'keys':{}},
    {'allow_development':1},{'revoked_key_ids':'key'}, {'revoked_key_ids':[{}]},
    {'revoked_artifact_sha256':['bad']},{'baseline_semantics_sha256':'f'*64},
    {'clock':'not callable'},{'valid_from':'2028-01-01T00:00:00Z'},
])
def test_policy_trusted_constructor_still_requires_strict_shape(setup_catalog,patch):
    from homensai_install.error_catalog_updates import CatalogUpdatePolicy
    _,policy,_,_,_=setup_catalog
    args=dict(keys=dict(policy.keys),policy_id=policy.policy_id,valid_from=policy.valid_from,valid_until=policy.valid_until)
    args.update(patch)
    with pytest.raises(InstallerError):CatalogUpdatePolicy(**args)


def test_inclusive_floor_immutable_policy_and_untrusted_symlink_inputs(setup_catalog,tmp_path):
    from dataclasses import replace, FrozenInstanceError
    from homensai_install.error_catalog_updates import CatalogUpdateStore
    store,policy,_,_,make=setup_catalog
    with pytest.raises(TypeError):policy.keys['other']=b'x'*32
    with pytest.raises(FrozenInstanceError):policy.min_sequence=0
    strict=CatalogUpdateStore(store.directory,policy=replace(policy,keys=dict(policy.keys),min_sequence=2))
    with pytest.raises(InstallerError):strict.preview(make(1))
    artifact=make(2);assert apply(strict,artifact)['sequence']==2
    original=artifact.read_bytes();artifact.unlink();artifact.symlink_to(tmp_path/'missing')
    before=files(store.directory)
    with pytest.raises(InstallerError):strict.preview(artifact)
    assert files(store.directory)==before


def test_expired_policy_is_explicit_even_without_initialized_state(setup_catalog):
    from dataclasses import replace
    from homensai_install.error_catalog_updates import CatalogUpdateStore
    store,policy,_,_,_=setup_catalog
    expired=CatalogUpdateStore(store.directory,policy=replace(policy,keys=dict(policy.keys),valid_until='2026-10-29T01:00:00Z'))
    assert expired.status()['status']=='policy_invalid' and not store.directory.exists()


def test_expired_artifact_preserves_authority_for_new_valid_higher_sequence(setup_catalog):
    store,_,_,clock,make=setup_catalog;apply(store,make(1));clock[0]+=timedelta(days=32)
    assert store.status()['status']=='expired' and store.status()['high_water_sequence']==1
    higher=make(2,patch={'issued_at':'2026-11-30T00:00:00Z','expires_at':'2026-12-29T00:00:00Z'})
    assert apply(store,higher)['sequence']==2 and store.status()['status']=='active'


def test_sqlite_full_code_is_space_error_but_other_sqlite_error_is_not(setup_catalog,monkeypatch):
    from homensai_install import error_catalog_updates as module
    store,_,_,_,make=setup_catalog;apply(store,make(1));higher=make(2);review=store.preview(higher)['review_digest'];old=files(store.directory)
    for typed_code,expected in [(sqlite3.SQLITE_FULL,'DISK_SPACE_LOW'),(sqlite3.SQLITE_IOERR,'STATE_STORAGE_UNAVAILABLE')]:
        def failure(connection):
            error=sqlite3.OperationalError('UNTRUSTED_DB_PATH');error.sqlite_errorcode=typed_code;raise error
        with monkeypatch.context() as m:
            m.setattr(module,'_commit',failure)
            with pytest.raises(InstallerError) as caught:store.apply(higher,expected_review_digest=review,confirmed=True)
        assert caught.value.code==expected and 'UNTRUSTED' not in json.dumps(caught.value.to_dict())
        assert store.status()['high_water_sequence']==1
    assert store.path.read_bytes()==old['catalog-state.sqlite3'][0]


def test_initial_commit_failure_is_not_a_new_authority_and_missing_initialized_db_not_recreated(setup_catalog,tmp_path,monkeypatch):
    from homensai_install import error_catalog_updates as module
    store,_,_,_,make=setup_catalog;artifact=make();review=store.preview(artifact)['review_digest']
    def fail(connection):raise OSError(28,'UNTRUSTED')
    with monkeypatch.context() as m:
        m.setattr(module,'_commit',fail)
        with pytest.raises(InstallerError):store.apply(artifact,expected_review_digest=review,confirmed=True)
    before=files(tmp_path)
    assert store.marker.exists() and store.status()['effective_source']=='builtin'
    with pytest.raises(InstallerError):store.apply(artifact,expected_review_digest=review,confirmed=True)
    assert files(tmp_path)==before


def test_artifact_inode_substitution_at_real_nofollow_open_refuses_before_namespace_creation(setup_catalog,tmp_path,monkeypatch):
    from homensai_install import error_catalog_updates as module
    store,_,_,_,make=setup_catalog;artifact=make();alternate=make(2);original=artifact.read_bytes();fired=[]
    actual=module.os.open
    def swap(path,flags,*args,**kwargs):
        if Path(path)==artifact and not fired:
            fired.append(True);artifact.rename(tmp_path/'original-preserved.json');alternate.rename(artifact)
        return actual(path,flags,*args,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(module.os,'open',swap)
        with pytest.raises(InstallerError):store.preview(artifact)
    assert fired and not store.directory.exists() and (tmp_path/'original-preserved.json').read_bytes()==original


def test_plain_scriptlike_text_is_data_and_no_product_files_or_builtin_are_mutated(setup_catalog,tmp_path):
    store,_,_,_,make=setup_catalog
    product=tmp_path/'product.py';product.write_text('sentinel code never imported')
    original=files(tmp_path);builtin=deepcopy(ErrorCatalog()._data)
    malicious_text='<script>window.sideEffect=true</script>; shell text is not a command'
    apply(store,make(text=malicious_text))
    assert store.resolve_catalog().describe('PLAN_STALE','en')['description']==malicious_text+' en'
    assert product.read_bytes()==original['product.py'][0] and ErrorCatalog()._data==builtin


def test_constructor_status_and_preview_do_not_execute_or_accept_clock_from_json(setup_catalog,tmp_path):
    from homensai_install.error_catalog_updates import CatalogUpdateStore, load_catalog_update_policy
    store,policy,_,_,make=setup_catalog;artifact=make();before=files(tmp_path)
    assert store.status()['configured'] and store.preview(artifact)['requires_confirmation']
    assert files(tmp_path)==before
    path=tmp_path/'clock-policy.json';value=policy._body();value['clock']='RUN_ME';path.write_text(canonical(value))
    with pytest.raises(InstallerError):load_catalog_update_policy(path,allow_development=True)
    assert not store.directory.exists()


def test_clock_rollback_at_actual_sqlite_insert_refuses_commit_and_preserves_previous_authority(setup_catalog,tmp_path,monkeypatch):
    from homensai_install import error_catalog_updates as module
    store,_,_,clock,make=setup_catalog;apply(store,make(1));higher=make(2)
    review=store.preview(higher)['review_digest'];original=store.path.read_bytes();fired=[]
    actual=module.sqlite3.connect
    class Connection:
        def __init__(self,connection):self.connection=connection
        def __getattr__(self,name):return getattr(self.connection,name)
        def execute(self,sql,*args):
            result=self.connection.execute(sql,*args)
            if sql.startswith('INSERT OR REPLACE INTO state'):
                fired.append(True);clock[0]-=timedelta(hours=1)
            return result
    def connect(*args,**kwargs):return Connection(actual(*args,**kwargs))
    with monkeypatch.context() as m:
        m.setattr(module.sqlite3,'connect',connect)
        with pytest.raises(InstallerError) as caught:store.apply(higher,expected_review_digest=review,confirmed=True)
    assert fired and caught.value.code=='INTEGRITY_FAILED'
    assert store.path.read_bytes()==original
    clock[0]+=timedelta(hours=1)
    assert store.status()['high_water_sequence']==1 and store.status()['status']=='active'


def test_unreadable_initialized_authority_reports_unknown_highwater_not_fabricated_zero(setup_catalog,tmp_path):
    store,_,_,_,make=setup_catalog
    assert store.status()['high_water_sequence']==0
    apply(store,make(1));store.path.unlink();before=files(tmp_path)
    assert store.status()['status']=='unavailable' and store.status()['high_water_sequence'] is None
    assert files(tmp_path)==before


def test_unsupported_host_existing_state_falls_back_before_fcntl_import(setup_catalog,tmp_path,monkeypatch):
    import builtins
    from homensai_install import error_catalog_updates as module
    store,_,_,_,make=setup_catalog;apply(store,make(1));before=files(tmp_path)
    actual=builtins.__import__
    def no_fcntl(name,*args,**kwargs):
        assert name!='fcntl','Unsupported reader must refuse before importing platform lock support'
        return actual(name,*args,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(module.sys,'platform','win32');m.setattr(builtins,'__import__',no_fcntl)
        assert store.status()['status']=='unavailable'
        assert store.resolve_catalog().version==ErrorCatalog().version
    assert files(tmp_path)==before


def test_first_apply_creates_only_missing_api_state_ancestors_private_and_preserves_existing_parent_mode(setup_catalog,tmp_path):
    from homensai_install.error_catalog_updates import CatalogUpdateStore
    from homensai_install.api.v1 import InstallerAPI
    _,policy,_,_,make=setup_catalog;artifact=make();existing=tmp_path/'existing-public-parent';existing.mkdir(mode=0o755);existing.chmod(0o755)
    state=existing/'fresh-api-state'
    store=CatalogUpdateStore(state/'error-catalog',policy=policy);review=store.preview(artifact)['review_digest']
    previous=os.umask(0o022)
    try:store.apply(artifact,expected_review_digest=review,confirmed=True)
    finally:os.umask(previous)
    assert state.stat().st_mode&0o777==0o700
    assert store.directory.stat().st_mode&0o777==0o700
    assert existing.stat().st_mode&0o777==0o755
    assert InstallerAPI(state).operations_list()['operations']==[]


def test_first_stale_review_does_not_create_api_state_catalogue_or_lock(setup_catalog,tmp_path):
    from homensai_install.error_catalog_updates import CatalogUpdateStore
    _,policy,_,_,make=setup_catalog;artifact=make();state=tmp_path/'new-sdk-state'
    store=CatalogUpdateStore(state/'error-catalog',policy=policy)
    before=files(tmp_path)
    assert store.preview(artifact)['review_digest']!='0'*64
    with pytest.raises(InstallerError) as caught:
        store.apply(artifact,expected_review_digest='0'*64,confirmed=True)
    assert caught.value.code=='PLAN_STALE'
    assert not state.exists() and files(tmp_path)==before
