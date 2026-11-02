"""Тестовый продукт вызывает публичный CLI установщика, без реальных установок."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from examples.demo_product.installer_client import InstallerClient, InstallerClientError

ROOT = Path(__file__).resolve().parents[1]


def test_real_client_negotiates_and_validates_product_manifest():
    client = InstallerClient(timeout=30)
    capabilities = client.capabilities()
    assert capabilities['api_major'] == 1 and capabilities['api_minor'] >= 10
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text())
    result = client.call('validate', {'manifest': manifest}, min_minor=0, language='de')
    assert result == {'product_id': manifest['product_id'], 'schema_version': 2}
    with pytest.raises(InstallerClientError) as exc:
        client.call('validate', {'manifest': {}}, language='en')
    assert exc.value.code == 'invalid_request'
    assert str(exc.value) == 'Manifest does not meet the contract'


def test_missing_capability_stops_before_action():
    client = InstallerClient(timeout=30)
    with pytest.raises(InstallerClientError) as exc:
        client.call('future-action', {}, min_minor=999)
    assert exc.value.code == 'incompatible_api'


def test_client_does_not_pass_secrets_or_use_shell(monkeypatch):
    monkeypatch.setenv('SYNTHETIC_SECRET', 'sensitive')
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        request = json.loads(kwargs['input'])
        result = {'api_major': 1, 'api_minor': 10, 'operations': ['validate'], 'languages': ['en']}
        if request['operation'] == 'validate':
            result = {'product_id': 'demo'}
        return SimpleNamespace(returncode=0, stdout=json.dumps({'api_major': 1, 'request_id': request['request_id'],
                                                                 'ok': True, 'result': result}).encode(), stderr=b'')
    monkeypatch.setattr('examples.demo_product.installer_client.subprocess.run', run)
    assert InstallerClient().call('validate', {'manifest': {}}, language='en') == {'product_id': 'demo'}
    assert len(seen) == 2
    for argv, kwargs in seen:
        assert argv[1:] == ['-B', '-m', 'shared.api']
        assert kwargs['shell'] is False and 'SYNTHETIC_SECRET' not in kwargs['env']
        assert b'sensitive' not in kwargs['input']


@pytest.mark.parametrize('response', [b'{}', b'{"api_major":1,"api_major":1}', b'bad', b'x' * (1024 * 1024 + 1)])
def test_invalid_response_does_not_disclose_stderr(monkeypatch, response):
    monkeypatch.setattr('examples.demo_product.installer_client.subprocess.run',
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=response, stderr=b'SECRET-SENTINEL'))
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().capabilities()
    assert exc.value.code == 'transport_error' and 'SECRET-SENTINEL' not in str(exc.value)


def test_timeout_does_not_retry_or_disclose_error(monkeypatch):
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise subprocess.TimeoutExpired('SECRET-SENTINEL', 1)
    monkeypatch.setattr('examples.demo_product.installer_client.subprocess.run', fail)
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().call('prepare-native-release', {})
    assert exc.value.code == 'transport_error' and 'SECRET-SENTINEL' not in str(exc.value)
    assert len(calls) == 1


def test_uncertain_mutating_call_is_not_retried(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input'])
        calls.append(request['operation'])
        if request['operation'] == 'capabilities':
            response = {'api_major': 1, 'request_id': request['request_id'], 'ok': True,
                        'result': {'api_major': 1, 'api_minor': 10,
                                   'operations': ['prepare-native-release'], 'languages': ['en']}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(response).encode(), stderr=b'')
        raise subprocess.TimeoutExpired('synthetic', 1)
    monkeypatch.setattr('examples.demo_product.installer_client.subprocess.run', run)
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().call('prepare-native-release', {'idempotency_key': 'a' * 32}, min_minor=8)
    assert exc.value.code == 'transport_error'
    assert calls == ['capabilities', 'prepare-native-release']


def test_wrong_response_request_id_is_rejected(monkeypatch):
    response = {'api_major': 1, 'request_id': 'unrelated', 'ok': True, 'result': {}}
    monkeypatch.setattr('examples.demo_product.installer_client.subprocess.run',
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(response).encode(), stderr=b''))
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().capabilities()
    assert exc.value.code == 'transport_error'
