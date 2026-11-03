#!/usr/bin/env python3
"""Сквозная проверка комплекта на чистом Debian без Python и без сети.

Собирает на хосте комплекты вымышленного продукта (версии 1, 2, 3 и 3 с
отказом здоровья), затем в одноразовом rootless Podman с user-systemd и
`--network=none` выполняет: установку, обновление 1→2, обновление с откатом,
обновление 2→3, прерванную и продолженную установку, удаление с сохранением
данных, переустановку и полное удаление. Требуется --run.

Нужны: Python 3.14 с cryptography на хосте, podman, загруженные заранее архив
PBS и wheels (см. --inputs). Образ Debian скачивается один раз при отсутствии.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'localhost/homensai-kit-e2e:bookworm'
CONTAINERFILE = '''FROM docker.io/library/debian:bookworm-slim
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \\
        systemd dbus procps >/dev/null && rm -rf /var/lib/apt/lists/* \\
    && useradd -m -u 1000 homenstest && ! command -v python3 && ! command -v python
'''
SETUP = '''#!/bin/sh
set -eu
mkdir -p /run/systemd/system /run/user/1000 /sys/fs/cgroup/homensai-test
chmod 0755 /run /run/user
chmod 0700 /run/user/1000
chown -R homenstest:homenstest /run/user/1000 /home/homenstest
chown homenstest:homenstest /sys/fs/cgroup/homensai-test /sys/fs/cgroup/homensai-test/cgroup.procs /sys/fs/cgroup/homensai-test/cgroup.threads /sys/fs/cgroup/homensai-test/cgroup.subtree_control
sh -c 'echo $$ > /sys/fs/cgroup/homensai-test/cgroup.procs; exec runuser -u homenstest -- env HOME=/home/homenstest XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus LANG=C.UTF-8 sh /harness/session.sh'
'''
SESSION = r'''#!/bin/sh
set -u
dbus-daemon --session --address=unix:path=/run/user/1000/bus --fork --print-pid > /tmp/bus.pid
/usr/lib/systemd/systemd --user > /tmp/systemd.log 2>&1 &
manager=$!
cleanup() { systemctl --user exit >/dev/null 2>&1 || :; kill "$manager" 2>/dev/null || :; }
trap cleanup EXIT
for i in 1 2 3 4 5 6 7 8 9 10; do
    systemctl --user show --property=Version --value >/dev/null 2>&1 && break
    sleep 1
done
step() { echo "=== $*"; }
cd /home/homenstest
export HOMENSAI_API_TRACE=/tmp/homensai-api-trace.log
fail() { echo "E2E FAIL: $*"; echo "--- трасса исключений API (хвост)"; grep -v "FileNotFoundError\|StopIteration" /tmp/homensai-api-trace.log | tail -40 2>/dev/null; systemctl --user --no-pager status 'homensai-kit-demo-*' 2>&1 | tail -40; exit 1; }
for name in 1 2 3 3bad; do
    mkdir -p "kits/$name" && tar -xzf "/kits/homensai-kit-demo-$name.tar.gz" -C "kits/$name" || fail "extract $name"
done
K1=kits/1/homensai-kit-demo-1; K2=kits/2/homensai-kit-demo-2; K3=kits/3/homensai-kit-demo-3
KB=kits/3bad/homensai-kit-demo-3bad
PY=$PWD/$K1/installer/linux-x86_64/python/bin/python
ROOTDIR="/home/homenstest/Программы/kit demo"
STATE=/home/homenstest/.local/state/homensai/homensai-kit-demo
http() { "$PY" -I -c 'import sys,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:18431"+sys.argv[1], timeout=3).read().decode())' "$1"; }
wait_release() {
    for i in $(seq 1 60); do
        out=$(http /healthz 2>/dev/null) && echo "$out" | grep -q "\"release\": \"$1\"" && return 0
        sleep 0.5
    done
    return 1
}
command -v python3 >/dev/null 2>&1 && fail "система не должна содержать python3"

step "проверка целостности комплекта"
(cd "$K1" && sha256sum -c --quiet SHA256SUMS) || fail "SHA256SUMS"

# SIGKILL установщика и процесса API в момент, когда в файле появилась отметка pending.
kill_at() {
    file=$1; pattern=$2; shift 2
    "$@" > /tmp/killed.log 2>&1 &
    pid=$!
    hit=0
    for i in $(seq 1 6000); do
        if grep -q "$pattern" "$file" 2>/dev/null; then
            pkill -KILL -f 'homensai_setup.py|-m shared.api'; hit=1; break
        fi
        kill -0 $pid 2>/dev/null || break
        sleep 0.05
    done
    wait $pid
    echo "прервано на '$pattern' в $(basename "$file"): $hit"
    [ "$hit" = 1 ] || { cat /tmp/killed.log; fail "не удалось прервать на $pattern"; }
}

step "установка, прерванная SIGKILL во время подготовки runtime (pending), и продолжение"
mkdir -p "/home/homenstest/Программы"
kill_at "$ROOTDIR/.homensai-control/linux-runtime.json" pending \
    "$K1/setup.sh" --yes --language ru install --root "$ROOTDIR"
"$K1/setup.sh" --yes --language ru install --root "$ROOTDIR" || fail "install v1"
wait_release 1 || fail "health v1"
http /notes | grep -q '\[\]' || fail "новая база должна быть пустой: $(http /notes)"
grep -q '"sizeof_int": 4' "$ROOTDIR/data/deps.json" || fail "двоичная зависимость cffi"
"$STATE/installer/setup.sh" --language en status || fail "status из сохранённого установщика"
"$K1/setup.sh" --yes install >/dev/null 2>&1 && fail "повторная установка должна отказать"

step "обновление без пароля отклоняется"
"$K2/setup.sh" --yes --language ru update && fail "update без пароля"
wait_release 1 || fail "v1 должна работать после отказа"

step "обновление 1 -> 2 с миграцией, прерванное SIGKILL при публикации служб в консолидации"
export HOMENSAI_ARCHIVE_PASSWORD='synthetic-e2e-password'
kill_at "$ROOTDIR/.homensai-control/user-systemd-publication.json" pending \
    "$K2/setup.sh" --yes --language ru update
grep -q '"status": "consolidating"' "$STATE/state.json" || fail "ожидалась прерванная консолидация"
unset HOMENSAI_ARCHIVE_PASSWORD
"$K2/setup.sh" --yes --language ru update || fail "продолжение консолидации без пароля"
wait_release 2 || fail "health v2"
http /notes | grep -q 'migrated notes-2' || fail "миграция notes-2: $(http /notes)"
ls "$STATE"/work/update-*/ | grep -q . || fail "архив обновления"

step "обновление с отказом здоровья -> откат"
HOMENSAI_ARCHIVE_PASSWORD='synthetic-e2e-password' "$KB/setup.sh" --yes --language ru update
code=$?
[ "$code" = 4 ] || fail "ожидался код отката 4, получен $code"
wait_release 2 || fail "после отката должна работать v2"
http /notes | grep -q 'migrated notes-3' && fail "миграция отменённого обновления осталась"

step "обновление 2 -> 3 после отката"
HOMENSAI_ARCHIVE_PASSWORD='synthetic-e2e-password' "$K3/setup.sh" --yes --language de update || fail "update 2->3"
wait_release 3 || fail "health v3"
http /notes | grep -q 'migrated notes-3' || fail "миграция notes-3"
"$STATE/installer/setup.sh" status || fail "status v3"

step "удаление с сохранением данных"
"$STATE/installer/setup.sh" --yes --language ru uninstall || fail "uninstall keep"
http /healthz >/dev/null 2>&1 && fail "служба должна быть остановлена"
ls ~/.config/systemd/user | grep -q homensai-kit-demo && fail "units должны быть удалены"
[ -f "$ROOTDIR/data/kitdemo.sqlite3" ] || fail "данные должны сохраниться"

step "переустановка той же версии на сохранённых данных"
"$K2/setup.sh" --yes install && fail "переустановка другой версии должна отказать"
"$K3/setup.sh" --yes --language ru install || fail "reinstall v3"
wait_release 3 || fail "health после переустановки"
http /notes | grep -q 'migrated notes-3' || fail "данные после переустановки"

step "полное удаление"
"$STATE/installer/setup.sh" --yes uninstall --purge || fail "purge"
[ -d "$ROOTDIR/data" ] && fail "данные должны быть удалены"
http /healthz >/dev/null 2>&1 && fail "служба после purge"
echo "E2E OK"
'''


def build_kits(inputs: Path, output: Path, python: str):
    """Собрать четыре комплекта из копий примера с разными версиями."""
    keys = output / 'keys'
    subprocess.run([python, '-m', 'shared.kit', 'keygen', str(keys)], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    variants = {'1': (['notes-1'], False), '2': (['notes-1', 'notes-2'], False),
                '3': (['notes-1', 'notes-2', 'notes-3'], False), '3bad': (['notes-1', 'notes-2', 'notes-3'], True)}
    for version, (migrations, broken) in variants.items():
        project = output / ('project-' + version)
        shutil.copytree(ROOT / 'examples/kit_demo', project, ignore=shutil.ignore_patterns('wheelhouse', 'inputs'))
        shutil.copytree(inputs / 'wheelhouse', project / 'installer/wheelhouse')
        (project / 'installer/inputs').mkdir()
        archive = next((inputs / 'pbs').glob('*-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'))
        shutil.copyfile(archive, project / 'installer/inputs' / archive.name)
        config = json.loads((project / 'homensai-kit.json').read_text())
        config['migrations'] = migrations
        (project / 'homensai-kit.json').write_text(json.dumps(config))
        if broken:
            (project / 'src/kitdemo/FAIL_HEALTH').write_text('synthetic health failure\n')
        subprocess.run([python, '-m', 'shared.kit', 'build', '--config', str(project / 'homensai-kit.json'),
                        '--version', version, '--signing-key', str(keys / 'publisher-ed25519.pem'),
                        '--output', str(output / 'kits')], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--inputs', type=Path, default=Path.home() / '.cache/homensai-kit-inputs',
                        help='каталог с pbs/*.tar.gz и wheelhouse/*.whl')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--keep', action='store_true', help='не удалять рабочий каталог')
    args = parser.parse_args()
    if os.getuid() == 0:
        print('Нужен непривилегированный пользователь', file=sys.stderr)
        return 2
    if subprocess.run(['podman', 'image', 'exists', IMAGE]).returncode != 0:
        with tempfile.TemporaryDirectory() as context:
            Path(context, 'Containerfile').write_text(CONTAINERFILE)
            subprocess.run(['podman', 'build', '-t', IMAGE, context], check=True)
    work = Path(tempfile.mkdtemp(prefix='homensai-kit-e2e-', dir=Path.home() / '.cache'))
    try:
        build_kits(args.inputs, work, args.python)
        harness = work / 'harness'
        harness.mkdir()
        (harness / 'setup.sh').write_text(SETUP)
        (harness / 'session.sh').write_text(SESSION)
        for path in (work, harness, work / 'kits'):
            path.chmod(0o755)
        for item in (work / 'kits').glob('*.tar.gz'):
            item.chmod(0o644)
        command = ['podman', 'run', '--rm', '--systemd=always', '--network=none',
                   '-v', str(work / 'kits') + ':/kits:ro', '-v', str(harness) + ':/harness:ro',
                   IMAGE, 'sh', '/harness/setup.sh']
        return subprocess.run(command, timeout=3600).returncode
    finally:
        if args.keep:
            print('Рабочий каталог:', work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
