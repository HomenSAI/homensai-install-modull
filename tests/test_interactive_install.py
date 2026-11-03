"""The interactive adapter must not start installation without confirmation."""
import base64
import json
import os
from pathlib import Path

import pytest

from shared import api
from shared.i18n import translate
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


def test_config_changed_during_read_is_rejected(tmp_path, monkeypatch):
    config = tmp_path / 'request.json'
    config.write_text('{"first":1}', encoding='utf-8')
    original = os.fstat
    count = 0
    def changing(fd):
        nonlocal count
        count += 1
        if count == 2:
            config.write_text('{"replacement":22}', encoding='utf-8')
        return original(fd)
    monkeypatch.setattr('shared.interactive_install.os.fstat', changing)
    with pytest.raises(ValueError):
        read_config(config)


def test_invalid_arguments_never_echo_accidental_secret(capsys):
    assert main(['--request-file', 'valid.json', '--password', 'synthetic-secret']) == 2
    assert 'synthetic-secret' not in capsys.readouterr().err


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_missing_terminal_error_is_localized(monkeypatch, capsys, language):
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    assert main(['--request-file', '/missing', '--language', language]) == 2
    assert capsys.readouterr().err.strip() == translate(language, 'interactive.input_failed')


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_inspect_cli_without_terminal_uses_real_api_and_does_not_install(workflow, tmp_path, monkeypatch, capsys, language):
    params = query_for(workflow, language)['params']
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(params), encoding='utf-8')
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    assert main(['--request-file', str(request), '--language', language, '--inspect']) == 0
    output = capsys.readouterr().out
    assert translate(language, 'interactive.inspection_only') in output
    assert not list(Path(workflow['work_dir']).glob('install-*.json'))


def test_pending_inspection_displays_progress_safely_without_prompt():
    params = {'expected_product_id': 'demo', 'expected_version': '2', 'install_root': '/tmp/test',
              'package_path': '/tmp/package', 'unit_dir': '/tmp/units', 'work_dir': '/tmp/work',
              'modes': ['autonomous'], 'trusted_public_key': base64.b64encode(b'x' * 32).decode('ascii')}
    calls, lines = [], []
    def handle(request):
        calls.append(request['operation'])
        return {'ok': True, 'result': {'state': 'pending', 'completed_steps': 2,
                                      'total_steps': 6, 'current_step': 'stage\x1b[2J'}}
    assert run(params, 'en', ask=lambda _: pytest.fail('unexpected prompt'), write=lines.append,
               handle=handle, inspect_only=True) == 0
    assert calls == ['inspect-portable-linux-install']
    assert any('2/6' in line for line in lines)
    assert any('stage\\u001b[2J' in line for line in lines)
    assert not any('\x1b' in line for line in lines)


def test_inspect_with_event_log_uses_fixed_schema(workflow, tmp_path, monkeypatch, capsys):
    params = query_for(workflow, 'ru')['params']
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(params), encoding='utf-8')
    directory = tmp_path / 'private-log'
    directory.mkdir(mode=0o700)
    logfile = directory / 'events.jsonl'
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    assert main(['--request-file', str(request), '--language', 'ru', '--inspect', '--event-log', str(logfile)]) == 0
    rows = [json.loads(line) for line in logfile.read_text().splitlines()]
    assert [row['event'] for row in rows] == ['inspection_started', 'inspection_succeeded']
    assert str(tmp_path) not in logfile.read_text()
    capsys.readouterr()


def test_unavailable_event_log_stops_before_inspection(workflow, tmp_path, monkeypatch, capsys):
    params = query_for(workflow, 'ru')['params']
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(params), encoding='utf-8')
    directory = tmp_path / 'unsafe-log'
    directory.mkdir(mode=0o755)
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    assert main(['--request-file', str(request), '--language', 'ru', '--inspect',
                 '--event-log', str(directory / 'events')]) == 1
    assert capsys.readouterr().err.strip() == translate('ru', 'interactive.log_failed')
    assert not list(Path(workflow['work_dir']).glob('install-*.lock'))
