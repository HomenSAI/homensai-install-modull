from pathlib import Path

import pytest

from shared.adapters_fs import FilesystemPorts
from shared.manifest import load_manifest
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _ACTIVE_EFFECT, _atomic_write


@pytest.fixture
def configured(tmp_path):
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    manifest['hooks']['health_checks'] = []
    manifest['update']['health_wait'] = 1
    install = tmp_path / 'install'
    ports = FilesystemPorts(manifest, install, work_dir=tmp_path / 'work', payloads={},
                            effects=object(), allow_unencrypted=True)
    plan = UpdatePlan(manifest, install, ports, release='2', modes=('platform',),
                      migration_ids=(), guard_effects=True)
    for version in ('1', '2'):
        ports.release_path(version).mkdir(parents=True, exist_ok=True)
    ports.pointer.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(ports.pointer, {'release': '2'})
    state = {'version': 1, 'transaction': 'a' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('health'),
             'failed_phase': None, 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    _atomic_write(plan.state_path, state)
    port = LocalUpdateHealthPort(plan, ports)
    url = f"http://127.0.0.1:{manifest['web']['port']}{manifest['web']['healthz']}"
    return plan, ports, port, state, url


def test_active_forward_and_rollback_use_expected_version(configured, monkeypatch):
    plan, ports, port, state, url = configured
    seen = []
    monkeypatch.setattr('shared.update_health_port.check_local_health',
                        lambda number, **kw: seen.append((number, kw['version'])))
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'health'))
    try:
        port.health(state['transaction'] + '/health', url, 1, ())
    finally:
        _ACTIVE_EFFECT.reset(token)
    state.update(direction='rollback', index=plan.rollback.index('rollback_health'), failed_phase='health')
    _atomic_write(plan.state_path, state)
    _atomic_write(ports.pointer, {'release': '1'})
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'rollback_health'))
    try:
        port.health(state['transaction'] + '/rollback_health', url, 1, ())
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert [version for _, version in seen] == ['2', '1']


def test_only_active_matching_plan_can_probe(configured, monkeypatch):
    plan, _, port, state, url = configured
    monkeypatch.setattr('shared.update_health_port.check_local_health', lambda *a, **k: pytest.fail('unexpected request'))
    with pytest.raises(UpdateError):
        port.health(state['transaction'] + '/health', url, 1, ())
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'health'))
    try:
        for bad_url, timeout, checks in [('http://example.com/health', 1, ()), (url, 2, ()), (url, 1, (('x',),))]:
            with pytest.raises(UpdateError):
                port.health(state['transaction'] + '/health', bad_url, timeout, checks)
    finally:
        _ACTIVE_EFFECT.reset(token)


def test_changed_pointer_during_probe_is_indeterminate(configured, monkeypatch):
    plan, ports, port, state, url = configured
    def change(*args, **kwargs):
        _atomic_write(ports.pointer, {'release': '1'})
    monkeypatch.setattr('shared.update_health_port.check_local_health', change)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'health'))
    try:
        with pytest.raises(EffectIndeterminate):
            port.health(state['transaction'] + '/health', url, 1, ())
    finally:
        _ACTIVE_EFFECT.reset(token)
