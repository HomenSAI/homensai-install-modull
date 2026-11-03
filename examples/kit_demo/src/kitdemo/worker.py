# HomenS.AI · © 2026 Serhii Khomenko
"""Фоновый процесс с heartbeat в каталоге данных."""
from __future__ import annotations

import json
import os
import time

from kitdemo.common import arguments, graceful_stop, release


def main() -> int:
    args = arguments('kitdemo worker')
    graceful_stop()
    _, version = release()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = args.data_dir / 'worker-heartbeat.json'
    while True:
        temporary = heartbeat.with_suffix('.tmp')
        temporary.write_text(json.dumps({'release': version, 'pid': os.getpid(), 'time': time.time()}))
        temporary.replace(heartbeat)
        time.sleep(1)


if __name__ == '__main__':
    raise SystemExit(main())
