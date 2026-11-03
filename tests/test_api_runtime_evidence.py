"""Expose selected signed runtime evidence without external network calls."""
import base64
import json
from pathlib import Path
import subprocess
import sys

import pytest

from homensai_installer_client import InstallerClient, contract_schema
from shared import api
from shared.i18n import translate
from test_runtime_lock_security import signed_runtime

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux API capability')


def query_for(args, evidence_path, language='en'):
    return {'api_major': 1, 'request_id': 'test', 'operation': 'verify-signed-runtime-evidence',
            'language': language, 'params': {'package_path': str(args['package']),
            'envelope': base64.b64encode(args['envelope']).decode('ascii'),
            'trusted_public_key': base64.b64encode(args['trusted_public_key']).decode('ascii'),
            'expected_product_id': args['product_id'], 'expected_version': args['version'],
            'platform': args['platform'], 'lock_path': str(args['lock']), 'evidence_path': str(evidence_path),
            'evidence_sha256': args['evidence_sha256'], 'max_age_seconds': args['max_age_seconds']}}


def test_independent_client_verifies_selected_lock_and_schema(signed_runtime, tmp_path):
    evidence = tmp_path / 'report.json'
    evidence.write_bytes(signed_runtime['evidence'])
    query = query_for(signed_runtime, evidence, 'ru')
    pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request')).validate(query)
    capabilities = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert capabilities['api_minor'] >= 65 and query['operation'] in capabilities['operations']
    result = InstallerClient(timeout=60).call(query['operation'], query['params'], min_minor=65, language='ru')
    assert result['signed_package_verified'] and result['selected_lock_matches_evidence']
    assert not result['complete_dependency_inventory'] and not result['malware_scanned'] and not result['release_safe']
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_evidence_tampering_is_sanitized(signed_runtime, tmp_path, language):
    evidence = tmp_path / 'report.json'
    evidence.write_bytes(signed_runtime['evidence'] + b'tampered')
    result = api.handle(query_for(signed_runtime, evidence, language))
    assert result['error']['code'] == 'runtime_evidence_failed'
    assert result['error']['message'] == translate(language, 'api.runtime_evidence_failed')
    assert str(tmp_path) not in json.dumps(result) and 'tampered' not in json.dumps(result)


def test_invalid_fields_and_unavailable_crypto_do_not_execute(signed_runtime, tmp_path, monkeypatch):
    evidence = tmp_path / 'report.json'
    evidence.write_bytes(signed_runtime['evidence'])
    query = query_for(signed_runtime, evidence)
    invalid = {**query, 'params': {**query['params'], 'max_age_seconds': True}}
    assert api.handle(invalid)['error']['code'] == 'invalid_request'
    monkeypatch.setattr(api, '_signature_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'


def test_event_logging_contains_no_input_data(signed_runtime, tmp_path):
    evidence = tmp_path / 'report.json'
    evidence.write_bytes(signed_runtime['evidence'])
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    log = private / 'events.jsonl'
    query = query_for(signed_runtime, evidence)
    query['event_log_path'] = str(log)
    assert api.handle(query)['ok'] is True
    assert [json.loads(line)['event'] for line in log.read_text().splitlines()] == ['api_started', 'api_succeeded']
    assert str(tmp_path) not in log.read_text()


@pytest.mark.parametrize('which', ['lock', 'evidence'])
def test_symlink_input_is_rejected(signed_runtime, tmp_path, which):
    evidence = tmp_path / 'report.json'
    evidence.write_bytes(signed_runtime['evidence'])
    query = query_for(signed_runtime, evidence)
    target = signed_runtime['lock'] if which == 'lock' else evidence
    link = tmp_path / ('link-' + which)
    link.symlink_to(target)
    query['params'][which + '_path'] = str(link)
    response = api.handle(query)
    assert response['error']['code'] == 'runtime_evidence_failed'
    assert str(tmp_path) not in json.dumps(response)


def test_cli_returns_operational_error_without_exposing_path(signed_runtime, tmp_path):
    evidence = tmp_path / 'missing-sensitive-report'
    query = query_for(signed_runtime, evidence, 'en')
    response = subprocess.run([sys.executable, '-B', '-m', 'shared.api'],
        cwd=Path(__file__).resolve().parents[1], input=json.dumps(query).encode(),
        capture_output=True, timeout=30, check=False)
    assert response.returncode == 1
    assert json.loads(response.stdout)['error']['code'] == 'runtime_evidence_failed'
    assert str(evidence).encode() not in response.stdout + response.stderr
