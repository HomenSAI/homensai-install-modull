"""Real authenticated loopback help lookup; no HTTP catalog import authority."""
from contextlib import contextmanager
import http.client
import importlib.util
import json
from pathlib import Path
import threading
from urllib.parse import urlencode

import pytest

from homensai_install.gui import WizardSession, create_server

_spec = importlib.util.spec_from_file_location('independent_public_catalog_fixture',
    Path(__file__).resolve().parents[1] / 'tests/test_error_catalog_public.py')
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


@contextmanager
def serving(session):
    server = create_server(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = '127.0.0.1:' + str(server.server_port)
    def request(path, *, method='GET', body=None, authenticated=True, headers=None):
        supplied = {'Host': origin}
        if authenticated:
            supplied['X-HInstall-Token'] = session.token
        supplied.update(headers or {})
        raw = None
        if body is not None:
            supplied['Content-Type'] = 'application/json'
            raw = json.dumps(body)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            connection.request(method, '/api/v1' + path, raw, supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.fixture
def http_catalog(tmp_path):
    value = _fixtures.catalog_bundle(tmp_path)
    api, artifact, _, _ = _fixtures.activate(value)
    session = WizardSession(api, {'product_id': 'synthetic-catalog-help'}, {}, locale='en', development=True)
    return value, api, artifact, session


def test_http_updated_help_uses_per_request_locale_and_does_not_write_state(http_catalog):
    value, api, _, session = http_catalog
    before = _fixtures.filesystem_image(value.state)
    with serving(session) as request:
        for locale in ('ru', 'en', 'de'):
            code, response = request('/errors/DISK_SPACE_LOW?' + urlencode({'locale': locale}))
            assert code == 200 and response['result']['title'] == 'CatalogHIM070 ' + locale
            assert response['result']['catalog_version'] == '1.0.1'
            code, response = request('/errors?' + urlencode({'locale': locale, 'query': 'CatalogHIM070'}))
            assert code == 200
            assert [item['code'] for item in response['result']['errors']] == ['DISK_SPACE_LOW']
            assert response['result']['errors'][0]['title'] == 'CatalogHIM070 ' + locale
            assert session.token not in json.dumps(response)
    assert api.locale == 'ru' and session.locale == session.preferred_locale == 'en'
    assert _fixtures.filesystem_image(value.state) == before


@pytest.mark.parametrize('injected', ['artifact', 'policy', 'keys', 'action', 'destination'])
def test_http_get_cannot_supply_import_policy_keys_or_paths(http_catalog, injected):
    value, api, artifact, session = http_catalog
    before = _fixtures.filesystem_image(value.state)
    original_status = api.errors_catalog_status()
    supplied = {'artifact': str(artifact), 'policy': str(value.root / 'not-trusted-policy.json'),
        'keys': '{"attacker":"SYNTHETIC_UNTRUSTED_KEY"}', 'action': 'apply',
        'destination': str(value.root / 'must-not-exist')}[injected]
    with serving(session) as request:
        code, response = request('/errors/DISK_SPACE_LOW?' + urlencode({injected: supplied, 'locale': 'de'}))
        assert code == 200 or 400 <= code < 500
        if code == 200:
            assert response['result']['title'] == 'CatalogHIM070 de'
        assert supplied not in json.dumps(response) and session.token not in json.dumps(response)
    assert api.errors_catalog_status() == original_status
    assert not (value.root / 'must-not-exist').exists()
    assert _fixtures.filesystem_image(value.state) == before


@pytest.mark.parametrize('path', ['/errors-catalog/apply', '/errors-catalog/import', '/errors', '/errors/DISK_SPACE_LOW'])
def test_http_has_no_catalog_mutation_post_route(http_catalog, path):
    value, api, artifact, session = http_catalog
    before = _fixtures.filesystem_image(value.state)
    original_status = api.errors_catalog_status()
    with serving(session) as request:
        code, response = request(path, method='POST', body={'artifact': str(artifact),
            'expected_review_digest': '0' * 64, 'confirmed': True, 'keys': {'attacker': 'untrusted'}})
        assert 400 <= code < 500 and 'error' in response
    assert api.errors_catalog_status() == original_status
    assert _fixtures.filesystem_image(value.state) == before


def test_http_help_keeps_token_host_and_origin_authority_guards(http_catalog):
    value, _, _, session = http_catalog
    before = _fixtures.filesystem_image(value.state)
    with serving(session) as request:
        assert request('/errors/DISK_SPACE_LOW', authenticated=False)[0] == 403
        for headers in ({'Host': 'attacker.invalid'}, {'Origin': 'https://attacker.invalid'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            assert request('/errors/DISK_SPACE_LOW', headers=headers)[0] == 403
    assert _fixtures.filesystem_image(value.state) == before
