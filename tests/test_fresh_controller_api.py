"""Контракт единой операции установки и безопасная обработка ошибок."""
import base64
import json
from pathlib import Path

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


def test_api_disables_operation_without_crypto_or_supported_platform(workflow, monkeypatch):
    query = query_for(workflow)
    monkeypatch.setattr(api, '_signature_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'
