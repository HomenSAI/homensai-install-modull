"""Проверка связи подготовленного пакета с защищённым портом выпуска."""
from pathlib import Path
import json
import sys

import pytest

from test_api_signed_preflight import signed_request
from test_linux_update_effects import FakeAdapter
from test_update_prepare import update
from shared.adapters_fs import _tree
from shared.fresh_prepare import prepare_fresh_native
from shared.host_target import HostTargetError, native_target
from shared.package import build_package
from shared.package_signing import sign_package
from shared.systemd_group import SystemdUserGroup
from shared.update_assembly import assemble_prepared_linux_update
from shared.update_plan import EffectIndeterminate, UpdateError, UpdateIndeterminate, _ACTIVE_EFFECT

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Требуется Linux')


@pytest.fixture
def assembled(signed_request, tmp_path):
    try:
        if native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}:
            pytest.skip('Требуется Linux x86-64')
    except HostTargetError:
        pytest.skip('Требуется Linux x86-64')
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    query, _, manifest = signed_request
    for name in ('pre_start', 'health_checks', 'post_update'):
        manifest['hooks'][name] = []
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    source = tmp_path / 'payload'
    old_package = tmp_path / 'old-compatible.zip'
    build_package(manifest, '1', source, old_package)
    old_envelope = sign_package(old_package, private_key=private,
                                expected_product_id=manifest['product_id'], expected_version='1')
    root = query['params']['install_root']
    prepare_fresh_native(old_package, old_envelope, root, trusted_public_key=public,
        expected_product_id=manifest['product_id'], expected_version='1',
        required_bytes=query['params']['required_bytes'], reserve_bytes=0)
    (source / 'new.py').write_text('synthetic new release', encoding='utf-8')
    new_package = tmp_path / 'new-compatible.zip'
    build_package(manifest, '2', source, new_package)
    new_envelope = sign_package(new_package, private_key=private,
                                expected_product_id=manifest['product_id'], expected_version='2')
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    modes = ('autonomous',)
    from shared.services import component_order
    adapters = {name: FakeAdapter(manifest, name) for name in component_order(manifest, modes)}
    group = SystemdUserGroup(manifest, modes=modes, adapters=adapters)
    options = dict(new_package=str(new_package), new_envelope=new_envelope, new_key=public,
        old_package=str(old_package), old_envelope=old_envelope, old_key=public,
        product_id=manifest['product_id'], old_version='1', new_version='2', install_root=root,
        work_dir=str(work), modes=modes, migration_ids=(), operation_id='a' * 32,
        group=group, password_provider=lambda: 'synthetic', interpreter='/test/python',
        interpreter_sha256='a' * 64, timeout=5)
    return options, adapters


def test_assembled_plan_stages_signed_payload_without_starting_services(assembled):
    options, adapters = assembled
    pointer = Path(options['install_root']) / 'services/active.json'
    before = pointer.read_bytes()
    plan = assemble_prepared_linux_update(**options)
    ports = plan.ports
    ports._backup('b' * 32 + '/archive', 'archive', ())
    assert plan.status() is None and not ports.release_path('2').exists()
    assert all(adapter.active for adapter in adapters.values())
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('stage'),
             'failed_phase': None, 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'stage'))
    try:
        ports.stage_release(state['transaction'] + '/stage', '2')
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert _tree(ports.release_path('2')) == _tree(ports.payloads['2'].directory)
    assert pointer.read_bytes() == before and all(adapter.active for adapter in adapters.values())


@pytest.mark.parametrize('damage', ['missing', 'modified', 'wrong-password'])
def test_stage_refuses_unverified_backup_before_publishing(assembled, damage):
    options, adapters = assembled
    pointer = Path(options['install_root']) / 'services/active.json'
    before = pointer.read_bytes()
    plan = assemble_prepared_linux_update(**options)
    ports = plan.ports
    key = 'b' * 32
    ports._backup(key + '/archive', 'archive', ())
    archive = ports.archive_path(key + '/archive')
    if damage == 'missing':
        archive.unlink()
    elif damage == 'modified':
        archive.write_bytes(b'damaged synthetic archive')
    else:
        ports.password_provider = lambda: 'wrong synthetic password'
    state = {'version': 1, 'transaction': key, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('stage'),
             'failed_phase': None, 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    token = _ACTIVE_EFFECT.set((plan, key, 'stage'))
    try:
        with pytest.raises(EffectIndeterminate):
            ports.stage_release(key + '/stage', '2')
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert not ports.release_path('2').exists() and pointer.read_bytes() == before
    assert all(adapter.active for adapter in adapters.values()) and plan.status() == state


def test_assembly_rejects_bad_configuration_before_preparing_payload(assembled):
    options, _ = assembled
    work = Path(options['work_dir'])
    for bad in ({'timeout': float('nan')}, {'password_provider': None},
                {'interpreter_sha256': 'invalid'}, {'group': object()}):
        with pytest.raises(UpdateError):
            assemble_prepared_linux_update(**{**options, **bad})
        assert not list(work.iterdir())


def test_assembly_rejects_version_pinned_fresh_units_before_preparation(signed_request, tmp_path):
    from hashlib import sha256
    from shared.services import component_order, render_portable_user_services_v1
    from shared.systemd_portable import RECIPE_PATH, RECIPE_V1

    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    query, _, manifest = signed_request
    manifest['secrets'] = []
    for name in ('pre_start', 'health_checks', 'post_update'):
        manifest['hooks'][name] = []
    for component in manifest['components']:
        component['cmd'] = ['@runtime/bin/python', '@release/main.py']
    source = tmp_path / 'payload'
    recipe = source / RECIPE_PATH
    recipe.parent.mkdir(parents=True, exist_ok=True)
    recipe.write_bytes(RECIPE_V1)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    packages, envelopes = {}, {}
    for version in ('1', '2'):
        packages[version] = tmp_path / ('signed-' + version + '.zip')
        build_package(manifest, version, source, packages[version])
        envelopes[version] = sign_package(packages[version], private_key=private,
                                          expected_product_id=manifest['product_id'], expected_version=version)
    root = query['params']['install_root']
    prepare_fresh_native(packages['1'], envelopes['1'], root, trusted_public_key=public,
        expected_product_id=manifest['product_id'], expected_version='1',
        required_bytes=query['params']['required_bytes'], reserve_bytes=0)
    modes = ('autonomous',)
    units = render_portable_user_services_v1(manifest, modes=modes, install_root=root, version='1')
    adapters = {name: FakeAdapter(manifest, name) for name in component_order(manifest, modes)}
    for adapter in adapters.values():
        adapter.digest = sha256(units[adapter.unit].encode()).hexdigest()
    group = SystemdUserGroup(manifest, modes=modes, adapters=adapters)
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    options = dict(new_package=str(packages['2']), new_envelope=envelopes['2'], new_key=public,
        old_package=str(packages['1']), old_envelope=envelopes['1'], old_key=public,
        product_id=manifest['product_id'], old_version='1', new_version='2', install_root=root,
        work_dir=str(work), modes=modes, migration_ids=(), operation_id='a' * 32,
        group=group, password_provider=lambda: 'synthetic', interpreter='/test/python',
        interpreter_sha256='a' * 64, timeout=5)
    from shared.update_assembly import _reject_version_pinned_portable_group
    with pytest.raises(UpdateError, match='привязаны к прежнему выпуску'):
        _reject_version_pinned_portable_group(group, options)
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options)
    assert not list(work.iterdir()) and all(adapter.active for adapter in adapters.values())
    digests = {name: adapter.digest for name, adapter in adapters.items()}
    for adapter in adapters.values():
        adapter.digest = 'b' * 64
    _reject_version_pinned_portable_group(group, options)  # Не подтверждает иные units.
    for name, adapter in adapters.items():
        adapter.digest = digests[name]
    from shared.adapters_fs import FilesystemPorts
    from shared.update_plan import UpdatePlan
    from shared.update_prepare import prepare_signed_linux_update
    prepare = {key: value for key, value in options.items() if key not in
               ('group', 'password_provider', 'interpreter', 'interpreter_sha256', 'timeout')}
    prepare_signed_linux_update(**prepare)
    ports = FilesystemPorts(manifest, Path(root), work_dir=work, payloads={}, effects=None,
                            password_provider=lambda: 'synthetic')
    existing = UpdatePlan(manifest, Path(root), ports, release='2', modes=modes,
                          migration_ids=(), guard_effects=True)
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': existing.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': 0, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    existing._save(state)
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True)
    state.update(direction='rollback', index=0, failed_phase='health', archive_ready=True)
    existing._save(state)
    assert assemble_prepared_linux_update(**options, reopen=True).status() == state
    assert all(adapter.active for adapter in adapters.values())


def test_assembly_rejects_unsupported_hooks_without_preparing_payload(update):
    options, manifest, _, _ = update
    from shared.services import component_order
    adapters = {name: FakeAdapter(manifest, name) for name in component_order(manifest, options['modes'])}
    group = SystemdUserGroup(manifest, modes=options['modes'], adapters=adapters)
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, group=group, password_provider=lambda: 'synthetic',
            interpreter='/test/python', interpreter_sha256='a' * 64, timeout=5)
    assert not list(Path(options['work_dir']).iterdir())


def test_reopen_refuses_when_new_package_wheel_audit_fails(assembled, monkeypatch):
    from shared.wheel_audit import WheelAuditError
    options, adapters = assembled
    plan = assemble_prepared_linux_update(**options)
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': 0, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    def fail(*args, **kwargs):
        raise WheelAuditError('synthetic')
    monkeypatch.setattr('shared.wheel_audit.audit_signed_wheels', fail)
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True)
    assert plan.status() == state and all(adapter.active for adapter in adapters.values())
    from shared.update_prepare import verify_started_linux_update
    inspect_options = {key: value for key, value in options.items() if key not in
                       ('group', 'password_provider', 'interpreter', 'interpreter_sha256', 'timeout')}
    assert verify_started_linux_update(**inspect_options)['state'] == state


def test_new_pointer_policy_blocks_forward_resume_but_not_rollback(assembled, monkeypatch):
    from shared.update_prepare import UpdatePrepareError
    options, adapters = assembled
    plan = assemble_prepared_linux_update(**options)
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': 0, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    def reject(*args):
        raise UpdatePrepareError('new synthetic policy')
    monkeypatch.setattr('shared.update_assembly._check_pointer_runtime_compatibility', reject)
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True)
    assert plan.status() == state and all(adapter.active for adapter in adapters.values())
    state.update(direction='rollback', index=0, failed_phase='health', archive_ready=True)
    plan._save(state)
    assert assemble_prepared_linux_update(**options, reopen=True).status() == state
    assert all(adapter.active for adapter in adapters.values())


def test_guarded_stage_rejects_tampered_prepared_payload(assembled):
    options, _ = assembled
    plan = assemble_prepared_linux_update(**options)
    plan.ports._backup('b' * 32 + '/archive', 'archive', ())
    (plan.ports.payloads['2'].directory / 'new.py').write_text('tampered', encoding='utf-8')
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('stage'),
             'failed_phase': None, 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'stage'))
    try:
        with pytest.raises(EffectIndeterminate):
            plan.ports.stage_release(state['transaction'] + '/stage', '2')
    finally:
        _ACTIVE_EFFECT.reset(token)
    assert not plan.ports.release_path('2').exists()


def test_reopen_requires_bound_running_checkpoint_and_never_resumes_automatically(assembled):
    options, adapters = assembled
    plan = assemble_prepared_linux_update(**options)
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True)
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': 0, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    reopened = assemble_prepared_linux_update(**options, reopen=True)
    assert reopened.status() == state and all(adapter.active for adapter in adapters.values())
    with pytest.raises(UpdateIndeterminate):
        reopened.run()
    assert reopened.status() == state and all(adapter.active for adapter in adapters.values())
    key = state['transaction'] + '/stop'
    assert assemble_prepared_linux_update(**options, reopen=True, reconcile_stop_key=key).status() == state
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True, reconcile_stop_key='c' * 32 + '/stop')
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**{**options, 'operation_id': 'd' * 32}, reopen=True)
    marker = Path(options['work_dir']) / ('prepare-' + options['operation_id'] + '.json')
    original = marker.read_bytes()
    malformed = json.loads(original)
    malformed['version'] = True
    marker.write_text(json.dumps(malformed), encoding='utf-8')
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True)
    marker.write_bytes(original)
    (plan.ports.payloads['2'].directory / 'new.py').write_bytes(b'tampered')
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True)


def test_reopen_after_switch_verifies_both_signed_releases(assembled):
    options, _ = assembled
    plan = assemble_prepared_linux_update(**options)
    ports = plan.ports
    ports._backup('b' * 32 + '/archive', 'archive', ())
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('stage'),
             'failed_phase': None, 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    token = _ACTIVE_EFFECT.set((plan, state['transaction'], 'stage'))
    try:
        ports.stage_release(state['transaction'] + '/stage', '2')
    finally:
        _ACTIVE_EFFECT.reset(token)
    ports._switch_pointer(state['transaction'] + '/switch', '2')
    state.update(index=plan.forward.index('start'))
    plan._save(state)
    assert assemble_prepared_linux_update(**options, reopen=True).status() == state
    (ports.release_path('1') / 'locks/demo-linux.lock').write_bytes(b'tampered')
    with pytest.raises(UpdateError):
        assemble_prepared_linux_update(**options, reopen=True)
