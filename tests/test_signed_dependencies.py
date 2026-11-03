"""План зависимостей строится только из проверенного подписанного пакета."""
import base64
from copy import deepcopy
import json
from pathlib import Path

import pytest

from homensai_installer_client import InstallerClient, contract_schema
from shared.api import handle
from test_api_signed_preflight import signed_request


def plan_request(signed_request):
    query, _, _ = signed_request
    query = deepcopy(query)
    query['operation'] = 'plan-signed-target-dependencies'
    for field in ('install_root', 'required_bytes', 'reserve_bytes'):
        del query['params'][field]
    return query


def test_signed_plan_uses_package_manifest_and_verified_artifacts(signed_request):
    query = plan_request(signed_request)
    original, path, manifest = signed_request
    before = path.read_bytes()
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    assert validator.is_valid(query)
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 47 and query['operation'] in caps['operations']
    assert query['operation'] not in caps['mutating_operations']
    response = handle(query)
    result = response['result']
    assert result['compatibility'] == 'supported' and not result['execution_authorized']
    assert result['package_signature_verified'] and result['package_artifact_hashes_verified']
    assert result['native_lock_candidate'] == manifest['runtime']['lock_files']['linux']
    assert 'security' in result['not_checked'] and 'artifact_contents' not in result['not_checked']
    assert InstallerClient(timeout=90).call(query['operation'], query['params'], min_minor=47) == result
    assert path.read_bytes() == before and not Path(original['params']['install_root']).exists()


def test_signed_plan_does_not_select_unsupported_targets_or_accept_overrides(signed_request):
    query = plan_request(signed_request)
    query['params']['target']['os'] = 'windows'
    result = handle(query)['result']
    assert result['compatibility'] == 'unsupported' and result['native_lock_candidate'] is None
    query['params']['manifest'] = signed_request[2]
    assert handle(query)['error']['code'] == 'invalid_request'
    assert not pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request')).is_valid(query)


def test_signed_plan_rejects_wrong_key_and_tampered_package(signed_request):
    query = plan_request(signed_request)
    path = signed_request[1]
    trusted = query['params']['trusted_public_key']
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    assert handle(query)['error']['code'] == 'verification_failed'
    query['params']['trusted_public_key'] = trusted
    path.write_bytes(path.read_bytes() + b'synthetic tampering')
    response = handle(query)
    assert response['error']['code'] == 'verification_failed' and str(path) not in json.dumps(response)


def test_signed_plan_not_advertised_without_signature_support(signed_request, monkeypatch):
    query = plan_request(signed_request)
    monkeypatch.setattr('shared.api._signature_available', lambda: False)
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert handle(query)['error']['code'] == 'unsupported_operation'
