"""Pointer update assembly and transaction with synthetic services and real archives."""
import json
from pathlib import Path
import stat

import pytest

from test_api_signed_preflight import signed_request
from test_systemd_pointer_signed import signed_pointer
from test_systemd_portable import FakeReloadManager, fake_manager
from shared.pointer_update import assemble_pointer_linux_update
from shared.runtime_native import prepare_signed_linux_runtime
from shared.systemd_pointer import publish_pointer_signed_user_units
from shared.systemd_user_load import load_pointer_signed_user_units
from shared.update_plan import UpdateError, UpdateIndeterminate


class StatefulManager(FakeReloadManager):
    def __init__(self, directory):
        super().__init__(directory)
        self.active = set()

    def __call__(self, argv, *, timeout, env):
        action, name = argv[4], argv[-1]
        if action in ('start', 'stop'):
            self.calls.append(argv[4:])
            self.active.add(name) if action == 'start' else self.active.discard(name)
            return ''
        output = super().__call__(argv, timeout=timeout, env=env)
        if action == 'show' and name in self.active:
            output = output.replace('ActiveState=inactive\nSubState=dead', 'ActiveState=active\nSubState=running')
        return output


@pytest.fixture
def installation(signed_pointer, tmp_path):
    _, trusted, build = signed_pointer
    for name in ('pre_start', 'health_checks', 'post_update'):
        build.manifest['hooks'][name] = []
    old, old_envelope, root = build('old-pointer')
    new, new_envelope, _ = build('new-pointer', version='2')
    directory, work = tmp_path / 'units', tmp_path / 'update-work'
    directory.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    prepare_signed_linux_runtime(old, old_envelope, str(root), **trusted,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publisher, _ = fake_manager(directory)
    published = publish_pointer_signed_user_units(old, old_envelope, str(root), **trusted,
        modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
        operation_id='b' * 32, reserve_bytes=0, execute=True, runner=publisher)
    manager = StatefulManager(directory)
    load_pointer_signed_user_units(old, old_envelope, str(root), **trusted,
        modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
        operation_id='c' * 32, reserve_bytes=0, runner=manager)
    manager.active = {item['name'] for item in published['units']}
    options = dict(new_package=str(new), new_envelope=new_envelope, new_key=trusted['trusted_public_key'],
        old_package=str(old), old_envelope=old_envelope, old_key=trusted['trusted_public_key'],
        product_id=trusted['expected_product_id'], old_version='1', new_version='2', install_root=str(root),
        work_dir=str(work), modes=('autonomous',), migration_ids=(), operation_id='d' * 32,
        unit_dir=str(directory), load_key='c' * 32, runtime_key='a' * 32, runtime_reserve_bytes=0,
        password_provider=lambda: 'synthetic password', interpreter='/test/python',
        interpreter_sha256='a' * 64, timeout=5, runner=manager)
    return root, work, options, manager


@pytest.mark.parametrize('fail_health', [False, True])
def test_pointer_update_and_rollback_with_verified_runtime(installation, monkeypatch, fail_health):
    root, _, options, manager = installation
    marker = root / 'data/synthetic.txt'
    marker.write_text('original')
    plan = assemble_pointer_linux_update(**options)
    assert plan.status() is None and plan.ports.current_release() == '1'
    calls = []
    def health(key, *_):
        calls.append(key.split('/')[-1])
        if fail_health and key.endswith('/health'):
            raise RuntimeError('synthetic failure')
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', health)
    result = plan.run()
    assert result['outcome'] == ('rolled_back' if fail_health else 'updated')
    assert plan.ports.current_release() == ('1' if fail_health else '2')
    assert marker.read_text() == 'original' and len(manager.active) == 3
    assert stat.S_IMODE((root / 'runtime/bin/python').stat().st_mode) == 0o700
    assert stat.S_IMODE((root / 'runtime/lib/homensai/portable_launcher.py').stat().st_mode) == 0o600
    assert calls == (['health', 'rollback_health'] if fail_health else ['health'])
    assert not any('synthetic password' in json.dumps(call) for call in manager.calls)


def test_pointer_update_refuses_damaged_runtime_before_preparation(installation):
    root, work, options, manager = installation
    (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
    before = list(manager.calls)
    with pytest.raises(UpdateError):
        assemble_pointer_linux_update(**options)
    assert not list(work.iterdir()) and manager.calls == before
    assert not (root / 'services/update.json').exists()


def test_generic_assembly_refuses_pointer_update_without_runtime_guard(installation):
    from shared.systemd_user_load import _pointer_signed_user_group
    from shared.update_assembly import assemble_prepared_linux_update
    root, work, options, manager = installation
    group = _pointer_signed_user_group(options['old_package'], options['old_envelope'], str(root),
        trusted_public_key=options['old_key'], expected_product_id=options['product_id'],
        expected_version='1', modes=options['modes'], unit_dir=options['unit_dir'],
        operation_id=options['load_key'], runtime_operation_id=options['runtime_key'], reserve_bytes=0,
        runner=manager, _require_active=True)
    excluded = {'unit_dir', 'load_key', 'runtime_key', 'runtime_reserve_bytes', 'runner'}
    generic = {name: value for name, value in options.items() if name not in excluded}
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(group=group, **generic)
    assert not list(work.iterdir()) and not (root / 'services/update.json').exists()


def test_damaged_runtime_blocks_restore_before_live_data_changes(installation, monkeypatch):
    root, _, options, _ = installation
    marker = root / 'data/synthetic.txt'
    marker.write_text('original')
    plan = assemble_pointer_linux_update(**options)
    def fail_and_damage(*_):
        marker.write_text('modified during update')
        (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
        raise RuntimeError('synthetic health failure')
    monkeypatch.setattr(plan.ports.effects.health_port, 'health', fail_and_damage)
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    state = plan.status()
    assert state['direction'] == 'rollback' and plan.rollback[state['index']] == 'restore'
    assert state['indeterminate'] and marker.read_text() == 'modified during update'
    assert plan.ports.current_release() == '2'
    assert plan.ports.archive_path(state['transaction'] + '/before_rollback').is_file()
