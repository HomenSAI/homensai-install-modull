"""Локальные ссылки образов проверяются фиктивным CLI, не Docker daemon."""
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile

import pytest

from test_api_signed_preflight import signed_request
from test_docker_recipe import signed_plan, api_query
from test_docker_prepare import preparation
from shared import api, docker_images
from shared.docker_images import DockerImagesError, inspect_signed_compose_images
from shared.docker_prepare import prepare_signed_compose
from shared.i18n import translate
from examples.demo_product.installer_client import InstallerClient

_SOCKET = socket.socket
pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux') or platform.machine().lower() != 'x86_64',
                                reason='Операция поддерживает Linux x86-64')


@pytest.fixture
def image_check(preparation, tmp_path, monkeypatch):
    package, envelope, options = preparation
    prepare_signed_compose(package, envelope, **options)
    cli = tmp_path / 'synthetic-docker'
    cli.write_text('#!/usr/bin/python3\nimport json, os, sys\n'
                   'assert "SYNTHETIC_DOCKER_SECRET" not in os.environ\n'
                   'assert sys.argv[-4:-1] == ["inspect", "--format", "{{json .RepoDigests}}"]\n'
                   'print(json.dumps([sys.argv[-1]]))\n', encoding='utf-8')
    cli.chmod(0o700)
    monkeypatch.setenv('SYNTHETIC_DOCKER_SECRET', 'not-for-child')
    with tempfile.TemporaryDirectory(prefix='docker-sock-', dir=os.environ.get('TMPDIR')) as directory:
        path = str(Path(directory) / 's')
        listener = _SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        try:
            yield package, envelope, {**options, 'executable': str(cli),
                'executable_sha256': hashlib.sha256(cli.read_bytes()).hexdigest(),
                'socket_path': path, 'timeout_seconds': 5}
        finally:
            listener.close()


def test_fake_cli_reports_all_pinned_images_without_running_containers(image_check):
    package, envelope, options = image_check
    work = Path(options['work_dir'])
    before = sorted(path.name for path in work.iterdir())
    result = inspect_signed_compose_images(package, envelope, **options)
    assert result['state'] == 'checked' and result['image_count'] == 3
    assert result['daemon_reported_matches'] is True and result['image_contents_verified'] is False
    assert result['publisher_verified'] is False and result['containers_started'] is False
    assert sorted(path.name for path in work.iterdir()) == before


@pytest.mark.parametrize('output', ['[]', 'null', '["wrong@sha256:' + 'a' * 64 + '"]', 'not-json'])
def test_wrong_or_unparseable_daemon_response_is_rejected(image_check, monkeypatch, output):
    package, envelope, options = image_check
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=output)
    monkeypatch.setattr(docker_images, '_bounded_run', fake)
    with pytest.raises(DockerImagesError):
        inspect_signed_compose_images(package, envelope, **options)
    assert len(calls) == 1 and calls[0][-4:-1] == ['inspect', '--format', '{{json .RepoDigests}}']


def test_bad_socket_and_tampered_compose_block_all_cli_calls(image_check, tmp_path, monkeypatch):
    package, envelope, options = image_check
    monkeypatch.setattr(docker_images, '_bounded_run', lambda *args, **kwargs: pytest.fail('CLI недопустим'))
    regular = tmp_path / 'not-a-socket'
    regular.write_bytes(b'')
    with pytest.raises(DockerImagesError):
        inspect_signed_compose_images(package, envelope, **{**options, 'socket_path': str(regular)})
    compose = Path(options['work_dir']) / ('compose-' + options['instance_id'] + '.json')
    compose.write_bytes(b'tampered')
    with pytest.raises(DockerImagesError):
        inspect_signed_compose_images(package, envelope, **options)


def test_cli_modification_after_invocation_is_rejected(image_check, monkeypatch):
    package, envelope, options = image_check
    def fake(argv, **kwargs):
        Path(options['executable']).write_text('changed')
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([argv[-1]]))
    monkeypatch.setattr(docker_images, '_bounded_run', fake)
    with pytest.raises(DockerImagesError):
        inspect_signed_compose_images(package, envelope, **options)


def inspect_query(package, envelope, options, language='en'):
    query = api_query(package, envelope, options, language)
    query['operation'] = 'inspect-signed-compose-images'
    query['params'].update(work_dir=options['work_dir'], idempotency_key=options['operation_id'],
        executable=options['executable'], executable_sha256=options['executable_sha256'],
        socket_path=options['socket_path'], timeout_seconds=options['timeout_seconds'])
    return query


def test_api_schema_and_independent_client_with_fake_cli(image_check):
    jsonschema = pytest.importorskip('jsonschema')
    package, envelope, options = image_check
    query = inspect_query(package, envelope, options)
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 35 and query['operation'] in caps['mutating_operations']
    result = api.handle(query)
    assert result['ok'] and result['result']['image_count'] == 3
    assert InstallerClient(timeout=90).call(query['operation'], query['params'], min_minor=35) == result['result']


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_api_sanitizes_errors(image_check, language):
    package, envelope, options = image_check
    query = inspect_query(package, envelope, options, language)
    query['params']['executable_sha256'] = 'b' * 64
    result = api.handle(query)
    assert result['error']['code'] == 'docker_images_failed'
    assert result['error']['message'] == translate(language, 'api.docker_images_failed')
    assert options['work_dir'] not in json.dumps(result)


def test_api_rejects_unsupported_target_and_timeout(image_check, monkeypatch):
    package, envelope, options = image_check
    query = inspect_query(package, envelope, options)
    for change in ({'timeout_seconds': True}, {'timeout_seconds': 0},
                   {'target': {'os': 'windows', 'architecture': 'x86_64', 'execution': 'docker'}}):
        invalid = {**query, 'params': {**query['params'], **change}}
        assert api.handle(invalid)['error']['code'] == 'invalid_request'
    monkeypatch.setattr(api, '_systemd_plan_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'
