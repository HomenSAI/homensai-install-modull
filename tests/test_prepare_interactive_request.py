"""Request preparation keeps stable keys and never starts installation."""
import json
from pathlib import Path
import stat

import pytest

from shared import api
from shared.interactive_install import read_config
from shared.prepare_interactive_request import main, prepare
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
