"""Проверка подписи выпуска при обновлении без миграций; фиктивные службы."""
import hashlib
from copy import deepcopy
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest

from test_linux_update_effects import FakeAdapter
from shared.adapters_fs import FilesystemPorts, ReleaseSource
from shared.host_target import HostTargetError, native_target
from shared.linux_update_effects import (LinuxSignedUpdateEffects, PreviousSignedRelease,
                                         _previous_manifest_compatible)
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package, verify_signed_package
from shared.signed_migration_port import SignedMigrationPort
from shared.systemd_group import SystemdUserGroup
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import EffectIndeterminate, LockBusy, UpdateError, UpdatePlan, _ACTIVE_EFFECT


@pytest.fixture
def signed(tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    try:
        target = native_target()
    except HostTargetError:
        pytest.skip('Неподдерживаемый хост')
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    manifest['compatibility'] = {'targets': [target]}
    for name in ('pre_start', 'health_checks', 'post_update'):
        manifest['hooks'][name] = []
    payload = tmp_path / 'payload'
    content = {'app.py': b'synthetic signed application', 'locks/linux.lock': b'linux lock',
               'locks/windows.lock': b'windows lock', 'vendor/demo.whl': b'synthetic wheel'}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in content.items()}
    for name, value in content.items():
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    manifest['runtime']['lock_files'] = {name: {'file': 'locks/' + name + '.lock',
        'sha256': hashes['locks/' + name + '.lock']} for name in ('linux', 'windows')}
    manifest['runtime']['vendor_wheels'] = [{'file': 'vendor/demo.whl', 'sha256': hashes['vendor/demo.whl']}]
    package = tmp_path / 'signed.zip'
    build_package(manifest, '2', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'],
                            expected_version='2')
    old_source = tmp_path / 'old-payload'
    shutil.copytree(payload, old_source)
    (old_source / 'app.py').write_bytes(b'older synthetic signed application')
    (old_source / 'locks/linux.lock').write_bytes(b'older linux lock')
    old_manifest = deepcopy(manifest)
    old_manifest['product_name'] = 'Previous synthetic release'
    old_manifest['runtime']['lock_files']['linux']['sha256'] = hashlib.sha256(b'older linux lock').hexdigest()
    old_manifest['migrations']['cmd'] = ['python', 'old_migrate.py']
    old_package = tmp_path / 'old-signed.zip'
    build_package(old_manifest, '1', old_source, old_package)
    old_envelope = sign_package(old_package, private_key=private, expected_product_id=manifest['product_id'],
                                expected_version='1')
    root = tmp_path / 'installation'
    root.mkdir(mode=0o700)
    (root / '.homensai-control').mkdir(mode=0o700)
    ports = FilesystemPorts(manifest, root, work_dir=tmp_path / 'work',
                            payloads={'2': ReleaseSource(payload, hashes)}, effects=object(),
                            password_provider=lambda: 'synthetic')
    plan = UpdatePlan(manifest, root, ports, release='2', modes=('autonomous',),
                      migration_ids=(), guard_effects=True)
    adapters = {name: FakeAdapter(manifest, name) for name in plan.components}
    for adapter in adapters.values():
        adapter.active = False
    group = SystemdUserGroup(manifest, modes=('autonomous',), adapters=adapters)
    lifecycle = SystemdLifecyclePort(plan, ports, install_root=str(root), group=group)
    migration = SignedMigrationPort(plan, ports, install_root=str(root), package_path=str(package),
        envelope=envelope, trusted_public_key=public, interpreter='/test/python',
        interpreter_sha256='a' * 64, timeout=10)
    previous = PreviousSignedRelease(str(old_package), old_envelope, public)
    ports.effects = LinuxSignedUpdateEffects(lifecycle, migration, LocalUpdateHealthPort(plan, ports),
                                             previous_release=previous)
    plan.state_path.parent.mkdir(parents=True)
    shutil.copytree(old_source, ports.release_path('1'))
    for directory in ports.home.roots.values():
        directory.mkdir(parents=True, exist_ok=True)
    ports._backup('a' * 32 + '/archive', 'archive', ())
    # Этот fixture изолирует проверки подписи; остановка отдельно проверяется
    # в test_linux_update_effects с реальным протоколом отметок.
    from shared.update_plan import _atomic_write
    archive_key = 'a' * 32 + '/archive'
    _atomic_write(ports._path(archive_key, '.quiescence.json'), {
        'version': 1, 'plan': plan.digest, 'key': archive_key,
        'stop_key': 'a' * 32 + '/stop', 'installation_sha256': lifecycle.install_hash,
        'units_sha256': lifecycle.units_hash, 'status': 'done'})
    state = {'version': 1, 'transaction': 'a' * 32, 'plan': plan.digest, 'old_release': '1', 'release': '2',
             'direction': 'forward', 'index': 0, 'failed_phase': None, 'archive_ready': True,
             'outcome': 'running', 'indeterminate': True}
    return plan, ports, state, adapters, payload


def invoke(signed, phase):
    plan, ports, state, _, _ = signed
    state['index'] = plan.forward.index(phase)
    plan._save(state)
    key = state['transaction'] + '/' + phase
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], phase))
    try:
        if phase == 'stage':
            ports.stage_release(key, plan.release)
        elif phase == 'switch':
            ports.switch_pointer(key, plan.release)
        else:
            ports.start(key, plan.components, ())
    finally:
        _ACTIVE_EFFECT.reset(token)


def test_signed_release_without_migrations_is_checked_before_stage_switch_and_start(signed):
    plan, ports, _, adapters, _ = signed
    invoke(signed, 'stage')
    invoke(signed, 'switch')
    assert ports.current_release() == plan.release
    invoke(signed, 'start')
    assert all(adapter.active for adapter in adapters.values())


def test_stage_guard_requires_active_plan(signed):
    plan, ports, _, _, _ = signed
    with pytest.raises(UpdateError):
        ports.stage_release('a' * 32 + '/stage', '2')
    assert not ports.release_path(plan.release).exists()


@pytest.mark.skipif(os.name != 'posix', reason='Блокировка user-systemd использует POSIX flock')
def test_update_cannot_start_while_fresh_lifecycle_is_running(signed):
    from shared.systemd_fresh_start import _lifecycle_lock
    plan, ports, state, adapters, _ = signed
    state.update(index=0, archive_ready=False)
    plan._save(state)
    before = plan.state_path.read_bytes()
    with _lifecycle_lock(ports.effects.migration.install_root):
        with pytest.raises(LockBusy):
            plan.run(resume_indeterminate=True)
    assert plan.state_path.read_bytes() == before and not plan.lock_path.exists()
    assert not any(adapter.active for adapter in adapters.values())


@pytest.mark.skipif(os.name != 'posix', reason='Блокировка user-systemd использует POSIX flock')
def test_update_holds_lifecycle_lock_across_phases_and_releases_on_crash(signed, monkeypatch):
    import shared.systemd_fresh_start as fresh
    from shared.systemd_fresh_start import FreshStartError, _lifecycle_lock
    class Crash(BaseException):
        pass
    plan, ports, state, _, _ = signed
    state.update(index=0, archive_ready=False)
    plan._save(state)
    root = ports.effects.migration.install_root
    phases = []
    starts = []
    monkeypatch.setattr(fresh, '_start_portable_fresh', lambda *args, **kwargs: starts.append(1))
    def execute(current, phase):
        phases.append(phase)
        with pytest.raises(BlockingIOError):
            with _lifecycle_lock(root):
                pytest.fail('Блокировка управления службами не удерживается')
        with pytest.raises(FreshStartError):
            fresh.start_portable_fresh('/synthetic/package', b'signature', root)
        assert starts == []
        if phase == 'stage':
            raise Crash()
    monkeypatch.setattr(plan, '_execute', execute)
    with pytest.raises(Crash):
        plan.run(resume_indeterminate=True)
    assert phases == ['stop', 'archive', 'stage']
    with _lifecycle_lock(root):
        pass
    fresh.start_portable_fresh('/synthetic/package', b'signature', root)
    assert starts == [1]


@pytest.mark.skipif(os.name != 'posix', reason='SIGKILL и flock предназначены для POSIX')
def test_lifecycle_lock_released_after_actual_sigkill(signed):
    from shared.systemd_fresh_start import _lifecycle_lock
    _, ports, _, _, _ = signed
    root = ports.effects.migration.install_root
    code = ('import os, signal, sys\n'
            'from shared.systemd_fresh_start import _lifecycle_lock\n'
            'with _lifecycle_lock(sys.argv[1]):\n'
            '    os.kill(os.getpid(), signal.SIGKILL)\n')
    result = subprocess.run([sys.executable, '-B', '-c', code, root], capture_output=True, timeout=30)
    assert result.returncode == -signal.SIGKILL, result.stderr
    with _lifecycle_lock(root):
        pass


def test_modified_source_with_consistent_untrusted_hash_cannot_be_staged(signed):
    plan, ports, _, _, payload = signed
    (payload / 'app.py').write_bytes(b'modified application')
    hashes = dict(ports.payloads['2'].sha256)
    hashes['app.py'] = hashlib.sha256((payload / 'app.py').read_bytes()).hexdigest()
    ports.payloads['2'] = ReleaseSource(payload, hashes)
    with pytest.raises(EffectIndeterminate):
        invoke(signed, 'stage')
    assert not ports.release_path(plan.release).exists()


def test_unknown_publisher_cannot_authorize_stage(signed):
    plan, ports, _, _, _ = signed
    ports.effects.migration.public_key = b'\0' * 32
    with pytest.raises(EffectIndeterminate):
        invoke(signed, 'stage')
    assert not ports.release_path(plan.release).exists()


@pytest.mark.parametrize('phase', ['switch', 'start'])
def test_changed_staged_release_cannot_be_switched_or_started(signed, phase):
    plan, ports, _, adapters, _ = signed
    invoke(signed, 'stage')
    if phase == 'start':
        invoke(signed, 'switch')
    (ports.release_path(plan.release) / 'app.py').write_bytes(b'modified application')
    with pytest.raises(EffectIndeterminate):
        invoke(signed, phase)
    if phase == 'switch':
        assert not ports.pointer.exists()
    assert not any(adapter.active for adapter in adapters.values())


@pytest.mark.parametrize('phase', ['switch', 'start'])
def test_damaged_archive_blocks_switch_and_forward_start(signed, phase):
    plan, ports, state, adapters, _ = signed
    invoke(signed, 'stage')
    if phase == 'start':
        invoke(signed, 'switch')
    before = ports.pointer.read_bytes() if ports.pointer.exists() else None
    archive = ports.archive_path(state['transaction'] + '/archive')
    archive.write_bytes(b'damaged synthetic archive')
    with pytest.raises(EffectIndeterminate):
        invoke(signed, phase)
    assert (ports.pointer.read_bytes() if ports.pointer.exists() else None) == before
    assert not any(adapter.active for adapter in adapters.values())


def test_rollback_pointer_back_uses_recorded_old_release(signed):
    plan, ports, state, _, _ = signed
    invoke(signed, 'stage')
    invoke(signed, 'switch')
    state.update(direction='rollback', index=plan.rollback.index('pointer_back'),
                 failed_phase='health', archive_ready=True, indeterminate=True)
    plan._save(state)
    key = state['transaction'] + '/pointer_back'
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'pointer_back'))
    try:
        ports.switch_pointer(key, '1')
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert ports.current_release() == '1'


def test_substitution_during_switch_is_indeterminate_after_pointer_write(signed, monkeypatch):
    plan, ports, _, adapters, _ = signed
    invoke(signed, 'stage')
    original = ports._switch_pointer
    def switch_and_tamper(key, release):
        original(key, release)
        (ports.release_path(release) / 'app.py').write_bytes(b'substituted after switch')
    monkeypatch.setattr(ports, '_switch_pointer', switch_and_tamper)
    with pytest.raises(EffectIndeterminate):
        invoke(signed, 'switch')
    assert ports.current_release() == plan.release
    assert not any(adapter.active for adapter in adapters.values())


@pytest.mark.parametrize('phase', ['stop', 'restore', 'pointer_back', 'rollback_start'])
@pytest.mark.parametrize('damage', ['missing_evidence', 'wrong_key', 'wrong_version', 'modified_release'])
def test_untrusted_old_release_blocks_forward_stop_and_rollback(signed, monkeypatch, phase, damage):
    plan, ports, state, adapters, _ = signed
    if damage == 'missing_evidence':
        ports.effects.previous_release = None
    elif damage == 'wrong_key':
        old = ports.effects.previous_release
        ports.effects.previous_release = PreviousSignedRelease(old.package_path, old.envelope, b'\0' * 32)
    elif damage == 'wrong_version':
        new = ports.effects.migration
        ports.effects.previous_release = PreviousSignedRelease(new.package_path, new.envelope, new.public_key)
    else:
        (ports.release_path('1') / 'app.py').write_bytes(b'modified old release')
    forward = phase == 'stop'
    state.update(direction='forward' if forward else 'rollback',
                 index=(plan.forward if forward else plan.rollback).index(phase),
                 failed_phase=None if forward else 'health', archive_ready=not forward, indeterminate=True)
    plan._save(state)
    calls = []
    monkeypatch.setattr(ports, '_restore', lambda *args: calls.append(args))
    key = state['transaction'] + '/' + phase
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], phase))
    try:
        with pytest.raises(EffectIndeterminate):
            if phase == 'stop':
                ports.stop(key, tuple(reversed(plan.components)))
            elif phase == 'restore':
                ports.restore(key, state['transaction'] + '/archive', ())
            elif phase == 'pointer_back':
                ports.switch_pointer(key, '1')
            else:
                ports.start(key, plan.components, ())
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert not calls and not ports.pointer.exists()
    assert not any(adapter.active for adapter in adapters.values())


def test_previous_release_is_rechecked_after_restore(signed, monkeypatch):
    plan, ports, state, _, _ = signed
    state.update(direction='rollback', index=plan.rollback.index('restore'), failed_phase='health',
                 archive_ready=True, indeterminate=True)
    plan._save(state)
    calls = []
    def restore_and_tamper(*args):
        calls.append(args)
        (ports.release_path('1') / 'app.py').write_bytes(b'changed during restore')
    monkeypatch.setattr(ports, '_restore', restore_and_tamper)
    key = state['transaction'] + '/restore'
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'restore'))
    try:
        with pytest.raises(EffectIndeterminate):
            ports.restore(key, state['transaction'] + '/archive', ())
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert len(calls) == 1 and not ports.pointer.exists()


@pytest.mark.parametrize('changed', ['paths', 'service_command', 'web'])
def test_signed_old_manifest_with_incompatible_contract_is_rejected(signed, tmp_path, changed):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    plan, ports, state, adapters, _ = signed
    old = ports.effects.previous_release
    metadata = verify_signed_package(old.package_path, old.envelope, trusted_public_key=old.public_key,
                                     expected_product_id=plan.manifest['product_id'], expected_version='1')
    manifest = deepcopy(metadata['manifest'])
    if changed == 'paths':
        manifest['paths']['data'] = 'different-data'
    elif changed == 'service_command':
        manifest['components'][0]['cmd'] = ['python', 'different.py']
    elif changed == 'web':
        manifest['web']['port'] = 8821
    assert not _previous_manifest_compatible(manifest, plan.manifest)
    package = tmp_path / ('incompatible-' + changed + '.zip')
    build_package(manifest, '1', ports.release_path('1'), package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'],
                            expected_version='1')
    ports.effects.previous_release = PreviousSignedRelease(str(package), envelope, public)
    state.update(direction='forward', index=0, failed_phase=None, archive_ready=False, indeterminate=True)
    plan._save(state)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'stop'))
    try:
        with pytest.raises(EffectIndeterminate):
            ports.stop(state['transaction'] + '/stop', tuple(reversed(plan.components)))
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert not any(adapter.active for adapter in adapters.values())


def test_runtime_interpreter_policy_remains_equal(signed):
    plan, _, _, _, _ = signed
    manifest = deepcopy(plan.manifest)
    manifest['runtime']['python'] = '3.15'
    assert not _previous_manifest_compatible(manifest, plan.manifest)
    manifest = deepcopy(plan.manifest)
    manifest['runtime']['offline'] = False
    assert not _previous_manifest_compatible(manifest, plan.manifest)
