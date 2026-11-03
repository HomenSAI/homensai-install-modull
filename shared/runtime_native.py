# HomenS.AI · © 2026 Serhii Khomenko
"""Подготовка файлов Linux runtime из подписанного выпуска без исполнения.

Рецепт и байты должны быть в подписанном payload. Это не проверка происхождения,
лицензий, ABI или безопасности исполняемых файлов.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from shared.archive import ArchiveError, _safe_name, _space
from shared.datahome import DataHomeError, checked_path, resolve_datahome
from shared.fresh_prepare import FreshPrepareError, verify_fresh_release
from shared.manifest import ManifestError
from shared.package import PackageError
from shared.package_signing import verify_signed_package
from shared.systemd_user_publish import (UnitPublicationError, _existing_lock, _new_publication_receipt,
                                         _publication_receipt, _read_unit, _require_absent)
from shared.update_plan import UpdateError, _atomic_write, _object

RECIPE_PATH = 'runtime-linux/runtime-v1.json'
PREFIX = 'runtime-linux/'
MAX_RECIPE = 16384


class NativeRuntimeError(RuntimeError):
    """Подготовка или проверка runtime не подтверждена."""


def _definition(package_path, envelope, install_root, options):
    metadata = verify_signed_package(package_path, envelope, **options)
    verified = verify_fresh_release(package_path, envelope, install_root, **options)
    if verified['process_target'] != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}:
        raise NativeRuntimeError('Требуется нативный Linux x86-64')
    root = checked_path(install_root)
    home, release, value, files = _runtime_spec(metadata, root, options['expected_version'])
    if (verify_signed_package(package_path, envelope, **options) != metadata
            or verify_fresh_release(package_path, envelope, install_root, **options) != verified):
        raise NativeRuntimeError('Выпуск изменился')
    return metadata, verified, root, home, release, value, files


def _runtime_spec(metadata, root, version):
    """Read a runtime recipe from a release whose inventory is verified by the caller."""
    home = resolve_datahome(metadata['manifest'], root)
    entries = {item['path']: item for item in metadata['files']}
    recipe = entries.get(RECIPE_PATH)
    if recipe is None or recipe['size'] > MAX_RECIPE:
        raise NativeRuntimeError('Нет подписанного рецепта runtime')
    release = home.releases / ('app-' + version)
    raw = _read_unit(release / RECIPE_PATH, recipe['sha256'])
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    if (type(value) is not dict or set(value) != {'format', 'target', 'python_version', 'executables'}
            or type(value['format']) is not int or value['format'] != 1
            or value['target'] != {'os': 'linux', 'architecture': 'x86_64'}
            or type(value['python_version']) is not str
            or re.fullmatch(r'3\.14\.(0|[1-9][0-9]{0,3})', value['python_version']) is None
            or type(value['executables']) is not list or not value['executables']
            or len(value['executables']) > 512 or any(type(name) is not str for name in value['executables'])):
        raise NativeRuntimeError('Недопустимый рецепт runtime')
    names = value['executables']
    if (names != sorted(names) or len(set(name.casefold() for name in names)) != len(names)
            or 'bin/python' not in names):
        raise NativeRuntimeError('Недопустимый состав исполняемых файлов')
    for name in names:
        _safe_name(name)
    files = {name[len(PREFIX):]: item for name, item in entries.items()
             if name.startswith(PREFIX) and name != RECIPE_PATH}
    if not files or any(name not in files for name in names):
        raise NativeRuntimeError('Отсутствует файл runtime')
    for name in files:
        _safe_name(name)
    return home, release, value, files


def _runtime_binding(*, product_id, version, transaction, recipe, files, root, runtime,
                     trusted_public_key, reserve_bytes):
    context = {'product_id': product_id, 'version': version, 'transaction': transaction,
               'recipe_sha256': hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
               'files_sha256': hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
               'install_sha256': hashlib.sha256(str(root).encode()).hexdigest(),
               'runtime_sha256': hashlib.sha256(str(runtime).encode()).hexdigest(),
               'publisher_sha256': hashlib.sha256(trusted_public_key).hexdigest(), 'reserve_bytes': reserve_bytes}
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _directories(files):
    result = {''}
    for name in files:
        parts = name.split('/')
        result.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    return result


def _inspect_runtime(directory, files, executables):
    expected_dirs = _directories(files)
    found_dirs, found_files = set(), set()
    pending = [directory]
    while pending:
        path = pending.pop()
        relative = path.relative_to(directory).as_posix()
        relative = '' if relative == '.' else relative
        info = path.lstat()
        if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
            raise NativeRuntimeError('Недопустимый владелец или ссылка runtime')
        if stat.S_ISDIR(info.st_mode):
            if stat.S_IMODE(info.st_mode) != 0o700 or relative not in expected_dirs:
                raise NativeRuntimeError('Недопустимый каталог runtime')
            found_dirs.add(relative)
            with os.scandir(path) as children:
                pending.extend(Path(child.path) for child in children)
        elif stat.S_ISREG(info.st_mode) and relative in files:
            expected_mode = 0o700 if relative in executables else 0o600
            if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != expected_mode:
                raise NativeRuntimeError('Недопустимые права файла runtime')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                after = os.fstat(stream.fileno())
            current = path.lstat()
            if ((info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
                    != (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink)
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                    or before.st_size != files[relative]['size'] or digest != files[relative]['sha256']):
                raise NativeRuntimeError('Файл runtime изменён')
            found_files.add(relative)
        else:
            raise NativeRuntimeError('Посторонний объект в runtime')
    if found_dirs != expected_dirs or found_files != set(files):
        raise NativeRuntimeError('Состав runtime не совпадает')


def _prepare_signed_linux_runtime(package_path, envelope, install_root, *, trusted_public_key,
                                  expected_product_id, expected_version, execute=False,
                                  operation_id=None, reserve_bytes=64 * 1024 * 1024, verify_only=False):
    """Проверить или скопировать runtime в пустой каталог без запуска кода.

    При прерывании pending остаётся и повтор запрещён; done проверяет дерево.
    Подпись не гарантирует, что runtime является Python или безопасен.
    """
    if (type(execute) is not bool or type(verify_only) is not bool or (verify_only and not execute)
            or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 2**63 - 1
            or (operation_id is not None and (type(operation_id) is not str
                or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None))
            or (execute and operation_id is None)):
        raise NativeRuntimeError('Недопустимые параметры runtime')
    options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                   expected_version=expected_version)
    try:
        definition = _definition(package_path, envelope, install_root, options)
        metadata, verified, root, home, release, recipe, files = definition
        total = sum(item['size'] for item in files.values())
        public = {'state': 'verified' if verify_only else ('prepared' if execute else 'planned'), 'file_count': len(files),
                  'bytes': total, 'python_version_declared': recipe['python_version'],
                  'runtime_executed': False, 'python_compatibility_verified': False}
        if not execute:
            return public
        runtime = checked_path(home.roots['runtime'])
        control = checked_path(root / '.homensai-control')
        for path in (root, control, runtime):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise NativeRuntimeError('Каталог runtime не защищён')
        binding = _runtime_binding(product_id=expected_product_id, version=expected_version,
            transaction=verified['transaction'], recipe=recipe, files=files, root=root, runtime=runtime,
            trusted_public_key=trusted_public_key, reserve_bytes=reserve_bytes)
        receipt = checked_path(control / 'linux-runtime.json')
        pending = {'version': 1, 'operation_id': operation_id, 'binding': binding, 'status': 'pending'}
        done = {**pending, 'status': 'done'}
        bootstrap = checked_path(root / '.homensai-bootstrap.lock')
        service_lock = checked_path(home.roots['services'] / 'operation.lock')
        with _existing_lock(bootstrap):
            with _existing_lock(service_lock):
                if _definition(package_path, envelope, install_root, options) != definition:
                    raise NativeRuntimeError('Выпуск изменился')
                update_state = checked_path(home.roots['services'] / metadata['manifest']['update']['state_file'].replace('\\', '/'))
                _require_absent(update_state)
                old = _publication_receipt(receipt)
                if verify_only and old != done:
                    raise NativeRuntimeError('Подготовка runtime не подтверждена')
                if old == done:
                    _inspect_runtime(runtime, files, set(recipe['executables']))
                    if (_definition(package_path, envelope, install_root, options) != definition
                            or _publication_receipt(receipt) != done):
                        raise NativeRuntimeError('Данные изменились при проверке')
                    return public
                if old is not None:
                    raise NativeRuntimeError('Результат подготовки требует восстановления')
                with os.scandir(runtime) as children:
                    if next(children, None) is not None:
                        raise NativeRuntimeError('Каталог runtime не пуст')
                _space(runtime, total + reserve_bytes)
                _new_publication_receipt(receipt, pending)
                for name in sorted(_directories(files) - {''}, key=lambda item: (item.count('/'), item)):
                    path = checked_path(runtime / name)
                    path.mkdir(mode=0o700)
                    os.chmod(path, 0o700, follow_symlinks=False)
                for name, item in sorted(files.items()):
                    source = checked_path(release / item['path'])
                    destination = checked_path(runtime / name)
                    incoming_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
                    with os.fdopen(incoming_fd, 'rb') as incoming:
                        before = os.fstat(incoming.fileno())
                        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                                or before.st_mode & 0o022 or before.st_nlink != 1 or before.st_size != item['size']):
                            raise NativeRuntimeError('Исходный файл runtime недоступен')
                        outgoing_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(outgoing_fd, 'wb') as outgoing:
                            digest, size = hashlib.sha256(), 0
                            while block := incoming.read(1024 * 1024):
                                size += len(block)
                                if size > item['size']:
                                    raise NativeRuntimeError('Исходный файл runtime изменился')
                                digest.update(block)
                                outgoing.write(block)
                            after = os.fstat(incoming.fileno())
                            current = source.lstat()
                            if (size != item['size'] or digest.hexdigest() != item['sha256']
                                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                                raise NativeRuntimeError('Исходный файл runtime изменился')
                            os.fchmod(outgoing.fileno(), 0o700 if name in recipe['executables'] else 0o600)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                for name in sorted(_directories(files), key=lambda item: (item.count('/'), len(item)), reverse=True):
                    fd = os.open(runtime / name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                _inspect_runtime(runtime, files, set(recipe['executables']))
                if (_definition(package_path, envelope, install_root, options) != definition
                        or _publication_receipt(receipt) != pending):
                    raise NativeRuntimeError('Результат подготовки не подтверждён')
                _atomic_write(receipt, done)
                if _publication_receipt(receipt) != done:
                    raise NativeRuntimeError('Отметка runtime не подтверждена')
        return public
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, DataHomeError, ArchiveError,
            ManifestError, PackageError, FreshPrepareError, UnitPublicationError, UpdateError):
        raise NativeRuntimeError('Не удалось подготовить Linux runtime') from None


def prepare_signed_linux_runtime(package_path, envelope, install_root, *, trusted_public_key,
                                 expected_product_id, expected_version, execute=False,
                                 operation_id=None, reserve_bytes=64 * 1024 * 1024):
    """Подготовить runtime либо вернуть план; никогда не исполнять его."""
    return _prepare_signed_linux_runtime(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, execute=execute, operation_id=operation_id, reserve_bytes=reserve_bytes)


def verify_signed_linux_runtime(package_path, envelope, install_root, *, trusted_public_key,
                                expected_product_id, expected_version, operation_id,
                                reserve_bytes=64 * 1024 * 1024):
    """Проверить подтверждённый runtime без создания файлов и исполнения."""
    return _prepare_signed_linux_runtime(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, execute=True, operation_id=operation_id,
        reserve_bytes=reserve_bytes, verify_only=True)
