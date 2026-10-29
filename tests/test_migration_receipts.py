"""Настоящие encrypted archive receipts; синтетические данные, без платформенной приёмки."""
from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from homensai_install.errors import InstallerError
from homensai_install.migration_receipts import BackupReceiptStore
from homensai_install.migration_snapshot import assert_recovery_snapshot_matches
from shared import archive as plain_archive
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


@pytest.fixture
def complete_receipt(tmp_path):
    """Real AEAD archive and exact restored snapshot comparison of all five roots."""
    manifest = json.loads((Path(__file__).parents[1] / 'examples/rent.manifest.json').read_text())
    home = resolve_datahome(manifest, tmp_path / 'live')
    for root in home.roots.values():
        root.mkdir(parents=True, mode=0o700)
        (root / 'synthetic-state').write_text('valuable synthetic ' + root.name)
    archive = tmp_path / 'before-migration.hsac'
    archive_crypto.build_encrypted_archive(home, archive, password_provider=lambda: 'synthetic-password')
    archive.chmod(0o600)
    binding = {'product_id': manifest['product_id'], 'installation_id': 'a'*32,
        'operation_id': 'op_synthetic', 'from_release': 'v1',
        'from_manifest_sha256': 'b'*64, 'from_inventory_sha256': 'c'*64,
        'from_schema_version': 'v1', 'plan_binding_sha256': 'd'*64}
    observed = []
    def verify(candidate, expected):
        observed.append(expected)
        assert expected == binding
        return assert_recovery_snapshot_matches(home.roots, candidate)
    store = BackupReceiptStore(tmp_path / 'receipts', password_provider=lambda: 'synthetic-password',
                               recovery_verifier=verify)
    result = store.issue(archive, binding, consistency_scope='owned_components_stopped_and_adapter_quiesced')
    assert result['migration_eligible'] is True and result['kind'] == 'encrypted_backup'
    return store, result, binding, archive, home, observed


class _NoSpaceStream:
    """Narrow real-file write fault; reads/close and other file IO stay real."""
    def __init__(self, stream): self.stream = stream
    def __enter__(self): self.stream.__enter__(); return self
    def __exit__(self, *args): return self.stream.__exit__(*args)
    def __getattr__(self, name): return getattr(self.stream, name)
    def write(self, data):
        raise OSError(errno.ENOSPC, 'SYNTHETIC_UNTRUSTED_OS_ERROR_TEXT', 'synthetic-private-filename')


@pytest.mark.skipif(os.name != 'posix', reason='Secure dir_fd full snapshot and /proc fd fault seam')
@pytest.mark.parametrize('action,fault', [
    ('issue', 'space'), ('verify', 'space'), ('issue', 'snapshot'),
    ('verify', 'snapshot'), ('issue', 'atomic'),
])
def test_typed_space_failure_preserves_cipher_and_authority_then_retries_real_verification(complete_receipt, tmp_path, monkeypatch, action, fault):
    from homensai_install import migration_receipts
    store, result, binding, archive, home, observed = complete_receipt
    if action == 'issue':
        store = BackupReceiptStore(tmp_path / 'new-space-receipts',
            password_provider=store.password_provider, recovery_verifier=store.recovery_verifier)
    original_cipher = archive.read_bytes()
    original_files = {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for root in home.roots.values() for p in root.rglob('*') if p.is_file()}
    original_receipt = (complete_receipt[0].directory / (result['receipt_id'] + '.json')).read_bytes()
    observed_before = len(observed)
    fired = []
    def call():
        if action == 'issue': return store.issue(archive, binding, consistency_scope='owned_components_stopped_and_adapter_quiesced')
        return store.verify(result['receipt_id'], expected_binding=binding)
    with monkeypatch.context() as faulted:
        if fault == 'space':
            usage = plain_archive.shutil.disk_usage(tmp_path)
            def no_free_space(path):
                fired.append('actual_archive_space_guard')
                return type(usage)(usage.total, usage.total, 0)
            faulted.setattr(plain_archive.shutil, 'disk_usage', no_free_space)
        elif fault == 'snapshot':
            actual = migration_receipts.os.fdopen
            def snapshot_write(fd, mode='r', *args, **kwargs):
                name = os.readlink('/proc/self/fd/' + str(fd))
                stream = actual(fd, mode, *args, **kwargs)
                if mode == 'wb' and Path(name).name == 'archive.hsac' and Path(name).parent.parent == store.directory:
                    fired.append('real_exclusive_snapshot_fd')
                    return _NoSpaceStream(stream)
                return stream
            faulted.setattr(migration_receipts.os, 'fdopen', snapshot_write)
        else:
            actual = Path.open
            def receipt_atomic_write(path, mode='r', *args, **kwargs):
                stream = actual(path, mode, *args, **kwargs)
                if mode == 'x' and path.parent == store.directory and path.name.startswith('.update-'):
                    fired.append('real_receipt_atomic_temporary')
                    return _NoSpaceStream(stream)
                return stream
            faulted.setattr(Path, 'open', receipt_atomic_write)
        with pytest.raises(InstallerError) as caught: call()
    assert fired
    assert caught.value.code == 'DISK_SPACE_LOW'
    envelope = caught.value.to_dict()
    assert envelope['phase'] == 'product_adapter'
    assert envelope['details'] == {'reason': 'backup_receipt_insufficient_space'}
    serialized = json.dumps(envelope)
    assert 'SYNTHETIC_UNTRUSTED_OS_ERROR_TEXT' not in serialized and 'synthetic-private-filename' not in serialized
    assert str(tmp_path) not in serialized
    assert archive.read_bytes() == original_cipher
    assert (complete_receipt[0].directory / (result['receipt_id'] + '.json')).read_bytes() == original_receipt
    assert original_files == {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for root in home.roots.values() for p in root.rglob('*') if p.is_file()}
    assert len(observed) == observed_before + (fault == 'atomic')
    assert list(store.directory.glob('verify-*')) == [] and list(store.directory.glob('.update-*')) == []
    if action == 'issue': assert list(store.directory.glob('*.json')) == []
    # A failed verification grants no new authority. Once actual space is
    # available, the same ciphertext is fully re-decrypted and re-verified.
    assert call() == result
    assert hashlib.sha256(archive.read_bytes()).digest() == hashlib.sha256(original_cipher).digest()


@pytest.mark.skipif(os.name != 'posix', reason='Secure dir_fd full snapshot verifier')
@pytest.mark.parametrize('fault', ['wrong_password', 'tamper', 'other_io', 'installer_error'])
def test_non_space_failures_are_not_relabelled_as_disk_space(complete_receipt, monkeypatch, fault):
    store, result, binding, archive, _, _ = complete_receipt
    original_error = None
    if fault == 'wrong_password': store.password_provider = lambda: 'different-synthetic-password'
    elif fault == 'tamper':
        raw = bytearray(archive.read_bytes()); raw[-1] ^= 1; archive.write_bytes(raw)
    elif fault == 'other_io':
        def read_failure(*args, **kwargs): raise OSError(errno.EIO, 'SYNTHETIC_UNTRUSTED_OS_ERROR_TEXT')
        monkeypatch.setattr(store, '_read', read_failure)
    else:
        original_error = InstallerError('INTEGRITY_FAILED', phase='product_adapter', details={'reason': 'synthetic_verified_refusal'})
        def trusted_refusal(*args, **kwargs): raise original_error
        monkeypatch.setattr(store, 'recovery_verifier', trusted_refusal)
    with pytest.raises(InstallerError) as caught:
        store.verify(result['receipt_id'], expected_binding=binding)
    assert caught.value.code != 'DISK_SPACE_LOW'
    if original_error is not None: assert caught.value is original_error
    assert 'SYNTHETIC_UNTRUSTED_OS_ERROR_TEXT' not in json.dumps(caught.value.to_dict())
