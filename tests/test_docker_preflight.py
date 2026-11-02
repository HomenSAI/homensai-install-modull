"""Только фиктивный Docker CLI; демон и контейнеры не изменяются."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from shared import api, ctl
from shared.docker_preflight import DockerPreflightError, check_docker

pytestmark = pytest.mark.skipif(not (sys.platform.startswith('linux') or os.name == 'nt'),
                                reason='Проверяются локальные endpoints Linux и Windows')


@pytest.fixture
def binary(tmp_path):
    path = tmp_path / 'docker synthetic'
    path.write_bytes(b'synthetic pinned executable')
    path.chmod(0o700)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def options(binary, tmp_path):
    return {'executable': str(binary[0]), 'executable_sha256': binary[1],
            'socket_path': str(tmp_path / 'local.sock') if os.name != 'nt' else None}


def test_pinned_local_checks_sanitized_and_never_run_containers(binary, tmp_path, monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'tcp://untrusted.invalid:2375')
    monkeypatch.setenv('SYNTHETIC_SECRET', 'private-value')
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        command = argv[5:]
        values = {('--version',): 'Docker version 28.5.1, build abcd123\n',
                  ('compose', 'version', '--short'): 'v2.32.4\n',
                  ('compose', 'up', '--help'): '  --no-build  Never build\n  --pull string  Pull policy\n',
                  ('version', '--format', '{{.Server.Version}}'): '28.5.1\n'}
        return subprocess.CompletedProcess(argv, 0, stdout=values[tuple(command)], stderr='')
    result = check_docker(**options(binary, tmp_path), runner=runner)
    assert result == {'client_version': '28.5.1', 'compose_version': '2.32.4', 'server_version': '28.5.1',
                      'compose_flags_present': True, 'installation_ready': False, 'images_verified': False}
    assert len(calls) == 4
    for argv, kwargs in calls:
        assert argv[0] == str(binary[0]) and argv[1] == '--config' and argv[3] == '--host'
        assert argv[4].startswith(('unix://', 'npipe:')) and 'untrusted.invalid' not in repr(argv)
        assert kwargs['shell'] is False and kwargs['timeout'] == 5
        assert 'DOCKER_HOST' not in kwargs['env'] and 'SYNTHETIC_SECRET' not in kwargs['env']
        assert kwargs['env']['DOCKER_CONFIG'] == kwargs['env']['HOME'] == argv[2]
    assert not Path(calls[0][0][2]).exists()


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'malformed', 'too_long'])
def test_unavailable_client_does_not_run_other_commands(binary, tmp_path, failure):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('secret', 1)
        return subprocess.CompletedProcess(argv, 1 if failure == 'exit' else 0,
            stdout='x' * 9000 if failure == 'too_long' else 'podman version 4.3.1', stderr='secret')
    result = check_docker(**options(binary, tmp_path), runner=runner)
    assert result['client_version'] is None and result['server_version'] is None
    assert result['installation_ready'] is False and len(calls) == 1
    assert 'secret' not in repr(result)


def test_missing_compose_or_daemon_is_reported_independently(binary, tmp_path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv[5:])
        if argv[5:] == ['--version']:
            return subprocess.CompletedProcess(argv, 0, stdout='Docker version 27.1.0, build abc', stderr='')
        return subprocess.CompletedProcess(argv, 1, stdout='', stderr='secret')
    result = check_docker(**options(binary, tmp_path), runner=runner)
    assert result['client_version'] == '27.1.0' and result['compose_version'] is None
    assert result['server_version'] is None and result['compose_flags_present'] is False
    assert calls == [['--version'], ['compose', 'version', '--short'], ['version', '--format', '{{.Server.Version}}']]


@pytest.mark.skipif(os.name != 'posix', reason='Тестовый исполняемый сценарий использует shebang')
def test_real_subprocess_uses_pinned_test_program_and_clean_environment(tmp_path, monkeypatch):
    executable = tmp_path / 'fake-docker'
    executable.write_text(f'#!{sys.executable}\n' + '''
import os, sys
if 'SYNTHETIC_SECRET' in os.environ or 'DOCKER_HOST' in os.environ:
    raise SystemExit(7)
args = sys.argv[5:]
outputs = {
    ('--version',): 'Docker version 28.5.1, build abc',
    ('compose', 'version', '--short'): 'v2.32.4',
    ('compose', 'up', '--help'): '  --no-build  Never build\\n  --pull string  Pull policy',
    ('version', '--format', '{{.Server.Version}}'): '28.5.1',
}
print(outputs[tuple(args)])
''', encoding='utf-8')
    executable.chmod(0o700)
    monkeypatch.setenv('SYNTHETIC_SECRET', 'not-in-child')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://untrusted.invalid:2375')
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    result = check_docker(executable=str(executable), executable_sha256=digest,
                          socket_path=str(tmp_path / 'local.sock'), timeout_seconds=10)
    assert result['client_version'] == '28.5.1' and result['server_version'] == '28.5.1'
    assert result['compose_version'] == '2.32.4' and result['compose_flags_present'] is True
    assert result['installation_ready'] is False


def test_changed_binary_and_remote_endpoint_are_rejected(binary, tmp_path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        binary[0].write_bytes(b'changed executable')
        return subprocess.CompletedProcess(argv, 0, stdout='Docker version 27.1.0, build abc', stderr='')
    with pytest.raises(DockerPreflightError):
        check_docker(**options(binary, tmp_path), runner=runner)
    assert len(calls) == 1
    binary[0].write_bytes(b'synthetic pinned executable')
    if os.name != 'nt':
        with pytest.raises(DockerPreflightError):
            check_docker(**{**options(binary, tmp_path), 'socket_path': 'tcp://example.invalid'},
                         runner=lambda *args, **kwargs: pytest.fail('remote execution'))


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_api_contract_and_localized_failure(binary, tmp_path, monkeypatch, language):
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    params = {**options(binary, tmp_path), 'timeout_seconds': 5}
    query = {'api_major': 1, 'request_id': 'docker-test', 'operation': 'check-docker-engine',
             'language': language, 'params': params}
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 28 and 'check-docker-engine' in caps['mutating_operations']
    report = {'client_version': '28.5.1', 'compose_version': None, 'server_version': None,
              'compose_flags_present': False, 'installation_ready': False, 'images_verified': False}
    monkeypatch.setattr(api, 'check_docker', lambda **kwargs: report)
    assert api.handle(query)['result'] == report
    def fail(**kwargs):
        raise DockerPreflightError('SECRET-SENTINEL')
    monkeypatch.setattr(api, 'check_docker', fail)
    response = api.handle(query)
    assert response['error']['code'] == 'docker_preflight_failed'
    assert 'SECRET-SENTINEL' not in json.dumps(response) and str(tmp_path) not in json.dumps(response)
    query['params']['timeout_seconds'] = True
    assert not validator.is_valid(query) and api.handle(query)['error']['code'] == 'invalid_request'
    query['params']['timeout_seconds'] = 5
    query['params']['socket_path'] = ''
    assert not validator.is_valid(query) and api.handle(query)['error']['code'] == 'invalid_request'


def test_cli_docker_check_without_manifest(binary, tmp_path, monkeypatch, capsys):
    report = {'client_version': '28.5.1', 'compose_version': None, 'server_version': None,
              'compose_flags_present': False, 'installation_ready': False, 'images_verified': False}
    calls = []
    monkeypatch.setattr(ctl, 'check_docker', lambda **kwargs: calls.append(kwargs) or report)
    args = ['docker-check', '--executable', str(binary[0]), '--executable-sha256', binary[1], '--json']
    if os.name != 'nt':
        args += ['--socket-path', str(tmp_path / 'local.sock')]
    assert ctl.main(args) == 0
    output = capsys.readouterr()
    assert output.err == '' and json.loads(output.out)['data']['docker'] == report
    assert calls == [{**options(binary, tmp_path), 'timeout_seconds': 5}]
