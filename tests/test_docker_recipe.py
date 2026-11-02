"""Подписанный Compose-план v2: фиктивные образы, Docker не вызывается."""
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from test_api_signed_preflight import signed_request
from shared.docker_recipe import DockerRecipeError, RECIPE_PATH, plan_signed_compose
from shared import api
from shared.i18n import translate
from shared.package import build_package
from shared.package_signing import sign_package
from examples.demo_product.installer_client import InstallerClient

LINUX = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'docker'}
WINDOWS = {'os': 'windows', 'architecture': 'x86_64', 'execution': 'docker'}


@pytest.fixture
def signed_plan(signed_request, tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    _, _, manifest = signed_request
    manifest['compatibility'] = {'targets': [LINUX, WINDOWS]}
    manifest['secrets'] = []
    manifest['hooks'] = {name: [] for name in manifest['hooks']}
    for component in manifest['components']:
        if component['name'] == 'bot':
            component['cmd'] = ['python', '-c', 'print("$HOME")']
    source = tmp_path / 'payload'
    recipe = {'format': 1, 'images': {item['name']: 'registry.example/demo/' + item['name'] + '@sha256:' + 'a' * 64
                                     for item in manifest['components']},
              'web_component': 'web', 'container_port': 8080}
    key = ed.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    def build(recipe_data, *, altered_manifest=None, suffix='package'):
        path = source / RECIPE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recipe_data if isinstance(recipe_data, bytes) else json.dumps(recipe_data).encode())
        package = tmp_path / (suffix + '.zip')
        selected_manifest = altered_manifest or manifest
        build_package(selected_manifest, '1', source, package)
        envelope = sign_package(package, private_key=key, expected_product_id=manifest['product_id'],
                                expected_version='1')
        options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'],
                   'expected_version': '1', 'target': dict(LINUX), 'modes': ('autonomous',),
                   'instance_id': '1' * 32}
        return package, envelope, options
    return manifest, recipe, build


def test_signed_recipe_builds_stable_multiple_service_plan_without_execution(signed_plan, monkeypatch):
    manifest, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('План не запускает процессы'))
    result = plan_signed_compose(str(package), envelope, **options)
    assert plan_signed_compose(str(package), envelope, **options) == result
    assert result['state'] == 'planned' and result['components'] == ['web', 'bot', 'scheduler']
    assert result['host_verified'] is False and result['images_verified'] is False
    assert result['containers_started'] is False and result['installation_complete'] is False
    assert result['compose_sha256'] == hashlib.sha256(result['compose_json'].encode('ascii')).hexdigest()
    compose = json.loads(result['compose_json'])
    assert compose['name'] == manifest['product_id'] + '-' + '1' * 32
    assert set(compose['services']) == set(result['components'])
    assert compose['services']['web']['ports'] == [f"127.0.0.1:{manifest['web']['port']}:8080"]
    assert compose['services']['bot']['depends_on'] == ['web']
    assert compose['services']['bot']['command'][-1] == 'print("$$HOME")'
    for service in compose['services'].values():
        assert service['image'].endswith('@sha256:' + 'a' * 64)
        assert service['pull_policy'] == 'never' and service['entrypoint'] == []
        assert service['read_only'] is True and service['cap_drop'] == ['ALL']
        assert service['volumes'] == ['data:/data'] and 'build' not in service
    windows = plan_signed_compose(str(package), envelope, **{**options, 'target': dict(WINDOWS)})
    assert windows['compose_json'] == result['compose_json'] and windows['host_verified'] is False
    other = plan_signed_compose(str(package), envelope, **{**options, 'instance_id': '2' * 32})
    assert json.loads(other['compose_json'])['name'] != compose['name']


@pytest.mark.parametrize('change', ['tag', 'digest', 'component', 'port', 'duplicate', 'bool'])
def test_bad_signed_recipe_fails_closed(signed_plan, change):
    _, recipe, build = signed_plan
    data = deepcopy(recipe)
    if change == 'tag':
        data['images']['web'] = 'registry.example/demo/web:latest'
    elif change == 'digest':
        data['images']['web'] = 'registry.example/demo/web@sha256:' + 'g' * 64
    elif change == 'component':
        del data['images']['bot']
    elif change == 'port':
        data['container_port'] = True
    elif change == 'bool':
        data['format'] = True
    else:
        data = b'{"format":1,"format":1}'
    package, envelope, options = build(data)
    with pytest.raises(DockerRecipeError):
        plan_signed_compose(str(package), envelope, **options)


def test_rejects_untrusted_key_undeclared_target_and_unsupported_hooks(signed_plan):
    manifest, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    for change in ({'trusted_public_key': b'x' * 32}, {'target': {**LINUX, 'execution': 'native'}},
                   {'modes': ('platform',)}, {'instance_id': '../escape'}):
        with pytest.raises(DockerRecipeError):
            plan_signed_compose(str(package), envelope, **{**options, **change})
    changed = deepcopy(manifest)
    changed['hooks']['pre_start'] = [['python', 'unexpected.py']]
    package, envelope, options = build(recipe, altered_manifest=changed, suffix='hooks')
    with pytest.raises(DockerRecipeError):
        plan_signed_compose(str(package), envelope, **options)


def test_tampered_package_is_rejected(signed_plan):
    _, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    package.write_bytes(package.read_bytes() + b'altered')
    with pytest.raises(DockerRecipeError):
        plan_signed_compose(str(package), envelope, **options)


def test_package_changed_during_planning_is_rejected(signed_plan, monkeypatch):
    from shared import docker_recipe
    _, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    original = docker_recipe.verify_signed_package
    calls = []
    def change_after_verification(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            with package.open('ab') as stream:
                stream.write(b'tampered')
        return result
    monkeypatch.setattr(docker_recipe, 'verify_signed_package', change_after_verification)
    with pytest.raises(DockerRecipeError):
        plan_signed_compose(str(package), envelope, **options)


def api_query(package, envelope, options, language='en'):
    return {'api_major': 1, 'request_id': 'docker-plan-test', 'operation': 'plan-signed-compose',
            'language': language, 'params': {'package_path': str(package),
                'envelope': base64.b64encode(envelope).decode('ascii'),
                'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode('ascii'),
                'expected_product_id': options['expected_product_id'],
                'expected_version': options['expected_version'], 'target': options['target'],
                'modes': list(options['modes']), 'instance_id': options['instance_id']}}


def test_api_schema_and_independent_client(signed_plan):
    jsonschema = pytest.importorskip('jsonschema')
    _, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    query = api_query(package, envelope, options)
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 33 and query['operation'] in caps['operations']
    assert query['operation'] not in caps['mutating_operations']
    result = api.handle(query)
    assert result['ok'] and result['result']['containers_started'] is False
    assert InstallerClient(timeout=90).call(query['operation'], query['params'], min_minor=33) == result['result']
    bad = {**query, 'params': {**query['params'], 'modes': ['autonomous', 'autonomous']}}
    assert not jsonschema.Draft202012Validator(schema).is_valid(bad)
    assert api.handle(bad)['error']['code'] == 'invalid_request'


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_api_invalid_publisher_is_redacted(signed_plan, language):
    _, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    query = api_query(package, envelope, options, language)
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    result = api.handle(query)
    assert result['error']['code'] == 'docker_plan_failed'
    assert result['error']['message'] == translate(language, 'api.docker_plan_failed')
    assert str(package) not in json.dumps(result)


def test_api_does_not_advertise_without_signature_support(signed_plan, monkeypatch):
    _, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    query = api_query(package, envelope, options)
    monkeypatch.setattr(api, '_signature_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'
