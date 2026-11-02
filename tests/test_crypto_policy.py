"""Неподходящие криптозависимости не включают шифрование и подпись."""
import builtins
from importlib.metadata import PackageNotFoundError
from pathlib import Path
import re

import pytest

from shared import archive_crypto, crypto_policy
from shared.api import handle
from shared.package import PackageError
from shared.package_signing import _crypto


@pytest.mark.parametrize('name', ['cryptography', 'cffi', 'pycparser'])
@pytest.mark.parametrize('missing', [False, True])
def test_unlocked_version_fails_before_crypto_import(name, missing, monkeypatch):
    def fake_version(package):
        if package == name:
            if missing:
                raise PackageNotFoundError(package)
            return '0.0.0'
        return crypto_policy.LOCKED_VERSIONS[package]
    monkeypatch.setattr(crypto_policy, 'version', fake_version)
    original = builtins.__import__
    attempted = []
    def guarded(module, *args, **kwargs):
        if module == 'cryptography' or module.startswith('cryptography.'):
            attempted.append(module)
            raise AssertionError('unapproved cryptography import')
        return original(module, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    with pytest.raises(archive_crypto.CryptoUnavailableError):
        archive_crypto._aesgcm()
    with pytest.raises(PackageError):
        _crypto()
    response = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})
    assert 'verify-package' not in response['result']['operations']
    assert 'prepare-native-release' not in response['result']['operations']
    assert attempted == []


def test_exact_versions_accepted(monkeypatch):
    monkeypatch.setattr(crypto_policy, 'version', lambda name: crypto_policy.LOCKED_VERSIONS[name])
    crypto_policy.require_locked_crypto()


def test_runtime_policy_matches_requirements_lock():
    lock = (Path(__file__).resolve().parents[1] / 'requirements-crypto.txt').read_text(encoding='utf-8')
    pins = dict(re.findall(r'^([a-z0-9_-]+)==([0-9.]+)', lock, flags=re.MULTILINE))
    assert pins == crypto_policy.LOCKED_VERSIONS
