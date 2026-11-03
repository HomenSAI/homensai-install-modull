"""Evidence policy over API must fail closed and bind resumed workflows."""
import json
from pathlib import Path

import pytest

from homensai_installer_client import InstallerClient, InstallerClientError, contract_schema
from shared import api
from shared.fresh_controller import FreshControllerError, run_portable_linux_install
from shared.interactive_install import read_config, run
from shared.prepare_interactive_request import prepare
from shared.prepare_interactive_request import wizard
from shared.preflight import NAMES
from test_api_signed_preflight import signed_request
from test_fresh_controller import workflow
from test_fresh_controller_api import query_for
from test_fresh_runtime_evidence import protected_workflow
from test_update_assembly import assembled


def test_api_accepts_policy_schema_and_blocks_tampered_evidence(protected_workflow):
    options, evidence = protected_workflow
    query = query_for(options, 'ru')
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    validator.validate(query)
    evidence.write_bytes(evidence.read_bytes() + b'tampered')
    result = api.handle(query)
    assert result['error']['code'] == 'runtime_evidence_failed' and not result['error']['retryable']
    assert str(evidence) not in json.dumps(result)
    assert not list(Path(options['work_dir']).glob('install-*.json'))


def test_api_inspection_and_resume_require_original_policy(protected_workflow, monkeypatch):
    options, evidence = protected_workflow
    with monkeypatch.context() as patch:
        patch.setattr(api, 'handle', lambda _: {'ok': False, 'error': {'code': 'synthetic_failure'}})
        with pytest.raises(FreshControllerError):
            run_portable_linux_install(**options)
    evidence.unlink()
    query = query_for(options)
    query['operation'] = 'inspect-portable-linux-install'
    result = api.handle(query)
    assert result['ok'] and result['result']['state'] == 'pending'
    assert result['result']['runtime_evidence_verified'] is False
    del query['params']['runtime_evidence']
    assert api.handle(query)['error']['code'] == 'fresh_controller_failed'


def test_client_refuses_policy_when_server_is_too_old(protected_workflow, monkeypatch):
    options, _ = protected_workflow
    client = InstallerClient()
    monkeypatch.setattr(client, 'capabilities', lambda: {'api_minor': 65,
        'operations': ['install-portable-linux'], 'languages': ['en']})
    with pytest.raises(InstallerClientError) as error:
        client.call('install-portable-linux', query_for(options)['params'])
    assert error.value.code == 'incompatible_api'


def test_template_and_interactive_inspection_preserve_and_show_policy(protected_workflow, tmp_path):
    options, _ = protected_workflow
    params = query_for(options, 'en')['params']
    template = {key: value for key, value in params.items()
                if key not in ('envelope', 'trusted_public_key', 'workflow_key', 'keys')}
    request = tmp_path / 'protected-request.json'
    prepare(template, options['envelope'], options['trusted_public_key'], str(request), language='en')
    saved = read_config(request)
    assert saved['runtime_evidence'] == options['runtime_evidence']
    lines = []
    assert run(saved, 'en', ask=lambda _: pytest.fail('unexpected prompt'), write=lines.append, inspect_only=True) == 0
    assert any('policy requested' in line for line in lines)
    assert any(options['runtime_evidence']['evidence_sha256'] in line for line in lines)


def test_preparation_rejects_tampered_evidence_before_publishing(protected_workflow, tmp_path):
    options, evidence = protected_workflow
    params = query_for(options)['params']
    template = {key: value for key, value in params.items()
                if key not in ('envelope', 'trusted_public_key', 'workflow_key', 'keys')}
    target = tmp_path / 'request.json'
    evidence.write_bytes(evidence.read_bytes() + b'tampered')
    with pytest.raises(ValueError):
        prepare(template, options['envelope'], options['trusted_public_key'], str(target))
    assert not target.exists()


@pytest.mark.parametrize('language', ['en', 'ru', 'de'])
def test_wizard_prepares_verified_evidence_policy(protected_workflow, tmp_path, language):
    options, _ = protected_workflow
    params = query_for(options)['params']
    policy = params['runtime_evidence']
    signature, key, target = (tmp_path / name for name in ('signature', 'key', 'request.json'))
    signature.write_bytes(options['envelope'])
    key.write_bytes(options['trusted_public_key'])
    answers = iter([params['package_path'], params['expected_product_id'], params['expected_version'],
        params['install_root'], params['unit_dir'], params['work_dir'], ','.join(params['modes']),
        *(str(params['required_bytes'][name]) for name in NAMES), str(params['reserve_bytes']), 'YES',
        policy['lock_path'], policy['evidence_path'], policy['evidence_sha256'],
        str(policy['max_age_seconds']), str(signature), str(key), str(target)])
    wizard(language, ask=lambda _: next(answers), write=lambda _: None)
    assert read_config(target)['runtime_evidence'] == policy


@pytest.mark.parametrize('change', ['null', 'unknown', 'boolean_age', 'bad_digest'])
def test_api_rejects_invalid_policy_without_checkpoint(protected_workflow, change):
    options, _ = protected_workflow
    query = query_for(options)
    policy = dict(query['params']['runtime_evidence'])
    if change == 'null':
        query['params']['runtime_evidence'] = None
    elif change == 'unknown':
        policy['unknown'] = 'synthetic-sensitive'
    elif change == 'boolean_age':
        policy['max_age_seconds'] = True
    else:
        policy['evidence_sha256'] = 'not-a-digest'
    if change != 'null':
        query['params']['runtime_evidence'] = policy
    result = api.handle(query)
    assert result['error']['code'] == 'invalid_request'
    assert 'synthetic-sensitive' not in json.dumps(result)
    assert not list(Path(options['work_dir']).glob('install-*.json'))
