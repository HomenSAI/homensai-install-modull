"""Совместимость проверяется по подписанному манифесту, не данным клиента."""
import base64
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from shared.api import handle
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.preflight import NAMES

ROOT = Path(__file__).resolve().parents[1]
TARGET = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}


@pytest.fixture
def signed_request(tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [deepcopy(TARGET)]}
    payload = tmp_path / 'payload'
    payload.mkdir()
    artifacts = [*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]
    for index, artifact in enumerate(artifacts):
        data = ('fictional test artifact ' + str(index)).encode()
        file = payload / artifact['file']
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        artifact['sha256'] = sha256(data).hexdigest()
    path = tmp_path / 'bundle.zip'
    build_package(manifest, '1', payload, path)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(path, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    query = {'api_major': 1, 'request_id': 'test', 'operation': 'inspect-fresh-package', 'params': {
        'package_path': str(path), 'envelope': base64.b64encode(envelope).decode(),
        'trusted_public_key': base64.b64encode(public).decode(), 'expected_product_id': manifest['product_id'],
        'expected_version': '1', 'target': deepcopy(TARGET), 'install_root': str(tmp_path / 'install'),
        'required_bytes': {name: 0 for name in NAMES}, 'reserve_bytes': 0}}
    return query, path, manifest


def test_signed_preflight_python_and_cli_are_read_only(signed_request):
    query, path, _ = signed_request
    response = handle(query)
    assert response['ok'] and response['result']['declared_target'] == 'supported'
    assert response['result']['preflight']['checks_passed']
    assert 'platform_compatibility' in response['result']['preflight']['not_checked']
    result = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                            input=json.dumps(query).encode(), capture_output=True, timeout=30)
    assert result.returncode == 0 and json.loads(result.stdout)['result']['product_id'] == response['result']['product_id']
    assert not Path(query['params']['install_root']).exists()
    assert path.exists()


def test_cannot_override_signed_manifest_or_select_undeclared_target(signed_request):
    query, _, manifest = signed_request
    query['params']['target']['execution'] = 'docker'
    assert handle(query)['error']['code'] == 'target_not_supported'
    manifest['compatibility']['targets'].append(deepcopy(query['params']['target']))
    query['params']['manifest'] = manifest
    assert handle(query)['error']['code'] == 'invalid_request'


def test_untrusted_key_tampering_and_target_failure_do_not_touch_destination(signed_request):
    query, path, _ = signed_request
    public_key = query['params']['trusted_public_key']
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    assert handle(query)['error']['code'] == 'verification_failed'
    query['params']['target']['os'] = 'SECRET-SENTINEL'
    response = handle(query)
    assert response['error']['code'] == 'verification_failed'
    assert 'SECRET-SENTINEL' not in json.dumps(response)
    assert not Path(query['params']['install_root']).exists()
    query['params']['trusted_public_key'] = public_key
    query['params']['target'] = deepcopy(TARGET)
    path.write_bytes(path.read_bytes() + b'tamper')
    assert handle(query)['error']['code'] == 'verification_failed'


def test_absent_compatibility_is_not_treated_as_supported(signed_request, monkeypatch):
    query, _, manifest = signed_request
    del manifest['compatibility']
    monkeypatch.setattr('shared.api.verify_signed_package', lambda *args, **kwargs: {
        'manifest': manifest, 'product_id': manifest['product_id'], 'version': '1', 'files': []})
    assert handle(query)['error']['code'] == 'target_not_supported'


def test_operation_is_unavailable_without_signature_support(signed_request, monkeypatch):
    query, _, _ = signed_request
    monkeypatch.setattr('shared.api._signature_available', lambda: False)
    capabilities = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})
    assert 'inspect-fresh-package' not in capabilities['result']['operations']
    assert handle(query)['error']['code'] == 'unsupported_operation'
