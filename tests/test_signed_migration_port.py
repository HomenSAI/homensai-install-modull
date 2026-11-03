"""Контракт ключа/журнала порта; файловая система и данные синтетические."""
from pathlib import Path
import os
import signal
import subprocess
import sys
import time

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


@pytest.mark.parametrize('damaged', ['archive', 'backup:0'])
def test_real_damaged_encrypted_archive_blocks_migration(tmp_path, monkeypatch, damaged):
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    root = tmp_path / 'installation'
    ports = FilesystemPorts(manifest, root, work_dir=tmp_path / 'work', payloads={}, effects=object(),
                            password_provider=lambda: 'synthetic password')
    for directory in ports.home.roots.values():
        directory.mkdir(parents=True, exist_ok=True)
    plan = UpdatePlan(manifest, root, ports, release='2', modes=('autonomous',),
                      migration_ids=('schema-2',), guard_effects=True)
    for name in ('archive', 'backup:0'):
        ports._backup(TRANSACTION + '/' + name, name, ())
    ports.archive_path(TRANSACTION + '/' + damaged).write_bytes(b'damaged synthetic archive')
    state = {'version': 1, 'transaction': TRANSACTION, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('migrate:0'),
             'failed_phase': None, 'archive_ready': True, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    port = SignedMigrationPort(plan, ports, install_root=str(root), package_path=str(tmp_path / 'synthetic.zip'),
        envelope=b'synthetic-envelope', trusted_public_key=b'x' * 32, interpreter='/synthetic/python',
        interpreter_sha256='a' * 64, timeout=1)
    calls = []
    monkeypatch.setattr(module, 'run_signed_native_migration', lambda **options: calls.append(options))
    with pytest.raises(EffectIndeterminate):
        invoke(plan, port)
    assert not calls and not ports._path(KEY, '.migration.json').exists()


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


_CRASH_CONTROLLER = r'''
from pathlib import Path
import os, sys, time
sys.path.insert(0, sys.argv[2])
import shared.signed_migration_port as module
from shared.adapters_fs import FilesystemPorts
from shared.manifest import load_manifest
from shared.signed_migration_port import SignedMigrationPort
from shared.update_plan import LockBusy, UpdateIndeterminate, UpdatePlan

sandbox, project, mode, boundary = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
manifest = load_manifest(project / 'examples/rent.manifest.json')
root = sandbox / 'installation'
class Effects:
    def migrate(self, *args):
        return self.port.migrate(*args)
effects = Effects()
ports = FilesystemPorts(manifest, root, work_dir=sandbox / 'work', payloads={}, effects=effects,
                        password_provider=lambda: 'synthetic')
plan = UpdatePlan(manifest, root, ports, release='2', modes=('autonomous',),
                  migration_ids=('schema-2',), guard_effects=True)
effects.port = SignedMigrationPort(plan, ports, install_root=str(root), package_path=str(sandbox / 'synthetic.zip'),
                                   envelope=b'synthetic-envelope', trusted_public_key=b'x' * 32,
                                   interpreter='/synthetic/python', interpreter_sha256='a' * 64, timeout=1)
module.archive_crypto.is_encrypted = lambda path: True
ports._verify_archive = lambda path: {'roots': list(ports.home.roots)}
def effect(**options):
    if options.get('execute'):
        with (sandbox / 'effect-count').open('ab') as stream:
            stream.write(b'executed\n')
            stream.flush()
            os.fsync(stream.fileno())
module.run_signed_native_migration = effect

if mode == 'crash':
    original = module._atomic_write
    def stop_at_receipt(path, value):
        if value.get('status') == 'done':
            if boundary == 'done':
                original(path, value)
            (sandbox / 'ready').write_text('ready')
            while True:
                time.sleep(1)
        original(path, value)
    module._atomic_write = stop_at_receipt
elif mode == 'retry':
    class Advanced(BaseException):
        pass
    original_save = plan._save
    def observe_advance(value):
        if value['index'] > plan.forward.index('migrate:0'):
            raise Advanced()
        original_save(value)
    plan._save = observe_advance

try:
    plan.run(resume_indeterminate=mode != 'check')
    print('unexpected-completion')
except UpdateIndeterminate:
    print('indeterminate')
except LockBusy:
    print('locked')
except BaseException as error:
    if mode == 'retry' and type(error).__name__ == 'Advanced':
        print('advanced')
    else:
        raise
'''


@pytest.mark.skipif(os.name != 'posix', reason='Проверка SIGKILL только на POSIX')
@pytest.mark.parametrize('boundary', ['pending', 'done'])
def test_sigkill_preserves_migration_receipt_and_prevents_duplicate(setup, tmp_path, boundary):
    plan, ports, port, _, _ = setup
    controller = tmp_path / 'controller.py'
    controller.write_text(_CRASH_CONTROLLER, encoding='utf-8')
    def command(mode):
        return [sys.executable, '-B', str(controller), str(tmp_path), str(ROOT), mode, boundary]
    process = subprocess.Popen(command('crash'), cwd=ROOT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / 'ready').exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail('Контроллер завершился до нужного этапа: ' + process.stderr.read().decode('utf-8', 'replace'))
            time.sleep(0.05)
        assert (tmp_path / 'ready').exists()
        concurrent = subprocess.run(command('check'), cwd=ROOT, capture_output=True, timeout=20, check=True)
        assert concurrent.stdout.strip() == b'locked'
        assert (tmp_path / 'effect-count').read_bytes() == b'executed\n'
    finally:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == -signal.SIGKILL, (stdout, stderr)
    assert (tmp_path / 'effect-count').read_bytes() == b'executed\n'
    assert plan.status()['indeterminate'] is True
    assert port._read(ports._path(KEY, '.migration.json'))['status'] == boundary
    check = subprocess.run(command('check'), cwd=ROOT, capture_output=True, timeout=20, check=True)
    assert check.stdout.strip() == b'indeterminate'
    retry = subprocess.run(command('retry'), cwd=ROOT, capture_output=True, timeout=20, check=True)
    assert retry.stdout.strip() == (b'indeterminate' if boundary == 'pending' else b'advanced')
    assert (tmp_path / 'effect-count').read_bytes() == b'executed\n'
