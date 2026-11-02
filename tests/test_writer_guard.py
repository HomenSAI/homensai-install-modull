"""Real Linux flock tests for cooperating writers; no claim about other processes."""
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from homensai_installer_client import CooperativeWriterLock, WriterGuardError

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Requires Linux flock')


def test_installer_and_client_use_the_same_lock_type():
    from shared.writer_guard import CooperativeWriterLock as InstallerLock
    assert InstallerLock is CooperativeWriterLock


def test_lock_reports_stable_file_identity(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(root / 'writers.lock'))
    with guard.hold(exclusive=True) as identity:
        info = guard.path.stat()
        assert (identity.device, identity.inode) == (info.st_dev, info.st_ino)


@pytest.mark.parametrize('path', ['relative/lock', '/tmp/../escape', '/tmp/invalid.', '/tmp/a\\b',
                                   '/tmp/control\nchar', '/tmp/name:stream', '//server/share'])
def test_lock_rejects_ambiguous_or_nonlocal_paths(path):
    with pytest.raises(WriterGuardError):
        CooperativeWriterLock(path)


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
    worker = subprocess.Popen([sys.executable, '-B', '-m', 'examples.demo_product.main', 'worker',
                               '--data-dir', str(roots['data']), '--writer-lock', str(guard.path)],
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


@pytest.mark.parametrize('action', ['migrate', 'serve', 'worker'])
def test_demo_writer_refuses_to_start_during_backup(tmp_path, action):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(private / 'writers.lock'))
    data = tmp_path / 'data'
    with guard.hold(exclusive=True):
        result = subprocess.run([sys.executable, '-B', '-m', 'examples.demo_product.main', action,
                                 '--data-dir', str(data), '--writer-lock', str(guard.path)],
                                capture_output=True, timeout=10)
        assert result.returncode != 0 and not data.exists()


def test_all_cooperating_demo_writers_must_stop_before_backup(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    guard = CooperativeWriterLock(str(private / 'writers.lock'))
    data = tmp_path / 'data'
    base = [sys.executable, '-B', '-m', 'examples.demo_product.main']
    processes = []
    try:
        for action in ('serve', 'worker'):
            processes.append(subprocess.Popen(base + [action, '--data-dir', str(data),
                                                       '--writer-lock', str(guard.path)],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (data / 'demo.sqlite3').exists() and (data / 'worker-heartbeat.json').exists():
                break
            if any(process.poll() is not None for process in processes):
                pytest.fail('demo writer exited before readiness')
            time.sleep(0.05)
        else:
            pytest.fail('demo writers did not become ready')
        with pytest.raises(WriterGuardError):
            with guard.hold(exclusive=True):
                pass
        processes[1].terminate()
        assert processes[1].wait(timeout=10) == 0
        with pytest.raises(WriterGuardError):
            with guard.hold(exclusive=True):
                pass
        processes[0].terminate()
        assert processes[0].wait(timeout=10) == 0
        with guard.hold(exclusive=True):
            pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stderr.close()
