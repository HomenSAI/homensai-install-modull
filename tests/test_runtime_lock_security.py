"""Tie offline vulnerability evidence to the selected signed runtime lock."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from shared import osv_audit, runtime_lock_security
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def signed_runtime(tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    payload = tmp_path / 'payload'
    payload.mkdir()
    contents = {'linux': b'example-package==1.0 --hash=sha256:' + b'a' * 64 + b'\n',
                'windows': b'example-package==2.0 --hash=sha256:' + b'b' * 64 + b'\n'}
    for name, artifact in manifest['runtime']['lock_files'].items():
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[name])
        artifact['sha256'] = hashlib.sha256(contents[name]).hexdigest()
    for artifact in manifest['runtime']['vendor_wheels']:
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic wheel')
        artifact['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    package = tmp_path / 'release.zip'
    build_package(manifest, '1', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    lock = tmp_path / 'audited.lock'
    lock.write_bytes(contents['linux'])
    report = osv_audit.audit([str(lock)], send=lambda _: {'results': [{}]})
    evidence = json.dumps(report).encode()
    return dict(package=package, envelope=envelope, trusted_public_key=public, product_id=manifest['product_id'],
                version='1', platform='linux', lock=lock, evidence=evidence,
                evidence_sha256=hashlib.sha256(evidence).hexdigest(), max_age_seconds=60)


def test_signed_runtime_lock_matches_only_selected_audited_input(signed_runtime):
    result = runtime_lock_security.verify_signed_runtime_lock(**signed_runtime)
    assert result['signed_package_verified'] and result['selected_lock_matches_evidence']
    assert result['package_count'] == 1 and result['platform'] == 'linux'
    assert not result['complete_dependency_inventory'] and not result['malware_scanned'] and not result['release_safe']


@pytest.mark.parametrize('change', ['key', 'version', 'digest', 'platform', 'unknown_platform', 'lock', 'package', 'advisory'])
def test_inconsistent_inputs_are_rejected(signed_runtime, change):
    args = dict(signed_runtime)
    if change == 'key':
        args['trusted_public_key'] = b'x' * 32
    elif change == 'version':
        args['version'] = '2'
    elif change == 'digest':
        args['evidence_sha256'] = '0' * 64
    elif change == 'platform':
        args['platform'] = 'windows'
    elif change == 'unknown_platform':
        args['platform'] = 'docker'
    elif change == 'lock':
        args['lock'].write_bytes(args['lock'].read_bytes() + b'#changed\n')
    elif change == 'package':
        args['package'].write_bytes(args['package'].read_bytes() + b'tampered')
    else:
        report = json.loads(args['evidence'])
        report['packages'][0]['advisories'] = [{'id': 'GHSA-test'}]
        args['evidence'] = json.dumps(report).encode()
        args['evidence_sha256'] = hashlib.sha256(args['evidence']).hexdigest()
    with pytest.raises(osv_audit.AuditError):
        runtime_lock_security.verify_signed_runtime_lock(**args)


def test_signed_package_is_rechecked(signed_runtime, monkeypatch):
    original = runtime_lock_security.verify_signed_package
    calls = 0
    def modify(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            signed_runtime['package'].write_bytes(signed_runtime['package'].read_bytes() + b'tampered')
        return result
    monkeypatch.setattr(runtime_lock_security, 'verify_signed_package', modify)
    with pytest.raises(osv_audit.AuditError):
        runtime_lock_security.verify_signed_runtime_lock(**signed_runtime)


def test_cli_accepts_offline_evidence_and_sanitizes_error(signed_runtime, tmp_path, capsys):
    args = deepcopy(signed_runtime)
    envelope, evidence = tmp_path / 'signature', tmp_path / 'report'
    envelope.write_bytes(args['envelope'])
    evidence.write_bytes(args['evidence'])
    command = ['--package', str(args['package']), '--envelope', str(envelope),
               '--trusted-public-key-hex', args['trusted_public_key'].hex(), '--product', args['product_id'],
               '--version', '1', '--platform', 'linux', '--lock', str(args['lock']), '--evidence', str(evidence),
               '--evidence-sha256', args['evidence_sha256'], '--max-age-seconds', '60']
    assert runtime_lock_security.main(command) == 0
    assert json.loads(capsys.readouterr().out)['selected_lock_matches_evidence']
    assert runtime_lock_security.main(command + ['--password', 'synthetic-secret']) == 2
    assert 'synthetic-secret' not in capsys.readouterr().err
