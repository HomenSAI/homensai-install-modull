"""Bind signed release bytes to trusted offline vulnerability evidence."""
import hashlib
import json
from pathlib import Path

import pytest

from shared import crypto_wheelhouse as wh, osv_audit, release_security
from shared.package import build_package
from shared.package_signing import sign_package
from test_crypto_wheelhouse import sample

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release(sample, tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    source, filename, lock = sample
    payload = tmp_path / 'payload'
    payload.mkdir()
    block = payload / 'crypto'
    wh.build_wheelhouse(source, block)
    manifest = json.loads((ROOT / 'examples/jobpilot.manifest.json').read_text())
    package = tmp_path / 'release.zip'
    build_package(manifest, '1', payload, package)
    key = ed.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=key, expected_product_id=manifest['product_id'], expected_version='1')
    report = osv_audit.audit([str(lock)], crypto_wheelhouse=block, send=lambda _: {'results': [{}]})
    raw = json.dumps(report).encode()
    args = dict(package=package, envelope=envelope, trusted_public_key=public, product_id=manifest['product_id'],
                version='1', crypto_wheelhouse=block, package_prefix='crypto', evidence=raw,
                evidence_sha256=hashlib.sha256(raw).hexdigest(), locks=[str(lock)], max_age_seconds=60)
    return args, filename, lock


def test_signed_release_matches_audited_block_without_extraction(release):
    args, _, _ = release
    before = args['package'].read_bytes()
    result = release_security.verify_signed_crypto_evidence(**args)
    assert result['signed_package_verified'] and result['crypto_block_matches_package']
    assert result['package_count'] == 1 and not result['malware_scanned'] and not result['release_safe']
    assert args['package'].read_bytes() == before


@pytest.mark.parametrize('change', ['key', 'version', 'digest', 'prefix', 'missing_prefix', 'advisory'])
def test_signed_release_rejects_untrusted_or_inconsistent_evidence(release, change):
    args, _, _ = release
    if change == 'key':
        args['trusted_public_key'] = b'x' * 32
    elif change == 'version':
        args['version'] = '2'
    elif change == 'digest':
        args['evidence_sha256'] = 'a' * 64
    elif change == 'prefix':
        args['package_prefix'] = '../crypto'
    elif change == 'missing_prefix':
        args['package_prefix'] = 'other'
    else:
        report = json.loads(args['evidence'])
        report['packages'][0]['advisories'] = [{'id': 'GHSA-test'}]
        args['evidence'] = json.dumps(report).encode()
        args['evidence_sha256'] = hashlib.sha256(args['evidence']).hexdigest()
    with pytest.raises(osv_audit.AuditError):
        release_security.verify_signed_crypto_evidence(**args)


@pytest.mark.parametrize('target', ['package', 'wheel', 'lock'])
def test_signed_release_rejects_changed_inputs(release, target):
    args, filename, lock = release
    path = {'package': args['package'], 'wheel': args['crypto_wheelhouse'] / 'vendor' / filename,
            'lock': lock}[target]
    path.write_bytes(path.read_bytes() + b'tampered')
    with pytest.raises(osv_audit.AuditError):
        release_security.verify_signed_crypto_evidence(**args)


def test_signed_release_rechecks_package_between_observations(release, monkeypatch):
    args, _, _ = release
    original = release_security.verify_signed_package
    calls = 0
    def change_after_first(*values, **options):
        nonlocal calls
        result = original(*values, **options)
        calls += 1
        if calls == 1:
            args['package'].write_bytes(args['package'].read_bytes() + b'tampered')
        return result
    monkeypatch.setattr(release_security, 'verify_signed_package', change_after_first)
    with pytest.raises(osv_audit.AuditError):
        release_security.verify_signed_crypto_evidence(**args)
    assert calls == 1


def test_offline_release_security_cli_rejects_wrong_key_and_changes(release, tmp_path, capsys):
    args, filename, _ = release
    envelope = tmp_path / 'release.sig'
    envelope.write_bytes(args['envelope'])
    evidence = tmp_path / 'osv.json'
    evidence.write_bytes(args['evidence'])
    command = ['--package', str(args['package']), '--envelope', str(envelope),
               '--trusted-public-key-hex', args['trusted_public_key'].hex(),
               '--product', args['product_id'], '--version', args['version'],
               '--crypto-wheelhouse', str(args['crypto_wheelhouse']), '--package-prefix', 'crypto',
               '--evidence', str(evidence), '--evidence-sha256', args['evidence_sha256'],
               '--max-age-seconds', '60', *args['locks']]
    assert release_security.main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['crypto_block_matches_package'] and not report['release_safe']
    wrong_key = list(command)
    wrong_key[wrong_key.index('--trusted-public-key-hex') + 1] = 'aa' * 32
    assert release_security.main(wrong_key) == 2 and capsys.readouterr().out == ''
    (args['crypto_wheelhouse'] / 'vendor' / filename).write_bytes(b'tampered')
    assert release_security.main(command) == 2 and capsys.readouterr().out == ''
