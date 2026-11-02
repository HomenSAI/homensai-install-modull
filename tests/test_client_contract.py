"""Versioned client contract snapshots must match the server sources."""
import json
from importlib import resources
from pathlib import Path

import pytest

from homensai_installer_client import CONTRACT_API_MAJOR, CONTRACT_API_MINOR, contract_schema
from shared import api

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_contract_matches_server_and_api_version():
    assert (CONTRACT_API_MAJOR, CONTRACT_API_MINOR) == (api.API_MAJOR, api.API_MINOR)
    for name in ('request', 'response'):
        filename = 'api-v1-' + name + '.schema.json'
        packed = resources.files('homensai_installer_client').joinpath('schemas', filename).read_bytes()
        assert packed == (ROOT / 'shared' / filename).read_bytes()
        assert contract_schema(name) == json.loads(packed)
    with pytest.raises(ValueError):
        contract_schema('../client.py')


def test_bundled_response_schema_covers_success_and_error_envelopes():
    jsonschema = pytest.importorskip('jsonschema')
    schema = contract_schema('response')
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    responses = [
        api.handle({'api_major': 1, 'operation': 'capabilities', 'request_id': 'contract', 'params': {}}),
        api.handle({'api_major': 2, 'operation': 'capabilities', 'request_id': 'contract', 'params': {}}),
        api.handle({'invalid': True}),
        api.handle({'api_major': 1, 'operation': 'validate', 'request_id': 'contract', 'params': {}}),
    ]
    for response in responses:
        assert validator.is_valid(response)
    assert validator.is_valid({**responses[0], 'future_field': True})
    assert not validator.is_valid({**responses[0], 'error': {'code': 'bad', 'message': '', 'retryable': False}})
    assert not validator.is_valid({**responses[1], 'result': {}})
    assert not validator.is_valid({**responses[0], 'request_id': None})


def test_bundled_request_schema_covers_advertised_operations():
    jsonschema = pytest.importorskip('jsonschema')
    schema = contract_schema('request')
    jsonschema.Draft202012Validator.check_schema(schema)
    capabilities = api.handle({'api_major': 1, 'operation': 'capabilities', 'request_id': 'contract', 'params': {}})
    assert set(capabilities['result']['operations']) <= set(schema['properties']['operation']['enum'])
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid({'api_major': 1, 'operation': 'capabilities', 'request_id': 'contract', 'params': {}})
    assert not validator.is_valid({'api_major': 1, 'operation': 'capabilities', 'request_id': 'contract', 'params': {'unexpected': 1}})
