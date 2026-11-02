"""Проверка подписи выпуска при обновлении без миграций; фиктивные службы."""
import hashlib
from pathlib import Path

import pytest

from test_linux_update_effects import FakeAdapter
from shared.adapters_fs import FilesystemPorts, ReleaseSource
from shared.host_target import HostTargetError, native_target
from shared.linux_update_effects import LinuxSignedUpdateEffects
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.signed_migration_port import SignedMigrationPort
from shared.systemd_group import SystemdUserGroup
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _ACTIVE_EFFECT


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
    root = tmp_path / 'installation'
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
    ports.effects = LinuxSignedUpdateEffects(lifecycle, migration, LocalUpdateHealthPort(plan, ports))
    plan.state_path.parent.mkdir(parents=True)
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


def test_rollback_pointer_back_uses_recorded_old_release(signed):
    plan, ports, state, _, _ = signed
    invoke(signed, 'stage')
    invoke(signed, 'switch')
    ports.release_path('1').mkdir(parents=True)
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
