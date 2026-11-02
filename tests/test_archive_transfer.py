"""Перенос шифрованной копии между двумя синтетическими установками."""
import hashlib
from pathlib import Path

import pytest

from shared import adapters_fs as fs, archive_crypto
from shared.adapters_fs import FilesystemPorts
from shared.datahome import resolve_datahome
from shared.effect_receipt import read_effect_receipt
from shared.manifest import load_manifest
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, operation_lock


def test_encrypted_backup_transfers_to_clean_installation_and_rejects_tampering(tmp_path, monkeypatch):
    pytest.importorskip('cryptography')
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    origin = tmp_path / 'исходная установка'
    target = tmp_path / 'новая установка'
    source_home = resolve_datahome(manifest, origin)
    target_home = resolve_datahome(manifest, target)
    for home in (source_home, target_home):
        for root in (home.app, home.releases, *home.roots.values()):
            root.mkdir(parents=True, exist_ok=True)
    for name, root in source_home.roots.items():
        (root / 'папка с пробелом').mkdir()
        (root / 'папка с пробелом' / 'данные.txt').write_text('synthetic ' + name, encoding='utf-8')
        (root / 'empty').mkdir()
    (target_home.app / 'keep.txt').write_bytes(b'installed application')
    (target_home.releases / 'keep.txt').write_bytes(b'installed releases')
    (target_home.roots['data'] / 'temporary.txt').write_bytes(b'destination before restore')
    source = FilesystemPorts(manifest, origin, work_dir=tmp_path / 'исходная работа',
                             payloads={}, effects=None, password_provider=lambda: 'synthetic password')
    destination = FilesystemPorts(manifest, target, work_dir=tmp_path / 'новая работа',
                                  payloads={}, effects=None, password_provider=lambda: 'synthetic password')
    transaction = 'd' * 32
    archive_key = transaction + '/archive'
    with operation_lock(source.lock):
        source.backup(archive_key, 'archive', ())
    source_archive = source.archive_path(archive_key)
    # Контрольную сумму в этом тесте сохраняют отдельно до передачи файла.
    expected = hashlib.sha256(source_archive.read_bytes()).hexdigest()
    transferred = destination.archive_path(archive_key)
    with operation_lock(destination.lock):
        destination.backup(transaction + '/before_rollback', 'before-rollback', ())
        destination.pointer.write_bytes(b'{"release":"target-version"}\n')
    plan = UpdatePlan(manifest, target, destination, release='2', modes=('autonomous',),
                      migration_ids=(), guard_effects=True)
    state = {'version': 1, 'transaction': transaction, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'rollback', 'index': plan.rollback.index('restore'),
             'failed_phase': 'stage', 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    with pytest.raises(EffectIndeterminate):
        destination.import_archive_for_restore(plan, source_archive, '0' * 64)
    assert not transferred.exists() and not transferred.with_suffix('.archive.json').exists()
    with monkeypatch.context() as patch:
        patch.setattr(fs.os, 'link', lambda *args: (_ for _ in ()).throw(OSError('synthetic publication failure')))
        with pytest.raises(EffectIndeterminate):
            destination.import_archive_for_restore(plan, source_archive, expected)
    assert not transferred.exists()
    assert read_effect_receipt(transferred.with_suffix('.archive.json'))['status'] == 'pending'
    class Crash(BaseException):
        pass
    link = fs.os.link
    def crash_after_publication(source, target):
        link(source, target)
        if target == transferred:
            raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(fs.os, 'link', crash_after_publication)
        with pytest.raises(Crash):
            destination.import_archive_for_restore(plan, source_archive, expected)
    assert read_effect_receipt(transferred.with_suffix('.archive.json'))['status'] == 'pending'
    destination.import_archive_for_restore(plan, source_archive, expected)
    destination.import_archive_for_restore(plan, tmp_path / 'missing source', expected)
    assert plan.status() == state
    assert read_effect_receipt(transferred.with_suffix('.archive.json'))['status'] == 'done'
    assert hashlib.sha256(source_archive.read_bytes()).hexdigest() == expected
    with operation_lock(destination.lock):
        original = transferred.read_bytes()
        damaged = bytearray(original)
        damaged[-1] ^= 1
        transferred.write_bytes(damaged)
        with pytest.raises(archive_crypto.ArchiveCryptoError):
            destination.restore(transaction + '/restore', archive_key, ())
        assert (target_home.roots['data'] / 'temporary.txt').read_bytes() == b'destination before restore'
        assert destination.pointer.read_bytes() == b'{"release":"target-version"}\n'
        assert plan.status() == state
        transferred.write_bytes(original)
        destination.restore(transaction + '/restore', archive_key, ())
        destination.restore(transaction + '/restore', archive_key, ())
        for name, root in target_home.roots.items():
            assert (root / 'папка с пробелом' / 'данные.txt').read_text(encoding='utf-8') == 'synthetic ' + name
            assert (root / 'empty').is_dir()
        assert not (target_home.roots['data'] / 'temporary.txt').exists()
        assert destination.pointer.read_bytes() == b'{"release":"target-version"}\n'
        assert plan.status() == state
        assert (target_home.app / 'keep.txt').read_bytes() == b'installed application'
        assert (target_home.releases / 'keep.txt').read_bytes() == b'installed releases'


def test_import_never_overwrites_untracked_archive_or_accepts_wrong_phase(tmp_path):
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    root = tmp_path / 'install'
    ports = FilesystemPorts(manifest, root, work_dir=tmp_path / 'work', payloads={}, effects=None,
                            password_provider=lambda: 'synthetic password')
    plan = UpdatePlan(manifest, root, ports, release='2', modes=('autonomous',),
                      migration_ids=(), guard_effects=True)
    plan.state_path.parent.mkdir(parents=True)
    ports.work.mkdir()
    state = {'version': 1, 'transaction': 'e' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'rollback', 'index': plan.rollback.index('restore'),
             'failed_phase': 'stage', 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    destination = ports.archive_path(state['transaction'] + '/archive')
    destination.write_bytes(b'untracked existing data')
    with pytest.raises(EffectIndeterminate):
        ports.import_archive_for_restore(plan, tmp_path / 'source', '0' * 64)
    assert destination.read_bytes() == b'untracked existing data'
    assert not destination.with_suffix('.archive.json').exists()
    state.update(direction='forward', index=plan.forward.index('stage'), failed_phase=None)
    plan._save(state)
    with pytest.raises(UpdateError):
        ports.import_archive_for_restore(plan, tmp_path / 'source', '0' * 64)
    assert destination.read_bytes() == b'untracked existing data' and plan.status() == state
