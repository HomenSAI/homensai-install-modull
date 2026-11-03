"""The interactive adapter must not start installation without confirmation."""
import base64
import json
from pathlib import Path

import pytest

from shared import api
from shared.interactive_install import main, read_config, run
from test_api_signed_preflight import signed_request
from test_fresh_controller import workflow
from test_fresh_controller_api import query_for
from test_update_assembly import assembled


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_real_api_inspection_then_cancel_does_not_start_install(workflow, language):
    params = query_for(workflow, language)['params']
    events, lines = [], []
    def handle(request):
        events.append(request['operation'])
        return api.handle(request)
    assert run(params, language, ask=lambda _: 'no', write=lines.append, handle=handle) == 2
    assert events == ['inspect-portable-linux-install']
    assert not list(Path(workflow['work_dir']).glob('install-*.json'))
    assert lines


@pytest.mark.parametrize('state', ['no_checkpoint', 'pending', 'recorded_complete'])
def test_one_confirmed_call_preserves_request_and_reports_recorded_state(state):
    params = {'expected_product_id': 'demo', 'expected_version': '2', 'install_root': '/tmp/unsafe\x1b[2J',
              'package_path': '/tmp/package', 'unit_dir': '/tmp/units', 'work_dir': '/tmp/work',
              'modes': ['autonomous'], 'trusted_public_key': base64.b64encode(b'x' * 32).decode('ascii')}
    calls, lines = [], []
    def handle(request):
        calls.append(request)
        return {'ok': True, 'result': {'state': state if len(calls) == 1 else 'recorded_complete'}}
    assert run(params, 'en', ask=lambda prompt: 'INSTALL', write=lines.append, handle=handle) == 0
    assert [item['operation'] for item in calls] == ['inspect-portable-linux-install', 'install-portable-linux']
    assert all(item['params'] is params for item in calls)
    assert not any('\x1b' in line for line in lines)
    assert 'not verified' in lines[-1]


def test_failed_inspection_or_interrupted_confirmation_never_installs():
    params = {'expected_product_id': 'demo', 'expected_version': '2', 'install_root': '/tmp/test',
              'package_path': '/tmp/package', 'unit_dir': '/tmp/units', 'work_dir': '/tmp/work',
              'modes': ['autonomous'], 'trusted_public_key': base64.b64encode(b'x' * 32).decode('ascii')}
    calls = []
    def fail(request):
        calls.append(request['operation'])
        return {'ok': False}
    assert run(params, 'en', ask=lambda _: pytest.fail('asked despite failure'), write=lambda _: None, handle=fail) == 1
    def interrupted(_):
        raise EOFError()
    def inspect(request):
        calls.append(request['operation'])
        return {'ok': True, 'result': {'state': 'pending'}}
    assert run(params, 'en', ask=interrupted, write=lambda _: None, handle=inspect) == 2
    assert calls == ['inspect-portable-linux-install', 'inspect-portable-linux-install']


def test_failed_installation_is_not_retried_or_printed():
    params = {'expected_product_id': 'demo', 'expected_version': '2', 'install_root': '/tmp/test',
              'package_path': '/tmp/package', 'unit_dir': '/tmp/units', 'work_dir': '/tmp/work',
              'modes': ['autonomous'], 'trusted_public_key': base64.b64encode(b'x' * 32).decode('ascii')}
    calls, lines = [], []
    def handle(request):
        calls.append(request['operation'])
        if len(calls) == 1:
            return {'ok': True, 'result': {'state': 'no_checkpoint'}}
        return {'ok': False, 'error': {'message': 'synthetic secret'}}
    assert run(params, 'en', ask=lambda _: 'INSTALL', write=lines.append, handle=handle) == 1
    assert calls == ['inspect-portable-linux-install', 'install-portable-linux']
    assert all('synthetic secret' not in line for line in lines)


def test_config_rejects_duplicates_and_symlinks(tmp_path):
    config = tmp_path / 'request.json'
    config.write_text('{"a":1,"a":2}', encoding='utf-8')
    with pytest.raises(ValueError):
        read_config(config)
    config.write_text(json.dumps({'a': 'public'}), encoding='utf-8')
    assert read_config(config) == {'a': 'public'}
    link = tmp_path / 'link.json'
    link.symlink_to(config)
    with pytest.raises(OSError):
        read_config(link)


def test_invalid_arguments_never_echo_accidental_secret(capsys):
    assert main(['--request-file', 'valid.json', '--password', 'synthetic-secret']) == 2
    assert 'synthetic-secret' not in capsys.readouterr().err
