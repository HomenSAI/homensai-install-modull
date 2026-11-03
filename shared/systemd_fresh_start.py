# HomenS.AI · © 2026 Serhii Khomenko
"""Однократный запуск свежей подписанной группы с устойчивой отметкой."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager

from shared.datahome import checked_path
from shared.runtime_native import verify_signed_linux_runtime
from shared.systemd_user_load import (signed_user_group, portable_signed_runtime_user_group,
                                     _pointer_signed_user_group, pointer_signed_runtime_user_group)
from shared.systemd_user_publish import _new_publication_receipt, _publication_receipt
from shared.update_plan import _atomic_write
from shared.wheel_audit import audit_signed_wheels


class FreshStartError(RuntimeError):
    """Результат запуска не подтверждён; повтор действия может быть небезопасен."""


@contextmanager
def _lifecycle_lock(install_root):
    import fcntl
    root = checked_path(install_root)
    control = checked_path(root / '.homensai-control')
    for path in (root, control):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise FreshStartError('Каталог управления не подтверждён')
    path = checked_path(control / 'user-systemd-lifecycle.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), 0o600)
    acquired = False
    try:
        info, current = os.fstat(fd), path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size != 0
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise FreshStartError('Блокировка служб не подтверждена')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _binding(root, directory, envelope, trusted_public_key, expected_product_id,
             expected_version, modes, load_key, runtime_key, reserve_bytes, pointer=False):
    context = {'product_id': expected_product_id, 'version': expected_version,
               'envelope_sha256': hashlib.sha256(envelope).hexdigest(),
               'publisher_sha256': hashlib.sha256(trusted_public_key).hexdigest(),
               'install_root': str(root), 'unit_dir': str(directory), 'modes': modes,
               'load_key': load_key, 'runtime_key': runtime_key, 'reserve_bytes': reserve_bytes}
    if pointer:
        context['generator'] = 'homensai-systemd-user-pointer-v2'
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _loaded_group(package_path, envelope, root, *, options, modes, directory, load_key, runtime_key,
                  reserve_bytes, runner, pointer, require_active=False, require_stable=False,
                  verify_runtime=True):
    if pointer:
        return _pointer_signed_user_group(package_path, envelope, str(root), **options, modes=modes,
            unit_dir=str(directory), operation_id=load_key, runtime_operation_id=runtime_key,
            reserve_bytes=reserve_bytes, runner=runner, _require_active=require_active,
            _require_stable=require_stable, _verify_runtime=verify_runtime)
    return signed_user_group(package_path, envelope, str(root), **options, modes=modes,
        unit_dir=str(directory), operation_id=load_key, portable=True, runner=runner,
        _require_active=require_active, _require_stable=require_stable)


def start_portable_fresh(package_path, envelope, install_root, **options):
    """Запуск с общей блокировкой start/stop для этой установки."""
    try:
        with _lifecycle_lock(install_root):
            return _start_portable_fresh(package_path, envelope, install_root, **options)
    except FreshStartError:
        raise
    except Exception:
        raise FreshStartError('Не удалось подтвердить запуск служб') from None


def stop_portable_fresh(package_path, envelope, install_root, *, trusted_public_key,
                        expected_product_id, expected_version, modes, unit_dir,
                        load_key, runtime_key, start_key, reserve_bytes, operation_id, runner=None,
                        reconcile=False, pointer=False):
    """Остановить стабильные units с отдельной отметкой, в том числе после pending start."""
    try:
        if (any(type(key) is not str or re.fullmatch(r'[a-f0-9]{32}', key) is None
                for key in (load_key, runtime_key, start_key, operation_id))
                or len({load_key, runtime_key, start_key, operation_id}) != 4
                or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 2**63 - 1
                or type(envelope) is not bytes or type(trusted_public_key) is not bytes
                or len(trusted_public_key) != 32 or type(modes) is not tuple or not modes
                or type(reconcile) is not bool or type(pointer) is not bool):
            raise FreshStartError('Недопустимые параметры остановки')
        with _lifecycle_lock(install_root):
            root, directory = checked_path(install_root), checked_path(unit_dir)
            control = checked_path(root / '.homensai-control')
            try:
                (control / 'user-systemd-cycle-v2.json').lstat()
            except FileNotFoundError:
                pass
            else:
                raise FreshStartError('Установка перешла к новому журналу цикла')
            publication = _publication_receipt(control / 'user-systemd-publication.json')
            if publication is None or publication['status'] != 'done' or publication['operation_id'] == operation_id:
                raise FreshStartError('Публикация не подтверждена')
            digest = _binding(root, directory, envelope, trusted_public_key, expected_product_id,
                              expected_version, modes, load_key, runtime_key, reserve_bytes, pointer=pointer)
            start_receipt = _publication_receipt(control / 'user-systemd-start.json')
            if (start_receipt is None or start_receipt['operation_id'] != start_key
                    or start_receipt['binding'] != digest):
                raise FreshStartError('Запуск не соответствует запросу')
            stop_binding = hashlib.sha256((digest + start_key).encode('ascii')).hexdigest()
            pending = {'version': 1, 'operation_id': operation_id, 'binding': stop_binding, 'status': 'pending'}
            done = {**pending, 'status': 'done'}
            receipt = checked_path(control / 'user-systemd-stop.json')
            old = _publication_receipt(receipt)
            if ((reconcile and old not in (pending, done)) or
                    (not reconcile and old is not None and old != done)):
                raise FreshStartError('Предыдущая остановка требует проверки')
            options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                           expected_version=expected_version)
            group = _loaded_group(package_path, envelope, root, options=options, modes=modes,
                        directory=directory, load_key=load_key, runtime_key=runtime_key,
                        reserve_bytes=reserve_bytes, runner=runner, pointer=pointer,
                        require_stable=(old is None and not reconcile), verify_runtime=False)
            if reconcile and old == pending:
                if (_publication_receipt(receipt) != pending or
                        _publication_receipt(control / 'user-systemd-start.json') != start_receipt or
                        _publication_receipt(control / 'user-systemd-publication.json') != publication):
                    raise FreshStartError('Отметки изменились')
                _atomic_write(receipt, done)
                _loaded_group(package_path, envelope, root, options=options, modes=modes,
                    directory=directory, load_key=load_key, runtime_key=runtime_key,
                    reserve_bytes=reserve_bytes, runner=runner, pointer=pointer, verify_runtime=False)
            elif old is None:
                _new_publication_receipt(receipt, pending)
                group.command('stop', execute=True)
                if _publication_receipt(receipt) != pending:
                    raise FreshStartError('Отметка остановки изменилась')
                _atomic_write(receipt, done)
            if _publication_receipt(receipt) != done:
                raise FreshStartError('Остановка не подтверждена')
            result = {'state': 'stopped', 'services_stopped': True, 'installation_complete': False,
                      'all_writers_stopped': False}
            if reconcile:
                result['prior_stop_command_confirmed'] = False
            return result
    except FreshStartError:
        raise
    except Exception:
        raise FreshStartError('Не удалось подтвердить остановку служб') from None


def _start_portable_fresh(package_path, envelope, install_root, *, trusted_public_key,
                         expected_product_id, expected_version, modes, unit_dir,
                         load_key, runtime_key, reserve_bytes, operation_id, runner=None, reconcile=False, pointer=False):
    """Однократно отправить start; pending никогда автоматически не повторяется."""
    if (any(type(key) is not str or re.fullmatch(r'[a-f0-9]{32}', key) is None
            for key in (load_key, runtime_key, operation_id))
            or len({load_key, runtime_key, operation_id}) != 3
            or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 2**63 - 1
            or type(envelope) is not bytes or type(trusted_public_key) is not bytes
            or len(trusted_public_key) != 32 or type(modes) is not tuple or not modes
            or type(reconcile) is not bool or type(pointer) is not bool):
        raise FreshStartError('Недопустимые параметры запуска')
    try:
        root, directory = checked_path(install_root), checked_path(unit_dir)
        control = checked_path(root / '.homensai-control')
        for path in (root, control):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FreshStartError('Каталог управления не подтверждён')
        publication = _publication_receipt(control / 'user-systemd-publication.json')
        if publication is None or publication['status'] != 'done' or publication['operation_id'] == operation_id:
            raise FreshStartError('Публикация не подтверждена')
        if _publication_receipt(control / 'user-systemd-stop.json') is not None:
            raise FreshStartError('Остановка требует отдельного нового цикла')
        digest = _binding(root, directory, envelope, trusted_public_key, expected_product_id,
                          expected_version, modes, load_key, runtime_key, reserve_bytes, pointer=pointer)
        pending = {'version': 1, 'operation_id': operation_id, 'binding': digest, 'status': 'pending'}
        done = {**pending, 'status': 'done'}
        receipt = checked_path(control / 'user-systemd-start.json')
        old = _publication_receipt(receipt)
        if ((reconcile and old not in (pending, done)) or
                (not reconcile and old is not None and old != done)):
            raise FreshStartError('Предыдущий запуск требует проверки')
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        if old is None and not reconcile:
            audit_signed_wheels(package_path, envelope, **options)
        verify_signed_linux_runtime(package_path, envelope, str(root), **options,
                                    operation_id=runtime_key, reserve_bytes=reserve_bytes)
        if old == done or reconcile:
            group = _loaded_group(package_path, envelope, root, options=options, modes=modes,
                        directory=directory, load_key=load_key, runtime_key=runtime_key,
                        reserve_bytes=reserve_bytes, runner=runner, pointer=pointer, require_active=True)
            if reconcile and old == pending:
                if (_publication_receipt(receipt) != pending or
                        _publication_receipt(control / 'user-systemd-publication.json') != publication or
                        _publication_receipt(control / 'user-systemd-stop.json') is not None):
                    raise FreshStartError('Отметки изменились')
                _atomic_write(receipt, done)
                verify_signed_linux_runtime(package_path, envelope, str(root), **options,
                                            operation_id=runtime_key, reserve_bytes=reserve_bytes)
                _loaded_group(package_path, envelope, root, options=options, modes=modes,
                    directory=directory, load_key=load_key, runtime_key=runtime_key,
                    reserve_bytes=reserve_bytes, runner=runner, pointer=pointer, require_active=True)
        else:
            if pointer:
                group = pointer_signed_runtime_user_group(package_path, envelope, str(root), **options,
                    modes=modes, unit_dir=str(directory), operation_id=load_key,
                    runtime_operation_id=runtime_key, reserve_bytes=reserve_bytes, runner=runner)
            else:
                group = portable_signed_runtime_user_group(package_path, envelope, str(root), **options,
                    modes=modes, unit_dir=str(directory), operation_id=load_key,
                    runtime_operation_id=runtime_key, runtime_reserve_bytes=reserve_bytes, runner=runner)
            _new_publication_receipt(receipt, pending)
            group.command('start', execute=True)
            if _publication_receipt(receipt) != pending:
                raise FreshStartError('Отметка запуска изменилась')
            _atomic_write(receipt, done)
        if _publication_receipt(receipt) != done:
            raise FreshStartError('Отметка запуска не подтверждена')
        result = {'state': 'started', 'services_started': True, 'installation_complete': False,
                  'health_verified': False, 'python_compatibility_verified': False}
        if reconcile:
            result['prior_start_command_confirmed'] = False
        return result
    except FreshStartError:
        raise
    except Exception:
        raise FreshStartError('Не удалось подтвердить запуск служб') from None
