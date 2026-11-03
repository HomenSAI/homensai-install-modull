"""Перенос шифрованной копии между двумя синтетическими установками."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from test_linux_signed_release import signed
from examples.demo_product.installer_client import InstallerClient
from shared import adapters_fs as fs, api, archive_crypto, ctl
from shared.adapters_fs import FilesystemPorts
from shared.datahome import resolve_datahome
from shared.effect_receipt import read_effect_receipt
from shared.manifest import load_manifest
from shared.update_plan import EffectIndeterminate, UpdateError, UpdateIndeterminate, UpdatePlan, operation_lock


def test_encrypted_backup_transfers_to_clean_installation_and_rejects_tampering(tmp_path, monkeypatch, capsys):
    pytest.importorskip('cryptography')
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    manifest['hooks']['pre_start'] = []
    manifest['hooks']['health_checks'] = []
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
    events = []
    class RestoredProduct:
        def start(self, key, components, pre_start):
            assert key.endswith('/rollback_start') and not pre_start
            assert (target_home.roots['data'] / 'папка с пробелом' / 'данные.txt').read_text() == 'synthetic data'
            events.append('start')
        def health(self, key, url, timeout, checks):
            assert key.endswith('/rollback_health') and not checks
            events.append('health')
    destination = FilesystemPorts(manifest, target, work_dir=tmp_path / 'новая работа',
                                  payloads={}, effects=RestoredProduct(), password_provider=lambda: 'synthetic password')
    (target_home.releases / 'app-1').mkdir()
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
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setenv('HOMENSAI_SECRET_TEST_ARCHIVE_PASSWORD', 'synthetic password')
    arguments = ['--manifest', str(manifest_path), 'update-import-archive', '--install-root', str(target),
                 '--work-dir', str(destination.work), '--archive', str(source_archive),
                 '--release', '2', '--mode', 'autonomous', '--no-migrations',
                 '--password-env', 'HOMENSAI_SECRET_TEST_ARCHIVE_PASSWORD', '--json']
    assert ctl.main([*arguments, '--expected-sha256', '0' * 64]) == 1
    failure = capsys.readouterr()
    assert failure.err == '' and json.loads(failure.out)['ok'] is False
    assert 'synthetic password' not in failure.out and str(source_archive) not in failure.out
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
    query = {'api_major': 1, 'request_id': 'test-import', 'operation': 'import-update-archive',
             'language': 'de', 'params': {'manifest': manifest, 'install_root': str(target),
             'work_dir': str(destination.work), 'archive': str(source_archive), 'release': '2',
             'modes': ['autonomous'], 'migration_ids': [], 'expected_sha256': expected,
             'password_env': 'MISSING_SYNTHETIC_PASSWORD'}}
    failure = api.handle(query)
    assert failure['error']['code'] == 'update_archive_import_failed'
    assert 'synthetic password' not in json.dumps(failure) and str(source_archive) not in json.dumps(failure)
    assert read_effect_receipt(transferred.with_suffix('.archive.json'))['status'] == 'pending'
    query['params']['password_env'] = 'HOMENSAI_SECRET_TEST_ARCHIVE_PASSWORD'
    result = api.handle(query)
    assert result['ok'] is True and result['result']['archive_imported'] is True
    assert result['result']['resume_authorized'] is False and 'synthetic password' not in json.dumps(result)
    client_result = InstallerClient(timeout=90).call('import-update-archive', query['params'],
        min_minor=27, language='en', secret_env_name='HOMENSAI_SECRET_TEST_ARCHIVE_PASSWORD')
    assert client_result['archive_imported'] is True and client_result['resume_authorized'] is False
    assert ctl.main([*arguments, '--expected-sha256', expected]) == 0
    success = capsys.readouterr()
    result = json.loads(success.out)
    assert success.err == '' and result['data']['archive_imported'] is True
    assert result['data']['resume_authorized'] is False and 'synthetic password' not in success.out
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
    result = plan.run(resume_indeterminate=True)
    assert result['outcome'] == 'rolled_back'
    assert destination.current_release() == '1' and events == ['start', 'health']
    assert plan.run() == result and events == ['start', 'health']
    for name, root in target_home.roots.items():
        assert (root / 'папка с пробелом' / 'данные.txt').read_text(encoding='utf-8') == 'synthetic ' + name
        assert (root / 'empty').is_dir()
    assert not (target_home.roots['data'] / 'temporary.txt').exists()
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


@pytest.mark.skipif(os.name != 'posix', reason='Блокировка user-systemd использует POSIX flock')
def test_transferred_backup_resumes_guarded_signed_rollback(signed, tmp_path, monkeypatch):
    pytest.importorskip('cryptography')
    plan, destination, state, adapters, _ = signed
    for root in (destination.home.app, destination.home.releases, *destination.home.roots.values()):
        root.mkdir(parents=True, exist_ok=True)
    source_root = tmp_path / 'другая установка'
    source_home = resolve_datahome(plan.manifest, source_root)
    for root in source_home.roots.values():
        root.mkdir(parents=True, exist_ok=True)
    (source_home.roots['data'] / 'database.txt').write_bytes(b'synthetic original database')
    (destination.home.roots['data'] / 'database.txt').write_bytes(b'synthetic later database')
    source = FilesystemPorts(plan.manifest, source_root, work_dir=tmp_path / 'source work',
                             payloads={}, effects=None, password_provider=lambda: 'synthetic')
    key = state['transaction'] + '/archive'
    with operation_lock(source.lock):
        source.backup(key, 'archive', ())
    source_archive = source.archive_path(key)
    expected = hashlib.sha256(source_archive.read_bytes()).hexdigest()
    # Сценарий моделирует потерю исходной копии на целевой установке.
    destination.archive_path(key).unlink()
    destination._path(key, '.archive.json').unlink()
    destination._path(key, '.quiescence.json').unlink()
    state.update(direction='rollback', index=plan.rollback.index('rollback_stop'), failed_phase='health',
                 archive_ready=True, indeterminate=True)
    plan._save(state)
    with pytest.raises(UpdateIndeterminate):
        plan.run(resume_indeterminate=True)
    waiting = plan.status()
    assert waiting['index'] == plan.rollback.index('restore') and waiting['indeterminate'] is True
    assert read_effect_receipt(destination._path(state['transaction'] + '/before_rollback',
                                                 '.quiescence.json'))['status'] == 'done'
    assert (destination.home.roots['data'] / 'database.txt').read_bytes() == b'synthetic later database'
    destination.import_archive_for_restore(plan, source_archive, expected)
    calls = []
    monkeypatch.setattr('shared.update_health_port.check_local_health', lambda *args, **kwargs: calls.append(1))
    result = plan.run(resume_indeterminate=True)
    assert result['outcome'] == 'rolled_back' and destination.current_release() == '1'
    assert (destination.home.roots['data'] / 'database.txt').read_bytes() == b'synthetic original database'
    assert all(adapter.active for adapter in adapters.values()) and calls == [1]
