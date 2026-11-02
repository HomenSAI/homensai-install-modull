"""Транзакция с синтетической SQLite и тестовыми адаптерами исполнения.

Opt-in сценарий использует настоящие временные user-systemd службы; порты
хуков и миграций тестовые, crash-resume не проверяется. Это не приёмка.
"""

from hashlib import sha256
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import time
import uuid
import zipfile

import pytest

from shared.adapters_fs import FilesystemPorts, ReleaseSource
from shared import archive_crypto
from shared.datahome import resolve_datahome
from shared.manifest import load_manifest, validate_manifest
from shared.update_plan import EffectIndeterminate, UpdateError, UpdateIndeterminate, UpdatePlan, operation_lock

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples/demo_product"
_SOCKET = socket.socket


def invoke(executable, data, *args):
    return subprocess.run([sys.executable, "-B", str(executable), "migrate",
                           "--data-dir", str(data), *args],
                          capture_output=True, text=True, timeout=15, check=True)


def entries(db):
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT text FROM entries ORDER BY id")]


class Effects:
    def __init__(self, home, *, fail_health, corrupt_archive=False):
        self.home = home
        self.fail_health = fail_health
        self.corrupt_archive = corrupt_archive
        self.ports = None
        self.calls = []
        self.signed_details = None
        self.migration_port = None

    def stop(self, key, components):
        self.calls.append(key.split("/")[-1])

    def migrate(self, key, migration, command):
        from shared.native_migration import run_python_migration, run_signed_native_migration
        self.calls.append("migrate")
        if self.migration_port is not None:
            return self.migration_port.migrate(key, migration, command)
        interpreter = Path(sys.executable).resolve()
        release = self.home.releases / 'app-2'
        if self.signed_details is not None:
            package, envelope, public, product_id = self.signed_details
            run_signed_native_migration(package_path=package, envelope=envelope, trusted_public_key=public,
                                        expected_product_id=product_id, expected_version='2',
                                        expected_manifest=self.ports.manifest, install_root=str(self.installation),
                                        migration_id=migration,
                                        release_root=str(release), interpreter=str(interpreter),
                                        interpreter_sha256=sha256(interpreter.read_bytes()).hexdigest(),
                                        timeout=15, execute=True)
            return
        run_python_migration(interpreter=str(interpreter), interpreter_sha256=sha256(interpreter.read_bytes()).hexdigest(),
                             release_root=str(release), script=str(release / 'main.py'),
                             script_sha256=self.ports.payloads['2'].sha256['main.py'],
                             args=('migrate', '--data-dir', str(self.home.roots['data']), '--migration', migration),
                             timeout=15, execute=True)

    def start(self, key, components, pre_start):
        self.calls.append(key.split("/")[-1])

    def health(self, key, url, timeout, checks):
        phase = key.split("/")[-1]
        self.calls.append(phase)
        if phase == "health" and self.fail_health:
            if self.corrupt_archive:
                path = self.ports.archive_path(key.split('/')[0] + '/archive')
                ciphertext = bytearray(path.read_bytes())
                ciphertext[-1] ^= 1
                path.write_bytes(ciphertext)
            raise RuntimeError("synthetic health failure")

    def post_update(self, key, hooks):
        self.calls.append("post_update")


class LiveEffects(Effects):
    """Только тестовый адаптер: управляет дочерними процессами этого теста."""
    def __init__(self, home, tmp_path, *, fail_health):
        super().__init__(home, fail_health=fail_health)
        self.tmp_path = tmp_path
        self.processes = []
        self.log = None
        self.port = None
        self.release = None
        self.manifest_port = 0
        self.health_port = None

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                if self.fail_health == 'stop':
                    process.kill()
                else:
                    process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)
        self.processes.clear()
        if self.log is not None:
            self.log.close()
            self.log = None

    def stop(self, key, components):
        super().stop(key, components)
        if self.fail_health == 'stop':
            for process in self.processes:
                process.terminate()
            try:
                for process in self.processes:
                    process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                raise EffectIndeterminate('Синтетический писатель не остановлен') from None
        self.close()

    def start(self, key, components, pre_start):
        super().start(key, components, pre_start)
        if self.processes:
            raise RuntimeError('test processes are already running')
        self.release = self.ports.current_release()
        executable = self.ports.release_path(self.release) / 'main.py'
        data = str(self.home.roots['data'])
        self.log = (self.tmp_path / 'synthetic-service.log').open('w+', encoding='utf-8')
        failure = ('stop' if self.fail_health == 'stop' else
                   'health' if self.fail_health and key.endswith('/start') else 'none')
        server = subprocess.Popen([sys.executable, '-B', str(executable), 'serve', '--data-dir', data,
                                   '--port', str(self.manifest_port), '--fail', failure], stdout=self.log, stderr=subprocess.DEVNULL)
        self.processes.append(server)
        worker = subprocess.Popen([sys.executable, '-B', str(executable), 'worker', '--data-dir', data,
                                   '--fail', 'stop' if failure == 'stop' else 'none'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(worker)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.log.seek(0)
            line = self.log.readline()
            if line:
                self.port = json.loads(line)['port']
                return
            if any(process.poll() is not None for process in self.processes):
                raise RuntimeError('test process exited during start')
            time.sleep(0.05)
        raise RuntimeError('test server did not report its port')

    def health(self, key, url, timeout, checks):
        self.calls.append(key.split('/')[-1])
        if self.health_port is not None:
            self.health_port.health(key, url, timeout, checks)
        code = ("import sys; from shared.health import check_local_health; "
                "check_local_health(int(sys.argv[1]),product_id='homensai-test-demo',version=sys.argv[2])")
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.port), self.release],
                                capture_output=True, timeout=8)
        if result.returncode != 0:
            raise RuntimeError('test health check failed')
        heartbeat = self.home.roots['data'] / 'worker-heartbeat.json'
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if heartbeat.exists():
                value = json.loads(heartbeat.read_text(encoding='utf-8'))
                if value['pid'] == self.processes[1].pid and value['release'] == self.release:
                    return
            if any(process.poll() is not None for process in self.processes):
                raise RuntimeError('test process exited during health check')
            time.sleep(0.05)
        raise RuntimeError('test worker did not report heartbeat')


class SystemdEffects(Effects):
    """Тестовый мост: не реализует durable-идемпотентность производственных портов."""
    def __init__(self, home, tmp_path, *, fail_health, manifest):
        super().__init__(home, fail_health=fail_health)
        self.tmp_path, self.manifest = tmp_path, manifest
        self.log = tmp_path / 'systemd-web.log'
        self.created = []
        self.group = None
        self.lifecycle_port = None
        self.composite = None
        runtime = '/run/user/' + str(os.getuid())
        self.env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
                    'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
        self.base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']

    def launch(self):
        from shared.systemd_group import SystemdUserGroup
        from shared.systemd_user import SystemdUserAdapter
        launcher = self.tmp_path / 'test-launcher.py'
        launcher.write_text(
            'import json, os, pathlib, sys\n'
            'root, pointer, data, action = sys.argv[1:]\n'
            'version = json.loads(pathlib.Path(pointer).read_text())["release"]\n'
            'script = pathlib.Path(root) / ("app-" + version) / "main.py"\n'
            'args = [sys.executable, "-B", str(script), action, "--data-dir", data]\n'
            f'if action == "serve": args += ["--port", "{self.manifest["web"]["port"]}"]\n'
            'os.execv(sys.executable, args)\n', encoding='utf-8')
        units = [self.manifest['product_id'] + '-' + n + '.service' for n in ('web', 'bot')]
        holder = self.manifest['product_id'] + '-holder.service'
        extra_env = ['--setenv=' + name + '=' + os.environ[name]
                     for name in ('PYTHONHOME', 'LD_LIBRARY_PATH') if name in os.environ]
        for unit, action in zip([*units, holder], ['serve', 'worker', 'holder']):
            extra = (['--property=StandardOutput=file:' + str(self.log)] if action == 'serve' else
                     ['--property=Wants=' + ' '.join(units)] if action == 'holder' else [])
            command = (['/usr/bin/sleep', '90'] if action == 'holder' else
                       [sys.executable, '-B', str(launcher), str(self.home.releases),
                        str(self.ports.pointer), str(self.home.roots['data']), action])
            result = subprocess.run(['/usr/bin/systemd-run', '--user', '--unit=' + unit,
                                     '--property=RuntimeMaxSec=90s', *extra_env, *extra, '--', *command],
                                    env=self.env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            if result.returncode:
                raise RuntimeError('Не удалось создать тестовую службу')
            self.created.append(unit)
        adapters = {}
        for name, unit in zip(('web', 'bot'), units):
            fragment = Path(subprocess.run(self.base + ['show', '--property=FragmentPath', '--value', '--', unit],
                                           env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                                           timeout=15, check=True).stdout.decode().strip())
            adapters[name] = SystemdUserAdapter(self.manifest, modes=('autonomous',), component=name,
                                                fragment_path=str(fragment),
                                                fragment_sha256=sha256(fragment.read_bytes()).hexdigest())
        self.group = SystemdUserGroup(self.manifest, modes=('autonomous',), adapters=adapters)

    def close(self):
        for unit in reversed(self.created):
            for action in ('stop', 'reset-failed'):
                subprocess.run(self.base + [action, '--', unit], env=self.env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)

    def stop(self, key, components):
        from shared.systemd_group import SystemdGroupError
        super().stop(key, components)
        assert components == ('bot', 'web')
        if self.lifecycle_port is not None:
            try:
                return (self.composite or self.lifecycle_port).stop(key, components)
            except EffectIndeterminate:
                for name, adapter in self.group.adapters.items():
                    result = subprocess.run(self.base + ['show', '--property=Id,LoadState,ActiveState,SubState,FragmentPath,DropInPaths,NeedDaemonReload', '--', adapter.unit],
                                            env=self.env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
                    print('synthetic systemd diagnostic', name, result.stdout.decode(errors='replace'))
                    try:
                        print('synthetic adapter inspection', adapter.inspect())
                    except Exception as error:
                        print('synthetic adapter error', type(error).__name__, str(error))
                    try:
                        from shared.systemd_user import _trusted_file
                        _trusted_file(adapter.path, adapter.digest)
                        print('synthetic unit file trusted')
                    except Exception as error:
                        print('synthetic unit file error', type(error).__name__, str(error))
                        for parent in reversed(adapter.path.parents):
                            info = parent.lstat()
                            print('synthetic unit parent', parent, info.st_uid, oct(info.st_mode & 0o7777))
                raise
        try:
            self.group.command('stop', execute=True)
        except SystemdGroupError:
            raise EffectIndeterminate() from None

    def start(self, key, components, pre_start):
        from shared.systemd_group import SystemdGroupError
        super().start(key, components, pre_start)
        assert components == ('web', 'bot')
        self.log.write_text('', encoding='utf-8')
        if self.lifecycle_port is not None:
            return (self.composite or self.lifecycle_port).start(key, components, pre_start)
        try:
            self.group.command('start', execute=True)
        except SystemdGroupError:
            raise EffectIndeterminate() from None

    def health(self, key, url, timeout, checks):
        phase = key.split('/')[-1]
        self.calls.append(phase)
        if phase == 'health' and self.fail_health is True:
            raise RuntimeError('synthetic health failure')
        if self.composite is not None:
            self.composite.health(key, url, timeout, checks)
        release = self.ports.current_release()
        deadline = time.monotonic() + min(timeout, 10)
        while time.monotonic() < deadline:
            try:
                port = json.loads(self.log.read_text().splitlines()[-1])['port']
                heartbeat = json.loads((self.home.roots['data'] / 'worker-heartbeat.json').read_text())
                pid = int(subprocess.run(self.base + ['show', '--property=MainPID', '--value', '--',
                                                    self.manifest['product_id'] + '-bot.service'],
                                         env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                                         timeout=5, check=True).stdout)
                if heartbeat['release'] != release or heartbeat['pid'] != pid or pid == 0:
                    time.sleep(0.05)
                    continue
                code = ("import sys; from shared.health import check_local_health; "
                        "check_local_health(int(sys.argv[1]),product_id=sys.argv[3],version=sys.argv[2])")
                result = subprocess.run([sys.executable, '-B', '-c', code, str(port), release, self.manifest['product_id']],
                                        capture_output=True, timeout=5)
                if result.returncode == 0:
                    self.release = release
                    return
            except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
                pass
            time.sleep(0.05)
        raise RuntimeError('test systemd product health failure')

    def migrate(self, key, migration, command):
        if self.composite is None:
            return super().migrate(key, migration, command)
        self.calls.append('migrate')
        return self.composite.migrate(key, migration, command)

    def post_update(self, key, hooks):
        super().post_update(key, hooks)
        if self.composite is not None:
            return self.composite.post_update(key, hooks)

    def guarded_backup(self, key, purpose, command, backup):
        if self.composite is not None:
            return self.composite.guarded_backup(key, purpose, command, backup)
        return backup(key, purpose, command)

    def guarded_stage_release(self, key, release, stage):
        if self.composite is not None:
            return self.composite.guarded_stage_release(key, release, stage)
        return stage(key, release)

    def guarded_switch_pointer(self, key, release, switch):
        if self.composite is not None:
            return self.composite.guarded_switch_pointer(key, release, switch)
        return switch(key, release)


@pytest.mark.parametrize('action', ['start', 'stop'])
def test_systemd_group_failure_has_indeterminate_effect(tmp_path, action):
    from shared.systemd_group import SystemdGroupError

    class FailingGroup:
        def command(self, *args, **kwargs):
            raise SystemdGroupError('synthetic sensitive output')

    effects = SystemdEffects(None, tmp_path, fail_health=False, manifest=None)
    effects.group = FailingGroup()
    with pytest.raises(EffectIndeterminate) as error:
        if action == 'start':
            effects.start('a' * 32 + '/start', ('web', 'bot'), ())
        else:
            effects.stop('a' * 32 + '/stop', ('bot', 'web'))
    assert 'sensitive' not in str(error.value)


@pytest.mark.parametrize("signed_release,encrypted,fail_health,corrupt,live", [
    (False, False, False, False, False), (False, False, True, False, False),
    (True, False, False, False, False), (True, False, True, False, False),
    (True, True, False, False, False), (True, True, True, False, False), (True, True, True, True, False),
    (True, True, False, False, True), (True, True, True, False, True),
    pytest.param(True, True, 'stop', False, True, marks=pytest.mark.skipif(
        os.name != 'posix', reason='Сигнал SIGTERM проверяется на POSIX')),
    pytest.param(True, True, False, False, 'systemd', marks=pytest.mark.skipif(
        os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix', reason='opt-in systemd')),
    pytest.param(True, True, True, False, 'systemd', marks=pytest.mark.skipif(
        os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix', reason='opt-in systemd')),
    pytest.param(True, True, 'migration', False, 'systemd', marks=pytest.mark.skipif(
        os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix', reason='opt-in systemd')),
    pytest.param(True, True, 'stop-crash', False, 'systemd', marks=pytest.mark.skipif(
        os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix', reason='opt-in systemd')),
])
def test_real_demo_migration_update_and_rollback(tmp_path, request, monkeypatch, signed_release, encrypted, fail_health, corrupt, live):
    source = tmp_path / "source"
    subprocess.run([sys.executable, "-B", str(DEMO / "build_releases.py"), str(source)],
                   capture_output=True, text=True, timeout=15, check=True)
    systemd_product = 'homensai-test-' + uuid.uuid4().hex[:16] if live == 'systemd' else None
    if systemd_product:
        for version in ('1', '2'):
            script = source / ('app-' + version) / 'main.py'
            text = script.read_text()
            assert text.count('homensai-test-demo') == 1
            script.write_text(text.replace('homensai-test-demo', systemd_product))
    synthetic = {"locks/windows.lock": b"synthetic windows lock",
                 "locks/linux.lock": b"synthetic linux lock",
                 "vendor/demo-0-py3-none-any.whl": b"synthetic wheel placeholder"}
    payloads = {}
    for version in ("1", "2"):
        path = source / ("app-" + version)
        for name, data in synthetic.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        hashes = {p.relative_to(path).as_posix(): sha256(p.read_bytes()).hexdigest()
                  for p in path.rglob("*") if p.is_file()}
        payloads[version] = ReleaseSource(path, hashes)

    manifest = load_manifest(ROOT / "examples/rent.manifest.json")
    manifest["product_name"] = "Синтетический тест обновления"
    manifest["product_id"] = "homensai-test-demo"
    if live is True or live == 'systemd':
        with _SOCKET() as probe:
            probe.bind(('127.0.0.1', 0))
            manifest['web']['port'] = probe.getsockname()[1]
        def loopback_only(address, timeout, source_address=None):
            if address != ('127.0.0.1', manifest['web']['port']) or source_address is not None:
                raise AssertionError('Тест допускает только loopback тестового продукта')
            connection = _SOCKET(socket.AF_INET, socket.SOCK_STREAM)
            try:
                connection.settimeout(timeout)
                connection.connect(address)
                return connection
            except BaseException:
                connection.close()
                raise
        monkeypatch.setattr(socket, 'create_connection', loopback_only)
        manifest['web']['healthz'] = '/health'
        manifest['update']['health_wait'] = 10
        manifest['hooks']['health_checks'] = []
    if live == 'systemd':
        manifest['product_id'] = systemd_product
        manifest['components'] = [c for c in manifest['components'] if c['name'] != 'scheduler']
        manifest['modes'][0]['components'] = ['web', 'bot']
        manifest['hooks']['pre_start'] = []
        manifest['hooks']['post_update'] = []
    manifest["runtime"]["lock_files"] = {
        name: {"file": "locks/" + name + ".lock", "sha256": sha256(synthetic["locks/" + name + ".lock"]).hexdigest()}
        for name in ("windows", "linux")}
    wheel = "vendor/demo-0-py3-none-any.whl"
    manifest["runtime"]["vendor_wheels"] = [{"file": wheel, "sha256": sha256(synthetic[wheel]).hexdigest()}]
    if signed_release:
        manifest['migrations']['cmd'] = ['python', 'main.py', 'migrate', '--data-dir', '@DATA_DIR@',
                                         '--migration', '@MIGRATION_ID@']
        if fail_health == 'migration':
            manifest['migrations']['cmd'].extend(['--fail', 'during'])
        from shared.host_target import HostTargetError, native_target
        try:
            manifest["compatibility"] = {"targets": [native_target()]}
        except HostTargetError:
            pytest.skip("Среда процесса не входит в нативный профиль")
    validate_manifest(manifest)
    if signed_release:
        ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
        ser = pytest.importorskip("cryptography.hazmat.primitives.serialization")
        from shared.package import build_package
        from shared.package_prepare import prepare_native_signed_package
        from shared.package_signing import sign_package
        private = ed.Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
        package = tmp_path / "release-2.zip"
        build_package(manifest, "2", source / "app-2", package)
        envelope = sign_package(package, private_key=private,
                                expected_product_id=manifest["product_id"], expected_version="2")
        prepared = tmp_path / "verified-release-2"
        receipt = prepare_native_signed_package(package, envelope, prepared, trusted_public_key=public,
                                                expected_product_id=manifest["product_id"], expected_version="2")
        assert receipt["process_target"] == manifest["compatibility"]["targets"][0]
        payloads["2"] = ReleaseSource(prepared, receipt["files"])
    installation = tmp_path / "installation"
    if signed_release:
        from shared.fresh_prepare import prepare_fresh_native
        from shared.preflight import NAMES
        package_one = tmp_path / "release-1.zip"
        build_package(manifest, "1", source / "app-1", package_one)
        envelope_one = sign_package(package_one, private_key=private,
                                    expected_product_id=manifest["product_id"], expected_version="1")
        fresh = prepare_fresh_native(package_one, envelope_one, installation,
                                     trusted_public_key=public, expected_product_id=manifest["product_id"],
                                     expected_version="1", required_bytes={name: 0 for name in NAMES},
                                     reserve_bytes=0)
        assert fresh['status'] == 'prepared'
    home = resolve_datahome(manifest, installation)
    for directory in (home.app, home.releases, *home.roots.values()):
        directory.mkdir(parents=True, exist_ok=True)
    effects = (SystemdEffects(home, tmp_path, fail_health=fail_health, manifest=manifest) if live == 'systemd'
               else LiveEffects(home, tmp_path, fail_health=fail_health) if live
               else Effects(home, fail_health=fail_health, corrupt_archive=corrupt))
    if signed_release:
        effects.signed_details = (str(package), envelope, public, manifest['product_id'])
        effects.installation = installation
    if live:
        request.addfinalizer(effects.close)
    password = "synthetic-test-password-not-a-secret"
    password_provider = (lambda: password) if encrypted else None
    ports = FilesystemPorts(manifest, installation, work_dir=tmp_path / "work",
                            payloads=payloads, effects=effects, allow_unencrypted=not encrypted,
                            password_provider=password_provider)
    effects.ports = ports
    if live is True:
        effects.manifest_port = manifest['web']['port']
    if not signed_release:
        with operation_lock(home.roots["services"] / "operation.lock"):
            ports.stage_release("0" * 32 + "/stage", "1")
            ports.switch_pointer("0" * 32 + "/switch", "1")
    invoke(ports.release_path("1") / "main.py", home.roots["data"])
    settings = home.roots["data"] / "settings.json"
    settings.write_text('{"display_name":"Example","locale":"de"}\n', encoding="utf-8")
    if live == 'systemd':
        effects.launch()
        effects.health('0' * 32 + '/initial_health', '', 10, ())
    elif live:
        effects.start('0' * 32 + '/initial_start', (), ())
        effects.health('0' * 32 + '/initial_health', '', 10, ())

    update = UpdatePlan(manifest, installation, ports, release="2", modes=("autonomous" if live == 'systemd' else "platform",),
                        migration_ids=("schema-2",), guard_effects=signed_release)
    if live is True:
        from shared.update_health_port import LocalUpdateHealthPort
        effects.health_port = LocalUpdateHealthPort(update, ports)
    if signed_release and encrypted:
        from shared.signed_migration_port import SignedMigrationPort
        interpreter = Path(sys.executable).resolve()
        effects.migration_port = SignedMigrationPort(update, ports, install_root=str(installation),
                                                     package_path=str(package), envelope=envelope,
                                                     trusted_public_key=public, interpreter=str(interpreter),
                                                     interpreter_sha256=sha256(interpreter.read_bytes()).hexdigest(),
                                                     timeout=15)
    if live == 'systemd':
        from shared.systemd_lifecycle_port import SystemdLifecyclePort
        from shared.update_health_port import LocalUpdateHealthPort
        from shared.linux_update_effects import LinuxSignedUpdateEffects
        effects.lifecycle_port = SystemdLifecyclePort(update, ports, install_root=str(installation), group=effects.group)
        effects.composite = LinuxSignedUpdateEffects(effects.lifecycle_port, effects.migration_port,
                                                   LocalUpdateHealthPort(update, ports))
    if fail_health == 'stop':
        with pytest.raises(UpdateIndeterminate):
            update.run()
        state = update.status()
        assert state['direction'] == 'forward' and state['indeterminate'] is True
        assert update.forward[state['index']] == 'stop' and state['archive_ready'] is False
        assert all(process.poll() is None for process in effects.processes)
        assert not ports.archive_path(state['transaction'] + '/archive').exists()
        assert 'migrate' not in effects.calls and ports.current_release() == '1'
        assert entries(home.roots['data'] / 'demo.sqlite3') == ['synthetic-schema-1']
        return
    if fail_health == 'stop-crash':
        import shared.systemd_lifecycle_port as lifecycle
        from shared.effect_receipt import read_effect_receipt
        class Crash(BaseException):
            pass
        original_write, original_command = lifecycle._atomic_write, effects.group.command
        commands = []
        def trace_command(action, **kwargs):
            commands.append(action)
            return original_command(action, **kwargs)
        def crash_before_done(path, value):
            if value.get('status') == 'done' and value.get('key', '').endswith('/stop'):
                raise Crash()
            return original_write(path, value)
        monkeypatch.setattr(effects.group, 'command', trace_command)
        with monkeypatch.context() as patch:
            patch.setattr(lifecycle, '_atomic_write', crash_before_done)
            with pytest.raises(Crash):
                update.run()
        state = update.status()
        key = state['transaction'] + '/stop'
        assert state['indeterminate'] is True and update.forward[state['index']] == 'stop'
        assert read_effect_receipt(ports._path(key, '.service.json'))['status'] == 'pending'
        assert all(adapter.confirm_stopped()['active_state'] == 'inactive'
                   for adapter in effects.group.adapters.values())
        effects.lifecycle_port = SystemdLifecyclePort(update, ports, install_root=str(installation),
                                                      group=effects.group, reconcile_key=key)
        effects.composite.lifecycle = effects.lifecycle_port
        result = update.run(resume_indeterminate=True)
        assert result['outcome'] == 'updated' and ports.current_release() == '2', result
        assert read_effect_receipt(ports._path(key, '.service.json'))['status'] == 'done'
        assert commands.count('stop') == 1 and commands.count('start') == 1
        assert entries(home.roots['data'] / 'demo.sqlite3') == ['synthetic-schema-1', 'synthetic-schema-2']
        return
    if fail_health == 'migration':
        from shared.api import handle
        from shared.effect_receipt import read_effect_receipt
        with pytest.raises(UpdateIndeterminate):
            update.run()
        state = update.status()
        assert state['direction'] == 'forward' and state['indeterminate'] is True
        assert update.forward[state['index']] == 'migrate:0'
        assert read_effect_receipt(ports._path(state['transaction'] + '/stop', '.service.json'))['status'] == 'done'
        assert read_effect_receipt(ports._path(state['transaction'] + '/migrate:0', '.migration.json'))['status'] == 'pending'
        assert ports.current_release() == '1'
        assert entries(home.roots['data'] / 'demo.sqlite3') == ['synthetic-schema-1']
        assert all(adapter.confirm_stopped()['active_state'] == 'inactive'
                   for adapter in effects.group.adapters.values())
        backup = ports.archive_path(state['transaction'] + '/backup:0')
        assert archive_crypto.is_encrypted(backup)
        assert archive_crypto.verify_archive(backup, password_provider=password_provider,
                                             expected_product_id=manifest['product_id'])['product_id'] == manifest['product_id']
        response = handle({'api_major': 1, 'request_id': 'synthetic', 'operation': 'update-plan-status',
                           'params': {'manifest': manifest, 'install_root': str(installation), 'release': '2',
                                      'modes': ['autonomous'], 'migration_ids': ['schema-2'], 'guard_effects': True}})
        assert response['ok'] is True and response['result']['state'] == state
        with pytest.raises(UpdateIndeterminate):
            update.run()
        assert ports.current_release() == '1' and 'rollback_health' not in effects.calls
        assert effects.calls.count('migrate') == 1
        calls = []
        monkeypatch.setattr('shared.signed_migration_port.run_signed_native_migration',
                            lambda *args, **kwargs: calls.append(kwargs))
        with pytest.raises(UpdateIndeterminate):
            update.run(resume_indeterminate=True)
        assert calls == [] and entries(home.roots['data'] / 'demo.sqlite3') == ['synthetic-schema-1']
        return
    if corrupt:
        with pytest.raises(UpdateError):
            update.run()
        # Повреждённый архив не должен частично восстановить или удалить данные.
        assert ports.current_release() == "2"
        assert entries(home.roots["data"] / "demo.sqlite3") == ["synthetic-schema-1", "synthetic-schema-2"]
        assert json.loads(settings.read_text(encoding="utf-8"))["locale"] == "de"
        assert 'rollback_health' not in effects.calls
        return
    result = update.run()
    assert result["outcome"] == ("rolled_back" if fail_health else "updated"), result
    assert ports.current_release() == ("1" if fail_health else "2")
    if live:
        assert effects.release == ('1' if fail_health else '2')
        if live != 'systemd':
            assert all(process.poll() is None for process in effects.processes)
        assert 'stop' in effects.calls and ('rollback_health' in effects.calls) == fail_health
    if live == 'systemd':
        from shared.effect_receipt import read_effect_receipt
        phases = ['stop', 'start'] + (['rollback_stop', 'rollback_start'] if fail_health else [])
        assert all(read_effect_receipt(ports._path(result['transaction'] + '/' + phase, '.service.json'))['status'] == 'done'
                   for phase in phases)
    expected = ["synthetic-schema-1"] + ([] if fail_health else ["synthetic-schema-2"])
    assert entries(home.roots["data"] / "demo.sqlite3") == expected
    assert json.loads(settings.read_text(encoding="utf-8"))["locale"] == "de"

    @contextmanager
    def read_snapshot(path):
        if encrypted:
            assert archive_crypto.is_encrypted(path)
            assert password.encode() not in path.read_bytes()
            assert b'synthetic-schema-1' not in path.read_bytes()
            with archive_crypto.decrypted_archive(path, password_provider=password_provider,
                                                   expected_product_id=manifest['product_id']) as (plain, _):
                with zipfile.ZipFile(plain) as archive:
                    yield archive
        else:
            with zipfile.ZipFile(path) as archive:
                yield archive

    before_migration = ports.archive_path(result["transaction"] + "/backup:0")
    with read_snapshot(before_migration) as archive:
        original_db = tmp_path / "before-migration.sqlite3"
        original_db.write_bytes(archive.read("roots/data/demo.sqlite3"))
    assert entries(original_db) == ["synthetic-schema-1"]
    if fail_health:
        snapshot = ports.archive_path(result["transaction"] + "/before_rollback")
        with read_snapshot(snapshot) as archive:
            migrated_db = tmp_path / "before-rollback.sqlite3"
            migrated_db.write_bytes(archive.read("roots/data/demo.sqlite3"))
        assert entries(migrated_db) == ["synthetic-schema-1", "synthetic-schema-2"]
        assert "rollback_health" in effects.calls
    if encrypted:
        assert archive_crypto.is_encrypted(ports.archive_path(result["transaction"] + "/archive"))
        assert not list((tmp_path / 'work').glob('*.zip'))
        assert not list((tmp_path / 'work').glob('.archive-crypto-*'))
        assert not list((tmp_path / 'work').glob('.archive-build-*'))
        assert not list((tmp_path / 'work').glob('.restore-*'))
