from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

from shared.adapters_fs import FilesystemPorts
from shared.linux_update_effects import LinuxSignedUpdateEffects
from shared.manifest import load_manifest
from shared.signed_migration_port import SignedMigrationPort
from shared.systemd_group import SystemdUserGroup
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.systemd_user import SystemdUserAdapter
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _ACTIVE_EFFECT
from shared.writer_guard import CooperativeWriterLock, WriterGuardError


class FakeAdapter(SystemdUserAdapter):
    def __init__(self, manifest, name):
        self.unit = manifest['product_id'] + '-' + name + '.service'
        self.path, self.digest, self.active = Path('/test/' + self.unit), 'a' * 64, True

    def inspect(self):
        return {'active_state': 'active' if self.active else 'inactive'}

    def confirm_stopped(self):
        if self.active:
            raise RuntimeError('active')
        return self.inspect()

    def command(self, action, *, execute=False):
        assert execute
        self.active = action == 'start'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    for name in ('pre_start', 'health_checks', 'post_update'):
        manifest['hooks'][name] = []
    root = tmp_path / 'installation'
    root.mkdir(mode=0o700)
    (root / '.homensai-control').mkdir(mode=0o700)
    ports = FilesystemPorts(manifest, root, work_dir=tmp_path / 'work', payloads={}, effects=object(),
                            password_provider=lambda: 'synthetic')
    plan = UpdatePlan(manifest, root, ports, release='2', modes=('autonomous',), guard_effects=True)
    adapters = {name: FakeAdapter(manifest, name) for name in plan.components}
    group = SystemdUserGroup(manifest, modes=('autonomous',), adapters=adapters)
    lifecycle = SystemdLifecyclePort(plan, ports, install_root=str(root), group=group)
    migration = SignedMigrationPort(plan, ports, install_root=str(root), package_path='/test/package',
                                    envelope=b'test', trusted_public_key=b'a' * 32, interpreter='/test/python',
                                    interpreter_sha256='a' * 64, timeout=10)
    health = LocalUpdateHealthPort(plan, ports)
    effects = LinuxSignedUpdateEffects(lifecycle, migration, health)
    # Изолированные тесты журналов служб; подпись отдельно проверяется в test_linux_signed_release.
    monkeypatch.setattr(effects, '_verify_previous', lambda: None)
    ports.effects = effects
    plan.state_path.parent.mkdir(parents=True)
    state = {'version': 1, 'transaction': 'a' * 32, 'plan': plan.digest, 'old_release': '1', 'release': '2',
             'direction': 'forward', 'index': 0, 'failed_phase': None, 'archive_ready': False,
             'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    return plan, ports, effects, state, adapters


def test_composed_stop_uses_durable_service_port(setup):
    plan, ports, effects, state, adapters = setup
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'stop'))
    try:
        ports.stop(state['transaction'] + '/stop', tuple(reversed(plan.components)))
        ports.stop(state['transaction'] + '/stop', tuple(reversed(plan.components)))
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert all(not adapter.active for adapter in adapters.values())
    from shared.effect_receipt import read_effect_receipt
    assert read_effect_receipt(ports._path(state['transaction'] + '/stop', '.service.json'))['status'] == 'done'


def test_empty_post_update_requires_active_checkpoint(setup):
    plan, ports, _, state, _ = setup
    key = state['transaction'] + '/post_update'
    with pytest.raises(UpdateError):
        ports.post_update(key, ())
    state.update(index=plan.forward.index('post_update'), archive_ready=True)
    plan._save(state)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'post_update'))
    try:
        ports.post_update(key, ())
        with pytest.raises(UpdateError):
            ports.post_update(key, (('arbitrary',),))
    finally:
        _ACTIVE_EFFECT.reset(token)


def test_mixed_transaction_ports_and_post_hooks_rejected(setup, tmp_path):
    plan, ports, effects, _, _ = setup
    other = UpdatePlan(plan.manifest, tmp_path / 'other', ports, release='3',
                       modes=('autonomous',), guard_effects=True)
    other_health = object.__new__(LocalUpdateHealthPort)
    other_health.plan, other_health.ports = other, ports
    with pytest.raises(UpdateError):
        LinuxSignedUpdateEffects(effects.lifecycle, effects.migration, other_health)
    plan.manifest['hooks']['post_update'] = [['arbitrary']]
    with pytest.raises(UpdateError):
        LinuxSignedUpdateEffects(effects.lifecycle, effects.migration, effects.health_port)


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason='Requires Linux flock')
def test_writer_lock_cannot_be_inside_persistent_roots(setup):
    _, ports, effects, _, _ = setup
    guard = CooperativeWriterLock(str(ports.home.roots['data'] / 'writers.lock'))
    with pytest.raises(UpdateError):
        LinuxSignedUpdateEffects(effects.lifecycle, effects.migration, effects.health_port, writer_lock=guard)


def _prepare_archive(setup):
    plan, ports, _, state, adapters = setup
    transaction = state['transaction']
    token = _ACTIVE_EFFECT.set((plan, transaction, 'stop'))
    try:
        ports.stop(transaction + '/stop', tuple(reversed(plan.components)))
    finally:
        _ACTIVE_EFFECT.reset(token)
    state['index'] = plan.forward.index('archive')
    plan._save(state)
    return plan, ports, state, adapters


def test_backup_requires_stopped_group_and_confirms_it_after_snapshot(setup, monkeypatch):
    plan, ports, state, adapters = _prepare_archive(setup)
    key = state['transaction'] + '/archive'
    calls = []
    def fake_backup(*args):
        calls.append(args)
        ports.archive_path(key).write_bytes(b'synthetic archive')
    monkeypatch.setattr(ports, '_backup', fake_backup)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
    try:
        ports.backup(key, 'archive', ())
        assert calls == [(key, 'archive', ())]
        from shared.effect_receipt import read_effect_receipt
        assert read_effect_receipt(ports._path(key, '.quiescence.json'))['status'] == 'done'
        adapters[plan.components[0]].active = True
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
        assert len(calls) == 1
    finally:
        _ACTIVE_EFFECT.reset(token)


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason='Requires Linux flock')
def test_cooperating_writer_blocks_backup_and_backup_blocks_writer(setup, tmp_path, monkeypatch):
    plan, ports, state, _ = _prepare_archive(setup)
    private = tmp_path / 'writer-lock'
    private.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(private / 'writers.lock'))
    ports.effects.writer_lock = guard
    key = state['transaction'] + '/archive'
    calls = []
    def backup(*args):
        calls.append(args)
        with pytest.raises(WriterGuardError):
            with guard.hold(exclusive=False):
                pytest.fail('cooperating writer entered during backup')
        ports.archive_path(key).write_bytes(b'synthetic archive')
    monkeypatch.setattr(ports, '_backup', backup)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
    try:
        with guard.hold(exclusive=False):
            with pytest.raises(EffectIndeterminate):
                ports.backup(key, 'archive', ())
        assert not ports._path(key, '.quiescence.json').exists() and calls == []
        ports.backup(key, 'archive', ())
        from shared.effect_receipt import read_effect_receipt
        record = read_effect_receipt(ports._path(key, '.quiescence.json'))
        assert record['status'] == 'done' and 'writer_lock_sha256' in record and len(calls) == 1
        ports.effects.writer_lock = None
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
        assert len(calls) == 1
    finally:
        _ACTIVE_EFFECT.reset(token)


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason='Requires Linux flock')
def test_replaced_writer_lock_leaves_backup_pending(setup, tmp_path, monkeypatch):
    plan, ports, state, _ = _prepare_archive(setup)
    private = tmp_path / 'writer-lock'
    private.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(private / 'writers.lock'))
    ports.effects.writer_lock = guard
    key = state['transaction'] + '/archive'
    def backup(*args):
        ports.archive_path(key).write_bytes(b'synthetic archive')
        guard.path.unlink()
        guard.path.touch(mode=0o600)
    monkeypatch.setattr(ports, '_backup', backup)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
    try:
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
        from shared.effect_receipt import read_effect_receipt
        assert read_effect_receipt(ports._path(key, '.quiescence.json'))['status'] == 'pending'
    finally:
        _ACTIVE_EFFECT.reset(token)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Requires Linux flock')
def test_replacing_writer_lock_between_retries_rejects_existing_receipt(setup, tmp_path, monkeypatch):
    plan, ports, state, _ = _prepare_archive(setup)
    private = tmp_path / 'writer-lock'
    private.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(private / 'writers.lock'))
    ports.effects.writer_lock = guard
    key = state['transaction'] + '/archive'
    calls = []
    def backup(*args):
        calls.append(args)
        ports.archive_path(key).write_bytes(b'synthetic archive')
    monkeypatch.setattr(ports, '_backup', backup)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
    old_fd = None
    try:
        ports.backup(key, 'archive', ())
        old_fd = os.open(guard.path, os.O_RDONLY)
        guard.path.unlink()
        guard.path.touch(mode=0o600)
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
        assert len(calls) == 1
    finally:
        if old_fd is not None:
            os.close(old_fd)
        _ACTIVE_EFFECT.reset(token)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Requires Linux flock')
def test_real_cooperating_worker_blocks_then_allows_encrypted_update_backup(setup, tmp_path, monkeypatch):
    pytest.importorskip('cryptography')
    from shared import archive_crypto
    from shared.effect_receipt import read_effect_receipt

    plan, ports, state, _ = _prepare_archive(setup)
    private = tmp_path / 'writer-lock'
    private.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(private / 'writers.lock'))
    ports.effects.writer_lock = guard
    for directory in ports.home.roots.values():
        directory.mkdir(parents=True, exist_ok=True)
    key = state['transaction'] + '/archive'
    command = [sys.executable, '-B', '-m', 'examples.demo_product.main', 'worker',
               '--data-dir', str(ports.home.roots['data']), '--writer-lock', str(guard.path)]
    worker = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    original = archive_crypto.build_encrypted_archive
    checked = []
    def attempt_restart_during_archive(*args, **kwargs):
        unwanted = tmp_path / 'unexpected-writer-data'
        result = subprocess.run([sys.executable, '-B', '-m', 'examples.demo_product.main', 'worker',
                                 '--data-dir', str(unwanted), '--writer-lock', str(guard.path)],
                                capture_output=True, timeout=10)
        assert result.returncode != 0 and not unwanted.exists()
        checked.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(archive_crypto, 'build_encrypted_archive', attempt_restart_during_archive)
    try:
        heartbeat = ports.home.roots['data'] / 'worker-heartbeat.json'
        deadline = time.monotonic() + 10
        while not heartbeat.exists() and time.monotonic() < deadline:
            if worker.poll() is not None:
                pytest.fail('writer exited before heartbeat')
            time.sleep(0.05)
        assert heartbeat.exists()
        token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
        try:
            with pytest.raises(EffectIndeterminate):
                ports.backup(key, 'archive', ())
            assert not ports.archive_path(key).exists()
            assert not ports._path(key, '.quiescence.json').exists()
            worker.terminate()
            assert worker.wait(timeout=10) == 0
            ports.backup(key, 'archive', ())
            assert checked == [True]
            record = read_effect_receipt(ports._path(key, '.quiescence.json'))
            assert (record['status'] == 'done' and 'writer_lock_sha256' in record
                    and 'writer_lock_file_sha256' in record)
            archive = ports.archive_path(key)
            assert archive_crypto.is_encrypted(archive)
            metadata = archive_crypto.verify_archive(archive, password_provider=lambda: 'synthetic',
                                                     expected_product_id=plan.manifest['product_id'])
            assert any(item['path'].endswith('/worker-heartbeat.json') for item in metadata['files'])
        finally:
            _ACTIVE_EFFECT.reset(token)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        worker.stderr.close()


def test_restart_during_backup_leaves_pending_and_never_reuses_snapshot(setup, monkeypatch):
    plan, ports, state, adapters = _prepare_archive(setup)
    key = state['transaction'] + '/archive'
    calls = []
    def restart(*args):
        calls.append(args)
        ports.archive_path(key).write_bytes(b'untrusted snapshot')
        adapters[plan.components[0]].active = True
    monkeypatch.setattr(ports, '_backup', restart)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
    try:
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
        adapters[plan.components[0]].active = False
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
        from shared.effect_receipt import read_effect_receipt
        assert read_effect_receipt(ports._path(key, '.quiescence.json'))['status'] == 'pending'
        assert len(calls) == 1
    finally:
        _ACTIVE_EFFECT.reset(token)


def test_null_quiescence_receipt_cannot_authorize_another_snapshot(setup, monkeypatch):
    plan, ports, state, _ = _prepare_archive(setup)
    key = state['transaction'] + '/archive'
    marker = ports._path(key, '.quiescence.json')
    marker.write_bytes(b'null')
    monkeypatch.setattr(ports, '_backup', lambda *args: pytest.fail('backup repeated'))
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
    try:
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert marker.read_bytes() == b'null'


@pytest.mark.skipif(os.name != 'posix', reason='Блокировка user-systemd использует POSIX flock')
def test_confirming_archive_does_not_confirm_interrupted_linux_quiescence(setup, monkeypatch):
    pytest.importorskip('cryptography')
    import shared.adapters_fs as fs
    from shared.effect_receipt import read_effect_receipt
    from shared.update_plan import UpdateIndeterminate

    class Crash(BaseException):
        pass

    plan, ports, state, _ = _prepare_archive(setup)
    for root in ports.home.roots.values():
        root.mkdir(parents=True, exist_ok=True)
    key = state['transaction'] + '/archive'
    original = fs._atomic_write
    captured = []
    def interrupted(path, value):
        if str(path).endswith('.archive.json') and value.get('status') == 'done':
            captured.append(value['sha256'])
            raise Crash()
        original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(fs, '_atomic_write', interrupted)
        token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
        try:
            with pytest.raises(Crash):
                ports.backup(key, 'archive', ())
        finally:
            _ACTIVE_EFFECT.reset(token)
    assert len(captured) == 1
    ports.recover_archive_binding(plan, key, captured[0])
    assert read_effect_receipt(ports.archive_path(key).with_suffix('.archive.json'))['status'] == 'done'
    marker = ports._path(key, '.quiescence.json')
    assert read_effect_receipt(marker)['status'] == 'pending'
    with pytest.raises(UpdateIndeterminate):
        plan.run(resume_indeterminate=True)
    assert read_effect_receipt(marker)['status'] == 'pending'
    assert plan.status() == state


def test_unrecorded_stop_and_existing_untracked_archive_are_rejected(setup, monkeypatch):
    plan, ports, state, _ = _prepare_archive(setup)
    key = state['transaction'] + '/archive'
    monkeypatch.setattr(ports, '_backup', lambda *args: pytest.fail('Unexpected backup'))
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'archive'))
    try:
        stop = ports._path(state['transaction'] + '/stop', '.service.json')
        saved = stop.read_bytes()
        stop.unlink()
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
        stop.write_bytes(saved)
        ports.archive_path(key).write_bytes(b'untracked')
        with pytest.raises(EffectIndeterminate):
            ports.backup(key, 'archive', ())
    finally:
        _ACTIVE_EFFECT.reset(token)


def test_rollback_backup_requires_rollback_stop_receipt(setup, monkeypatch):
    plan, ports, _, state, _ = setup
    state.update(direction='rollback', index=0, failed_phase='health', archive_ready=True)
    plan._save(state)
    transaction = state['transaction']
    token = _ACTIVE_EFFECT.set((plan, transaction, 'rollback_stop'))
    try:
        ports.stop(transaction + '/rollback_stop', tuple(reversed(plan.components)))
    finally:
        _ACTIVE_EFFECT.reset(token)
    state['index'] = plan.rollback.index('before_rollback')
    plan._save(state)
    key = transaction + '/before_rollback'
    monkeypatch.setattr(ports, '_backup', lambda *args: ports.archive_path(key).write_bytes(b'test'))
    token = _ACTIVE_EFFECT.set((plan, transaction, 'before_rollback'))
    try:
        ports.backup(key, 'before-rollback', ())
        from shared.effect_receipt import read_effect_receipt
        receipt = read_effect_receipt(ports._path(key, '.quiescence.json'))
        assert receipt['stop_key'] == transaction + '/rollback_stop' and receipt['status'] == 'done'
    finally:
        _ACTIVE_EFFECT.reset(token)
