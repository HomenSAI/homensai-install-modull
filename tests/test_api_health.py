"""Публичный локальный health-контракт; сеть проверяется отдельно в E2E."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from shared.api import handle
from shared.health import HealthError

ROOT = Path(__file__).resolve().parents[1]


def request():
    return {'api_major': 1, 'request_id': 'sample', 'operation': 'check-local-health', 'language': 'en',
            'params': {'port': 12345, 'product_id': 'homensai-test-demo', 'version': '1',
                       'path': '/health', 'timeout_seconds': 3}}


def test_health_advertised_without_signature_dependency(monkeypatch):
    monkeypatch.setattr('shared.api._signature_available', lambda: False)
    caps = handle({'api_major': 1, 'request_id': 't', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 12 and 'check-local-health' in caps['operations']
    assert 'check-local-health' not in caps['mutating_operations']


def test_health_passes_exact_limited_contract(monkeypatch):
    seen = []
    monkeypatch.setattr('shared.api.check_local_health', lambda *a, **kw: seen.append((a, kw)))
    query = request()
    before = deepcopy(query)
    response = handle(query)
    assert response['ok'] and response['result'] == {'health_response_matches': True, 'process_identity_verified': False}
    assert seen == [((12345,), {'product_id': 'homensai-test-demo', 'version': '1', 'path': '/health', 'timeout': 3})]
    assert query == before


@pytest.mark.parametrize('field,value', [('port', True), ('port', 0), ('port', 65536),
    ('port', 'secret'), ('product_id', '../secret'), ('version', '../secret'),
    ('path', 'http://example.com/secret'), ('path', '/health?secret'),
    ('timeout_seconds', True), ('timeout_seconds', 31), ('timeout_seconds', 0)])
def test_invalid_health_parameters_do_not_call_probe_or_echo(monkeypatch, field, value):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid request must not probe')
    monkeypatch.setattr('shared.api.check_local_health', forbidden)
    query = request()
    query['params'][field] = value
    result = handle(query)
    assert result['error']['code'] == 'invalid_request'
    assert 'secret' not in json.dumps(result)


def test_health_failure_is_localized_and_does_not_echo(monkeypatch):
    def fail(*args, **kwargs):
        raise HealthError('SECRET-SENTINEL')
    monkeypatch.setattr('shared.api.check_local_health', fail)
    query = request()
    response = handle(query)
    assert response['error']['code'] == 'health_check_failed'
    assert response['error']['message'] == 'Could not confirm product health'
    assert 'SECRET-SENTINEL' not in json.dumps(response)
    query['language'] = 'de'
    assert handle(query)['error']['message'] == 'Der Produktzustand konnte nicht bestätigt werden'


def test_health_schema_and_api_agree():
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text())
    validator = jsonschema.Draft202012Validator(schema)
    query = request()
    assert validator.is_valid(query)
    query['params']['port'] = True
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'


def test_health_failure_returns_operational_cli_code(monkeypatch):
    import io
    import sys
    import shared.api as api

    def fail(*args, **kwargs):
        raise HealthError('SECRET-SENTINEL')
    data = io.TextIOWrapper(io.BytesIO(json.dumps(request()).encode('utf-8')), encoding='utf-8')
    output = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(api, 'check_local_health', fail)
        patch.setattr(sys, 'argv', ['shared.api'])
        patch.setattr(sys, 'stdin', data)
        patch.setattr(sys, 'stdout', output)
        assert api.main() == 1
    assert json.loads(output.getvalue())['error']['code'] == 'health_check_failed'
    assert 'SECRET-SENTINEL' not in output.getvalue()
