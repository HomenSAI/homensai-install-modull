#!/usr/bin/env python3
"""Запустить реальный тест PBS в одноразовом rootless Podman без сети.

Архив и wheels должны быть заранее загружены; тестовый образ и все данные
монтируются отдельно, исходники и входы доступны только для чтения.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--archive', required=True)
    parser.add_argument('--wheelhouse', required=True)
    args = parser.parse_args()
    archive, wheelhouse = Path(args.archive), Path(args.wheelhouse)
    if (not args.run or not hasattr(os, 'getuid') or os.getuid() == 0 or
            not archive.is_absolute() or not archive.is_file() or archive.is_symlink() or
            not wheelhouse.is_absolute() or not wheelhouse.is_dir() or wheelhouse.is_symlink()):
        parser.error('Нужны --run, непривилегированный пользователь и абсолютные безопасные входы')
    with tempfile.TemporaryDirectory(prefix='homensai-pbs-isolated-') as temp:
        work = Path(temp)
        (work / 'tmp').mkdir(mode=0o700)
        setup = '''set -eu
export PIP_NO_CACHE_DIR=1
python -m pip install -q --no-index --find-links /wheels --target /work/deps pytest
python -m pip install -q --no-index --find-links /wheels --target /work/deps -r /project/requirements-crypto.txt
export PYTHONPATH=/work/deps:/project
export PYTHONDONTWRITEBYTECODE=1
export HOMENSAI_PBS_ISOLATED=1
export HOMENSAI_PBS_ARCHIVE=/input/runtime.tar.gz
export TMPDIR=/work/tmp
cd /project
python -m pytest -q -ra -p no:cacheprovider tests/test_runtime_pbs.py tests/test_runtime_block.py tests/test_package.py tests/test_package_signing.py tests/test_package_prepare.py tests/test_crypto_wheelhouse.py
'''
        (work / 'setup.sh').write_text(setup, encoding='utf-8')
        cidfile = work / 'container.cid'
        cmd = ['podman', 'run', '--rm', '--cidfile', str(cidfile), '--network=none', '--userns=keep-id',
               '--user', f'{os.getuid()}:{os.getgid()}', '-v', str(ROOT) + ':/project:ro',
               '-v', str(archive) + ':/input/runtime.tar.gz:ro', '-v', str(wheelhouse) + ':/wheels:ro',
               '-v', str(work) + ':/work:rw', '-w', '/project', 'python:3.14-slim-bookworm',
               'sh', '/work/setup.sh']
        try:
            return subprocess.run(cmd, timeout=900, check=False).returncode
        except (OSError, subprocess.TimeoutExpired):
            print('Не удалось завершить изолированный тест PBS')
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
