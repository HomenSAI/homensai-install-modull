"""Контракт единой операции установки и безопасная обработка ошибок."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from test_api_signed_preflight import signed_request
from test_update_assembly import assembled
from test_fresh_controller import workflow
from shared import api, fresh_controller
from shared.i18n import translate


def query_for(options, language='en'):
    params = dict(options)
    params['expected_product_id'] = params.pop('product_id')
    params['expected_version'] = params.pop('version')
    for name in ('envelope', 'trusted_public_key'):
        params[name] = base64.b64encode(params[name]).decode('ascii')
    params['modes'] = list(params['modes'])
    params.pop('language', None)
    return {'api_major': 1, 'request_id': 'controller-api-test', 'operation': 'install-portable-linux',
            'language': language, 'params': params}


def test_api_validates_schema_advertises_and_passes_exact_inputs(workflow, monkeypatch):
    validator = pytest.importorskip('jsonschema').Draft202012Validator(
        json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text()))
    query = query_for(workflow, 'de')
    assert validator.is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 31 and query['operation'] in caps['mutating_operations']
    seen = []
    def run(**kwargs):
        seen.append(kwargs)
        return {'state': 'recorded_complete', 'installation_complete': False}
    monkeypatch.setattr(fresh_controller, 'run_portable_linux_install', run)
    result = api.handle(query)
    assert result['ok'] is True and result['result']['installation_complete'] is False
    assert seen[0]['envelope'] == workflow['envelope'] and seen[0]['trusted_public_key'] == workflow['trusted_public_key']
    assert seen[0]['keys'] == workflow['keys'] and seen[0]['language'] == 'de'
    for edit in ({'workflow_key': workflow['keys']['start']}, {'modes': ['autonomous', 'autonomous']},
                 {'reserve_bytes': True}, {'unknown': 'value'}):
        invalid = {**query, 'params': {**query['params'], **edit}}
        assert api.handle(invalid)['error']['code'] == 'invalid_request'
    assert len(seen) == 1


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_api_errors_hide_input_and_do_not_claim_no_changes(workflow, monkeypatch, language):
    def fail(**kwargs):
        raise fresh_controller.FreshControllerError('synthetic_sensitive_detail')
    monkeypatch.setattr(fresh_controller, 'run_portable_linux_install', fail)
    query = query_for(workflow, language)
    result = api.handle(query)
    assert result['error']['code'] == 'fresh_controller_failed'
    assert result['error']['message'] == translate(language, 'api.fresh_controller_failed')
    assert workflow['install_root'] not in json.dumps(result) and 'synthetic_sensitive_detail' not in json.dumps(result)


def test_fresh_controller_rejects_unsafe_signed_wheel_before_any_step(workflow, signed_request, tmp_path, monkeypatch):
    from shared.package import build_package
    from shared.package_signing import sign_package
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = deepcopy(signed_request[2])
    source = tmp_path / 'payload'
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('../../outside', 'synthetic unsafe entry')
    artifact = manifest['runtime']['vendor_wheels'][0]
    (source / artifact['file']).write_bytes(wheel.getvalue())
    artifact['sha256'] = hashlib.sha256(wheel.getvalue()).hexdigest()
    package = tmp_path / 'unsafe-signed.zip'
    build_package(manifest, '2', source, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='2')
    options = {**workflow, 'package_path': str(package), 'envelope': envelope, 'trusted_public_key': public}
    work, units = Path(options['work_dir']), Path(options['unit_dir'])
    pointer = Path(options['install_root']) / 'services/active.json'
    original_pointer = pointer.read_bytes()
    def forbidden(request):
        pytest.fail('installation step executed before wheel audit')
    with monkeypatch.context() as patch:
        patch.setattr(api, 'handle', forbidden)
        with pytest.raises(fresh_controller.FreshControllerError) as failure:
            fresh_controller.run_portable_linux_install(**options)
    assert failure.value.code == 'wheel_audit_failed'
    for language in ('en', 'de', 'ru'):
        response = api.handle(query_for(options, language))
        assert response['error']['code'] == 'wheel_audit_failed'
        assert response['error']['message'] == translate(language, 'api.wheel_audit_failed')
        assert str(tmp_path) not in json.dumps(response)
    inspection = query_for(options)
    inspection['operation'] = 'inspect-portable-linux-install'
    assert api.handle(inspection)['result']['state'] == 'no_checkpoint'
    assert not list(work.glob('install-*.json')) and not list(units.iterdir())
    assert pointer.read_bytes() == original_pointer and not (tmp_path / 'outside').exists()


def test_api_disables_operation_without_crypto_or_supported_platform(workflow, monkeypatch):
    query = query_for(workflow)
    monkeypatch.setattr(api, '_signature_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'


def test_status_api_has_same_parameters_and_never_starts_installation(workflow, monkeypatch):
    query = query_for(workflow)
    query['operation'] = 'inspect-portable-linux-install'
    validator = pytest.importorskip('jsonschema').Draft202012Validator(
        json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text()))
    assert validator.is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 32 and query['operation'] in caps['mutating_operations']
    marker = Path(workflow['work_dir']) / ('install-' + workflow['workflow_key'] + '.json')
    response = api.handle(query)
    assert response['ok'] and response['result']['state'] == 'no_checkpoint'
    assert response['result']['resume_authorized'] is False and not marker.exists()
    def fail(request):
        return {'ok': False, 'error': {'code': 'fresh_prepare_failed'}}
    with monkeypatch.context() as patch:
        patch.setattr(api, 'handle', fail)
        with pytest.raises(fresh_controller.FreshControllerError):
            fresh_controller.run_portable_linux_install(**workflow)
    before = marker.read_bytes()
    response = api.handle(query)
    assert response['ok'] and response['result']['state'] == 'pending'
    assert response['result']['current_step'] == 'prepare-native-release'
    assert response['result']['step_may_have_executed'] and marker.read_bytes() == before
