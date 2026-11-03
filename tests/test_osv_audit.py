"""Release-time OSV audit: explicit coverage and fail-closed parsing."""
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shared import osv_audit as osv
from test_crypto_wheelhouse import sample

ROOT = Path(__file__).resolve().parents[1]
HASH = 'a' * 64


def test_repository_lock_inventory():
    packages, sources = osv.read_locks([ROOT / 'requirements-crypto.txt', ROOT / 'build-requirements.txt'])
    assert packages == {'cryptography': '50.0.2', 'cffi': '2.1.1', 'pycparser': '3.0', 'setuptools': '84.2.1'}
    assert len(sources) == 2 and all(len(item['sha256']) == 64 for item in sources)


@pytest.mark.parametrize('text', [
    'example>=1 --hash=sha256:' + HASH,
    'example==1',
    'example==not-a-version --hash=sha256:' + HASH,
    'example==1.0rc1 --hash=sha256:' + HASH,
    'example==1 --hash=sha256:' + HASH + ' --index-url=https://example.test',
    '-r other.txt',
    'example==1 --hash=sha256:' + HASH + '\nexample==1 --hash=sha256:' + HASH,
    'example==1 --hash=sha256:' + HASH + ' \\\n',
])
def test_unsupported_or_incomplete_locks_fail(tmp_path, text):
    lock = tmp_path / 'lock.txt'
    lock.write_text(text)
    with pytest.raises(osv.AuditError):
        osv.read_locks([lock])


def test_pagination_and_evidence(tmp_path):
    lock = tmp_path / 'lock.txt'
    lock.write_text('Example_Name==1.2 --hash=sha256:' + HASH + '\nother==3 --hash=sha256:' + HASH)
    requests = []
    def send(queries):
        requests.append(queries)
        if len(requests) == 1:
            return {'results': [{'vulns': [{'id': 'GHSA-123', 'modified': '2026-01-01T00:00:00Z'}],
                                 'next_page_token': 'next'}, {}]}
        return {'results': [{'vulns': [{'id': 'PYSEC-123', 'modified': '2026-02-01T00:00:00Z'}]}]}
    report = osv.audit([lock], send=send)
    assert requests[0][0] == {'package': {'name': 'example-name', 'ecosystem': 'PyPI'}, 'version': '1.2'}
    assert len(requests[1]) == 1 and requests[1][0]['page_token'] == 'next'
    assert [x['id'] for x in report['packages'][0]['advisories']] == ['GHSA-123', 'PYSEC-123']
    assert not report['complete_dependency_inventory'] and not report['malware_scanned'] and not report['release_safe']


@pytest.mark.parametrize('result', [
    {}, {'results': []}, {'results': [None]}, {'results': [{'vulns': None}]},
    {'results': [{'vulns': [{'id': 'GHSA-123'}]}]},
    {'results': [{'vulns': [{'id': 'GHSA-123', 'modified': 'not-a-date'}]}]},
    {'results': [{'next_page_token': ''}]},
])
def test_invalid_responses_fail(tmp_path, result):
    lock = tmp_path / 'lock.txt'
    lock.write_text('example==1 --hash=sha256:' + HASH)
    with pytest.raises(osv.AuditError):
        osv.audit([lock], send=lambda _: result)


def test_trusted_evidence_requires_fresh_matching_clean_inventory(tmp_path):
    lock = tmp_path / 'lock.txt'
    lock.write_text('example==1 --hash=sha256:' + HASH)
    report = osv.audit([lock], send=lambda _: {'results': [{}]})
    checked = datetime.fromisoformat(report['checked_at'])
    def encoded(value):
        raw = json.dumps(value).encode()
        return raw, hashlib.sha256(raw).hexdigest()
    raw, digest = encoded(report)
    result = osv.verify_clean_evidence(raw, expected_sha256=digest, locks=iter([lock]),
                                       max_age_seconds=3600, now=checked + timedelta(seconds=30))
    assert result['package_count'] == 1 and not result['malware_scanned'] and not result['release_safe']
    for current in (checked - timedelta(seconds=1), checked + timedelta(seconds=3601)):
        with pytest.raises(osv.AuditError):
            osv.verify_clean_evidence(raw, expected_sha256=digest, locks=[lock],
                                      max_age_seconds=3600, now=current)
    with pytest.raises(osv.AuditError):
        osv.verify_clean_evidence(raw + b' ', expected_sha256=digest, locks=[lock], max_age_seconds=3600)
    for field, value in [('advisories', [{'id': 'GHSA-test'}]), ('version', '2')]:
        modified = json.loads(raw)
        modified['packages'][0][field] = value
        changed, changed_digest = encoded(modified)
        with pytest.raises(osv.AuditError):
            osv.verify_clean_evidence(changed, expected_sha256=changed_digest, locks=[lock], max_age_seconds=3600)
    lock.write_text('example==2 --hash=sha256:' + HASH)
    with pytest.raises(osv.AuditError):
        osv.verify_clean_evidence(raw, expected_sha256=digest, locks=[lock], max_age_seconds=3600)


def test_evidence_rejects_duplicate_keys_and_naive_clock(tmp_path):
    lock = tmp_path / 'lock.txt'
    lock.write_text('example==1 --hash=sha256:' + HASH)
    raw = b'{"source":"x","source":"y"}'
    with pytest.raises(osv.AuditError):
        osv.verify_clean_evidence(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                  locks=[lock], max_age_seconds=60)
    report = osv.audit([lock], send=lambda _: {'results': [{}]})
    raw = json.dumps(report).encode()
    with pytest.raises(osv.AuditError):
        osv.verify_clean_evidence(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                  locks=[lock], max_age_seconds=60, now=datetime(2026, 1, 1))


def test_evidence_rejects_lock_change_during_verification(tmp_path, monkeypatch):
    lock = tmp_path / 'lock.txt'
    lock.write_text('example==1 --hash=sha256:' + HASH)
    raw = json.dumps(osv.audit([lock], send=lambda _: {'results': [{}]})).encode()
    original = osv.read_locks
    count = 0
    def changing(paths):
        nonlocal count
        result = original(paths)
        count += 1
        if count == 1:
            lock.write_text('example==2 --hash=sha256:' + HASH)
        return result
    monkeypatch.setattr(osv, 'read_locks', changing)
    with pytest.raises(osv.AuditError):
        osv.verify_clean_evidence(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                  locks=[lock], max_age_seconds=60)
    assert count == 2


def test_repeated_page_and_duplicate_json_keys_fail(tmp_path):
    lock = tmp_path / 'lock.txt'
    lock.write_text('example==1 --hash=sha256:' + HASH)
    with pytest.raises(osv.AuditError):
        osv.audit([lock], send=lambda _: {'results': [{'next_page_token': 'same'}]})
    with pytest.raises(osv.AuditError):
        json.loads('{"results":[],"results":[]}', object_pairs_hook=osv._unique)


@pytest.mark.parametrize('advisories,exit_code', [([], 0), ([{'id': 'GHSA-test'}], 1)])
def test_cli_exit_status_for_findings(monkeypatch, capsys, advisories, exit_code):
    monkeypatch.setattr('sys.argv', ['osv_audit', 'lock.txt'])
    monkeypatch.setattr(osv, 'audit', lambda paths, **kwargs: {'packages': [{'advisories': advisories}]})
    assert osv.main() == exit_code
    assert json.loads(capsys.readouterr().out)['packages'][0]['advisories'] == advisories


def test_cli_does_not_report_clean_on_network_failure(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['osv_audit', 'lock.txt'])
    def fail(_, **kwargs):
        raise OSError('network unavailable')
    monkeypatch.setattr(osv, 'audit', fail)
    assert osv.main() == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'incomplete' in captured.err


def test_cli_verifies_evidence_offline_and_rejects_tampering(tmp_path, monkeypatch, capsys):
    lock = tmp_path / 'lock.txt'
    lock.write_text('example==1 --hash=sha256:' + HASH)
    raw = json.dumps(osv.audit([str(lock)], send=lambda _: {'results': [{}]})).encode()
    report_path = tmp_path / 'evidence.json'
    report_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    def no_network(*_, **__):
        raise AssertionError('offline verification must not query OSV')
    monkeypatch.setattr(osv, 'audit', no_network)
    arguments = ['osv_audit', '--verify-evidence', str(report_path), '--evidence-sha256', digest,
                 '--max-age-seconds', '3600', str(lock)]
    monkeypatch.setattr('sys.argv', arguments)
    assert osv.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['package_count'] == 1 and not result['malware_scanned'] and not result['release_safe']
    report_path.write_bytes(raw + b' ')
    assert osv.main() == 2
    assert capsys.readouterr().out == ''
    arguments[2] = ''
    assert osv.main() == 2
    assert capsys.readouterr().out == ''


def test_cli_rejects_partial_evidence_parameters(monkeypatch):
    for arguments in (['--verify-evidence', 'report.json', 'lock.txt'],
                      ['--evidence-sha256', 'a' * 64, 'lock.txt'],
                      ['--verify-evidence', 'report.json', '--evidence-sha256', 'a' * 64,
                       '--max-age-seconds', '60', '--crypto-wheelhouse', 'block', 'lock.txt']):
        monkeypatch.setattr('sys.argv', ['osv_audit', *arguments])
        with pytest.raises(SystemExit) as error:
            osv.main()
        assert error.value.code == 2


def test_optional_wheelhouse_matches_lock_and_checks_real_artifacts(sample, tmp_path):
    from shared import crypto_wheelhouse as wh
    source, filename, lock = sample
    block = tmp_path / 'block'
    wh.build_wheelhouse(source, block)
    calls = []
    def send(queries):
        calls.append(queries)
        return {'results': [{}]}
    report = osv.audit([lock], crypto_wheelhouse=block, send=send)
    assert report['crypto_wheelhouse']['vendor_wheels'][0]['file'] == 'vendor/' + filename
    assert not report['publisher_provenance_verified'] and not report['release_safe']
    assert len(calls) == 1
    other = tmp_path / 'different.lock'
    other.write_text('demo==1.2 --hash=sha256:' + HASH)
    with pytest.raises(osv.AuditError, match='do not match'):
        osv.audit([other], crypto_wheelhouse=block, send=send)
    assert len(calls) == 1


def test_changed_lock_or_wheelhouse_during_lookup_fails(sample, tmp_path):
    from shared import crypto_wheelhouse as wh
    source, filename, lock = sample
    block = tmp_path / 'block'
    wh.build_wheelhouse(source, block)
    def tamper_wheel(_):
        (block / 'vendor' / filename).write_bytes(b'modified')
        return {'results': [{}]}
    with pytest.raises(wh.WheelhouseError):
        osv.audit([lock], crypto_wheelhouse=block, send=tamper_wheel)
    def tamper_lock(_):
        lock.write_text(lock.read_text() + '# changed during request\n')
        return {'results': [{}]}
    with pytest.raises(osv.AuditError, match='lock changed'):
        osv.audit([lock], send=tamper_lock)
