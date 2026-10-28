"""Standalone restore hooks не принимают один opaque ID за право mutation."""
from copy import deepcopy
import json

import pytest

from homensai_install.errors import InstallerError
from test_adapters_protocol import adapter, bundle, request


def recovery_request():
    return request('recovery', **{'installation_id': 'a'*32, 'release': '0.0.1',
        'recovery_action': 'restore', 'source_archive_sha256': 'b'*64, 'before_recovery_receipt_id': 'c'*64})


@pytest.mark.parametrize('hook', ['restore', 'post_restore'])
def test_well_formed_name_without_trusted_authorizer_never_executes(bundle, hook):
    with pytest.raises(InstallerError) as caught:
        adapter(bundle).invoke(hook, recovery_request())
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert not bundle['app_state'].exists()


@pytest.mark.parametrize('key,value', [('before_recovery_receipt_id', 'not-a-proof'),
    ('source_archive_sha256', None), ('recovery_action', 'migration'), ('release', ''), ('installation_id', False)])
def test_malformed_recovery_binding_refused_before_intent(bundle, key, value):
    req = recovery_request(); req['data'][key] = value
    with pytest.raises(InstallerError): adapter(bundle, recovery_authorizer=lambda request: True).invoke('restore', req)
    assert not bundle['app_state'].exists() and not bundle['state_dir'].exists()


def test_authorizer_only_runs_for_new_effect_and_does_not_mask_cached_result(bundle):
    observed = []
    req = recovery_request()
    result = adapter(bundle, recovery_authorizer=lambda request: observed.append(request) or True).invoke('restore', req)
    assert observed == [req] and result['result']['applied'] is True
    again = adapter(bundle).invoke('restore', req)
    assert again['replayed'] and again['result'] == result['result']
    assert json.loads(bundle['app_state'].read_text())['calls'] == 1
    another = deepcopy(req); another['idempotency_key'] = 'another-recovery'
    with pytest.raises(InstallerError): adapter(bundle).invoke('restore', another)
    assert json.loads(bundle['app_state'].read_text())['calls'] == 1


def test_authorization_refusal_has_no_unknown_intent_and_same_request_can_be_retried(bundle):
    req = recovery_request()
    with pytest.raises(InstallerError): adapter(bundle, recovery_authorizer=lambda request: False).invoke('restore', req)
    assert not bundle['app_state'].exists()
    result = adapter(bundle, recovery_authorizer=lambda request: True).invoke('restore', req)
    assert result['result']['applied'] is True and result['replayed'] is False
