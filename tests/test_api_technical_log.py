"""Optional API event logging must not expose inputs or repeat effects."""
import json
from pathlib import Path

import pytest

from homensai_installer_client import InstallerClient, InstallerClientError, contract_schema
from shared import api, technical_log
from test_api_signed_preflight import signed_request
from test_fresh_controller import workflow
from test_fresh_controller_api import query_for
from test_update_assembly import assembled


@pytest.fixture
def logfile(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    return directory / 'events.jsonl'


def test_api_records_capability_request_and_schema(logfile):
    request = {'api_major': 1, 'request_id': 'test', 'operation': 'capabilities',
               'params': {}, 'event_log_path': str(logfile), 'language': 'ru'}
    jsonschema = pytest.importorskip('jsonschema')
    jsonschema.Draft202012Validator(contract_schema('request')).validate(request)
    response = api.handle(request)
    assert response['ok'] and response['result']['api_minor'] >= 63
    rows = [json.loads(line) for line in logfile.read_text().splitlines()]
    assert [row['event'] for row in rows] == ['api_started', 'api_succeeded']
    assert str(logfile) not in logfile.read_text() and 'request_id' not in logfile.read_text()


def test_unavailable_log_prevents_operation(tmp_path, monkeypatch):
    directory = tmp_path / 'unsafe'
    directory.mkdir(mode=0o755)
    seen = []
    monkeypatch.setattr(api, '_handle', lambda request: seen.append(request))
    result = api.handle({'api_major': 1, 'request_id': 'safe', 'operation': 'validate',
                         'params': {'secret': 'synthetic'}, 'event_log_path': str(directory / 'events')})
    assert result['error']['code'] == 'technical_log_failed' and not result['error']['retryable']
    assert seen == [] and 'synthetic' not in json.dumps(result)


def test_failure_after_effect_returns_unconfirmed_without_retry(logfile, monkeypatch):
    seen = []
    monkeypatch.setattr(api, '_handle', lambda request: seen.append(request) or
                        {'api_major': 1, 'request_id': request['request_id'], 'ok': True, 'result': {}})
    original = technical_log.record
    def fail_last(path, event):
        if event == 'api_succeeded':
            raise technical_log.TechnicalLogError('synthetic detail')
        original(path, event)
    monkeypatch.setattr(technical_log, 'record', fail_last)
    result = api.handle({'api_major': 1, 'request_id': 'safe', 'operation': 'validate',
                         'params': {'secret': 'synthetic'}, 'event_log_path': str(logfile), 'language': 'en'})
    assert result['error']['code'] == 'technical_log_failed' and not result['error']['retryable']
    assert len(seen) == 1 and 'event_log_path' not in seen[0]
    assert 'synthetic' not in json.dumps(result) and 'synthetic' not in logfile.read_text()
    assert [json.loads(line)['event'] for line in logfile.read_text().splitlines()] == ['api_started']


def test_unexpected_exception_after_effect_is_reported_as_unconfirmed(logfile, monkeypatch):
    seen = []
    def fail(request):
        seen.append(request)
        raise ValueError('synthetic private detail')
    monkeypatch.setattr(api, '_handle', fail)
    result = api.handle({'api_major': 1, 'request_id': 'safe', 'operation': 'validate',
                         'params': {}, 'event_log_path': str(logfile), 'language': 'en'})
    assert result['error']['code'] == 'api_operation_unconfirmed' and not result['error']['retryable']
    assert len(seen) == 1 and 'synthetic' not in json.dumps(result)
    assert [json.loads(line)['event'] for line in logfile.read_text().splitlines()] == ['api_started', 'api_unconfirmed']


def test_independent_client_logs_explicit_call(logfile):
    client = InstallerClient(timeout=60)
    result = client.call('capabilities', {}, event_log_path=str(logfile), language='de')
    assert result['api_minor'] >= 63
    assert [json.loads(line)['event'] for line in logfile.read_text().splitlines()] == ['api_started', 'api_succeeded']


def test_client_refuses_logging_against_older_api(logfile, monkeypatch):
    client = InstallerClient()
    monkeypatch.setattr(client, 'capabilities', lambda: {'api_minor': 62, 'operations': ['capabilities'],
                                                        'languages': ['en']})
    with pytest.raises(InstallerClientError) as error:
        client.call('capabilities', {}, event_log_path=str(logfile))
    assert error.value.code == 'incompatible_api' and not logfile.exists()


def test_logged_install_calls_real_api_dispatch_once(workflow, logfile, tmp_path, monkeypatch):
    from shared import fresh_controller
    marker = tmp_path / 'synthetic-effect'
    calls = []
    def install(**kwargs):
        calls.append(kwargs)
        marker.write_text('synthetic')
        return {'state': 'recorded_complete', 'installation_complete': False}
    monkeypatch.setattr(fresh_controller, 'run_portable_linux_install', install)
    query = query_for(workflow, 'ru')
    query['event_log_path'] = str(logfile)
    response = api.handle(query)
    assert response['ok'] and marker.read_text() == 'synthetic' and len(calls) == 1
    assert [json.loads(line)['event'] for line in logfile.read_text().splitlines()] == ['api_started', 'api_succeeded']
    assert str(tmp_path) not in logfile.read_text()
