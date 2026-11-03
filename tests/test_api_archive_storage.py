"""Проверка защищённого временного хранения через публичный контракт API."""
import json
from pathlib import Path
import sys

import pytest

from homensai_installer_client import InstallerClient, contract_schema
from shared import api, archive_crypto


pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux O_TMPFILE')


def request(directory, **params):
    return {'api_major': 1, 'request_id': 'storage-test', 'operation': 'check-archive-storage',
            'params': {'directory': str(directory), **params}}


def test_storage_check_contract_and_independent_client(tmp_path):
    jsonschema = pytest.importorskip('jsonschema')
    pytest.importorskip('cryptography')
    directory = tmp_path / 'архив с пробелом'
    directory.mkdir()
    query = request(directory)
    assert jsonschema.Draft202012Validator(contract_schema('request')).is_valid(query)
    caps = api.handle({**query, 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 44 and query['operation'] in caps['mutating_operations']
    result = api.handle(query)['result']
    assert result['crypto_available'] and result['anonymous_temporary'] and result['ready']
    assert 'password' in result['not_checked'] and 'writers' in result['not_checked']
    assert str(directory) not in json.dumps(result) and not list(directory.iterdir())
    assert InstallerClient().call(query['operation'], query['params'], min_minor=44) == result
    assert not list(directory.iterdir())


def test_storage_check_separates_crypto_and_filesystem_failures(tmp_path, monkeypatch):
    def unavailable_crypto():
        raise archive_crypto.CryptoUnavailableError('synthetic')
    monkeypatch.setattr(archive_crypto, '_aesgcm', unavailable_crypto)
    result = api.handle(request(tmp_path))['result']
    assert result['crypto_available'] is False and result['anonymous_temporary'] is True
    assert result['ready'] is False
    def unavailable_filesystem(parent):
        raise OSError('sensitive path')
    monkeypatch.setattr(archive_crypto, '_anonymous_temporary', unavailable_filesystem)
    result = api.handle(request(tmp_path))['result']
    assert not result['anonymous_temporary'] and not result['ready']
    assert 'sensitive path' not in json.dumps(result)


def test_storage_check_is_unavailable_on_other_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(api.sys, 'platform', 'win32')
    assert api.handle(request(tmp_path))['error']['code'] == 'unsupported_operation'
    caps = api.handle({**request(tmp_path), 'operation': 'capabilities', 'params': {}})['result']
    assert 'check-archive-storage' not in caps['operations']


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_storage_check_rejects_bad_paths_and_extra_fields(tmp_path, language):
    bad = request(tmp_path / 'missing')
    bad['language'] = language
    response = api.handle(bad)
    assert response['error']['code'] == 'preflight_failed'
    assert str(tmp_path) not in json.dumps(response)
    assert api.handle(request('relative'))['error']['code'] == 'preflight_failed'
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    assert api.handle(request(link))['error']['code'] == 'preflight_failed'
    query = request(tmp_path, extra=True)
    assert api.handle(query)['error']['code'] == 'invalid_request'
    assert not pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request')).is_valid(query)
