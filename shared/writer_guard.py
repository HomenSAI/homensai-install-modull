"""Optional POSIX advisory lock shared by cooperating writers and backups.

This is not a sandbox: processes that ignore the lock can still write.
The parent directory and every participant must be trusted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys

from shared.datahome import checked_path


class WriterGuardError(RuntimeError):
    """The cooperative writer lock cannot be trusted or acquired."""


@dataclass(frozen=True)
class CooperativeWriterLock:
    """A persistent private lock file: shared for writers, exclusive for backup."""
    path: Path

    def __init__(self, path: str):
        if (type(path) is not str or not sys.platform.startswith('linux')
                or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'getuid')):
            raise WriterGuardError('Неподдерживаемая блокировка')
        try:
            object.__setattr__(self, 'path', checked_path(path))
        except Exception:
            raise WriterGuardError('Недопустимый путь блокировки') from None

    @contextmanager
    def hold(self, *, exclusive: bool):
        if type(exclusive) is not bool:
            raise WriterGuardError('Недопустимый режим блокировки')
        import fcntl
        fd = None
        try:
            path = checked_path(self.path)
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
            current = checked_path(path).lstat()
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
            yield
        finally:
            try:
                current = checked_path(path).lstat()
                if ((info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
                        != (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink)):
                    raise WriterGuardError('Файл блокировки изменился')
            except (OSError, ValueError):
                raise WriterGuardError('Файл блокировки недоступен') from None
            finally:
                os.close(fd)
