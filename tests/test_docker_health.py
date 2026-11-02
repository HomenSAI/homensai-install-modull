"""Signed Compose health is observed without starting or stopping services."""
import json
from pathlib import Path
import platform
import sys

import pytest

from test_api_signed_preflight import signed_request
from test_docker_recipe import signed_plan
from test_docker_prepare import preparation
from test_docker_images import image_check, inspect_query
from test_docker_lifecycle import lifecycle
from shared import api, docker_health
from shared.docker_lifecycle import control_signed_compose
from shared.health import HealthError

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux') or platform.machine().lower() != 'x86_64',
                                reason='Signed Compose control requires Linux x86-64')


def query_for(package, envelope, options):
    request = inspect_query(package, envelope, {**options, 'operation_id': options['preparation_id']})
    request['operation'] = 'check-signed-compose-health'
    request['params'].update(action_id='b' * 32, health_timeout_seconds=3)
    return request


def test_health_requires_confirmed_start_and_never_starts_container(lifecycle, monkeypatch):
    package, envelope, options, daemon = lifecycle
    monkeypatch.setattr(docker_health, 'check_local_health', lambda *a, **k: pytest.fail('health must not run'))
    response = api.handle(query_for(package, envelope, options))
    assert response['error']['code'] == 'health_check_failed'
    assert daemon.calls == []
    assert not (Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')).exists()


def test_health_uses_signed_endpoint_and_checks_running_state_twice(lifecycle, monkeypatch):
    package, envelope, options, daemon = lifecycle
    control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    observed = []
    monkeypatch.setattr(docker_health, 'check_local_health', lambda *a, **kw: observed.append((a, kw)))
    request = query_for(package, envelope, options)
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(request)
    response = api.handle(request)
    assert response['ok'] and response['result'] == {'health_response_matches': True,
        'daemon_state_verified': True, 'process_identity_verified': False,
        'all_components_healthy': False, 'installation_complete': False}
    assert observed == [((8820,), {'product_id': 'homensai-rent-demo', 'version': '1',
                                    'path': '/healthz', 'timeout': 3})]
    assert len([call for call in daemon.calls if '--detach' in call]) == 1


@pytest.mark.parametrize('change', ['failed_health', 'daemon_changed', 'receipt_removed'])
def test_health_failure_or_changed_daemon_is_not_success(lifecycle, monkeypatch, change):
    package, envelope, options, daemon = lifecycle
    control_signed_compose(package, envelope, **options, action='start', action_id='b' * 32)
    def health(*args, **kwargs):
        if change == 'failed_health':
            raise HealthError('synthetic secret')
        if change == 'daemon_changed':
            daemon.state = 'exited'
        else:
            marker = Path(options['work_dir']) / ('compose-lifecycle-' + options['instance_id'] + '.json')
            marker.unlink()
    monkeypatch.setattr(docker_health, 'check_local_health', health)
    response = api.handle(query_for(package, envelope, options))
    assert response['error']['code'] == 'health_check_failed'
    assert 'synthetic secret' not in json.dumps(response)
    assert len([call for call in daemon.calls if '--detach' in call]) == 1
