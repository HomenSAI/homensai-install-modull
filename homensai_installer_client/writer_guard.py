"""Optional POSIX advisory lock shared by cooperating writers and backups.

This is not a sandbox: processes that ignore the lock can still write.
The parent directory and every participant must be trusted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import ntpath
from pathlib import Path
import stat
import sys

class WriterGuardError(RuntimeError):
    """The cooperative writer lock cannot be trusted or acquired."""


@dataclass(frozen=True)
class LockIdentity:
    """Observed file identity; meaningful only on the current host and filesystem."""
    device: int
    inode: int


def _checked_path(value: str | Path) -> Path:
    """Absolute Linux path with no links or ambiguous components."""
    text = str(value)
    if (not text or not text.startswith('/') or text.startswith('//') or '\\' in text or ':' in text
            or ntpath.isreserved(text) or any(ord(c) < 32 or ord(c) == 127 for c in text)
            or any(part == '..' or (part not in ('', '.') and part.endswith(('.', ' ')))
                   for part in text.split('/'))):
        raise WriterGuardError('Недопустимый путь блокировки')
    path = Path(text)
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise WriterGuardError('Ссылки запрещены')
    return path.resolve(strict=False)


@dataclass(frozen=True)
class CooperativeWriterLock:
    """A persistent private lock file: shared for writers, exclusive for backup."""
    path: Path

    def __init__(self, path: str):
        if (type(path) is not str or not sys.platform.startswith('linux')
                or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'getuid')):
            raise WriterGuardError('Неподдерживаемая блокировка')
        try:
            object.__setattr__(self, 'path', _checked_path(path))
        except Exception:
            raise WriterGuardError('Недопустимый путь блокировки') from None

    @contextmanager
    def hold(self, *, exclusive: bool):
        if type(exclusive) is not bool:
            raise WriterGuardError('Недопустимый режим блокировки')
        import fcntl
        fd = None
        try:
            path = _checked_path(self.path)
            parent = path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise WriterGuardError('Каталог блокировки не защищён')
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
            fd = os.open(path, flags, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise WriterGuardError('Файл блокировки не защищён')
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            current = _checked_path(path).lstat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise WriterGuardError('Файл блокировки изменился')
        except WriterGuardError:
            if fd is not None:
                os.close(fd)
            raise
        except (OSError, ValueError):
            if fd is not None:
                os.close(fd)
            raise WriterGuardError('Блокировка писателей недоступна') from None
        try:
            yield LockIdentity(info.st_dev, info.st_ino)
        finally:
            try:
                current = _checked_path(path).lstat()
                if ((info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
                        != (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink)):
                    raise WriterGuardError('Файл блокировки изменился')
            except (OSError, ValueError):
                raise WriterGuardError('Файл блокировки недоступен') from None
            finally:
                os.close(fd)
