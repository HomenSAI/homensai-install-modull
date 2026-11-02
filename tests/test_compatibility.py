"""Декларация совместимости не проверяет фактическую готовность компьютера."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from shared.api import handle
from shared.manifest import ManifestError, check_target, validate_manifest

ROOT = Path(__file__).resolve().parents[1]
V2 = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
V1 = json.loads((ROOT / 'examples/jobpilot.manifest.json').read_text(encoding='utf-8'))
WINDOWS = {'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'}
LINUX_DOCKER = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'docker'}


def declared():
    manifest = deepcopy(V2)
    manifest['compatibility'] = {'targets': [deepcopy(WINDOWS), deepcopy(LINUX_DOCKER)]}
    return manifest


def test_declared_combinations_are_not_cartesian_product():
    assert check_target(declared(), WINDOWS) == {'status': 'supported'}
    assert check_target(declared(), LINUX_DOCKER) == {'status': 'supported'}
    assert check_target(declared(), {**WINDOWS, 'execution': 'docker'}) == {'status': 'unsupported'}
    assert check_target(declared(), {**LINUX_DOCKER, 'execution': 'native'}) == {'status': 'unsupported'}


def test_missing_declaration_is_unknown_and_v1_cannot_declare():
    assert check_target(V1, WINDOWS) == {'status': 'unknown'}
    assert check_target(V2, WINDOWS) == {'status': 'unknown'}
    legacy = deepcopy(V1)
    legacy['compatibility'] = {'targets': [deepcopy(WINDOWS)]}
    with pytest.raises(ManifestError):
        validate_manifest(legacy)


@pytest.mark.parametrize('targets', [[], [WINDOWS, WINDOWS], [{**WINDOWS, 'os': 'macos'}],
                                      [{**WINDOWS, 'extra': True}], [WINDOWS] * 5])
def test_invalid_declarations(targets):
    manifest = deepcopy(V2)
    manifest['compatibility'] = {'targets': deepcopy(targets)}
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize('target', [None, {}, {**WINDOWS, 'os': 'SECRET-SENTINEL'},
                                    {**WINDOWS, 'architecture': 'arm64'},
                                    {**WINDOWS, 'execution': True}, {**WINDOWS, 'extra': 1}])
def test_invalid_target_fails_without_echo(target):
    request = {'api_major': 1, 'request_id': 'test', 'operation': 'check-target',
               'params': {'manifest': declared(), 'target': target}}
    response = handle(request)
    assert response['error']['code'] == 'invalid_request'
    assert 'SECRET-SENTINEL' not in json.dumps(response)


def test_api_python_and_cli_agree():
    request = {'api_major': 1, 'request_id': 'test', 'operation': 'check-target',
               'params': {'manifest': declared(), 'target': deepcopy(WINDOWS)}}
    result = handle(request)
    assert result['result'] == {'status': 'supported'}
    process = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                             input=json.dumps(request).encode(), capture_output=True, timeout=15)
    assert process.returncode == 0 and json.loads(process.stdout) == result
