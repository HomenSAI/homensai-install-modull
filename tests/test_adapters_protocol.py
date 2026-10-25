"""Реальные signed synthetic subprocess, без установки продукта и без сети."""

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from homensai_install.adapters import ExecutableProductAdapter, HOOKS, ProductAdapter
from homensai_install.api.model import digest, target_model
from homensai_install.errors import InstallerError
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r'''
import hashlib, json, os, pathlib, sys, time
request = json.load(sys.stdin)
hook = request['hook']
data = request['data']
mode = data.get('mode')
state_file = pathlib.Path(os.environ['HINSTALL_TEST_STATE'])
state = json.loads(state_file.read_text()) if state_file.exists() else {'calls': 0, 'applied': {}}
state['calls'] += 1
def save():
    state_file.write_text(json.dumps(state))
save()
if mode == 'timeout':
    time.sleep(5)
if mode == 'overflow':
    sys.stdout.write('x' * 400000)
    sys.stdout.flush()
    time.sleep(5)
if mode == 'nonzero':
    sys.stderr.write(data['secret'])
    sys.exit(17)
if mode == 'malformed':
    print('{"duplicate":1,"duplicate":2}')
    sys.exit(0)
if hook == 'describe':
    descriptor = json.loads(pathlib.Path('installer/adapter.json').read_text())
    result = {'protocol_version': '1.0', 'product_id': request['product_id'],
              'adapter_version': descriptor['adapter_version'], 'capabilities': sorted(descriptor['capabilities'])}
elif hook in ('readiness', 'health'):
    clean = 'UNDECLARED_PARENT_SECRET' not in os.environ and 'PYTHONPATH' not in os.environ and 'PATH' not in os.environ
    result = {'ready': mode != 'not_ready', 'checks': [
        {'id': os.environ.get('HINSTALL_ALLOWED', 'clean_env') if mode == 'echo_environment' else 'clean_env',
         'status': 'ok' if clean and mode != 'not_ready' else 'failed'}]}
elif hook == 'migration_plan':
    result = {'from_version': 'v1', 'to_version': 'v2',
              'steps': [{'id': 'add_column', 'backup_required': True, 'reversible': True}]}
    result['plan_digest'] = hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
elif hook == 'operation_status':
    old = state['applied'].get(request['idempotency_key'])
    result = {'outcome': 'applied' if old else 'not_applied', 'request_digest': request['request_digest']}
    if old: result['result'] = old['result']
elif hook == 'quiesce':
    result = {'quiesced': True, 'active_writers': 0}
elif hook == 'backup':
    result = {'artifact_id': 'synthetic_archive', 'sha256': hashlib.sha256(b'synthetic bytes').hexdigest()}
else:
    result = {'applied': True, 'receipt_id': 'synthetic_receipt', 'state_version': 'v2'}
    state['applied'][request['idempotency_key']] = {'result': result, 'request_digest': request['request_digest']}
    save()
    if mode == 'fail_after_apply':
        sys.exit(18)
response = {key: request[key] for key in ('protocol_version','product_id','request_id','request_digest','hook')}
response.update(status='ok', result=result)
if mode == 'wrong_request': response['request_id'] = 'adapter_wrong'
if mode == 'extra_field': response['stdout'] = 'must not escape'
print(json.dumps(response))
'''


def resign(value):
    payload = value['payload']
    inventory = {'schema_version': 1, 'product_id': value['manifest']['product_id'], 'release': '0.0.1',
                 'target': {key: value['target'][key] for key in ('os','arch','mode')},
                 'files': {path.relative_to(payload).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted(payload.rglob('*')) if path.is_file()},
                 'development': True, 'manifest_sha256': hash_manifest(value['manifest']),
                 'runtime_executable': 'runtime/python'}
    signature = {'algorithm': 'ed25519', 'key_id': 'ephemeral-test',
                 'signature': base64.b64encode(value['private'].sign(canonical_bytes(inventory))).decode()}
    Path(value['options']['inventory_file']).write_bytes(canonical_bytes(inventory))
    Path(value['options']['signature_file']).write_bytes(canonical_bytes(signature))


@pytest.fixture
def bundle(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Linux process-group implementation; Windows execution explicitly unsupported')
    payload = tmp_path / 'payload'
    (payload / 'runtime').mkdir(parents=True)
    (payload / 'installer').mkdir()
    # Это signed executable для protocol test; не доказательство замкнутого production runtime.
    runtime_source = Path('/usr/bin/python3')
    if not runtime_source.exists():
        pytest.skip('Protocol fixture needs system CPython with installed stdlib; embedded runtime acceptance is separate')
    shutil.copy2(runtime_source.resolve(), payload / 'runtime/python')
    (payload / 'runtime/python').chmod(0o700)
    (payload / 'adapter.py').write_text(SCRIPT)
    descriptor = {'schema_version': 1, 'protocol_version': '1.0', 'product_id': 'adapter-test',
                  'adapter_id': 'product', 'adapter_version': '0.1.0',
                  'command': ['runtime/python', '-I', '-B', 'adapter.py'],
                  'environment_allowlist': ['HINSTALL_TEST_STATE', 'HINSTALL_ALLOWED'],
                  'capabilities': {hook: {'mutating': mutating, 'idempotency': 'application' if mutating else 'read_only'}
                                   for hook, mutating in HOOKS.items()}}
    (payload / 'installer/adapter.json').write_bytes(canonical_bytes(descriptor))
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text())
    manifest['product_id'] = 'adapter-test'
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    value = {'payload': payload, 'private': private, 'manifest': manifest, 'target': target_model(),
             'options': {'payload_dir': str(payload), 'inventory_file': str(tmp_path / 'inventory.json'),
                         'signature_file': str(tmp_path / 'signature.json'), 'release': '0.0.1',
                         'runtime_executable': 'runtime/python'},
             'state_dir': tmp_path / 'ledger', 'app_state': tmp_path / 'app-state.json',
             'verifier': SignedPayloadVerifier({'ephemeral-test': public}, allow_test_keys=True)}
    resign(value)
    return value


def adapter(bundle, **kwargs):
    environment = {'HINSTALL_TEST_STATE': str(bundle['app_state']), **kwargs.pop('environment', {})}
    return ExecutableProductAdapter(bundle['manifest'], bundle['target'], bundle['options'],
        verifier=bundle['verifier'], state_dir=bundle['state_dir'], environment=environment, **kwargs)


def request(key='one', *, phase='execute', **data):
    return {'operation_id': 'op_synthetic', 'idempotency_key': key, 'phase': phase, 'data': data}


def mutation(key='one', *, phase='execute', **data):
    return request(key, phase=phase, plan_digest=digest({'synthetic': 'migration'}),
                   backup_receipt_id='synthetic_backup_receipt', **data)


def test_signed_capabilities_are_static_and_protocol_compatible(bundle, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('capabilities must not execute product hooks')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    subject = adapter(bundle)
    assert isinstance(subject, ProductAdapter)
    capabilities = subject.capabilities()
    assert capabilities['capability_source'] == 'signed_descriptor'
    assert capabilities['hooks']['migration_apply']['mutating']
    assert not bundle['state_dir'].exists() and not bundle['app_state'].exists()


def test_real_handshake_health_migration_plan_and_allowlisted_environment(bundle, monkeypatch):
    monkeypatch.setenv('UNDECLARED_PARENT_SECRET', 'private-parent-value')
    monkeypatch.setenv('PYTHONPATH', '/untrusted/path')
    subject = adapter(bundle)
    description = subject.invoke('describe', request('describe', phase='diagnostic'))
    assert description['result']['protocol_version'] == '1.0'
    health = subject.invoke('readiness', request('health', phase='diagnostic'))
    assert health['result']['ready'] is True
    plan = subject.invoke('migration_plan', request('plan', phase='diagnostic'))['result']
    assert plan['steps'][0]['backup_required']
    assert plan['plan_digest'] == digest({key: value for key, value in plan.items() if key != 'plan_digest'})


def test_success_replay_survives_new_adapter_without_repeating_effect(bundle):
    result = adapter(bundle).invoke('migration_apply', mutation())
    again = adapter(bundle).invoke('migration_apply', mutation())
    assert not result['replayed'] and again['replayed']
    assert result['result'] == again['result']
    assert json.loads(bundle['app_state'].read_text())['calls'] == 1
    with pytest.raises(InstallerError) as caught:
        adapter(bundle).invoke('migration_apply', mutation(changed=True))
    assert caught.value.code == 'OPERATION_CONFLICT'


def test_unknown_effect_requires_explicit_reconciliation_then_cached_result(bundle):
    subject = adapter(bundle)
    req = mutation(mode='fail_after_apply')
    with pytest.raises(InstallerError) as caught:
        subject.invoke('migration_apply', req)
    assert caught.value.code == 'ADAPTER_EXECUTION_FAILED'
    with pytest.raises(InstallerError) as caught:
        adapter(bundle).invoke('migration_apply', req)
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert json.loads(bundle['app_state'].read_text())['calls'] == 1
    reconciled = adapter(bundle).reconcile('one')
    assert reconciled['outcome'] == 'applied'
    final = adapter(bundle).invoke('migration_apply', req)
    assert final['replayed'] and final['result']['applied']
    assert json.loads(bundle['app_state'].read_text())['calls'] == 2


def test_not_applied_reconciliation_requires_explicit_resume_phase(bundle):
    subject = adapter(bundle)
    # Ошибка до mutation; synthetic script не создал business receipt.
    req = mutation(mode='nonzero', secret='must-never-appear-in-public-error')
    with pytest.raises(InstallerError): subject.invoke('migration_apply', req)
    assert subject.reconcile('one')['outcome'] == 'not_applied'
    with pytest.raises(InstallerError) as caught:
        subject.invoke('migration_apply', req)
    assert caught.value.code == 'RECOVERY_REQUIRED'
    resumed = deepcopy(req)
    resumed['phase'] = 'resume'
    with pytest.raises(InstallerError) as caught:
        subject.invoke('migration_apply', resumed)
    assert caught.value.code == 'ADAPTER_EXECUTION_FAILED'
    assert json.loads(bundle['app_state'].read_text())['calls'] == 3


@pytest.mark.parametrize('mode,code', [('timeout','ADAPTER_TIMEOUT'), ('overflow','ADAPTER_PROTOCOL_INVALID'),
    ('nonzero','ADAPTER_EXECUTION_FAILED'), ('malformed','ADAPTER_PROTOCOL_INVALID'),
    ('wrong_request','ADAPTER_PROTOCOL_INVALID'), ('extra_field','ADAPTER_PROTOCOL_INVALID')])
def test_timeout_output_and_response_fail_closed_without_output_dumps(bundle, mode, code):
    subject = adapter(bundle, timeout=0.25 if mode == 'timeout' else 2)
    marker = 'private-output-must-never-escape'
    with pytest.raises(InstallerError) as caught:
        subject.invoke('health', request(mode=mode, secret=marker))
    assert caught.value.code == code
    assert marker not in json.dumps(caught.value.to_dict())
    assert marker.encode() not in (bundle['state_dir'] / 'adapter-invocations.sqlite3').read_bytes()
    with pytest.raises(InstallerError) as replay:
        subject.invoke('health', request(mode=mode, secret=marker))
    assert replay.value.code == 'RECOVERY_REQUIRED'


@pytest.mark.parametrize('phase', ['plan','validate','discover','preview'])
def test_product_execution_rejected_in_planning_phases(bundle, phase):
    with pytest.raises(InstallerError):
        adapter(bundle).invoke('migration_apply', mutation(phase=phase))
    assert not bundle['app_state'].exists() and not bundle['state_dir'].exists()


def test_mutation_requires_migration_digest_backup_and_correct_phase(bundle):
    for req in (request(), mutation(phase='diagnostic')):
        with pytest.raises(InstallerError): adapter(bundle).invoke('migration_apply', req)
    assert not bundle['app_state'].exists()


@pytest.mark.parametrize('change', ['bytes','descriptor','signature','extra_file'])
def test_unsigned_or_modified_code_never_executes(bundle, change):
    subject = adapter(bundle)
    if change == 'bytes': (bundle['payload'] / 'adapter.py').write_text('raise SystemExit(0)')
    elif change == 'descriptor': (bundle['payload'] / 'installer/adapter.json').write_text('{}')
    elif change == 'signature': Path(bundle['options']['signature_file']).write_text('{}')
    else: (bundle['payload'] / 'injected.py').write_text('print(1)')
    with pytest.raises(InstallerError) as caught:
        subject.invoke('health', request())
    assert caught.value.code == 'INTEGRITY_FAILED'
    assert not bundle['app_state'].exists() and not bundle['state_dir'].exists()


def test_loader_environment_and_unknown_environment_fail_closed(bundle):
    with pytest.raises(InstallerError): adapter(bundle, environment={'UNDECLARED': 'value'})
    path = bundle['payload'] / 'installer/adapter.json'
    descriptor = json.loads(path.read_text())
    descriptor['environment_allowlist'].append('LD_PRELOAD')
    path.write_bytes(canonical_bytes(descriptor))
    resign(bundle)
    with pytest.raises(InstallerError): adapter(bundle)
    assert not bundle['app_state'].exists()


def test_known_environment_secret_cannot_become_protocol_identifier(bundle):
    marker = 'private-allowed-environment-value'
    with pytest.raises(InstallerError) as caught:
        adapter(bundle, environment={'HINSTALL_ALLOWED': marker}).invoke('health', request(mode='echo_environment'))
    assert caught.value.code == 'ADAPTER_PROTOCOL_INVALID'
    assert marker not in json.dumps(caught.value.to_dict())
    assert marker.encode() not in (bundle['state_dir'] / 'adapter-invocations.sqlite3').read_bytes()


def test_transport_ok_does_not_invent_health_success(bundle):
    result = adapter(bundle).invoke('health', request(mode='not_ready'))
    assert result['result']['ready'] is False


def test_unsigned_bool_verifier_and_unsupported_platform_are_rejected(bundle):
    with pytest.raises(InstallerError):
        ExecutableProductAdapter(bundle['manifest'], bundle['target'], bundle['options'],
                                 verifier=lambda *args: {}, state_dir=bundle['state_dir'])
    bundle['target']['os'] = 'windows'
    resign(bundle)
    subject = adapter(bundle)
    with pytest.raises(InstallerError) as caught:
        subject.invoke('health', request())
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert not bundle['state_dir'].exists()


@pytest.mark.parametrize('field,value', [('mode','docker'), ('scope','service')])
def test_unimplemented_target_never_falls_back_to_host_execution(bundle, field, value):
    bundle['target'][field] = value
    resign(bundle)
    with pytest.raises(InstallerError) as caught:
        adapter(bundle).invoke('health', request())
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert not bundle['state_dir'].exists() and not bundle['app_state'].exists()
