"""Сравнение live persistent roots с восстановленным backup перед migration.

Linux adapter only: каталог открывается от / через O_NOFOLLOW/dir_fd. Вызывающий
engine держит installation lock и остановил всех писателей. Ни hooks, ни SQLite
здесь не вызываются. Исключаются только bytes собственного update journal;
его наличие, тип файла, владение и отсутствие гонки проверяются с обеих сторон.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

from shared.archive import _safe_name
from shared.datahome import PERSISTENT_ROOTS, checked_path, contains

from .errors import InstallerError


def _fail():
    raise InstallerError("RECOVERY_REQUIRED", phase="product_adapter",
                         details={"reason": "migration_live_snapshot_not_verified"})


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(info):
    # Ancestors include shared /tmp. Unrelated entries can legitimately change
    # their nlink/size/times; inode, type, permissions and owner must stay bound.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _path(value):
    original = Path(value)
    if checked_path(original) != original:
        _fail()
    return original


def _safe(info, *, directory=False):
    if (getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1
            or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        _fail()


@contextmanager
def _root_handle(root):
    """Не следовать ссылке ни в одном сегменте; удерживать все parent handles."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    handles, edges = [], []
    try:
        handles.append(os.open(root.anchor, flags))
        parts = root.parts[1:]
        for index, part in enumerate(parts):
            parent = handles[-1]
            before = os.stat(part, dir_fd=parent, follow_symlinks=False)
            child = os.open(part, flags, dir_fd=parent)
            handles.append(child)
            captured = os.fstat(child)
            signature = _signature if index == len(parts) - 1 else _directory_identity
            if signature(before) != signature(captured):
                _fail()
            edges.append((parent, part, child, _directory_identity(captured)))
        yield handles[-1]
        # Parent directories могут менять unrelated entries; важна identity
        # данного пути, а не mtime всего /tmp или домашнего каталога.
        for parent, part, child, identity in edges:
            named = os.stat(part, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if (not stat.S_ISDIR(named.st_mode)
                    or getattr(named, "st_file_attributes", 0) & 0x400
                    or _directory_identity(named) != identity or _directory_identity(opened) != identity):
                _fail()
    finally:
        for handle in reversed(handles):
            os.close(handle)


def _scan(root, label, journal_name):
    directories, files, signatures = set(), {}, {}
    ignored_seen = False

    def visit(folder, relative):
        nonlocal ignored_seen
        before = os.fstat(folder)
        _safe(before, directory=True)
        key = label + ("/" + relative if relative else "")
        _safe_name("roots/" + key)
        directories.add(key)
        signatures[key] = _signature(before)
        with os.scandir(folder) as entries:
            names = sorted(entry.name for entry in entries)
        if len({name.casefold() for name in names}) != len(names):
            _fail()
        for name in names:
            child_relative = relative + "/" + name if relative else name
            child_key = label + "/" + child_relative
            _safe_name("roots/" + child_key)
            named = os.stat(name, dir_fd=folder, follow_symlinks=False)
            directory = stat.S_ISDIR(named.st_mode)
            _safe(named, directory=directory)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory:
                flags |= os.O_DIRECTORY
            handle = os.open(name, flags, dir_fd=folder)
            try:
                captured = os.fstat(handle)
                _safe(captured, directory=directory)
                if _signature(captured) != _signature(named):
                    _fail()
                if directory:
                    visit(handle, child_relative)
                else:
                    signatures[child_key] = _signature(captured)
                    ignored = label == "services" and child_relative == journal_name
                    if ignored:
                        ignored_seen = True
                    else:
                        digest, size = hashlib.sha256(), 0
                        while block := os.read(handle, 1024 * 1024):
                            size += len(block)
                            digest.update(block)
                        if size != captured.st_size:
                            _fail()
                        files[child_key] = (size, digest.hexdigest())
                if (_signature(os.fstat(handle)) != _signature(captured)
                        or _signature(os.stat(name, dir_fd=folder, follow_symlinks=False)) != _signature(captured)):
                    _fail()
            finally:
                os.close(handle)
        if _signature(os.fstat(folder)) != _signature(before):
            _fail()

    with _root_handle(root) as handle:
        visit(handle, "")
    if label == "services" and journal_name is not None and not ignored_seen:
        _fail()
    return (directories, files), signatures


def _restored_container(root):
    with _root_handle(root) as handle:
        before = os.fstat(handle)
        _safe(before, directory=True)
        with os.scandir(handle) as entries:
            if {entry.name for entry in entries} != set(PERSISTENT_ROOTS):
                _fail()
        if _signature(os.fstat(handle)) != _signature(before):
            _fail()
        return _signature(before)


def assert_live_snapshot_matches(live_roots: Mapping[str, Path], restored: Path, *, own_update_journal: Path):
    """True только при равенстве всех пяти persistent roots (bytes + dirs).

    restored — private candidate из BackupReceiptStore, содержащий roots/<name>.
    own_update_journal обязан быть services/update-<32 lowercase hex>.json.
    Unsafe/неподдерживаемая среда, отсутствующие данные или drift дают один
    безопасный RECOVERY_REQUIRED без путей и содержимого в ошибке.
    """
    if own_update_journal is None:
        _fail()
    return _assert_snapshot_matches(live_roots, restored, own_update_journal=own_update_journal)


def assert_recovery_snapshot_matches(live_roots: Mapping[str, Path], restored: Path):
    """Полное сравнение recovery: ни один файл services не исключается."""
    try:
        return _assert_snapshot_matches(live_roots, restored, own_update_journal=None)
    except InstallerError:
        raise InstallerError("RECOVERY_REQUIRED", phase="product_adapter",
            details={"reason": "recovery_live_snapshot_not_verified"}) from None


def _assert_snapshot_matches(live_roots, restored, *, own_update_journal):
    try:
        if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
                or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
                or os.scandir not in os.supports_fd or not isinstance(live_roots, Mapping)
                or set(live_roots) != set(PERSISTENT_ROOTS)):
            _fail()
        live = {name: _path(path) for name, path in live_roots.items()}
        candidate = _path(restored)
        journal_name = None
        if own_update_journal is not None:
            journal = _path(own_update_journal)
            if (journal.parent != live["services"]
                    or re.fullmatch(r"update-[0-9a-f]{32}\.json", journal.name) is None):
                _fail()
            journal_name = journal.name
        roots = [*live.values(), candidate]
        if any(a == Path(a.anchor) or contains(a, b) or contains(b, a)
               for index, a in enumerate(roots) for b in roots[index + 1:]):
            _fail()
        with _root_handle(candidate) as handle:
            _safe(os.fstat(handle), directory=True)
        restored_roots = _path(candidate / "roots")
        container_before = _restored_container(restored_roots)
        # Второй проход обнаруживает изменения уже прочитанного root во время
        # чтения другой стороны. Inode/mode restored и live не сравниваются.
        first_live = {name: _scan(path, name, journal_name) for name, path in sorted(live.items())}
        first_backup = {name: _scan(restored_roots / name, name, journal_name) for name in sorted(live)}
        last_live = {name: _scan(path, name, journal_name) for name, path in sorted(live.items())}
        last_backup = {name: _scan(restored_roots / name, name, journal_name) for name in sorted(live)}
        if first_live != last_live or first_backup != last_backup:
            _fail()
        if _restored_container(restored_roots) != container_before:
            _fail()
        if any(first_live[name][0] != first_backup[name][0] for name in live):
            _fail()
        return True
    except InstallerError:
        raise
    except Exception:
        _fail()
