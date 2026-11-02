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
EXAMPLE_TARGETS = [
    {'os': os_name, 'architecture': 'x86_64', 'execution': execution}
    for os_name in ('windows', 'linux') for execution in ('native', 'docker')
]
EXAMPLES = [
    ('windows-native', {0}), ('linux-native', {2}),
    ('windows-native-docker', {0, 1}), ('all-targets', {0, 1, 2, 3}),
]


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


@pytest.mark.parametrize('name,supported', EXAMPLES)
def test_standalone_example_declares_exact_targets_and_builds_common_package(tmp_path, name, supported):
    from shared.manifest import load_manifest
    from shared.package import build_package, verify_package
    root = ROOT / 'examples/compatibility'
    manifest = load_manifest(root / (name + '.json'))
    for index, target in enumerate(EXAMPLE_TARGETS):
        expected = 'supported' if index in supported else 'unsupported'
        assert check_target(manifest, target) == {'status': expected}
        request = {'api_major': 1, 'request_id': 'example', 'operation': 'check-target',
                   'params': {'manifest': manifest, 'target': target}}
        assert handle(request)['result'] == {'status': expected}
    package = tmp_path / (name + '.zip')
    built = build_package(manifest, '1', root / 'payload', package)
    verified = verify_package(package, expected_sha256=built['sha256'])
    assert verified['manifest'] == manifest
    assert {entry['path'] for entry in verified['files']} >= {
        'locks/linux.lock', 'locks/windows.lock', 'vendor/synthetic_demo-0.0.0-py3-none-any.whl'}


@pytest.mark.parametrize('name,supported', EXAMPLES)
@pytest.mark.parametrize('target_index', [0, 2])
def test_signed_example_rejects_undeclared_native_target(tmp_path, monkeypatch, name, supported, target_index):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    from shared.manifest import load_manifest
    from shared.package import PackageError, build_package
    from shared.package_prepare import prepare_native_signed_package
    from shared.package_signing import sign_package
    root = ROOT / 'examples/compatibility'
    manifest = load_manifest(root / (name + '.json'))
    package = tmp_path / 'example.zip'
    build_package(manifest, '1', root / 'payload', package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: EXAMPLE_TARGETS[target_index])
    destination = tmp_path / 'prepared'
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    if target_index in supported:
        result = prepare_native_signed_package(package, envelope, destination, **options)
        assert result['process_target'] == EXAMPLE_TARGETS[target_index]
        assert (destination / 'README.txt').is_file()
    else:
        with pytest.raises(PackageError):
            prepare_native_signed_package(package, envelope, destination, **options)
        assert not destination.exists()
