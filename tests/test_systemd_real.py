"""Необязательная проверка изолированной временной user-службы на Linux.

Запускается только с HOMENSAI_TEST_REAL_SYSTEMD=1; использует случайное имя,
не меняет unit-файлы пользователя и очищает только созданную временную службу.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from shared.manifest import load_manifest
from shared.systemd_user import SystemdUserAdapter, SystemdUserError

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Реальный пользовательский systemd проверяется только явно')
def test_transient_user_service_inspect_and_stop():
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    unit = manifest['product_id'] + '-web.service'
    uid = os.getuid()
    runtime = '/run/user/' + str(uid)
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
           'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    created = False
    try:
        result = subprocess.run(['/usr/bin/systemd-run', '--user', '--unit=' + unit,
                                 '--remain-after-exit', '--', '/usr/bin/sleep', '90'],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        assert result.returncode == 0, 'Не удалось запустить отдельную синтетическую user-службу'
        created = True
        result = subprocess.run(base + ['show', '--property=FragmentPath', '--value', '--', unit],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True)
        fragment = Path(result.stdout.decode().strip())
        digest = hashlib.sha256(fragment.read_bytes()).hexdigest()
        wrong = SystemdUserAdapter(manifest, modes=('autonomous',), component='web',
                                   fragment_path=str(fragment), fragment_sha256='0' * 64)
        with pytest.raises(SystemdUserError):
            wrong.command('stop', execute=True)
        assert subprocess.run(base + ['is-active', '--quiet', '--', unit], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15).returncode == 0
        service = SystemdUserAdapter(manifest, modes=('autonomous',), component='web',
                                     fragment_path=str(fragment), fragment_sha256=digest)
        assert service.inspect()['active_state'] == 'active'
        with tempfile.TemporaryDirectory(prefix='homensai-test-', dir=runtime) as directory:
            link = Path(directory) / 'hardlink'
            os.link(fragment, link)
            with pytest.raises(SystemdUserError):
                service.command('stop', execute=True)
            link.unlink()
        assert service.inspect()['active_state'] == 'active'
        assert service.command('stop', execute=True)['active_state'] == 'inactive'
    finally:
        if created:
            subprocess.run(base + ['stop', '--', unit], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            subprocess.run(base + ['reset-failed', '--', unit], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Реальный пользовательский systemd проверяется только явно')
def test_transient_user_service_can_stop_and_start():
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    unit = manifest['product_id'] + '-web.service'
    holder = manifest['product_id'] + '-holder.service'
    runtime = '/run/user/' + str(os.getuid())
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
           'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    created = []
    try:
        for name, extra in ((unit, []), (holder, ['--property=Wants=' + unit])):
            result = subprocess.run(['/usr/bin/systemd-run', '--user', '--unit=' + name,
                                     *extra, '--', '/usr/bin/sleep', '90'], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            assert result.returncode == 0, 'Не удалось запустить отдельную синтетическую user-службу'
            created.append(name)
        result = subprocess.run(base + ['show', '--property=FragmentPath', '--value', '--', unit],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True)
        fragment = Path(result.stdout.decode().strip())
        service = SystemdUserAdapter(manifest, modes=('autonomous',), component='web',
                                     fragment_path=str(fragment), fragment_sha256=hashlib.sha256(fragment.read_bytes()).hexdigest())
        assert service.command('stop', execute=True)['active_state'] == 'inactive'
        assert service.command('start', execute=True)['active_state'] == 'active'
    finally:
        for name in reversed(created):
            subprocess.run(base + ['stop', '--', name], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            subprocess.run(base + ['reset-failed', '--', name], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Реальный пользовательский systemd проверяется только явно')
def test_group_start_and_stop_with_real_user_manager():
    from shared.systemd_group import SystemdGroupError, SystemdUserGroup
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    names = ('web', 'bot', 'scheduler')
    units = [manifest['product_id'] + '-' + name + '.service' for name in names]
    holder = manifest['product_id'] + '-holder.service'
    runtime = '/run/user/' + str(os.getuid())
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
           'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    created = []
    try:
        for name in [*units, holder]:
            extra = ['--property=Wants=' + ' '.join(units)] if name == holder else []
            result = subprocess.run(['/usr/bin/systemd-run', '--user', '--unit=' + name,
                                     *extra, '--', '/usr/bin/sleep', '90'], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            assert result.returncode == 0, 'Не удалось запустить синтетическую user-службу'
            created.append(name)
        adapters = {}
        for name, unit in zip(names, units):
            result = subprocess.run(base + ['show', '--property=FragmentPath', '--value', '--', unit],
                                    env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True)
            fragment = Path(result.stdout.decode().strip())
            adapters[name] = SystemdUserAdapter(manifest, modes=('autonomous',), component=name,
                                                fragment_path=str(fragment),
                                                fragment_sha256=hashlib.sha256(fragment.read_bytes()).hexdigest())
        group = SystemdUserGroup(manifest, modes=('autonomous',), adapters=adapters)
        bad = dict(adapters)
        bot = adapters['bot']
        bad['bot'] = SystemdUserAdapter(manifest, modes=('autonomous',), component='bot',
                                        fragment_path=str(bot.path), fragment_sha256='0' * 64)
        with pytest.raises(SystemdGroupError):
            SystemdUserGroup(manifest, modes=('autonomous',), adapters=bad).command('stop', execute=True)
        for unit in units:
            result = subprocess.run(base + ['is-active', '--quiet', '--', unit], env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=15)
            assert result.returncode == 0
        assert [x['active_state'] for x in group.command('stop', execute=True)] == ['inactive'] * 3
        assert [x['active_state'] for x in group.command('start', execute=True)] == ['active'] * 3
    finally:
        for name in reversed(created):
            subprocess.run(base + ['stop', '--', name], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            subprocess.run(base + ['reset-failed', '--', name], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Реальный пользовательский systemd проверяется только явно')
def test_demo_product_runs_under_real_user_manager(tmp_path):
    """Проверка тестового продукта, не регистрация настоящих служб установщиком."""
    from shared.systemd_group import SystemdUserGroup

    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    manifest['components'] = [c for c in manifest['components'] if c['name'] != 'scheduler']
    manifest['modes'][0]['components'] = ['web', 'bot']
    units = [manifest['product_id'] + '-' + name + '.service' for name in ('web', 'bot')]
    holder = manifest['product_id'] + '-holder.service'
    runtime = '/run/user/' + str(os.getuid())
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
           'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    data, log = tmp_path / 'data', tmp_path / 'web.log'
    script = ROOT / 'examples/demo_product/main.py'
    created = []

    def wait_ready(old_pid=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                heartbeat = json.loads((data / 'worker-heartbeat.json').read_text())
                port = json.loads(log.read_text().splitlines()[-1])['port']
                if heartbeat['release'] != '1' or heartbeat['pid'] == old_pid:
                    time.sleep(0.05)
                    continue
                code = ("import sys; from shared.health import check_local_health; "
                        "check_local_health(int(sys.argv[1]),product_id='homensai-test-demo',version='1')")
                check = subprocess.run([sys.executable, '-B', '-c', code, str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if check.returncode == 0:
                    return heartbeat['pid']
            except (OSError, ValueError, IndexError, KeyError):
                pass
            time.sleep(0.05)
        pytest.fail('Тестовый продукт не подтвердил здоровье и heartbeat')

    try:
        # RuntimeMaxSec ограничивает жизнь тестовых служб даже при аварии теста.
        shared = ['--property=RuntimeMaxSec=60s']
        for name in ('PYTHONHOME', 'LD_LIBRARY_PATH'):
            if name in os.environ:
                shared.append('--setenv=' + name + '=' + os.environ[name])
        commands = [
            (units[0], ['--property=StandardOutput=file:' + str(log)],
             [sys.executable, '-B', str(script), 'serve', '--data-dir', str(data), '--port', '0']),
            (units[1], [], [sys.executable, '-B', str(script), 'worker', '--data-dir', str(data)]),
            (holder, ['--property=Wants=' + ' '.join(units)], ['/usr/bin/sleep', '60']),
        ]
        for unit, extra, command in commands:
            result = subprocess.run(['/usr/bin/systemd-run', '--user', '--unit=' + unit,
                                     *shared, *extra, '--', *command], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            assert result.returncode == 0, 'Не удалось запустить временную тестовую службу'
            created.append(unit)
        adapters = {}
        for name, unit in zip(('web', 'bot'), units):
            result = subprocess.run(base + ['show', '--property=FragmentPath', '--value', '--', unit],
                                    env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True)
            fragment = Path(result.stdout.decode().strip())
            adapters[name] = SystemdUserAdapter(manifest, modes=('autonomous',), component=name,
                                                fragment_path=str(fragment),
                                                fragment_sha256=hashlib.sha256(fragment.read_bytes()).hexdigest())
        group = SystemdUserGroup(manifest, modes=('autonomous',), adapters=adapters)
        first_pid = wait_ready()
        assert [s['active_state'] for s in group.command('stop', execute=True)] == ['inactive'] * 2
        assert [s['active_state'] for s in group.command('start', execute=True)] == ['active'] * 2
        assert wait_ready(first_pid) != first_pid
    finally:
        for name in reversed(created):
            subprocess.run(base + ['stop', '--', name], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            subprocess.run(base + ['reset-failed', '--', name], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
