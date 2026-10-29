"""Recovery authorization отделена от migration и повторно проверяет ciphertext."""
from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from homensai_install.errors import InstallerError
from homensai_install.recovery_receipts import RecoveryBackupReceiptStore
from homensai_install.migration_snapshot import assert_recovery_snapshot_matches
from shared import archive_crypto
from shared import archive as plain_archive
from shared.datahome import resolve_datahome, PERSISTENT_ROOTS


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux secure dir_fd recovery snapshot backend')


@pytest.fixture
def proof(tmp_path):
    manifest = json.loads((Path(__file__).parents[1] / 'examples/rent.manifest.json').read_text())
    home = resolve_datahome(manifest, tmp_path / 'live')
    for root in home.roots.values(): root.mkdir(parents=True, mode=0o700)
    (home.roots['data'] / 'document').write_text('valuable synthetic data')
    (home.roots['services'] / 'operation.lock').write_text('all bytes matter')
    archive = tmp_path / 'before-restore.hsac'
    archive_crypto.build_encrypted_archive(home, archive, password_provider=lambda: 'synthetic-password')
    binding = {'product_id': manifest['product_id'], 'installation_id': 'a'*32, 'operation_id': 'op_synthetic',
        'from_release': 'v2', 'from_manifest_sha256': 'b'*64, 'from_inventory_sha256': 'c'*64,
        'from_metadata_sha256': 'd'*64, 'recovery_action': 'restore', 'source_archive_sha256': 'e'*64}
    observed = []
    def verify(candidate, expected):
        observed.append(expected)
        return assert_recovery_snapshot_matches(home.roots, candidate)
    store = RecoveryBackupReceiptStore(tmp_path / 'receipts', password_provider=lambda: 'synthetic-password',
                                      recovery_verifier=verify)
    result = store.issue(archive, binding, consistency_scope='owned_components_stopped')
    return store, result, binding, archive, home, observed


def test_real_recovery_receipt_redecrypts_complete_snapshot_and_never_authorizes_migration(proof):
    store, result, binding, _, _, observed = proof
    assert store.verify(result['receipt_id'], expected_binding=binding, consistency_scope='owned_components_stopped') == result
    assert len(observed) == 2
    assert result['migration_eligible'] is False and result['kind'] == 'before_recovery_backup'


@pytest.mark.parametrize('key', ['product_id', 'installation_id', 'operation_id', 'from_release',
    'from_manifest_sha256', 'from_inventory_sha256', 'from_metadata_sha256', 'recovery_action', 'source_archive_sha256'])
def test_every_foreign_binding_field_is_refused(proof, key):
    store, result, binding, _, _, observed = proof
    changed = deepcopy(binding)
    changed[key] = ('rollback' if key == 'recovery_action' else 'f'*64 if key.endswith('sha256') else 'foreign')
    with pytest.raises(InstallerError):
        store.verify(result['receipt_id'], expected_binding=changed, consistency_scope='owned_components_stopped')
    assert len(observed) == 1


@pytest.mark.parametrize('fault', ['missing', 'tamper', 'receipt', 'hardlink', 'symlink', 'consistency'])
def test_safety_file_or_consistency_loss_refuses_authorization(proof, tmp_path, fault):
    store, result, binding, archive, _, observed = proof
    scope = 'owned_components_stopped'
    if fault == 'missing': archive.unlink()
    elif fault == 'tamper':
        raw = bytearray(archive.read_bytes()); raw[-1] ^= 1; archive.write_bytes(raw)
    elif fault == 'receipt':
        path = store.directory / (result['receipt_id'] + '.json')
        path.write_text('{}'); path.chmod(0o600)
    elif fault == 'hardlink': os.link(archive, tmp_path / 'linked')
    elif fault == 'symlink':
        original = tmp_path / 'original'; archive.rename(original); archive.symlink_to(original)
    else: scope = 'owned_components_stopped_and_adapter_quiesced'
    with pytest.raises(InstallerError):
        store.verify(result['receipt_id'], expected_binding=binding, consistency_scope=scope)
    assert len(observed) == 1


@pytest.mark.parametrize('root', PERSISTENT_ROOTS)
def test_every_root_and_services_journal_content_is_protected(proof, root):
    store, result, binding, _, home, _ = proof
    path = home.roots[root] / ('update-' + 'a'*32 + '.json' if root == 'services' else 'new-file')
    path.write_text('new state cannot be discarded')
    with pytest.raises(InstallerError):
        store.verify(result['receipt_id'], expected_binding=binding, consistency_scope='owned_components_stopped')


def test_exact_recovery_snapshot_includes_all_journals_without_exceptions(proof, tmp_path):
    _, _, _, _, home, _ = proof
    candidate = tmp_path / 'candidate'
    for name, root in home.roots.items(): shutil.copytree(root, candidate / 'roots' / name)
    assert assert_recovery_snapshot_matches(home.roots, candidate) is True
    (home.roots['services'] / 'operation.lock').write_text('modified lock bytes')
    with pytest.raises(InstallerError): assert_recovery_snapshot_matches(home.roots, candidate)


def test_unrelated_ancestor_directory_creation_does_not_invalidate_owned_snapshot(proof, tmp_path, monkeypatch):
    from homensai_install import migration_snapshot as snapshot
    store, result, binding, _, _, _ = proof
    actual = snapshot.os.open
    fired = []
    def unrelated_entry(path, flags, *args, **kwargs):
        if path == tmp_path.name and not fired:
            fired.append(True)
            (tmp_path / 'unrelated-neighbor').mkdir(mode=0o700)
        return actual(path, flags, *args, **kwargs)
    monkeypatch.setattr(snapshot.os, 'open', unrelated_entry)
    monkeypatch.setattr(snapshot.os, 'supports_dir_fd', {*os.supports_dir_fd, unrelated_entry})
    assert store.verify(result['receipt_id'], expected_binding=binding,
                        consistency_scope='owned_components_stopped') == result
    assert fired


@pytest.mark.parametrize('replacement', ['inode', 'symlink', 'mode'])
def test_ancestor_identity_or_permissions_substitution_remains_rejected(proof, tmp_path, monkeypatch, replacement):
    from homensai_install import migration_snapshot as snapshot
    store, result, binding, _, _, _ = proof
    ancestor = tmp_path / 'live'
    alternate = tmp_path / 'alternate-live'
    shutil.copytree(ancestor, alternate)
    actual = snapshot.os.open
    fired = []
    def substitute(path, flags, *args, **kwargs):
        if path == ancestor.name and not fired:
            fired.append(True)
            if replacement == 'mode':
                ancestor.chmod(0o755 if ancestor.stat().st_mode & 0o777 != 0o755 else 0o700)
            else:
                ancestor.rename(tmp_path / 'previous-live')
                if replacement == 'inode': alternate.rename(ancestor)
                else: ancestor.symlink_to(alternate, target_is_directory=True)
        return actual(path, flags, *args, **kwargs)
    monkeypatch.setattr(snapshot.os, 'open', substitute)
    monkeypatch.setattr(snapshot.os, 'supports_dir_fd', {*os.supports_dir_fd, substitute})
    with pytest.raises(InstallerError):
        store.verify(result['receipt_id'], expected_binding=binding, consistency_scope='owned_components_stopped')
    assert fired


class _NoSpaceStream:
    """Narrow real-file write fault; reads/close and other file IO stay real."""
    def __init__(self, stream): self.stream = stream
    def __enter__(self): self.stream.__enter__(); return self
    def __exit__(self, *args): return self.stream.__exit__(*args)
    def __getattr__(self, name): return getattr(self.stream, name)
    def write(self, data):
        raise OSError(errno.ENOSPC, 'SYNTHETIC_UNTRUSTED_OS_ERROR_TEXT', 'synthetic-private-filename')


@pytest.mark.parametrize('action,fault', [
    ('issue', 'space'), ('verify', 'space'), ('issue', 'snapshot'),
    ('verify', 'snapshot'), ('issue', 'atomic'),
])
def test_typed_space_failure_preserves_cipher_and_authority_then_retries_real_verification(proof, tmp_path, monkeypatch, action, fault):
    from homensai_install import migration_receipts
    store, result, binding, archive, home, observed = proof
    if action == 'issue':
        store = RecoveryBackupReceiptStore(tmp_path / 'new-space-receipts',
            password_provider=store.password_provider, recovery_verifier=store.recovery_verifier)
    original_cipher = archive.read_bytes()
    original_files = {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for root in home.roots.values() for p in root.rglob('*') if p.is_file()}
    original_receipt = (proof[0].directory / (result['receipt_id'] + '.json')).read_bytes()
    observed_before = len(observed)
    fired = []
    def call():
        if action == 'issue': return store.issue(archive, binding, consistency_scope='owned_components_stopped')
        return store.verify(result['receipt_id'], expected_binding=binding, consistency_scope='owned_components_stopped')
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
    assert envelope['details'] == {'reason': 'recovery_receipt_insufficient_space'}
    serialized = json.dumps(envelope)
    assert 'SYNTHETIC_UNTRUSTED_OS_ERROR_TEXT' not in serialized and 'synthetic-private-filename' not in serialized
    assert str(tmp_path) not in serialized
    assert archive.read_bytes() == original_cipher
    assert (proof[0].directory / (result['receipt_id'] + '.json')).read_bytes() == original_receipt
    assert original_files == {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for root in home.roots.values() for p in root.rglob('*') if p.is_file()}
    assert len(observed) == observed_before + (fault == 'atomic')
    assert list(store.directory.glob('verify-*')) == [] and list(store.directory.glob('.update-*')) == []
    if action == 'issue': assert list(store.directory.glob('*.json')) == []
    # A failed verification grants no new authority. Once actual space is
    # available, the same ciphertext is fully re-decrypted and re-verified.
    assert call() == result
    assert hashlib.sha256(archive.read_bytes()).digest() == hashlib.sha256(original_cipher).digest()


@pytest.mark.parametrize('fault', ['wrong_password', 'tamper', 'other_io', 'installer_error'])
def test_non_space_failures_are_not_relabelled_as_disk_space(proof, monkeypatch, fault):
    store, result, binding, archive, _, _ = proof
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
        store.verify(result['receipt_id'], expected_binding=binding, consistency_scope='owned_components_stopped')
    assert caught.value.code != 'DISK_SPACE_LOW'
    if original_error is not None: assert caught.value is original_error
    assert 'SYNTHETIC_UNTRUSTED_OS_ERROR_TEXT' not in json.dumps(caught.value.to_dict())
