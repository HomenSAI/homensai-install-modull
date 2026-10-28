"""POSIX private recovery copies: bytes never get public temporary modes."""
import os
from pathlib import Path
import stat
import inspect

import pytest

from shared.adapters_fs import FilesystemPorts, _copy_file
from shared.datahome import DataHomeError
from shared.update_plan import UpdateError


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX user-private recovery mode contract')


@pytest.mark.parametrize('mask', [0, 0o022, 0o077, 0o777])
def test_atomic_copy_is_private_even_before_first_secret_byte_and_replaces_readonly_destination(tmp_path, monkeypatch, mask):
    source = tmp_path / 'source'; source.write_bytes(b'synthetic-private-database-and-credential')
    source.chmod(0o400)
    destination = tmp_path / 'restored'; destination.write_bytes(b'old bytes'); destination.chmod(0o444)
    actual = Path.open
    checked = []
    class Reader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def read(self, size):
            temporary, = tmp_path.glob('.copy-*.tmp')
            assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
            checked.append(True)
            return self.stream.read(size)
    def watch(path, *args, **kwargs):
        stream = actual(path, *args, **kwargs)
        return Reader(stream) if path == source and args == ('rb',) else stream
    monkeypatch.setattr(Path, 'open', watch)
    previous = os.umask(mask)
    try: _copy_file(source, destination)
    finally: os.umask(previous)
    assert checked and destination.read_bytes() == b'synthetic-private-database-and-credential'
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(source.stat().st_mode) == 0o400
    assert not list(tmp_path.glob('.copy-*.tmp'))


def test_reconcile_private_dirs_files_and_preserved_control_leave_unrelated_parent_unchanged(tmp_path):
    source = tmp_path / 'candidate'; source.mkdir(mode=0o700)
    (source / 'private').mkdir(mode=0o700)
    (source / 'private/database.sqlite').write_bytes(b'synthetic database')
    (source / 'private/credential').write_bytes(b'synthetic credential')
    unrelated = tmp_path / 'export-parent'; unrelated.mkdir(mode=0o750)
    target = unrelated / 'owned'; target.mkdir(mode=0o755)
    (target / 'private').mkdir(mode=0o755)
    control = target / 'preserved-control'; control.write_bytes(b'control'); control.chmod(0o640)
    before = unrelated.stat().st_mode
    previous = os.umask(0)
    try: FilesystemPorts._reconcile(source, target, {'preserved-control'})
    finally: os.umask(previous)
    assert unrelated.stat().st_mode == before
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE((target / 'private').stat().st_mode) == 0o700
    for name in ('database.sqlite', 'credential'):
        assert stat.S_IMODE((target / 'private' / name).stat().st_mode) == 0o600
        assert (target / 'private' / name).read_bytes() == (source / 'private' / name).read_bytes()
    assert stat.S_IMODE(control.stat().st_mode) == 0o640 and control.read_bytes() == b'control'


def test_reconcile_refuses_foreign_directory_before_chmod(tmp_path, monkeypatch):
    source = tmp_path / 'candidate'; source.mkdir(mode=0o700)
    target = tmp_path / 'foreign'; target.mkdir(mode=0o755)
    previous = target.stat().st_mode
    monkeypatch.setattr('shared.adapters_fs.os.geteuid', lambda: os.getuid() + 1)
    with pytest.raises(UpdateError): FilesystemPorts._reconcile(source, target, set())
    assert target.stat().st_mode == previous


def test_directory_swapped_to_symlink_after_owner_check_cannot_chmod_foreign_target(tmp_path, monkeypatch):
    source = tmp_path / 'candidate'; source.mkdir(mode=0o700)
    target = tmp_path / 'owned'; target.mkdir(mode=0o755)
    foreign = tmp_path / 'foreign-unrelated'; foreign.mkdir(mode=0o750)
    foreign_mode = foreign.stat().st_mode
    actual = Path.lstat
    fired = []
    def substitute(path):
        info = actual(path)
        if (path == target and not fired
                and inspect.currentframe().f_back.f_code.co_name == '_private_recovery_directory'):
            fired.append(True)
            target.rename(tmp_path / 'original-owned')
            target.symlink_to(foreign, target_is_directory=True)
        return info
    monkeypatch.setattr(Path, 'lstat', substitute)
    with pytest.raises((UpdateError, DataHomeError)): FilesystemPorts._reconcile(source, target, set())
    assert fired and foreign.stat().st_mode == foreign_mode
