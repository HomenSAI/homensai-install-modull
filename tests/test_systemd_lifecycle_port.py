"""Синтетические user-units: журнал, порядок, сбой и запрет повтора."""
from pathlib import Path

import pytest

import shared.systemd_lifecycle_port as module
from shared.adapters_fs import FilesystemPorts
from shared.manifest import load_manifest
from shared.systemd_group import SystemdUserGroup
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.systemd_user import SystemdUserAdapter
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _ACTIVE_EFFECT

ROOT = Path(__file__).resolve().parents[1]
TX = 'a' * 32


class FakeAdapter(SystemdUserAdapter):
    def __init__(self, manifest, name, events):
        self.unit = manifest['product_id'] + '-' + name + '.service'
        self.path, self.digest = Path('/synthetic/' + self.unit), 'a' * 64
        self.name, self.events, self.active, self.fail = name, events, True, False

    def inspect(self):
        return {'unit': self.unit, 'active_state': 'active' if self.active else 'inactive'}

    def confirm_stopped(self):
        if self.active:
            raise RuntimeError('synthetic failure')
        return self.inspect()

    def command(self, action, *, execute=False):
        if not execute:
            return {'action': action, 'unit': self.unit}
        self.events.append((action, self.name))
        if self.fail:
            raise RuntimeError('synthetic secret')
        self.active = action == 'start'
        return self.inspect()


@pytest.fixture
def setup(tmp_path):
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['hooks']['pre_start'] = []
    root = tmp_path / 'installation'
    ports = FilesystemPorts(manifest, root, work_dir=tmp_path / 'work', payloads={}, effects=object(),
                            password_provider=lambda: 'synthetic')
    plan = UpdatePlan(manifest, root, ports, release='2', modes=('autonomous',), guard_effects=True)
    events = []
    adapters = {name: FakeAdapter(manifest, name, events) for name in plan.components}
    group = SystemdUserGroup(manifest, modes=('autonomous',), adapters=adapters)
    subject = SystemdLifecyclePort(plan, ports, install_root=str(root), group=group)
    plan.state_path.parent.mkdir(parents=True)
    state = {'version': 1, 'transaction': TX, 'plan': plan.digest, 'old_release': '1', 'release': '2',
             'direction': 'forward', 'index': 0, 'failed_phase': None, 'archive_ready': False,
             'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    return plan, ports, subject, adapters, events


def invoke(plan, subject, phase='stop'):
    token = _ACTIVE_EFFECT.set((plan, TX, phase))
    try:
        if phase in ('stop', 'rollback_stop'):
            subject.stop(TX + '/' + phase, tuple(reversed(plan.components)))
        else:
            subject.start(TX + '/' + phase, plan.components, ())
    finally:
        _ACTIVE_EFFECT.reset(token)


def test_stop_once_and_recheck_before_acknowledging(setup):
    plan, _, subject, adapters, events = setup
    invoke(plan, subject)
    assert events == [('stop', 'scheduler'), ('stop', 'bot'), ('stop', 'web')]
    invoke(plan, subject)
    assert len(events) == 3
    adapters['web'].active = True
    with pytest.raises(EffectIndeterminate):
        invoke(plan, subject)
    assert len(events) == 3


def test_partial_stop_is_pending_and_never_retried(setup):
    plan, ports, subject, adapters, events = setup
    adapters['bot'].fail = True
    with pytest.raises(EffectIndeterminate) as error:
        invoke(plan, subject)
    assert 'synthetic secret' not in str(error.value)
    assert events == [('stop', 'scheduler'), ('stop', 'bot')]
    assert module.read_effect_receipt(ports._path(TX + '/stop', '.service.json'))['status'] == 'pending'
    adapters['bot'].fail = False
    with pytest.raises(EffectIndeterminate):
        invoke(plan, subject)
    assert len(events) == 2


def test_crash_before_done_keeps_pending(setup, monkeypatch):
    class Crash(BaseException):
        pass
    plan, ports, subject, _, events = setup
    original = module._atomic_write
    def crash(path, value):
        if value['status'] == 'done':
            raise Crash()
        original(path, value)
    monkeypatch.setattr(module, '_atomic_write', crash)
    with pytest.raises(Crash):
        invoke(plan, subject)
    assert module.read_effect_receipt(ports._path(TX + '/stop', '.service.json'))['status'] == 'pending'
    with pytest.raises(EffectIndeterminate):
        invoke(plan, subject)
    assert len(events) == 3


def test_wrong_context_and_changed_unit_are_refused(setup):
    plan, _, subject, adapters, events = setup
    with pytest.raises(UpdateError):
        subject.stop(TX + '/stop', tuple(reversed(plan.components)))
    adapters['web'].digest = 'b' * 64
    with pytest.raises(EffectIndeterminate):
        invoke(plan, subject)
    assert not events


def test_corrupt_service_receipt_prevents_action(setup):
    plan, ports, subject, _, events = setup
    ports.work.mkdir()
    ports._path(TX + '/stop', '.service.json').write_bytes(b'{"status":"pending","status":"done"}')
    with pytest.raises(EffectIndeterminate):
        invoke(plan, subject)
    assert events == []


def test_start_uses_forward_order_and_requires_no_hooks(setup):
    plan, _, subject, adapters, events = setup
    for adapter in adapters.values():
        adapter.active = False
    state = plan.status()
    state.update(index=plan.forward.index('start'), archive_ready=True)
    plan._save(state)
    token = _ACTIVE_EFFECT.set((plan, TX, 'start'))
    try:
        with pytest.raises(UpdateError):
            subject.start(TX + '/start', plan.components, (('unexpected-hook',),))
    finally:
        _ACTIVE_EFFECT.reset(token)
    invoke(plan, subject, 'start')
    assert events == [('start', 'web'), ('start', 'bot'), ('start', 'scheduler')]
