"""Optional evidence policy is bound to the durable installation workflow."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from shared import api, osv_audit
from shared.fresh_controller import FreshControllerError, run_portable_linux_install
from shared.package import build_package
from shared.package_signing import sign_package
from test_api_signed_preflight import signed_request
from test_fresh_controller import workflow
from test_update_assembly import assembled


@pytest.fixture
def protected_workflow(workflow, signed_request, tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = deepcopy(signed_request[2])
    source = tmp_path / 'payload'
    lock_bytes = b'synthetic-dependency==1.0 --hash=sha256:' + b'a' * 64 + b'\n'
    lock = source / manifest['runtime']['lock_files']['linux']['file']
    lock.write_bytes(lock_bytes)
    manifest['runtime']['lock_files']['linux']['sha256'] = hashlib.sha256(lock_bytes).hexdigest()
    package = tmp_path / 'protected-release.zip'
    build_package(manifest, '2', source, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='2')
    report = json.dumps(osv_audit.audit([str(lock)], send=lambda _: {'results': [{}]})).encode()
    evidence = tmp_path / 'evidence.json'
    evidence.write_bytes(report)
    policy = {'lock_path': str(lock), 'evidence_path': str(evidence),
              'evidence_sha256': hashlib.sha256(report).hexdigest(), 'max_age_seconds': 600}
    return {**workflow, 'package_path': str(package), 'envelope': envelope, 'trusted_public_key': public,
            'runtime_evidence': policy}, evidence


def responses(operation):
    expected = {'prepare-native-release': ('status', 'prepared'), 'prepare-linux-runtime': ('state', 'prepared'),
                'publish-portable-systemd-user-units': ('state', 'published'),
                'load-portable-systemd-user-units': ('state', 'loaded'),
                'start-portable-linux': ('state', 'started'), 'check-local-health': ('health_response_matches', True)}
    key, value = expected[operation]
    return {'ok': True, 'result': {key: value}}


def test_policy_checks_real_evidence_before_and_after_steps(protected_workflow, monkeypatch):
    options, _ = protected_workflow
    calls = []
    def handle(request):
        calls.append(request['operation'])
        return responses(request['operation'])
    monkeypatch.setattr(api, 'handle', handle)
    result = run_portable_linux_install(**options)
    assert result['runtime_evidence_verified'] and not result['complete_dependency_inventory']
    assert not result['malware_scanned'] and len(calls) == 6


def test_bad_evidence_blocks_before_checkpoint_or_effect(protected_workflow, monkeypatch):
    options, evidence = protected_workflow
    evidence.write_bytes(evidence.read_bytes() + b'tampered')
    monkeypatch.setattr(api, 'handle', lambda request: pytest.fail('effect executed'))
    with pytest.raises(FreshControllerError) as error:
        run_portable_linux_install(**options)
    assert error.value.code == 'runtime_evidence_failed'
    assert not list(Path(options['work_dir']).glob('install-*.json'))


def test_policy_cannot_be_removed_or_changed_when_resuming(protected_workflow, tmp_path, monkeypatch):
    options, evidence = protected_workflow
    calls = []
    def fail_first(request):
        calls.append(request['operation'])
        return {'ok': False, 'error': {'code': 'synthetic_failed'}}
    monkeypatch.setattr(api, 'handle', fail_first)
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**options)
    before = list(calls)
    without = {key: value for key, value in options.items() if key != 'runtime_evidence'}
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**without)
    copied = tmp_path / 'same-evidence.json'
    copied.write_bytes(evidence.read_bytes())
    changed = {**options, 'runtime_evidence': {**options['runtime_evidence'], 'evidence_path': str(copied)}}
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**changed)
    assert calls == before
    evidence.unlink()
    status = run_portable_linux_install(**options, inspect_only=True)
    assert status['state'] == 'pending' and status['runtime_evidence_verified'] is False


def test_changed_evidence_after_step_leaves_checkpoint_unconfirmed(protected_workflow, monkeypatch):
    options, evidence = protected_workflow
    calls = []
    def handle(request):
        calls.append(request['operation'])
        evidence.write_bytes(evidence.read_bytes() + b'tampered')
        return responses(request['operation'])
    monkeypatch.setattr(api, 'handle', handle)
    with pytest.raises(FreshControllerError) as error:
        run_portable_linux_install(**options)
    assert error.value.code == 'runtime_evidence_failed' and calls == ['prepare-native-release']
    marker = Path(options['work_dir']) / ('install-' + options['workflow_key'] + '.json')
    assert json.loads(marker.read_text())['index'] == 0
