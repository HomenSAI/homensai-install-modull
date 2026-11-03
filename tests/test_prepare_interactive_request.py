"""Request preparation keeps stable keys and never starts installation."""
import json
from pathlib import Path
import stat

import pytest

from shared import api
from shared.interactive_install import read_config
from shared.i18n import translate
from shared.prepare_interactive_request import main, prepare, wizard
from shared.preflight import NAMES
from test_api_signed_preflight import signed_request
from test_fresh_controller import workflow
from test_fresh_controller_api import query_for
from test_update_assembly import assembled


def template_for(workflow):
    params = query_for(workflow)['params']
    return {key: value for key, value in params.items()
            if key not in ('keys', 'workflow_key', 'envelope', 'trusted_public_key')}


def test_prepares_verified_request_once_with_persistent_keys(workflow, tmp_path):
    template = template_for(workflow)
    target = tmp_path / 'request.json'
    prepare(template, workflow['envelope'], workflow['trusted_public_key'], str(target), language='ru')
    params = read_config(target)
    identifiers = [params['workflow_key'], *params['keys'].values()]
    assert len(set(identifiers)) == 6 and all(len(value) == 32 for value in identifiers)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert all(params[key] == value for key, value in template.items())
    query = {'api_major': 1, 'request_id': 'test', 'operation': 'inspect-portable-linux-install',
             'language': 'ru', 'params': params}
    assert api.handle(query)['result']['state'] == 'no_checkpoint'
    assert not list(Path(workflow['work_dir']).glob('install-*.json'))
    original = target.read_bytes()
    with pytest.raises(ValueError):
        prepare(template, workflow['envelope'], workflow['trusted_public_key'], str(target))
    assert target.read_bytes() == original


def test_invalid_signature_or_extra_fields_never_create_request(workflow, tmp_path):
    target = tmp_path / 'request.json'
    template = template_for(workflow)
    with pytest.raises(ValueError):
        prepare(template, workflow['envelope'], b'x' * 32, str(target))
    with pytest.raises(ValueError):
        prepare({**template, 'workflow_key': 'a' * 32}, workflow['envelope'], workflow['trusted_public_key'], str(target))
    assert not target.exists()


def test_cli_reads_template_and_public_files_without_exposing_inputs(workflow, tmp_path, capsys):
    template = tmp_path / 'template.json'
    signature = tmp_path / 'signature.json'
    key = tmp_path / 'trusted.key'
    target = tmp_path / 'request.json'
    template.write_text(json.dumps(template_for(workflow)), encoding='utf-8')
    signature.write_bytes(workflow['envelope'])
    key.write_bytes(workflow['trusted_public_key'])
    assert main(['--template', str(template), '--envelope-file', str(signature), '--public-key-file', str(key),
                 '--output', str(target), '--language', 'de']) == 0
    output = capsys.readouterr()
    assert str(tmp_path) not in output.out and str(tmp_path) not in output.err
    assert read_config(target)['expected_product_id'] == workflow['product_id']


def test_fsync_failure_does_not_publish_partial_request(workflow, tmp_path, monkeypatch):
    target = tmp_path / 'request.json'
    def fail(_):
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr('shared.prepare_interactive_request.os.fsync', fail)
    with pytest.raises(OSError):
        prepare(template_for(workflow), workflow['envelope'], workflow['trusted_public_key'], str(target),
                handle=lambda request: {'ok': True, 'result': {'state': 'no_checkpoint'}})
    assert not target.exists()
    assert not list(tmp_path.glob('.homensai-request-*'))


def test_competing_destination_is_not_overwritten(workflow, tmp_path, monkeypatch):
    import os
    target = tmp_path / 'request.json'
    original_link = os.link
    def competing_link(source, destination, **kwargs):
        Path(destination).write_bytes(b'other request')
        return original_link(source, destination, **kwargs)
    monkeypatch.setattr('shared.prepare_interactive_request.os.link', competing_link)
    with pytest.raises(FileExistsError):
        prepare(template_for(workflow), workflow['envelope'], workflow['trusted_public_key'], str(target),
                handle=lambda request: {'ok': True, 'result': {'state': 'no_checkpoint'}})
    assert target.read_bytes() == b'other request'
    assert not list(tmp_path.glob('.homensai-request-*'))


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_preparation_failure_is_localized_and_sanitized(tmp_path, capsys, language):
    result = main(['--template', str(tmp_path / 'missing'), '--envelope-file', '/missing-envelope',
                   '--public-key-file', '/missing-key', '--output', str(tmp_path / 'output'), '--language', language])
    assert result == 2
    assert capsys.readouterr().err.strip() == translate(language, 'interactive.request_failed')


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_wizard_creates_request_using_real_api(workflow, tmp_path, language):
    template = template_for(workflow)
    signature, key, output = (tmp_path / name for name in ('signature', 'key', 'request.json'))
    signature.write_bytes(workflow['envelope'])
    key.write_bytes(workflow['trusted_public_key'])
    values = iter([template['package_path'], template['expected_product_id'], template['expected_version'],
        template['install_root'], template['unit_dir'], template['work_dir'], ','.join(template['modes']),
        *(str(template['required_bytes'][name]) for name in NAMES), str(template['reserve_bytes']),
        str(signature), str(key), str(output)])
    prompts, lines = [], []
    def ask(prompt):
        prompts.append(prompt)
        return next(values)
    wizard(language, ask=ask, write=lines.append)
    result = read_config(output)
    assert all(result[key] == value for key, value in template.items())
    assert len(prompts) == 11 + len(NAMES)
    assert not list(Path(workflow['work_dir']).glob('install-*.json'))


def test_wizard_cancel_does_not_create_request(workflow, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    def cancelled(_):
        raise EOFError()
    monkeypatch.setattr('builtins.input', cancelled)
    assert main(['--wizard', '--language', 'ru']) == 2
    assert capsys.readouterr().err.strip() == translate('ru', 'interactive.request_failed')
    assert not list(tmp_path.glob('request*.json'))
