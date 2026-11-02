"""Необязательная проверка изолированной временной user-службы на Linux.

Запускается только с HOMENSAI_TEST_REAL_SYSTEMD=1; использует случайное имя,
не меняет unit-файлы пользователя и очищает только созданную временную службу.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
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
