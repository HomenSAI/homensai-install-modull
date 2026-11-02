# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальный POSIX-запуск закреплённой Python-миграции без shell.

Хэши должны поступать из независимого доверенного источника. Этот модуль не
проверяет подпись, происхождение интерпретатора, импортируемые модули или все
изменения миграции и не реализует идемпотентность UpdatePorts.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess

from shared.datahome import DataHomeError, checked_path
from shared.update_plan import EffectIndeterminate, UpdateError

_HASH = re.compile(r'[a-f0-9]{64}\Z')


def _checked_file(path: str, expected_hash: str, *, executable: bool = False) -> Path:
    if type(path) is not str or type(expected_hash) is not str or _HASH.fullmatch(expected_hash) is None:
        raise UpdateError('Некорректные данные закреплённого файла')
    resolved = checked_path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(resolved, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > 1024 * 1024 * 1024
                or (executable and not os.access(resolved, os.X_OK))):
            raise UpdateError('Недопустимый файл миграции')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    current = resolved.lstat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or digest != expected_hash):
        raise UpdateError('Файл не соответствует доверенному хэшу')
    return resolved


def run_python_migration(*, interpreter: str, interpreter_sha256: str,
                         release_root: str, script: str, script_sha256: str,
                         args: tuple[str, ...], timeout: float, execute: bool = False) -> None:
    """Проверить закреплённые байты и при execute=True запустить одну команду.

    Без доступа к stdout/stderr/stdin и окружению вызывающего. Ошибочный выход,
    timeout и неполученное подтверждение после запуска означают неизвестный
    исход. Чистый код выхода не подтверждает семантическую корректность данных.
    """
    if (os.name != 'posix' or type(execute) is not bool or type(args) is not tuple or len(args) > 128
            or any(type(a) is not str or len(a) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in a)
                   for a in args)
            or type(timeout) not in (int, float) or not 0 < timeout <= 3600 or not math.isfinite(timeout)
            or type(release_root) is not str):
        raise UpdateError('Недопустимые параметры миграции')
    try:
        root = checked_path(release_root)
        if not root.is_dir() or root == Path(root.anchor):
            raise UpdateError('Недопустимый каталог выпуска')
        executable = _checked_file(interpreter, interpreter_sha256, executable=True)
        payload = _checked_file(script, script_sha256)
        if root == payload or not payload.is_relative_to(root):
            raise UpdateError('Миграция находится вне выпуска')
    except (OSError, DataHomeError, ValueError):
        raise UpdateError('Не удалось проверить файлы миграции') from None
    if not execute:
        return
    environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
    process = None
    try:
        process = subprocess.Popen([str(executable), '-I', '-B', str(payload), *args],
                                   shell=False, cwd=str(root), env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        returncode = process.wait(timeout=timeout)
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            # Дочерний процесс мог продолжить менять данные после завершения
            # лидера. Убрать группу и не объявлять успешный результат.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise EffectIndeterminate('Процессы миграции остались активными')
        if returncode != 0:
            raise EffectIndeterminate('Миграция завершилась с ошибкой')
    except subprocess.TimeoutExpired:
        raise EffectIndeterminate('Время миграции истекло') from None
    except OSError:
        if process is not None:
            raise EffectIndeterminate('Результат миграции не подтверждён') from None
        raise UpdateError('Не удалось запустить миграцию') from None
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            finally:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise EffectIndeterminate('Остановка миграции не подтверждена') from None
