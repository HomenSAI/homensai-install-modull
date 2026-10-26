"""Настоящие encrypted archive receipts; синтетические данные, без платформенной приёмки."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from homensai_install.errors import InstallerError
from homensai_install.migration_receipts import BackupReceiptStore
from shared import archive_crypto
from shared.datahome import resolve_datahome
from test_adapters_protocol import adapter, bundle, mutation


@pytest.fixture
def receipt(tmp_path):
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json').read_text())
    home = resolve_datahome(manifest, tmp_path / 'installation')
    for root in (home.app, home.releases, *home.roots.values()):
        root.mkdir(parents=True, exist_ok=True)
    (home.roots['data'] / 'document.txt').write_text('Синтетический документ')
    archive = tmp_path / 'full.hsac'
    archive_crypto.build_encrypted_archive(home, archive, password_provider=lambda: 'synthetic-password')
    archive.chmod(0o600)
    binding = {'product_id': manifest['product_id'], 'installation_id': 'a' * 32,
        'operation_id': 'op_synthetic', 'from_release': 'v1',
        'from_manifest_sha256': 'b' * 64, 'from_inventory_sha256': 'c' * 64,
        'from_schema_version': 'v1', 'plan_binding_sha256': 'd' * 64}
    observed = []
    def recovery(candidate, expected):
        observed.append(candidate)
        assert (candidate / 'roots/data/document.txt').read_text() == 'Синтетический документ'
        return all(expected[key] == binding[key] for key in ('product_id', 'installation_id', 'from_release',
            'from_manifest_sha256', 'from_inventory_sha256'))
    store = BackupReceiptStore(tmp_path / 'receipts', password_provider=lambda: 'synthetic-password',
                               recovery_verifier=recovery)
    result = store.issue(archive, binding, consistency_scope='owned_components_stopped_and_adapter_quiesced')
    return store, result, binding, archive, observed


def test_receipt_proves_actual_decryption_restore_and_has_digest_identity(receipt):
    store, result, binding, archive, observed = receipt
    assert observed and not observed[0].exists()
    assert result['migration_eligible'] is True
    assert result['verification_scope'] == 'encrypted_archive_and_recovery_payload_restored'
    verified = store.verify(result['receipt_id'], expected_binding=binding)
    assert verified == result and len(observed) == 2
    assert archive_crypto.is_encrypted(archive)
    assert result['archive_sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.mark.parametrize('change', ['missing', 'ciphertext', 'receipt', 'foreign_installation',
    'foreign_release', 'foreign_operation', 'foreign_plan', 'unknown_schema', 'hardlink', 'symlink'])
def test_untrusted_backup_is_refused_before_any_new_mutation(receipt, change, tmp_path):
    store, result, binding, archive, observed = receipt
    expected = deepcopy(binding)
    if change == 'missing': archive.unlink()
    elif change == 'ciphertext':
        data = bytearray(archive.read_bytes()); data[-1] ^= 1; archive.write_bytes(data)
    elif change == 'receipt':
        path = store.directory / (result['receipt_id'] + '.json')
        record = json.loads(path.read_text()); record['body']['archive_sha256'] = '0' * 64
        path.write_text(json.dumps(record)); path.chmod(0o600)
    elif change == 'foreign_installation': expected['installation_id'] = 'e' * 32
    elif change == 'foreign_release': expected['from_release'] = 'other'
    elif change == 'foreign_operation': expected['operation_id'] = 'op_other'
    elif change == 'foreign_plan': expected['plan_binding_sha256'] = 'f' * 64
    elif change == 'unknown_schema': expected['from_schema_version'] = None
    elif change == 'hardlink': os.link(archive, tmp_path / 'linked.hsac')
    elif change == 'symlink':
        original = tmp_path / 'original.hsac'; archive.rename(original); archive.symlink_to(original)
    with pytest.raises(InstallerError): store.verify(result['receipt_id'], expected_binding=expected)
    assert len(observed) == 1


def test_recovery_only_receipt_cannot_authorize_migration(receipt):
    store, _, binding, archive, _ = receipt
    unknown = {**binding, 'from_schema_version': None, 'plan_binding_sha256': None}
    result = store.issue(archive, unknown, consistency_scope='owned_components_stopped_and_adapter_quiesced')
    assert result['migration_eligible'] is False and result['verification_scope'] == 'recovery_only'
    with pytest.raises(InstallerError): store.verify(result['receipt_id'], expected_binding=unknown)


def test_default_adapter_cannot_turn_a_well_formed_receipt_name_into_authorization(bundle):
    with pytest.raises(InstallerError) as caught:
        adapter(bundle, migration_authorizer=None).invoke('migration_apply', mutation())
    assert caught.value.code == 'CAPABILITY_UNSUPPORTED'
    assert not bundle['app_state'].exists()


def test_real_receipt_authorizer_prevents_tamper_and_rechecks_new_effect_only(bundle, receipt):
    store, result, binding, archive, _ = receipt
    req = mutation(backup_receipt_id=result['receipt_id'])
    def authorize(request):
        store.verify(request['data']['backup_receipt_id'], expected_binding=binding)
        return True
    first = adapter(bundle, migration_authorizer=authorize).invoke('migration_apply', req)
    archive.unlink()
    cached = adapter(bundle, migration_authorizer=authorize).invoke('migration_apply', req)
    assert cached['replayed'] and cached['result'] == first['result']
    another = deepcopy(req); another['idempotency_key'] = 'another'
    with pytest.raises(InstallerError): adapter(bundle, migration_authorizer=authorize).invoke('migration_apply', another)
    assert json.loads(bundle['app_state'].read_text())['calls'] == 1


def test_auth_failure_leaves_no_unknown_intent_and_fixed_receipt_can_retry_same_key(bundle, receipt):
    store, result, binding, archive, _ = receipt
    req = mutation(backup_receipt_id='0' * 64)
    def authorize(request):
        store.verify(request['data']['backup_receipt_id'], expected_binding=binding)
        return True
    with pytest.raises(InstallerError): adapter(bundle, migration_authorizer=authorize).invoke('migration_apply', req)
    assert not bundle['app_state'].exists()
    req['data']['backup_receipt_id'] = result['receipt_id']
    assert adapter(bundle, migration_authorizer=authorize).invoke('migration_apply', req)['result']['applied']


def test_lost_ack_reconciliation_never_requires_deleted_backup_or_repeats_migration(bundle, receipt):
    store, result, binding, archive, _ = receipt
    req = mutation(backup_receipt_id=result['receipt_id'], mode='fail_after_apply')
    def authorize(request):
        store.verify(request['data']['backup_receipt_id'], expected_binding=binding)
        return True
    with pytest.raises(InstallerError): adapter(bundle, migration_authorizer=authorize).invoke('migration_apply', req)
    archive.unlink()
    with pytest.raises(InstallerError) as caught: adapter(bundle, migration_authorizer=authorize).invoke('migration_apply', req)
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert adapter(bundle, migration_authorizer=None).reconcile('one')['outcome'] == 'applied'
    assert adapter(bundle, migration_authorizer=None).invoke('migration_apply', req)['replayed']
    assert json.loads(bundle['app_state'].read_text())['calls'] == 2


@pytest.mark.skipif(os.name == 'nt', reason='POSIX O_NOFOLLOW/nonblocking FIFO receipt boundary')
@pytest.mark.parametrize('replacement', ['symlink', 'fifo'])
def test_snapshot_open_rejects_path_substitution_before_read(receipt, monkeypatch, tmp_path, replacement):
    import homensai_install.migration_receipts as module
    store, result, binding, archive, observed = receipt
    original = archive.read_bytes()
    real_open = module.os.open
    calls = []
    def substitute(path, flags, *args, **kwargs):
        if Path(path) == archive:
            calls.append(flags)
            # First open hashes the archive; the next opens the snapshot input.
            if len(calls) == 2:
                archive.unlink()
                if replacement == 'fifo': os.mkfifo(archive, mode=0o600)
                else:
                    foreign = tmp_path / 'foreign.hsac'; foreign.write_bytes(original)
                    archive.symlink_to(foreign)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', substitute)
    with pytest.raises(InstallerError): store.verify(result['receipt_id'], expected_binding=binding)
    assert len(observed) == 1
