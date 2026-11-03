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

from shared.datahome import DataHomeError, checked_path, resolve_datahome
from shared.host_target import HostTargetError, native_target
from shared.manifest import ManifestError, check_target, validate_manifest
from shared.package import PackageError
from shared.package_signing import verify_signed_package
from shared.update_plan import EffectIndeterminate, UpdateError, _identifier
from shared.wheel_audit import audit_signed_wheels

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


def run_signed_native_migration(*, package_path: str, envelope: bytes, trusted_public_key: bytes,
                                expected_product_id: str, expected_version: str, release_root: str,
                                expected_manifest: dict, install_root: str, migration_id: str | None = None,
                                interpreter: str, interpreter_sha256: str, timeout: float,
                                execute: bool = False) -> None:
    """Запустить только объявленную в подписанном манифесте Python-миграцию.

    Контракт команды: ["python", "relative/script.py", ...args], при этом
    целые аргументы @DATA_DIR@ и @MIGRATION_ID@ подставляются строго по контракту.
    Ключ, продукт, версия и хэш runtime независимо задаются вызывающим. Эта
    проверка не является sandbox, idempotency ledger или проверкой отсутствия
    вредоносного кода. Параметры из доверенного манифеста видны в argv процесса.
    """
    try:
        metadata = verify_signed_package(package_path, envelope, trusted_public_key=trusted_public_key,
                                         expected_product_id=expected_product_id, expected_version=expected_version)
        audit_signed_wheels(package_path, envelope, trusted_public_key=trusted_public_key,
                            expected_product_id=expected_product_id, expected_version=expected_version)
        manifest = metadata['manifest']
        if (manifest['schema_version'] != 2 or check_target(manifest, native_target())['status'] != 'supported'):
            raise UpdateError('Целевая среда не подтверждена')
        if validate_manifest(expected_manifest) != manifest:
            raise UpdateError('Подписанный манифест не соответствует плану')
        command = manifest['migrations']['cmd']
        if len(command) < 2 or command[0] != 'python' or not command[1].endswith('.py'):
            raise UpdateError('Команда миграции не поддерживается')
        expected = {item['path']: item['sha256'] for item in metadata['files']}
        script = command[1]
        if script not in expected:
            raise UpdateError('Команда отсутствует в подписанном составе')
        root = checked_path(release_root)
        if not root.is_dir() or root == Path(root.anchor):
            raise UpdateError('Недопустимый каталог выпуска')
        home = resolve_datahome(manifest, install_root)
        if root != checked_path(home.releases / ('app-' + expected_version)):
            raise UpdateError('Выпуск не принадлежит установке')
        arguments = command[2:]
        data_token, migration_token = '@DATA_DIR@', '@MIGRATION_ID@'
        if (arguments.count(data_token) > 1 or arguments.count(migration_token) > 1
                or any((data_token in arg or migration_token in arg) and arg not in (data_token, migration_token)
                       for arg in arguments)):
            raise UpdateError('Недопустимые шаблоны миграции')
        if migration_token in arguments:
            if not _identifier(migration_id):
                raise UpdateError('Недопустимый идентификатор миграции')
        elif migration_id is not None:
            raise UpdateError('Идентификатор не предусмотрен манифестом')
        if data_token in arguments and not home.roots['data'].is_dir():
            raise UpdateError('Каталог данных не подготовлен')
        arguments = tuple(str(home.roots['data']) if arg == data_token else
                          migration_id if arg == migration_token else arg for arg in arguments)
        directories = {'.'}
        for name in expected:
            parts = name.split('/')
            directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        remaining_files = set(expected)
        remaining_dirs = directories - {'.'}
        pending = [root]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    relative = (current / entry.name).relative_to(root).as_posix()
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and relative in remaining_dirs:
                        remaining_dirs.remove(relative)
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and relative in remaining_files:
                        remaining_files.remove(relative)
                        _checked_file(str(current / entry.name), expected[relative])
                    else:
                        raise UpdateError('Состав выпуска не соответствует подписи')
        if remaining_files or remaining_dirs:
            raise UpdateError('Состав выпуска не соответствует подписи')
        run_python_migration(interpreter=interpreter, interpreter_sha256=interpreter_sha256,
                             release_root=str(root), script=str(root.joinpath(*script.split('/'))),
                             script_sha256=expected[script], args=arguments,
                             timeout=timeout, execute=execute)
    except (PackageError, HostTargetError, ManifestError, DataHomeError, OSError, ValueError, KeyError, TypeError):
        raise UpdateError('Не удалось подтвердить подписанную миграцию') from None
