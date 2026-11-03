"""Проверка подключаемых словарей без записи и исполнения."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from homensai_installer_client import InstallerClient, contract_schema
from shared.api import handle


ROOT = Path(__file__).resolve().parents[1]


def request(catalog, language='en'):
    return {'api_major': 1, 'request_id': 'language-test', 'operation': 'validate-language-catalog',
            'language': language, 'params': {'catalog': catalog}}


@pytest.mark.parametrize('language', ['en', 'ru', 'de'])
def test_validate_installed_languages_and_schema(language):
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    catalog = json.loads((ROOT / 'shared/locales' / (language + '.json')).read_text(encoding='utf-8'))
    query = request(catalog, language)
    assert validator.is_valid(query)
    result = handle(query)['result']
    assert result['valid'] and result['message_count'] == len(catalog)
    assert result['compact_utf8_bytes'] == len(json.dumps(catalog, ensure_ascii=False,
                                                          separators=(',', ':')).encode('utf-8'))
    assert InstallerClient().call(query['operation'], query['params'], min_minor=45, language=language) == result


@pytest.mark.parametrize('change', ['missing', 'extra', 'variable', 'attribute', 'conversion', 'oversize', 'surrogate'])
def test_invalid_catalog_rejected_without_exposing_input(change):
    catalog = json.loads((ROOT / 'shared/locales/en.json').read_text(encoding='utf-8'))
    if change == 'missing':
        del catalog['installer.ready']
    elif change == 'extra':
        catalog['evil.extra'] = 'SENSITIVE-SENTINEL'
    elif change == 'variable':
        catalog['installer.ready'] = 'SENSITIVE-SENTINEL {wrong}'
    elif change == 'attribute':
        catalog['installer.ready'] = '{product.__class__}'
    elif change == 'conversion':
        catalog['installer.ready'] = '{product!r}'
    elif change == 'oversize':
        for key in catalog:
            catalog[key] = catalog[key] + 'x' * 3000
    else:
        catalog['api.invalid_request'] = '\ud800'
    before = deepcopy(catalog)
    response = handle(request(catalog))
    assert response['error']['code'] == 'invalid_request'
    assert 'SENSITIVE-SENTINEL' not in json.dumps(response) and catalog == before


def test_rejects_unknown_parameters_and_schema_shape():
    catalog = json.loads((ROOT / 'shared/locales/en.json').read_text(encoding='utf-8'))
    query = request(catalog)
    query['params']['save'] = True
    assert handle(query)['error']['code'] == 'invalid_request'
    assert not pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request')).is_valid(query)
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 45 and 'validate-language-catalog' in caps['operations']
    assert 'validate-language-catalog' not in caps['mutating_operations']
