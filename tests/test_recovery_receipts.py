"""Recovery authorization отделена от migration и повторно проверяет ciphertext."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil

import pytest

from homensai_install.errors import InstallerError
from homensai_install.recovery_receipts import RecoveryBackupReceiptStore
from homensai_install.migration_snapshot import assert_recovery_snapshot_matches
from shared import archive_crypto
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
