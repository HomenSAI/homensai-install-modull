"""Real Linux flock tests for cooperating writers; no claim about other processes."""
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from shared.writer_guard import CooperativeWriterLock, WriterGuardError

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Requires Linux flock')


def test_real_writer_and_backup_exclude_each_other(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    lock = CooperativeWriterLock(str(root / 'writers.lock'))
    code = ('import sys\nfrom shared.writer_guard import CooperativeWriterLock\n'
            'with CooperativeWriterLock(sys.argv[1]).hold(exclusive=False):\n'
            ' print("ready", flush=True)\n sys.stdin.read()\n')
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(lock.path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready and process.stdout.readline().strip() == 'ready'
        with pytest.raises(WriterGuardError):
            with lock.hold(exclusive=True):
                pytest.fail('backup must not start while writer holds the lock')
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        with lock.hold(exclusive=True):
            blocked = subprocess.run([sys.executable, '-B', '-c', code, str(lock.path)],
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            assert blocked.returncode != 0 and 'ready' not in blocked.stdout
        with lock.hold(exclusive=False):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for stream in (process.stdout, process.stderr):
            stream.close()


def test_crashed_writer_releases_os_lock(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(root / 'writers.lock'))
    code = ('import sys,time\nfrom shared.writer_guard import CooperativeWriterLock\n'
            'with CooperativeWriterLock(sys.argv[1]).hold(exclusive=False):\n'
            ' print("ready",flush=True)\n time.sleep(60)\n')
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(guard.path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready and process.stdout.readline().strip() == 'ready'
        process.kill()
        process.wait(timeout=10)
        with guard.hold(exclusive=True):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize('case', ['directory_permissions', 'file_permissions', 'hardlink', 'symlink'])
def test_lock_rejects_unsafe_paths(tmp_path, case):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'writers.lock'
    if case == 'directory_permissions':
        root.chmod(0o755)
    elif case == 'file_permissions':
        path.touch(mode=0o644)
    elif case == 'hardlink':
        path.touch(mode=0o600)
        os.link(path, root / 'alias')
    else:
        target = root / 'target'
        target.touch(mode=0o600)
        path.symlink_to(target)
    with pytest.raises(WriterGuardError):
        lock = CooperativeWriterLock(str(path))
        with lock.hold(exclusive=True):
            pytest.fail('unsafe lock accepted')


def test_replaced_lock_is_detected_before_guard_reports_success(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'writers.lock'
    lock = CooperativeWriterLock(str(path))
    with pytest.raises(WriterGuardError):
        with lock.hold(exclusive=True):
            path.unlink()
            path.touch(mode=0o600)


def test_demo_worker_quiesces_before_real_archive(tmp_path):
    from shared.archive import build_archive, verify_archive
    from shared.datahome import DataHome

    lock_dir = tmp_path / 'private'
    lock_dir.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(lock_dir / 'writers.lock'))
    roots = {name: tmp_path / 'persistent' / name for name in ('data', 'backup', 'services', 'config', 'runtime')}
    for root in roots.values():
        root.mkdir(parents=True)
    home = DataHome('homensai-test-demo', tmp_path / 'app', tmp_path / 'releases', roots)
    archive = tmp_path / 'synthetic.zip'
    code = ('import sys\nfrom shared.writer_guard import CooperativeWriterLock\n'
            'from examples.demo_product.main import main\n'
            'lock, data = sys.argv[1:]\n'
            'with CooperativeWriterLock(lock).hold(exclusive=False):\n'
            ' sys.argv=["demo", "worker", "--data-dir", data]\n main()\n')
    worker = subprocess.Popen([sys.executable, '-B', '-c', code, str(guard.path), str(roots['data'])],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        heartbeat = roots['data'] / 'worker-heartbeat.json'
        while not heartbeat.exists() and time.monotonic() < deadline:
            if worker.poll() is not None:
                pytest.fail('demo worker exited before writing heartbeat')
            time.sleep(0.05)
        assert heartbeat.exists()
        with pytest.raises(WriterGuardError):
            with guard.hold(exclusive=True):
                build_archive(home, archive)
        assert not archive.exists()
        worker.terminate()
        assert worker.wait(timeout=10) == 0
        with guard.hold(exclusive=True):
            metadata = build_archive(home, archive)
        assert verify_archive(archive, expected_product_id=home.product_id) == metadata
        assert any(item['path'].endswith('/worker-heartbeat.json') for item in metadata['files'])
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        worker.stderr.close()
