"""Независимый клиент проверяет привязку начатого обновления через API."""
import base64
import json
from pathlib import Path

import pytest

from test_api_signed_preflight import signed_request
from test_update_assembly import assembled
from test_update_prepare import api_query
from examples.demo_product.installer_client import InstallerClient
from shared import api
from shared.i18n import translate
from shared.systemd_fresh_start import _lifecycle_lock
from shared.update_assembly import assemble_prepared_linux_update


def started(assembled):
    options, adapters = assembled
    plan = assemble_prepared_linux_update(**options)
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': 0, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    prep_options = {key: value for key, value in options.items() if key not in
                    ('group', 'password_provider', 'interpreter', 'interpreter_sha256', 'timeout')}
    query = api_query(prep_options)
    query['operation'] = 'inspect-signed-linux-update'
    return options, adapters, plan, state, query


def test_independent_client_inspects_signed_checkpoint_without_resuming(assembled):
    jsonschema = pytest.importorskip('jsonschema')
    options, adapters, plan, state, query = started(assembled)
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 30 and query['operation'] in caps['operations']
    assert query['operation'] in caps['mutating_operations']  # Служебные блокировки.
    before = plan.state_path.read_bytes()
    pointer = plan.ports.pointer.read_bytes()
    response = api.handle(query)
    assert response['ok'] is True and response['result']['state'] == state
    assert response['result']['effects_verified'] is False and response['result']['resume_authorized'] is False
    assert InstallerClient(timeout=90).call(query['operation'], query['params'], min_minor=30) == response['result']
    assert plan.state_path.read_bytes() == before and plan.ports.pointer.read_bytes() == pointer
    assert all(adapter.active for adapter in adapters.values())
    assert options['install_root'] not in json.dumps(response)


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_inspection_rejects_wrong_key_tampering_and_busy_lock(assembled, language):
    options, _, plan, state, query = started(assembled)
    query['language'] = language
    query['params']['new_key'] = base64.b64encode(b'x' * 32).decode('ascii')
    response = api.handle(query)
    assert response['error']['code'] == 'update_inspect_failed'
    assert response['error']['message'] == translate(language, 'api.update_inspect_failed')
    assert options['install_root'] not in json.dumps(response)
    assert plan.status() == state
    query['params']['new_key'] = base64.b64encode(options['new_key']).decode('ascii')
    query['operation'] = 'inspect-signed-linux-update'
    with _lifecycle_lock(options['install_root']):
        assert api.handle(query)['error']['code'] == 'update_inspect_failed'
    (plan.ports.payloads['2'].directory / 'new.py').write_bytes(b'tampered')
    assert api.handle(query)['error']['code'] == 'update_inspect_failed'
    assert plan.status() == state


def test_inspection_unavailable_without_crypto_and_rejects_extra_parameters(assembled, monkeypatch):
    options, _, plan, state, query = started(assembled)
    query['params']['resume'] = True
    assert api.handle(query)['error']['code'] == 'invalid_request'
    del query['params']['resume']
    monkeypatch.setattr(api, '_signature_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'
    assert plan.status() == state
