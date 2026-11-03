"""Release-time OSV audit: explicit coverage and fail-closed parsing."""
import json
from pathlib import Path

import pytest

from shared import osv_audit as osv

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
    monkeypatch.setattr(osv, 'audit', lambda paths: {'packages': [{'advisories': advisories}]})
    assert osv.main() == exit_code
    assert json.loads(capsys.readouterr().out)['packages'][0]['advisories'] == advisories


def test_cli_does_not_report_clean_on_network_failure(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['osv_audit', 'lock.txt'])
    def fail(_):
        raise OSError('network unavailable')
    monkeypatch.setattr(osv, 'audit', fail)
    assert osv.main() == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'incomplete' in captured.err
