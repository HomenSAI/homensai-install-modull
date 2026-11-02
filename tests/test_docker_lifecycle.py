"""Одноразовый цикл Compose с фиктивным daemon; реальный Docker не вызывается."""
import json
import base64
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

import pytest

from test_api_signed_preflight import signed_request
from test_docker_recipe import signed_plan
from test_docker_prepare import preparation
from test_docker_images import image_check, inspect_query
from shared import api, docker_images, docker_lifecycle
from shared.docker_lifecycle import DockerLifecycleError, _status_records, _verify_runtime, control_signed_compose, status_signed_compose
from shared.effect_receipt import read_effect_receipt
from shared.i18n import translate
from examples.demo_product.installer_client import InstallerClient

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux') or platform.machine().lower() != 'x86_64',
                                reason='Ограниченное управление требует Linux x86-64')


def runtime_views(project, service):
    ports = None
    if 'ports' in service:
        ip, source, target = service['ports'][0].split(':')
        ports = {target + '/tcp': [{'HostIp': ip, 'HostPort': source}]}
    config = {'User': service['user'], 'Cmd': [x.replace('$$', '$') for x in service['command']], 'Entrypoint': []}
    host = {'ReadonlyRootfs': True, 'Privileged': False, 'CapAdd': [], 'CapDrop': ['ALL'], 'Devices': [], 'DeviceRequests': None,
            'SecurityOpt': ['no-new-privileges'], 'NetworkMode': 'bridge', 'PortBindings': ports}
    mounts = [{'Type': 'volume', 'Name': project + '_data', 'Destination': '/data', 'RW': True}]
    return config, host, mounts, {project + '_default': {}}


class FakeDaemon:
    def __init__(self, options):
        path = Path(options['work_dir']) / ('compose-' + options['instance_id'] + '.json')
        self.compose = json.loads(path.read_text())
        self.state = 'absent'
        self.calls = []
        self.conflict = None
        self.crash_after_start = False
        self.crash_after_stop = False
        self.bad_image = False
        self.bad_hash = False
        self.unsafe_runtime = False

    def __call__(self, argv, *, timeout, env):
        assert env['DOCKER_CONFIG'] == env['HOME'] and 'SYNTHETIC_DOCKER_SECRET' not in env
        self.calls.append(argv)
        args = argv[5:]
        project, services = self.compose['name'], self.compose['services']
        if args[:2] == ['image', 'inspect']:
            output = json.dumps('sha256:' + 'd' * 64 if args[3] in ('{{json .Id}}', '{{json .ID}}') else [args[-1]])
        elif args == ['compose', 'version', '--short']:
            output = 'v2.30.3\n'
        elif args == ['compose', 'up', '--help']:
            output = '  --no-build\n  --no-recreate\n  --pull string\n'
        elif args[:2] == ['container', 'ls']:
            output = 'unrelated-id\n' if self.conflict == 'container' else ''
        elif args == ['volume', 'ls', '--quiet']:
            output = project + '_data\n' if self.conflict == 'volume' else ''
        elif args == ['network', 'ls', '--format', '{{.Name}}']:
            output = project + '_default\n' if self.conflict == 'network' else ''
        elif args[:4] == ['compose', '-p', project, '-f']:
            tail = args[5:]
            if tail == ['up', '--detach', '--no-build', '--no-recreate', '--pull', 'never']:
                self.state = 'running'
                if self.crash_after_start:
                    raise OSError('synthetic unknown result')
                output = ''
            elif tail == ['stop', '--timeout', '10']:
                self.state = 'exited'
                if self.crash_after_stop:
                    raise OSError('synthetic unknown stop')
                output = ''
            elif tail == ['ps', '--all', '--format', 'json']:
                output = '\n'.join(json.dumps({'ID': format(index + 1, '064x'), 'Project': project,
                    'Service': name, 'State': self.state}) for index, name in enumerate(services))
            elif tail == ['config', '--hash', '*']:
                output = '\n'.join(name + ' ' + 'f' * 64 for name in services)
            else:
                raise AssertionError('неизвестная команда Compose')
        elif args[:3] == ['inspect', '--format', '{{json .Image}}']:
            output = json.dumps('sha256:' + ('e' if self.bad_image else 'd') * 64)
        elif args[:3] == ['inspect', '--format', '{{json (index .Config.Labels "com.docker.compose.config-hash")}}']:
            output = json.dumps(('a' if self.bad_hash else 'f') * 64)
        elif args[:2] == ['inspect', '--format'] and args[2] in ('{{json .Config}}', '{{json .HostConfig}}',
                                                                  '{{json .Mounts}}', '{{json .NetworkSettings.Networks}}'):
            name = list(services)[int(args[-1], 16) - 1]
            views = runtime_views(project, services[name])
            if self.unsafe_runtime:
                views[1]['Privileged'] = True
            index = ('{{json .Config}}', '{{json .HostConfig}}', '{{json .Mounts}}',
                     '{{json .NetworkSettings.Networks}}').index(args[2])
            output = json.dumps(views[index])
        else:
            raise AssertionError('неизвестная команда Docker')
        return subprocess.CompletedProcess(argv, 0, stdout=output)


@pytest.fixture
def lifecycle(image_check, monkeypatch):
    package, envelope, options = image_check
    daemon = FakeDaemon(options)
    monkeypatch.setattr(docker_images, '_bounded_run', daemon)
    monkeypatch.setattr(docker_lifecycle, '_bounded_run', daemon)
    params = dict(options)
    params['preparation_id'] = params.pop('operation_id')
    return package, envelope, params, daemon


def actions(daemon, name):
    return [argv for argv in daemon.calls if name in argv]


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'orphan', 'bad_id', 'wrong_project', 'wrong_state'])
def test_container_status_requires_exact_components_and_identities(change):
    services = {'web': {}, 'bot': {}}
    rows = [{'ID': 'a' * 64, 'Project': 'project', 'Service': 'web', 'State': 'running'},
            {'ID': 'b' * 64, 'Project': 'project', 'Service': 'bot', 'State': 'running'}]
    if change == 'missing':
        rows.pop()
    elif change == 'duplicate':
        rows[1]['Service'] = 'web'
    elif change == 'orphan':
        rows[1]['Service'] = 'other'
    elif change == 'bad_id':
        rows[1]['ID'] = '--malicious'
    elif change == 'wrong_project':
        rows[1]['Project'] = 'other'
    else:
        rows[1]['State'] = 'exited'
    with pytest.raises(DockerLifecycleError):
        _status_records('\n'.join(json.dumps(row) for row in rows), 'project', services)


def test_compose_short_container_id_is_accepted():
    # Compose v2 may report the daemon's 12-character abbreviated ID.
    row = {'ID': '08acf1fba08d', 'Project': 'project', 'Service': 'web', 'State': 'running'}
    assert _status_records(json.dumps(row), 'project', {'web': {}}) == {'web': row['ID']}


@pytest.mark.parametrize('change', ['command', 'user', 'entrypoint', 'readwrite_root', 'privileged',
    'capability', 'missing_drops', 'device', 'security', 'host_network', 'host_pid', 'host_ipc', 'host_uts',
    'extra_network', 'bind_mount', 'extra_mount',
    'public_port', 'extra_port'])
def test_runtime_rejects_unsafe_or_unexpected_settings(change):
    service = {'user': '10001:10001', 'command': ['python', '-c', 'print("$$HOME")'],
               'ports': ['127.0.0.1:43817:8080']}
    config, host, mounts, networks = deepcopy(runtime_views('project', service))
    _verify_runtime(config, host, mounts, networks, service, 'project')
    if change == 'command': config['Cmd'] = ['sh']
    elif change == 'user': config['User'] = '0:0'
    elif change == 'entrypoint': config['Entrypoint'] = ['/bin/sh']
    elif change == 'readwrite_root': host['ReadonlyRootfs'] = False
    elif change == 'privileged': host['Privileged'] = True
    elif change == 'capability': host['CapAdd'] = ['SYS_ADMIN']
    elif change == 'missing_drops': host['CapDrop'] = []
    elif change == 'device': host['Devices'] = [{'PathOnHost': '/dev/sda'}]
    elif change == 'security': host['SecurityOpt'] = ['seccomp=unconfined']
    elif change == 'host_network': host['NetworkMode'] = 'host'
    elif change == 'host_pid': host['PidMode'] = 'host'
    elif change == 'host_ipc': host['IpcMode'] = 'host'
    elif change == 'host_uts': host['UTSMode'] = 'host'
    elif change == 'extra_network': networks['foreign'] = {}
    elif change == 'bind_mount': mounts[0]['Type'] = 'bind'
    elif change == 'extra_mount': mounts.append({'Type': 'bind', 'Destination': '/host'})
    elif change == 'public_port': host['PortBindings']['8080/tcp'][0]['HostIp'] = '0.0.0.0'
    else: host['PortBindings']['9000/tcp'] = [{'HostIp': '0.0.0.0', 'HostPort': '9000'}]
    with pytest.raises(DockerLifecycleError):
        _verify_runtime(config, host, mounts, networks, service, 'project')


def test_signed_start_stop_and_retries_do_not_repeat_actions(lifecycle):
    package, envelope, options, daemon = lifecycle
    start = control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert start['state'] == 'running' and start['container_count'] == 3
    assert start['health_verified'] is False and start['all_writers_verified'] is False
    assert control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32) == start
    assert len(actions(daemon, 'up')) == 2  # --help и одна команда up.
    stop = control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32)
    assert stop['state'] == 'stopped'
    assert control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32) == stop
    assert len(actions(daemon, 'stop')) == 1
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)


def test_status_reports_bound_receipt_without_contacting_daemon(lifecycle, monkeypatch):
    package, envelope, options, daemon = lifecycle
    assert status_signed_compose(package, envelope, **{k: v for k, v in options.items() if k != 'timeout_seconds'})['state'] == 'not_recorded'
    control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    count = len(daemon.calls)
    status = status_signed_compose(package, envelope, **{k: v for k, v in options.items() if k != 'timeout_seconds'})
    assert status == {'state': 'running', 'receipt_present': True, 'daemon_state_verified': False,
                      'recovery_authorized': False}
    assert len(daemon.calls) == count
    changed = {k: v for k, v in options.items() if k != 'timeout_seconds'}
    changed['executable_sha256'] = 'f' * 64
    with pytest.raises(DockerLifecycleError):
        status_signed_compose(package, envelope, **changed)


def test_status_api_reads_pending_without_socket_or_cli(lifecycle, monkeypatch):
    package, envelope, options, daemon = lifecycle
    daemon.bad_image = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    query = inspect_query(package, envelope, {**options, 'operation_id': options['preparation_id']})
    query['operation'] = 'compose-control-status'
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    jsonschema = pytest.importorskip('jsonschema')
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    assert 'compose-control-status' in api.handle({'api_major': 1, 'request_id': 'cap',
                                                    'operation': 'capabilities', 'params': {}})['result']['operations']
    monkeypatch.setattr(docker_lifecycle, '_bounded_run', lambda *a, **kw: pytest.fail('CLI must not run'))
    monkeypatch.setattr(docker_images, '_bounded_run', lambda *a, **kw: pytest.fail('CLI must not run'))
    Path(options['socket_path']).unlink()
    response = api.handle(query)
    assert response['ok'] and response['result'] == {'state': 'start_pending', 'receipt_present': True,
                                                      'daemon_state_verified': False, 'recovery_authorized': False}


def test_status_rejects_corrupted_receipt_and_reports_stop_pending(lifecycle):
    package, envelope, options, daemon = lifecycle
    status_options = {k: v for k, v in options.items() if k != 'timeout_seconds'}
    control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    daemon.crash_after_stop = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32)
    assert status_signed_compose(package, envelope, **status_options)['state'] == 'stop_pending'
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    marker.write_text('{"status":"running"}', encoding='utf-8')
    with pytest.raises(DockerLifecycleError):
        status_signed_compose(package, envelope, **status_options)


@pytest.mark.parametrize('conflict', ['container', 'volume', 'network'])
def test_existing_foreign_resources_block_start(lifecycle, conflict):
    package, envelope, options, daemon = lifecycle
    daemon.conflict = conflict
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert not any('up' in argv and '--detach' in argv for argv in daemon.calls)
    assert not (Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')).exists()


def test_unknown_start_result_stays_pending_and_is_not_repeated(lifecycle):
    package, envelope, options, daemon = lifecycle
    daemon.crash_after_start = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'
    assert len([argv for argv in daemon.calls if '--detach' in argv]) == 1
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert len([argv for argv in daemon.calls if '--detach' in argv]) == 1


def test_reconcile_confirms_unknown_start_and_stop_without_repeating_effects(lifecycle):
    package, envelope, options, daemon = lifecycle
    daemon.crash_after_start = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='reconcile-start', action_id='d' * 32)
    result = control_signed_compose(package, envelope, **options, action='reconcile-start', action_id='b' * 32)
    assert result['state'] == 'running'
    assert control_signed_compose(package, envelope, **options, action='reconcile-start', action_id='b' * 32) == result
    assert len([argv for argv in daemon.calls if '--detach' in argv]) == 1
    daemon.crash_after_stop = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32)
    result = control_signed_compose(package, envelope, **options, action='reconcile-stop', action_id='c' * 32)
    assert result['state'] == 'stopped' and len(actions(daemon, 'stop')) == 1


def test_reconcile_start_is_available_through_api_schema(lifecycle):
    package, envelope, options, daemon = lifecycle
    daemon.crash_after_start = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    query = inspect_query(package, envelope, {**options, 'operation_id': options['preparation_id']})
    query['operation'] = 'control-signed-compose'
    query['params'].update(action='reconcile-start', action_id='b' * 32)
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    result = api.handle(query)
    assert result['ok'] and result['result']['state'] == 'running'
    assert len([argv for argv in daemon.calls if '--detach' in argv]) == 1


@pytest.mark.parametrize('failure', ['bad_hash', 'bad_image', 'missing', 'wrong_state'])
def test_reconcile_requires_matching_running_services_and_configuration(lifecycle, failure):
    package, envelope, options, daemon = lifecycle
    daemon.crash_after_start = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    if failure == 'bad_hash':
        daemon.bad_hash = True
    elif failure == 'bad_image':
        daemon.bad_image = True
    elif failure == 'missing':
        del daemon.compose['services']['web']
    else:
        daemon.state = 'exited'
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='reconcile-start', action_id='b' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'
    assert len([argv for argv in daemon.calls if '--detach' in argv]) == 1
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert len([argv for argv in daemon.calls if '--detach' in argv]) == 1


def test_wrong_container_image_does_not_confirm_start(lifecycle):
    package, envelope, options, daemon = lifecycle
    daemon.bad_image = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'


def test_wrong_configuration_hash_never_confirms_start(lifecycle):
    package, envelope, options, daemon = lifecycle
    daemon.bad_hash = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'


def test_privileged_container_is_not_confirmed_even_with_valid_compose_label(lifecycle):
    package, envelope, options, daemon = lifecycle
    daemon.unsafe_runtime = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'


def test_changed_configuration_blocks_stop_before_command(lifecycle):
    package, envelope, options, daemon = lifecycle
    control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    daemon.bad_hash = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32)
    assert not actions(daemon, 'stop')
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'running'


def test_mismatched_container_image_id_does_not_confirm_start_or_retry(lifecycle, monkeypatch):
    package, envelope, options, daemon = lifecycle
    calls = []
    def tagged_image(argv, *, timeout, env):
        calls.append(argv)
        if argv[5:8] == ['inspect', '--format', '{{json .Image}}']:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps('sha256:' + 'e' * 64))
        return daemon(argv, timeout=timeout, env=env)
    monkeypatch.setattr(docker_lifecycle, '_bounded_run', tagged_image)
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'
    count = len(calls)
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert len(calls) == count  # A retry must not contact the daemon or start containers again.


def test_daemon_uppercase_image_id_and_unprefixed_hash_are_accepted(lifecycle, monkeypatch):
    package, envelope, options, daemon = lifecycle
    calls = []
    def podman_style(argv, *, timeout, env):
        args = argv[5:]
        calls.append(args)
        if args[:4] == ['image', 'inspect', '--format', '{{json .Id}}']:
            return subprocess.CompletedProcess(argv, 125, stdout='')
        if args[:4] == ['image', 'inspect', '--format', '{{json .ID}}']:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps('d' * 64))
        if args[:3] == ['inspect', '--format', '{{json .Image}}']:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps('d' * 64))
        return daemon(argv, timeout=timeout, env=env)
    monkeypatch.setattr(docker_lifecycle, '_bounded_run', podman_style)
    result = control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert result['state'] == 'running'
    assert any('{{json .ID}}' in args for args in calls)


@pytest.mark.parametrize('failure', ['digest_disappears', 'image_id_changes', 'malformed_id'])
def test_changed_daemon_image_binding_never_confirms_start(lifecycle, monkeypatch, failure):
    package, envelope, options, daemon = lifecycle
    seen = {'digest': 0, 'id': 0}
    def unstable(argv, *, timeout, env):
        args = argv[5:]
        if args[:2] == ['image', 'inspect'] and args[3] == '{{json .RepoDigests}}':
            seen['digest'] += 1
            # The first digest check happens during the image preflight.
            if failure == 'digest_disappears' and seen['digest'] > 3:
                return subprocess.CompletedProcess(argv, 0, stdout='[]')
        if args[:2] == ['image', 'inspect'] and args[3] == '{{json .Id}}':
            seen['id'] += 1
            if failure == 'malformed_id':
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps('wrong-id'))
            if failure == 'image_id_changes' and seen['id'] > 1:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps('sha256:' + 'e' * 64))
        return daemon(argv, timeout=timeout, env=env)
    monkeypatch.setattr(docker_lifecycle, '_bounded_run', unstable)
    monkeypatch.setattr(docker_images, '_bounded_run', unstable)
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'


def test_unknown_stop_result_stays_pending_and_does_not_repeat(lifecycle):
    package, envelope, options, daemon = lifecycle
    control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    daemon.crash_after_stop = True
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32)
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'stop_pending'
    assert len(actions(daemon, 'stop')) == 1
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32)
    assert len(actions(daemon, 'stop')) == 1


def test_stop_allows_a_previously_exited_signed_component_set(lifecycle):
    package, envelope, options, daemon = lifecycle
    control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    daemon.state = 'exited'
    result = control_signed_compose(package, envelope, **options, action='stop', action_id='c' * 32)
    assert result['state'] == 'stopped' and len(actions(daemon, 'stop')) == 1


def test_modified_compose_is_rejected_before_cli(lifecycle):
    package, envelope, options, daemon = lifecycle
    path = Path(options['work_dir']) / ('compose-' + options['instance_id'] + '.json')
    path.write_bytes(b'tampered')
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert daemon.calls == []


def test_sigkill_after_start_request_leaves_pending_and_never_replays(lifecycle):
    package, envelope, options, daemon = lifecycle
    payload = {'package': package, 'envelope': base64.b64encode(envelope).decode(), **options}
    payload['trusted_public_key'] = base64.b64encode(payload['trusted_public_key']).decode()
    script = r'''
import base64, json, os, signal, subprocess, sys
from shared import docker_images, docker_lifecycle
p = json.load(sys.stdin)
p['envelope'] = base64.b64decode(p['envelope'])
p['trusted_public_key'] = base64.b64decode(p['trusted_public_key'])
p['modes'] = tuple(p['modes'])
def fake(argv, **kwargs):
    args = argv[5:]
    if args[:2] == ['image', 'inspect']:
        output = json.dumps([args[-1]])
    elif args == ['compose', 'version', '--short']:
        output = 'v2.30.3'
    elif args == ['compose', 'up', '--help']:
        output = '  --no-build\n  --no-recreate\n  --pull string\n'
    elif args[:2] == ['container', 'ls'] or args[:2] == ['volume', 'ls'] or args[:2] == ['network', 'ls']:
        output = ''
    elif '--detach' in args:
        os.kill(os.getpid(), signal.SIGKILL)
    else:
        raise AssertionError('unexpected fake command')
    return subprocess.CompletedProcess(argv, 0, stdout=output)
docker_images._bounded_run = fake
docker_lifecycle._bounded_run = fake
docker_lifecycle.control_signed_compose(p.pop('package'), p.pop('envelope'),
    **p, action='start', action_id='b' * 32)
'''
    process = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps(payload),
                             text=True, capture_output=True, timeout=90)
    assert process.returncode == -signal.SIGKILL, process.stderr
    marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'start_pending'
    with pytest.raises(DockerLifecycleError):
        control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    assert daemon.calls == []


def test_api_schema_start_and_stop_with_fake_daemon(lifecycle):
    jsonschema = pytest.importorskip('jsonschema')
    package, envelope, options, daemon = lifecycle
    api_options = {**options, 'operation_id': options['preparation_id']}
    query = inspect_query(package, envelope, api_options)
    query['operation'] = 'control-signed-compose'
    query['params'].update(action='start', action_id='b' * 32)
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 36 and query['operation'] in caps['mutating_operations']
    conflict = {**query, 'params': {**query['params'], 'action_id': query['params']['idempotency_key']}}
    assert api.handle(conflict)['error']['code'] == 'invalid_request'
    result = api.handle(query)
    assert result['ok'] and result['result']['state'] == 'running'
    query['params'].update(action='stop', action_id='c' * 32)
    assert api.handle(query)['result']['state'] == 'stopped'
    assert len(actions(daemon, 'stop')) == 1
    query['params']['action'] = 'delete'
    assert api.handle(query)['error']['code'] == 'invalid_request'


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_api_error_is_sanitized_and_does_not_expose_paths(lifecycle, language):
    package, envelope, options, daemon = lifecycle
    daemon.conflict = 'volume'
    query = inspect_query(package, envelope, {**options, 'operation_id': options['preparation_id']}, language)
    query['operation'] = 'control-signed-compose'
    query['params'].update(action='start', action_id='b' * 32)
    result = api.handle(query)
    assert result['error']['code'] == 'docker_control_failed'
    assert result['error']['message'] == translate(language, 'api.docker_control_failed')
    assert options['work_dir'] not in json.dumps(result)


def test_independent_client_controls_only_synthetic_daemon(image_check, tmp_path):
    package, envelope, options = image_check
    cli = Path(options['executable'])
    state = tmp_path / 'synthetic-daemon-state.json'
    compose = Path(options['work_dir']) / ('compose-' + options['instance_id'] + '.json')
    script = '''#!/usr/bin/python3
import json, pathlib, sys
state = pathlib.Path(STATE_PATH)
compose = json.loads(pathlib.Path(COMPOSE_PATH).read_text())
args = sys.argv[5:]
project, services = compose['name'], compose['services']
if args[:2] == ['image', 'inspect']:
    output = json.dumps('sha256:' + 'd' * 64 if args[3] in ('{{json .Id}}', '{{json .ID}}') else [args[-1]])
elif args == ['compose', 'version', '--short']:
    output = 'v2.30.3'
elif args == ['compose', 'up', '--help']:
    output = '  --no-build\\n  --no-recreate\\n  --pull string\\n'
elif args[:2] in (['container', 'ls'], ['volume', 'ls'], ['network', 'ls']):
    output = ''
elif args[:4] == ['compose', '-p', project, '-f']:
    tail = args[5:]
    if tail == ['up', '--detach', '--no-build', '--no-recreate', '--pull', 'never']:
        state.write_text('running')
        output = ''
    elif tail == ['stop', '--timeout', '10']:
        state.write_text('exited')
        output = ''
    elif tail == ['ps', '--all', '--format', 'json']:
        output = '\\n'.join(json.dumps({'ID': format(i+1, '064x'), 'Project': project,
            'Service': name, 'State': state.read_text()}) for i, name in enumerate(services))
    elif tail == ['config', '--hash', '*']:
        output = '\\n'.join(name + ' ' + 'f' * 64 for name in services)
    else:
        raise SystemExit(2)
elif args[:3] == ['inspect', '--format', '{{json .Image}}']:
    output = json.dumps('sha256:' + 'd' * 64)
elif args[:3] == ['inspect', '--format', '{{json (index .Config.Labels "com.docker.compose.config-hash")}}']:
    output = json.dumps('f' * 64)
elif args[:2] == ['inspect', '--format']:
    service = services[list(services)[int(args[-1],16)-1]]
    ports = None
    if 'ports' in service:
        ip, source, target = service['ports'][0].split(':')
        ports = {target + '/tcp': [{'HostIp': ip, 'HostPort': source}]}
    views = {'{{json .Config}}': {'User': service['user'], 'Cmd': [x.replace('$$', '$') for x in service['command']], 'Entrypoint': []},
             '{{json .HostConfig}}': {'ReadonlyRootfs': True, 'Privileged': False, 'CapAdd': [], 'CapDrop': ['ALL'], 'Devices': [],
               'DeviceRequests': None, 'SecurityOpt': ['no-new-privileges'], 'NetworkMode': 'bridge', 'PortBindings': ports},
             '{{json .Mounts}}': [{'Type':'volume','Name':project+'_data','Destination':'/data','RW':True}],
             '{{json .NetworkSettings.Networks}}': {project+'_default': {}}}
    output = json.dumps(views[args[2]])
else:
    raise SystemExit(2)
sys.stdout.write(output + ('\\n' if output else ''))
'''.replace('STATE_PATH', repr(str(state))).replace('COMPOSE_PATH', repr(str(compose)))
    cli.write_text(script)
    options['executable_sha256'] = hashlib.sha256(cli.read_bytes()).hexdigest()
    query = inspect_query(package, envelope, options)
    query['params'].update(action='start', action_id='b' * 32)
    client = InstallerClient(timeout=90)
    start = client.call('control-signed-compose', query['params'], min_minor=36)
    assert start['state'] == 'running' and state.read_text() == 'running'
    query['params'].update(action='stop', action_id='c' * 32)
    stop = client.call('control-signed-compose', query['params'], min_minor=36)
    assert stop['state'] == 'stopped' and state.read_text() == 'exited'
