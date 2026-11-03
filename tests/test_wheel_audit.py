"""Подписанный ZIP: проверка внутренних путей wheels без запуска кода."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import zipfile

import pytest

from homensai_installer_client import InstallerClient, contract_schema
from shared.api import handle
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.wheel_audit import WheelAuditError, audit_signed_wheels


ROOT = Path(__file__).resolve().parents[1]


def make_signed(tmp_path, *, entry='demo/__init__.py', extra=None, include_wheel=True):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = load_manifest(ROOT / 'examples/jobpilot.manifest.json')
    payload = tmp_path / 'payload'
    payload.mkdir()
    marker = tmp_path / 'code-executed'
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr(entry, f'open({str(marker)!r}, "w").write("executed")')
    if include_wheel:
        (payload / 'test-1-py3-none-any.whl').write_bytes(wheel.getvalue())
    else:
        (payload / 'readme.txt').write_text('no wheels')
    if extra is not None:
        (payload / 'additional.whl').write_bytes(extra)
    package = tmp_path / 'package.zip'
    build_package(manifest, '1', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(public).decode(),
              'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    return params, package, marker


def query(params):
    return {'api_major': 1, 'request_id': 'wheel-audit', 'operation': 'audit-signed-wheels', 'params': params}


def test_signed_audit_checks_wheel_paths_without_running_code(tmp_path):
    params, package, marker = make_signed(tmp_path)
    before = package.read_bytes()
    request = query(params)
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    assert validator.is_valid(request)
    result = handle(request)['result']
    assert result['wheel_count'] == 1 and result['package_signature_verified'] and result['wheel_paths_checked']
    assert not result['malicious_code_scanned'] and not result['wheel_format_completeness_verified']
    assert not result['publisher_trust_verified'] and not result['execution_authorized']
    assert InstallerClient(timeout=90).call(request['operation'], params, min_minor=48) == result
    assert package.read_bytes() == before and not marker.exists()


@pytest.mark.parametrize('entry', ['../../outside', '/absolute', 'demo\\escape'])
def test_correctly_signed_package_with_unsafe_wheel_is_rejected(tmp_path, entry):
    params, _, marker = make_signed(tmp_path, entry=entry)
    response = handle(query(params))
    assert response['error']['code'] == 'verification_failed' and str(tmp_path) not in json.dumps(response)
    assert not marker.exists() and not (tmp_path / 'outside').exists()


def test_audit_checks_undeclared_wheels_too(tmp_path):
    params, _, marker = make_signed(tmp_path, extra=b'not a zip')
    assert handle(query(params))['error']['code'] == 'verification_failed'
    assert not marker.exists()


def test_audit_explicitly_reports_no_wheels(tmp_path):
    params, _, _ = make_signed(tmp_path, include_wheel=False)
    result = handle(query(params))['result']
    assert result['wheel_count'] == 0 and not result['malicious_code_scanned']
    assert not result['execution_authorized']


def test_audit_rejects_wrong_key_and_tampering(tmp_path):
    params, package, _ = make_signed(tmp_path)
    wrong = deepcopy(params)
    wrong['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    assert handle(query(wrong))['error']['code'] == 'verification_failed'
    package.write_bytes(package.read_bytes() + b'tamper')
    assert handle(query(params))['error']['code'] == 'verification_failed'


def test_audit_rejects_package_replacement_after_signature_check(tmp_path, monkeypatch):
    params, package, _ = make_signed(tmp_path)
    from shared import wheel_audit
    original = wheel_audit.verify_signed_package
    def replace_after_verification(*args, **kwargs):
        result = original(*args, **kwargs)
        package.write_bytes(package.read_bytes() + b'changed after verification')
        return result
    monkeypatch.setattr(wheel_audit, 'verify_signed_package', replace_after_verification)
    with pytest.raises(WheelAuditError):
        audit_signed_wheels(package, base64.b64decode(params['envelope']),
                            trusted_public_key=base64.b64decode(params['trusted_public_key']),
                            expected_product_id=params['expected_product_id'], expected_version='1')


def test_audit_enforces_resource_limit_and_unavailable_crypto(tmp_path, monkeypatch):
    params, _, _ = make_signed(tmp_path)
    from shared import wheel_audit
    monkeypatch.setattr(wheel_audit, 'MAX_WHEEL_BYTES', 1)
    assert handle(query(params))['error']['code'] == 'verification_failed'
    monkeypatch.setattr('shared.api._signature_available', lambda: False)
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert 'audit-signed-wheels' not in caps['operations']
    assert handle(query(params))['error']['code'] == 'unsupported_operation'
