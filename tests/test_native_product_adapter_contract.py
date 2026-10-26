"""Additive protocol identity и trusted engine registration без процессов."""
from copy import deepcopy
import os

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.engine import NativeEngine, EngineError
from homensai_install.errors import InstallerError
from test_adapters_protocol import bundle, adapter


def observation():
    return {'ready':True,'checks':[{'id':'live','status':'ok'}],
            'observed_identity':{'product_id':'adapter-test','release':'1.0.0',
                                 'installation_id':'synthetic-instance','probe_id':'fresh-probe'}}


def test_observed_identity_is_additive_for_standalone_adapter(bundle):
    subject=adapter(bundle)
    result=observation()
    assert subject._validate_result('readiness',result)==result
    del result['observed_identity']
    assert subject._validate_result('readiness',result)==result


@pytest.mark.parametrize('field,value',[('product_id','invalid/product'),('release','v1\n'),
    ('installation_id',None),('probe_id',True),('probe_id','')])
def test_malformed_observed_identity_fails_protocol_validation(bundle,field,value):
    result=observation();result['observed_identity'][field]=value
    with pytest.raises(InstallerError):adapter(bundle)._validate_result('readiness',result)


def test_observed_identity_never_accepts_unknown_fields(bundle):
    result=observation();result['observed_identity']['path']='/tmp/untrusted'
    with pytest.raises(InstallerError):adapter(bundle)._validate_result('readiness',result)


@pytest.mark.parametrize('descriptor',['../adapter.json','/adapter.json','a//b','a\\b','x:cmd',''])
def test_engine_registration_is_trusted_and_relative_only(bundle,descriptor):
    with pytest.raises(EngineError):NativeEngine(bundle['verifier'],product_adapter_descriptor=descriptor)


def test_engine_registration_rejects_an_unverified_callback():
    with pytest.raises(EngineError) as caught:NativeEngine(lambda *args:{},product_adapter_descriptor='installer/adapter.json')
    assert caught.value.code=='INTEGRITY_FAILED'


def test_default_engine_does_not_claim_product_adapter_migration_execution():
    target={'os':'linux','arch':'x64','mode':'native','scope':'user'}
    caps=NativeEngine().capabilities(target)['product_adapter']
    assert caps['registered'] is False and caps['migration_execution'] is False
    assert caps['migration_actions']==[]


def test_registered_adapter_does_not_claim_migration_on_unsupported_target(bundle):
    engine=NativeEngine(bundle['verifier'],product_adapter_descriptor='installer/adapter.json')
    caps=engine.capabilities({'os':'windows','arch':'x64','mode':'native','scope':'user'})
    assert caps['actions']['install']['supported'] is False
    assert caps['product_adapter']['migration_execution'] is False
    assert caps['product_adapter']['migration_actions']==[]


def test_discovery_documents_opt_in_maintenance_scope_without_migration():
    import json
    from pathlib import Path
    from homensai_install.api.v1 import InstallerAPI
    root=Path(__file__).resolve().parents[1]
    published=json.loads((root/'api/discovery.json').read_text())['product_adapter']
    assert published==InstallerAPI(root/'build/not-created').describe()['product_adapter']
    assert published['lifecycle_engine_actions']==['start','stop','backup']
    assert published['lifecycle_engine_registration'] is True
    assert published['migration_execution'] is False and published['default_registration'] is False
    assert (root/'api/discovery.json').read_bytes()==(root/'homensai_install/api/resources/discovery.json').read_bytes()


def test_standalone_reconciliation_does_not_create_a_fresh_missing_ledger(bundle):
    subject=adapter(bundle)
    with pytest.raises(InstallerError) as caught:subject.reconcile('never_invoked')
    assert caught.value.code=='STATE_STORAGE_UNAVAILABLE'
    assert not bundle['state_dir'].exists()


def test_existing_ledger_invocation_never_initializes_a_missing_database(bundle):
    from test_adapters_protocol import request
    with pytest.raises(InstallerError) as caught:adapter(bundle).invoke('health',request(),existing_ledger=True)
    assert caught.value.code=='STATE_STORAGE_UNAVAILABLE' and not bundle['app_state'].exists()
    assert not bundle['state_dir'].exists()


def test_fabricated_backup_receipt_never_authorizes_standalone_migration(bundle):
    from test_adapters_protocol import mutation
    subject = ExecutableProductAdapter(bundle['manifest'], bundle['target'], bundle['options'],
        verifier=bundle['verifier'], state_dir=bundle['state_dir'],
        environment={'HINSTALL_TEST_STATE': str(bundle['app_state'])})
    with pytest.raises(InstallerError) as caught:
        subject.invoke('migration_apply', mutation())
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert not bundle['app_state'].exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file identity/mode/symlink profile; native adapter execution is Linux only')
@pytest.mark.parametrize('change',['missing','replacement','hardlink','symlink','mode'])
def test_live_ledger_instance_rechecks_authoritative_file_before_transactions(tmp_path,change):
    import os,shutil
    from homensai_install.adapters import _Ledger
    ledger=_Ledger(tmp_path/'ledger')
    original=ledger.path.read_bytes()
    if change=='missing':ledger.path.unlink()
    elif change=='replacement':
        replacement=tmp_path/'replacement';replacement.write_bytes(original);replacement.chmod(0o600)
        ledger.path.unlink();replacement.rename(ledger.path)
    elif change=='hardlink':os.link(ledger.path,tmp_path/'linked')
    elif change=='symlink':
        source=tmp_path/'source';source.write_bytes(original);source.chmod(0o600)
        ledger.path.unlink();ledger.path.symlink_to(source)
    else:ledger.path.chmod(0o644)
    with pytest.raises(InstallerError) as caught:ledger.get('test')
    assert caught.value.code=='STATE_STORAGE_UNAVAILABLE'

