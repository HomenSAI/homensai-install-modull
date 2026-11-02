from pathlib import Path

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
def setup(tmp_path):
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    for name in ('pre_start', 'health_checks', 'post_update'):
        manifest['hooks'][name] = []
    root = tmp_path / 'installation'
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
