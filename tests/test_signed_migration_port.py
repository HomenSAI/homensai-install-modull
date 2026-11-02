"""Контракт ключа/журнала порта; файловая система и данные синтетические."""
from pathlib import Path

import pytest

import shared.signed_migration_port as module
from shared.adapters_fs import FilesystemPorts
from shared.manifest import load_manifest
from shared.signed_migration_port import SignedMigrationPort
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _ACTIVE_EFFECT

ROOT = Path(__file__).resolve().parents[1]
TRANSACTION = 'a' * 32
KEY = TRANSACTION + '/migrate:0'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    root = tmp_path / 'installation'
    ports = FilesystemPorts(manifest, root, work_dir=tmp_path / 'work', payloads={}, effects=object(),
                            password_provider=lambda: 'synthetic')
    plan = UpdatePlan(manifest, root, ports, release='2', modes=('autonomous',),
                      migration_ids=('schema-2',), guard_effects=True)
    plan.state_path.parent.mkdir(parents=True)
    ports.work.mkdir()
    state = {'version': 1, 'transaction': TRANSACTION, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('migrate:0'),
             'failed_phase': None, 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    params = {'install_root': str(root), 'package_path': str(tmp_path / 'synthetic.zip'),
              'envelope': b'synthetic-envelope', 'trusted_public_key': b'x' * 32,
              'interpreter': '/synthetic/python', 'interpreter_sha256': 'a' * 64, 'timeout': 1}
    port = SignedMigrationPort(plan, ports, **params)
    monkeypatch.setattr(module.archive_crypto, 'is_encrypted', lambda path: True)
    monkeypatch.setattr(ports, '_verify_archive', lambda path: {'roots': list(ports.home.roots)})
    calls = []
    monkeypatch.setattr(module, 'run_signed_native_migration', lambda **options: calls.append(options['execute'] if 'execute' in options else False))
    return plan, ports, port, params, calls


def invoke(plan, port, key=KEY, migration='schema-2', command=None):
    token = _ACTIVE_EFFECT.set((plan, TRANSACTION, 'migrate:0'))
    try:
        port.migrate(key, migration, tuple(plan.manifest['migrations']['cmd']) if command is None else command)
    finally:
        _ACTIVE_EFFECT.reset(token)


def test_only_matching_active_plan_can_start(setup):
    plan, _, port, _, calls = setup
    with pytest.raises(UpdateError):
        port.migrate(KEY, 'schema-2', tuple(plan.manifest['migrations']['cmd']))
    for key, migration, command in ((KEY, 'wrong', None), ('b' * 32 + '/migrate:0', 'schema-2', None),
                                    (KEY, 'schema-2', ('unexpected',))):
        with pytest.raises(UpdateError):
            invoke(plan, port, key, migration, command)
    assert calls == []


def test_checkpoint_must_be_exactly_the_migration_step(setup):
    plan, _, port, _, calls = setup
    state = plan.status()
    state['index'] = plan.forward.index('backup:0')
    plan._save(state)
    with pytest.raises(UpdateError):
        invoke(plan, port)
    assert calls == []


def test_done_receipt_prevents_second_execution(setup):
    plan, ports, port, _, calls = setup
    invoke(plan, port)
    assert calls == [False, True, False]
    invoke(plan, port)
    assert calls == [False, True, False, False]
    record = port._read(ports._path(KEY, '.migration.json'))
    assert record['status'] == 'done' and record['migration'] == 'schema-2'


def test_done_receipt_rechecks_release_before_returning(setup, monkeypatch):
    plan, _, port, _, calls = setup
    invoke(plan, port)
    def changed(**options):
        calls.append(options.get('execute', False))
        raise UpdateError('release changed')
    monkeypatch.setattr(module, 'run_signed_native_migration', changed)
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert calls == [False, True, False, False]


def test_pending_receipt_blocks_repeat_and_changed_binding(setup, monkeypatch):
    plan, ports, port, params, calls = setup
    def fail(**options):
        calls.append(options.get('execute', False))
        if options.get('execute'):
            raise RuntimeError('synthetic secret')
    monkeypatch.setattr(module, 'run_signed_native_migration', fail)
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert port._read(ports._path(KEY, '.migration.json'))['status'] == 'pending'
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    altered = SignedMigrationPort(plan, ports, **{**params, 'interpreter_sha256': 'b' * 64})
    with pytest.raises(EffectIndeterminate):
        invoke(plan, altered)
    assert calls == [False, True]


def test_crash_after_effect_leaves_pending_and_blocks_repeat(setup, monkeypatch):
    class Crash(BaseException):
        pass
    plan, ports, port, _, calls = setup
    original = module._atomic_write
    def crash(path, value):
        if value['status'] == 'done':
            raise Crash()
        original(path, value)
    monkeypatch.setattr(module, '_atomic_write', crash)
    with pytest.raises(Crash):
        invoke(plan, port)
    assert port._read(ports._path(KEY, '.migration.json'))['status'] == 'pending'
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert calls == [False, True, False]


def test_changed_release_after_execution_stays_pending(setup, monkeypatch):
    plan, ports, port, _, calls = setup
    def changed(**options):
        calls.append(options.get('execute', False))
        if calls == [False, True, False]:
            raise UpdateError('release changed')
    monkeypatch.setattr(module, 'run_signed_native_migration', changed)
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert port._read(ports._path(KEY, '.migration.json'))['status'] == 'pending'
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert calls == [False, True, False]


def test_unverified_backup_blocks_execution(setup, monkeypatch):
    plan, ports, port, _, calls = setup
    monkeypatch.setattr(ports, '_verify_archive', lambda path: (_ for _ in ()).throw(ValueError('bad archive')))
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert calls == []


def test_corrupt_receipt_never_reexecutes(setup):
    plan, ports, port, _, calls = setup
    ports._path(KEY, '.migration.json').write_bytes(b'{"status":"pending","status":"done"}')
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert calls == []


def test_unencrypted_ports_and_mismatched_installation_rejected(setup, tmp_path):
    plan, ports, _, params, _ = setup
    with pytest.raises(UpdateError):
        SignedMigrationPort(plan, ports, **{**params, 'install_root': str(tmp_path / 'other')})
    ports.password_provider = None
    with pytest.raises(UpdateError):
        SignedMigrationPort(plan, ports, **params)
