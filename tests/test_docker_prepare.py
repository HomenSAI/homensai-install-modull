"""Подготовка подписанного Compose-файла без Docker и контейнеров."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

import pytest

from test_api_signed_preflight import signed_request
from test_docker_recipe import signed_plan, api_query
from shared import api
from shared.docker_prepare import DockerPrepareError, prepare_signed_compose
from shared.effect_receipt import read_effect_receipt
from shared.i18n import translate
from examples.demo_product.installer_client import InstallerClient

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux') or platform.machine().lower() != 'x86_64',
                                reason='Тестируется ограниченная операция Linux x86-64')


@pytest.fixture
def preparation(signed_plan, tmp_path):
    _, recipe, build = signed_plan
    package, envelope, options = build(recipe)
    work = tmp_path / 'protected-work'
    work.mkdir(mode=0o700)
    return str(package), envelope, {**options, 'work_dir': str(work), 'operation_id': 'a' * 32}


def test_preparation_is_durable_idempotent_and_rejects_tampering(preparation):
    package, envelope, options = preparation
    result = prepare_signed_compose(package, envelope, **options)
    work = Path(options['work_dir'])
    path = work / result['compose_name']
    before = path.read_bytes()
    assert result['state'] == 'prepared' and result['containers_started'] is False
    assert result['images_verified'] is False and result['host_verified'] is False
    assert path.stat().st_mode & 0o777 == 0o600
    assert prepare_signed_compose(package, envelope, **options) == result
    assert path.read_bytes() == before
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **{**options, 'operation_id': 'b' * 32})
    assert path.read_bytes() == before
    path.write_bytes(b'tampered')
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **options)
    assert path.read_bytes() == b'tampered'


def test_verify_only_requires_done_and_never_creates_compose(preparation):
    package, envelope, options = preparation
    work = Path(options['work_dir'])
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **options, verify_only=True)
    assert not (work / ('compose-' + options['instance_id'] + '.json')).exists()
    assert not (work / ('prepare-compose-' + options['operation_id'] + '.json')).exists()
    prepared = prepare_signed_compose(package, envelope, **options)
    verified = prepare_signed_compose(package, envelope, **options, verify_only=True)
    assert verified == {**prepared, 'state': 'verified'}


def test_untracked_destination_and_unsafe_work_directory_never_overwrite(preparation):
    package, envelope, options = preparation
    work = Path(options['work_dir'])
    destination = work / ('compose-' + options['instance_id'] + '.json')
    destination.write_bytes(b'preserve')
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **options)
    assert destination.read_bytes() == b'preserve'
    assert not (work / ('prepare-compose-' + options['operation_id'] + '.json')).exists()
    work.chmod(0o755)
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **options)


@pytest.mark.parametrize('change', ['permissions', 'hardlink', 'symlink'])
def test_completed_file_must_remain_private_regular_and_unlinked(preparation, tmp_path, change):
    package, envelope, options = preparation
    result = prepare_signed_compose(package, envelope, **options)
    path = Path(options['work_dir']) / result['compose_name']
    if change == 'permissions':
        path.chmod(0o644)
    elif change == 'hardlink':
        os.link(path, tmp_path / 'hardlink')
    else:
        replacement = tmp_path / 'outside.json'
        replacement.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(replacement)
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **options)


def test_interruption_after_creation_remains_pending_without_replay(preparation, monkeypatch):
    from shared import docker_prepare
    class Crash(BaseException):
        pass
    package, envelope, options = preparation
    original = docker_prepare._create_compose
    def crash(*args):
        original(*args)
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(docker_prepare, '_create_compose', crash)
        with pytest.raises(Crash):
            prepare_signed_compose(package, envelope, **options)
    work = Path(options['work_dir'])
    marker = work / ('prepare-compose-' + options['operation_id'] + '.json')
    path = work / ('compose-' + options['instance_id'] + '.json')
    before = path.read_bytes()
    assert read_effect_receipt(marker)['status'] == 'pending'
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **options)
    assert path.read_bytes() == before and read_effect_receipt(marker)['status'] == 'pending'


def test_real_sigkill_after_file_creation_keeps_pending(preparation):
    package, envelope, options = preparation
    payload = {'package': package, 'envelope': base64.b64encode(envelope).decode(), **options}
    payload['trusted_public_key'] = base64.b64encode(payload['trusted_public_key']).decode()
    script = r'''
import base64, json, os, signal, sys
import shared.docker_prepare as module
p = json.load(sys.stdin)
p['envelope'] = base64.b64decode(p['envelope'])
p['trusted_public_key'] = base64.b64decode(p['trusted_public_key'])
p['modes'] = tuple(p['modes'])
original = module._create_compose
def crash(*args):
    original(*args)
    os.kill(os.getpid(), signal.SIGKILL)
module._create_compose = crash
module.prepare_signed_compose(p.pop('package'), p.pop('envelope'), **p)
'''
    result = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps(payload),
                            text=True, capture_output=True, timeout=90)
    assert result.returncode == -signal.SIGKILL, result.stderr
    work = Path(options['work_dir'])
    marker = work / ('prepare-compose-' + options['operation_id'] + '.json')
    assert read_effect_receipt(marker)['status'] == 'pending'
    with pytest.raises(DockerPrepareError):
        prepare_signed_compose(package, envelope, **options)


def test_competing_operations_cannot_replace_same_instance(preparation):
    package, envelope, options = preparation
    def call(operation_id):
        try:
            return prepare_signed_compose(package, envelope, **{**options, 'operation_id': operation_id})
        except DockerPrepareError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call, ['a' * 32, 'b' * 32]))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    path = Path(options['work_dir']) / winner['compose_name']
    assert path.is_file() and winner['compose_sha256']


def test_prepare_api_and_independent_client(preparation):
    jsonschema = pytest.importorskip('jsonschema')
    package, envelope, options = preparation
    query = api_query(package, envelope, options)
    query['operation'] = 'prepare-signed-compose'
    query['params'].update(work_dir=options['work_dir'], idempotency_key=options['operation_id'])
    schema = json.loads((Path(__file__).resolve().parents[1] / 'shared/api-v1-request.schema.json').read_text())
    assert jsonschema.Draft202012Validator(schema).is_valid(query)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 34 and query['operation'] in caps['mutating_operations']
    response = api.handle(query)
    assert response['ok'] and response['result']['containers_started'] is False
    assert InstallerClient(timeout=90).call(query['operation'], query['params'], min_minor=34) == response['result']


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_prepare_api_untrusted_key_is_redacted(preparation, language):
    package, envelope, options = preparation
    query = api_query(package, envelope, options, language)
    query['operation'] = 'prepare-signed-compose'
    query['params'].update(work_dir=options['work_dir'], idempotency_key=options['operation_id'])
    query['params']['trusted_public_key'] = base64.b64encode(b'x' * 32).decode()
    response = api.handle(query)
    assert response['error']['code'] == 'docker_prepare_failed'
    assert response['error']['message'] == translate(language, 'api.docker_prepare_failed')
    assert package not in json.dumps(response) and not list(Path(options['work_dir']).iterdir())


def test_prepare_api_not_advertised_for_unsupported_host(preparation, monkeypatch):
    package, envelope, options = preparation
    query = api_query(package, envelope, options)
    query['operation'] = 'prepare-signed-compose'
    query['params'].update(work_dir=options['work_dir'], idempotency_key=options['operation_id'])
    monkeypatch.setattr(api, '_systemd_plan_available', lambda: False)
    caps = api.handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert query['operation'] not in caps['operations']
    assert api.handle(query)['error']['code'] == 'unsupported_operation'
    assert not list(Path(options['work_dir']).iterdir())
