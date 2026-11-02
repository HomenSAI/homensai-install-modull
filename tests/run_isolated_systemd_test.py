#!/usr/bin/env python3
"""Явно запустить экспериментальный тест в одноразовом rootless Podman.

Сценарий скачивает тестовые зависимости и системные пакеты; это не окружение
выпускной сборки. Код проекта монтируется только для чтения. Требуется --run.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'python:3.14-slim-bookworm'
SETUP = '''#!/bin/sh
set -eu
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends systemd dbus >/dev/null
python -m pip install -q pytest jsonschema
python -m pip install -q -r /project/requirements-crypto.txt
useradd -m -u 1000 homenstest
mkdir -p /run/systemd/system /run/user/1000 /sys/fs/cgroup/homensai-test /home/homenstest/.config/systemd/user
chmod 0755 /run /run/user
chmod 0700 /run/user/1000
chown -R homenstest:homenstest /run/user/1000 /home/homenstest
chown homenstest:homenstest /sys/fs/cgroup/homensai-test /sys/fs/cgroup/homensai-test/cgroup.procs /sys/fs/cgroup/homensai-test/cgroup.threads /sys/fs/cgroup/homensai-test/cgroup.subtree_control
sh -c 'echo $$ > /sys/fs/cgroup/homensai-test/cgroup.procs; exec runuser -u homenstest -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus HOMENSAI_TEST_REAL_SYSTEMD=1 HOMENSAI_TEST_ISOLATED_SYSTEMD=1 PYTHONDONTWRITEBYTECODE=1 sh /harness/session.sh'
'''
SESSION = '''#!/bin/sh
set -eu
dbus-daemon --session --address=unix:path=/run/user/1000/bus --fork --print-pid > /tmp/homensai-test-bus.pid
/usr/lib/systemd/systemd --user > /tmp/homensai-test-systemd.log 2>&1 &
manager=$!
cleanup() { systemctl --user exit >/dev/null 2>&1 || :; kill "$manager" 2>/dev/null || :; wait "$manager" 2>/dev/null || :; }
trap cleanup EXIT
ready=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if systemctl --user show --property=Version --value >/dev/null 2>&1; then ready=1; break; fi
    if ! kill -0 "$manager" 2>/dev/null; then break; fi
    sleep 1
done
if [ "$ready" != 1 ]; then cat /tmp/homensai-test-systemd.log; exit 1; fi
cd /project
python -m pytest -q -p no:cacheprovider tests/test_isolated_signed_demo.py tests/test_services.py tests/test_linux_sh.py tests/test_runtime_native.py tests/test_systemd_group.py tests/test_systemd_portable.py tests/test_systemd_user_publish.py tests/test_api.py tests/test_api_signed_preflight.py tests/test_i18n.py
'''


def main() -> int:
    if sys.argv[1:] not in (['--run'], ['--run', '--runtime'], ['--run', '--update'],
                            ['--run', '--assembled']) or not hasattr(os, 'getuid') or os.getuid() == 0:
        print('Нужны непривилегированный пользователь и --run [--runtime|--update|--assembled]', file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix='homensai-isolated-systemd-') as temp:
        harness = Path(temp)
        harness.chmod(0o755)  # Скрипты не содержат секретов; их читает тестовый uid контейнера.
        (harness / 'setup.sh').write_text(SETUP, encoding='utf-8')
        session = SESSION
        if sys.argv[1:] == ['--run', '--runtime']:
            session = SESSION.rsplit('python -m pytest', 1)[0] + (
                'python -m pytest -q -p no:cacheprovider tests/test_runtime_native.py tests/test_systemd_group.py tests/test_systemd_fresh_start.py tests/test_api.py tests/test_api_signed_preflight.py tests/test_i18n.py\n')
        elif sys.argv[1:] == ['--run', '--update']:
            session = SESSION.rsplit('python -m pytest', 1)[0] + (
                'python -m pytest -q -ra --tb=short -x -p no:cacheprovider tests/test_demo_update.py -k systemd\n')
        elif sys.argv[1:] == ['--run', '--assembled']:
            session = SESSION.rsplit('python -m pytest', 1)[0] + (
                'python -m pytest -q -ra --tb=short -x -p no:cacheprovider tests/test_demo_update.py -k systemd_assembled\n')
        (harness / 'session.sh').write_text(session, encoding='utf-8')
        cidfile = harness / 'container.cid'
        command = ['podman', 'run', '--rm', '--cidfile', str(cidfile), '--systemd=always',
                   '--network=slirp4netns', '-v', str(ROOT) + ':/project:ro',
                   '-v', str(harness) + ':/harness:ro', '-w', '/project', IMAGE, 'sh', '/harness/setup.sh']
        try:
            return subprocess.run(command, check=False, timeout=900).returncode
        except (OSError, subprocess.TimeoutExpired):
            print('Не удалось завершить изолированный тест', file=sys.stderr)
            return 1
        finally:
            if cidfile.is_file():
                container_id = cidfile.read_text(encoding='ascii').strip()
                if re.fullmatch(r'[a-f0-9]{64}', container_id):
                    subprocess.run(['podman', 'stop', '--time', '10', container_id],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    subprocess.run(['podman', 'rm', container_id], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False)


if __name__ == '__main__':
    raise SystemExit(main())
