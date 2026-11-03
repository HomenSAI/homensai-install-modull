"""Декларативный план для единого пакета не является проверкой среды."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from homensai_installer_client import InstallerClient, contract_schema
from shared.api import handle
from shared.manifest import load_manifest, plan_target_dependencies
from shared.package import build_package, verify_package


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples/compatibility'
WINDOWS = {'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'}
LINUX = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
DOCKER = {'os': 'windows', 'architecture': 'x86_64', 'execution': 'docker'}


@pytest.mark.parametrize('target,status,selected', [(WINDOWS, 'supported', 'windows'),
                                                     (LINUX, 'unsupported', None),
                                                     (DOCKER, 'supported', None)])
def test_target_plan_preserves_complete_bundle_and_only_selects_declared_native_lock(target, status, selected):
    manifest = load_manifest(EXAMPLES / 'windows-native-docker.json')
    before = deepcopy(manifest)
    plan = plan_target_dependencies(manifest, target)
    assert plan['compatibility'] == status and plan['execution_authorized'] is False
    assert plan['target'] == target and plan['declared_python'] == manifest['runtime']['python']
    required = plan['package_artifacts']
    assert {(item['kind'], item['file']) for item in required} == {
        ('lock', manifest['runtime']['lock_files']['linux']['file']),
        ('lock', manifest['runtime']['lock_files']['windows']['file']),
        *(('wheel', item['file']) for item in manifest['runtime']['vendor_wheels'])}
    assert plan['native_lock_candidate'] == (manifest['runtime']['lock_files'][selected] if selected else None)
    assert {'artifact_contents', 'package_signature', 'host_platform', 'security'} <= set(plan['not_checked'])
    plan['package_artifacts'][0]['sha256'] = 'changed'
    assert manifest == before


def test_package_requirements_match_real_package_inventory(tmp_path):
    manifest = load_manifest(EXAMPLES / 'all-targets.json')
    output = tmp_path / 'package.zip'
    built = build_package(manifest, '1', EXAMPLES / 'payload', output)
    inventory = {item['path']: item['sha256'] for item in verify_package(output, expected_sha256=built['sha256'])['files']}
    plan = plan_target_dependencies(manifest, LINUX)
    assert all(inventory[item['file'].replace('\\', '/')] == item['sha256'] for item in plan['package_artifacts'])
    assert plan['native_lock_candidate'] == manifest['runtime']['lock_files']['linux']


def test_unknown_and_legacy_targets_do_not_choose_a_native_lock():
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    plan = plan_target_dependencies(manifest, WINDOWS)
    assert plan['compatibility'] == 'unknown' and plan['native_lock_candidate'] is None
    legacy = load_manifest(ROOT / 'examples/jobpilot.manifest.json')
    result = plan_target_dependencies(legacy, DOCKER)
    assert result['compatibility'] == 'unknown' and result['package_artifacts'] == []
    assert result['legacy_docker_base_image'] == legacy['docker']['base_image']


def test_api_schema_and_independent_client_never_authorize_execution():
    manifest = load_manifest(EXAMPLES / 'windows-native.json')
    query = {'api_major': 1, 'request_id': 'requirements', 'operation': 'plan-target-dependencies',
             'params': {'manifest': manifest, 'target': WINDOWS}}
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    assert validator.is_valid(query)
    response = handle(query)
    assert response['ok'] and response['result']['execution_authorized'] is False
    assert InstallerClient().call(query['operation'], query['params'], min_minor=46) == response['result']
    query['params']['target']['os'] = 'SENSITIVE-SENTINEL'
    response = handle(query)
    assert response['error']['code'] == 'invalid_request' and 'SENSITIVE-SENTINEL' not in json.dumps(response)
    assert not validator.is_valid(query)
