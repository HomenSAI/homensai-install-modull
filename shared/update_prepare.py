"""Ограниченная подготовка подписанного Linux-выпуска до начала обновления."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import re
import stat

from shared.adapters_fs import FilesystemPorts, _tree
from shared.datahome import checked_path, contains
from shared.effect_receipt import read_effect_receipt
from shared.fresh_prepare import status_fresh_native, verify_fresh_release
from shared.host_target import native_target
from shared.linux_update_effects import _previous_manifest_compatible
from shared.manifest import check_target
from shared.package_prepare import prepare_native_signed_package
from shared.package_signing import verify_signed_package
from shared.systemd_fresh_start import _lifecycle_lock
from shared.update_plan import UpdatePlan, _atomic_write, operation_lock
from shared.writer_guard import writer_lock_digest


class UpdatePrepareError(RuntimeError):
    """Подготовка не подтверждена; состояние может требовать ручной проверки."""


_UPDATE_LOCK_SCOPE = ContextVar('homensai_update_lock_scope', default=None)


class _UpdateLease:
    def __init__(self, root, lock_path):
        self.root, self.lock_path, self.active = str(root), str(lock_path), True


@contextmanager
def _locked_update_scope(root, lock_path):
    """Acquire both update locks and bind the lease to this execution context."""
    root, lock_path = checked_path(root), checked_path(lock_path)
    with _lifecycle_lock(str(root)):
        with operation_lock(lock_path):
            lease = _UpdateLease(root, lock_path)
            token = _UPDATE_LOCK_SCOPE.set(lease)
            try:
                yield
            finally:
                lease.active = False
                _UPDATE_LOCK_SCOPE.reset(token)


def _check_pointer_runtime_compatibility(old_metadata, new_metadata):
    """Pointer-v2 updates retain the signed runtime and unit recipe exactly."""
    from shared.systemd_pointer import LAUNCHER_PATH, LAUNCHER_SHA256, RECIPE_PATH, RECIPE_V2

    old = {item['path']: item for item in old_metadata['files']}
    new = {item['path']: item for item in new_metadata['files']}
    if RECIPE_PATH not in old and RECIPE_PATH not in new:
        return False
    expected_recipe = hashlib.sha256(RECIPE_V2).hexdigest()
    for entries in (old, new):
        recipe = entries.get(RECIPE_PATH)
        launcher = entries.get(LAUNCHER_PATH)
        if (recipe is None or recipe['size'] != len(RECIPE_V2) or recipe['sha256'] != expected_recipe
                or launcher is None or launcher['sha256'] != LAUNCHER_SHA256
                or 'runtime-linux/bin/python' not in entries or 'runtime-linux/runtime-v1.json' not in entries
                or any(path.startswith('systemd-user/') and path != RECIPE_PATH for path in entries)):
            raise UpdatePrepareError('Рецепт или загрузчик обновления не подтверждены')
    old_runtime = {path: (item['size'], item['sha256']) for path, item in old.items()
                   if path.startswith('runtime-linux/')}
    new_runtime = {path: (item['size'], item['sha256']) for path, item in new.items()
                   if path.startswith('runtime-linux/')}
    if old_runtime != new_runtime:
        raise UpdatePrepareError('Замена runtime пока не поддерживается')
    return True


def _expected_tree(metadata):
    files = {item['path']: item['sha256'] for item in metadata['files']}
    directories = {'.'}
    for name in files:
        parts = name.split('/')
        directories.update('/'.join(parts[:index]) for index in range(1, len(parts)))
    return directories, files


def _binding(*, root, work, old_path, new_path, old_envelope, new_envelope,
             old_key, new_key, old_transaction, old_metadata, new_metadata, plan, operation_id):
    context = {'root': str(root), 'work': str(work), 'old_path': str(old_path), 'new_path': str(new_path),
               'old_envelope': hashlib.sha256(old_envelope).hexdigest(),
               'new_envelope': hashlib.sha256(new_envelope).hexdigest(),
               'old_key': hashlib.sha256(old_key).hexdigest(), 'new_key': hashlib.sha256(new_key).hexdigest(),
               'old_transaction': old_transaction, 'old_metadata': old_metadata,
               'new_metadata': new_metadata, 'plan': plan.digest, 'operation_id': operation_id}
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':')).encode('ascii')).hexdigest()


def prepare_signed_linux_update(*, new_package: str, new_envelope: bytes, new_key: bytes,
                                old_package: str, old_envelope: bytes, old_key: bytes,
                                product_id: str, old_version: str, new_version: str,
                                install_root: str, work_dir: str, modes: tuple[str, ...],
                                migration_ids: tuple[str, ...], operation_id: str,
                                writer_lock_path: str | None = None) -> dict:
    """Проверить и подготовить payload; не останавливать службы и не запускать update.

    Рабочий каталог должен быть создан интегратором и защищён. Прерванный
    pending не повторяется автоматически; результат не подтверждает runtime,
    службы, остановку писателей или безопасность подписанного кода.
    """
    if (any(type(value) is not str or not value for value in
            (new_package, old_package, product_id, old_version, new_version, install_root, work_dir))
            or any(type(value) is not bytes or not value for value in (new_envelope, old_envelope))
            or any(type(value) is not bytes or len(value) != 32 for value in (new_key, old_key))
            or type(operation_id) is not str or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None
            or type(modes) is not tuple or not modes or any(type(value) is not str for value in modes)
            or type(migration_ids) is not tuple or any(type(value) is not str for value in migration_ids)
            or old_version == new_version):
        raise UpdatePrepareError('Недопустимые параметры подготовки')
    try:
        root, work = checked_path(install_root), checked_path(work_dir)
        new_path, old_path = checked_path(new_package), checked_path(old_package)
        if contains(root, work) or contains(work, root):
            raise UpdatePrepareError('Рабочий каталог должен быть вне установки')
        if native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}:
            raise UpdatePrepareError('Требуется Linux x86-64')
        info = work.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise UpdatePrepareError('Рабочий каталог не защищён')
        new_options = {'trusted_public_key': new_key, 'expected_product_id': product_id,
                       'expected_version': new_version}
        old_options = {'trusted_public_key': old_key, 'expected_product_id': product_id,
                       'expected_version': old_version}
        new_metadata = verify_signed_package(new_path, new_envelope, **new_options)
        old_metadata = verify_signed_package(old_path, old_envelope, **old_options)
        if (not _previous_manifest_compatible(old_metadata['manifest'], new_metadata['manifest'])
                or check_target(new_metadata['manifest'], native_target())['status'] != 'supported'
                or check_target(old_metadata['manifest'], native_target())['status'] != 'supported'):
            raise UpdatePrepareError('Подписанные выпуски несовместимы')
        if _check_pointer_runtime_compatibility(old_metadata, new_metadata):
            from shared.systemd_pointer import plan_pointer_signed_user_units
            # Validate the old signed recipe and source runtime before writing a checkpoint.
            plan_pointer_signed_user_units(old_path, old_envelope, str(root), **old_options, modes=modes)
        from shared.wheel_audit import audit_signed_wheels
        # Новый выпуск проверяется до создания checkpoint и извлечения payload.
        # Прежний выпуск не блокируем, чтобы не исключить его восстановление.
        audit_signed_wheels(new_path, new_envelope, **new_options)
        ports = FilesystemPorts(new_metadata['manifest'], root, work_dir=work, payloads={},
                                effects=None, password_provider=lambda: None)
        plan = UpdatePlan(new_metadata['manifest'], root, ports, release=new_version,
                          modes=modes, migration_ids=migration_ids, guard_effects=True,
                          writer_lock_sha256=writer_lock_digest(writer_lock_path, forbidden_roots=(
                              root, work, ports.home.app, ports.home.releases, *ports.home.roots.values())))
        old_verified = verify_fresh_release(old_path, old_envelope, str(root), **old_options)
        digest = _binding(root=root, work=work, old_path=old_path, new_path=new_path,
            old_envelope=old_envelope, new_envelope=new_envelope, old_key=old_key, new_key=new_key,
            old_transaction=old_verified['transaction'], old_metadata=old_metadata,
            new_metadata=new_metadata, plan=plan, operation_id=operation_id)
        marker = checked_path(work / ('prepare-' + operation_id + '.json'))
        destination = checked_path(work / ('payload-' + operation_id))
        pending = {'version': 1, 'operation_id': operation_id, 'binding': digest, 'status': 'pending'}
        done = {**pending, 'status': 'done'}
        def verify_again():
            if (verify_signed_package(new_path, new_envelope, **new_options) != new_metadata
                    or verify_signed_package(old_path, old_envelope, **old_options) != old_metadata
                    or verify_fresh_release(old_path, old_envelope, str(root), **old_options) != old_verified
                    or plan.status() is not None):
                raise UpdatePrepareError('Установка или пакеты изменились')
        with _lifecycle_lock(str(root)):
            with operation_lock(plan.lock_path):
                verify_again()
                record = read_effect_receipt(marker)
                if record is not None and (type(record.get('version')) is not int or record != done):
                    raise UpdatePrepareError('Предыдущая подготовка требует проверки')
                if record is None:
                    if destination.exists():
                        raise UpdatePrepareError('Каталог без отметки нельзя заменить')
                    _atomic_write(marker, pending)
                    prepare_native_signed_package(new_path, new_envelope, destination, **new_options)
                if _tree(destination) != _expected_tree(new_metadata):
                    raise UpdatePrepareError('Подготовленный выпуск отличается от пакета')
                verify_again()
                expected_record = pending if record is None else done
                if read_effect_receipt(marker) != expected_record:
                    raise UpdatePrepareError('Отметка подготовки изменилась')
                if record is None:
                    _atomic_write(marker, done)
                if read_effect_receipt(marker) != done:
                    raise UpdatePrepareError('Подготовка не подтверждена')
        return {'state': 'prepared', 'payload_name': destination.name, 'plan_sha256': plan.digest,
                'file_count': len(new_metadata['files']), 'update_started': False,
                'runtime_verified': False, 'services_verified': False}
    except Exception:
        raise UpdatePrepareError('Не удалось подтвердить подготовку обновления') from None


def verify_started_linux_update(*, new_package: str, new_envelope: bytes, new_key: bytes,
                                old_package: str, old_envelope: bytes, old_key: bytes,
                                product_id: str, old_version: str, new_version: str,
                                install_root: str, work_dir: str, modes: tuple[str, ...],
                                migration_ids: tuple[str, ...], operation_id: str,
                                writer_lock_path: str | None = None,
                                _allow_terminal: bool = False, _locks_held: bool = False) -> dict:
    """Сверить подготовку и checkpoint, не продолжая транзакцию.

    Закрытое расширение _allow_terminal применяется для наблюдения результата;
    сам checkpoint не подтверждает текущее здоровье продукта.
    """
    try:
        if (type(_allow_terminal) is not bool or type(_locks_held) is not bool
                or type(operation_id) is not str or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None
                or type(old_key) is not bytes or len(old_key) != 32
                or type(new_key) is not bytes or len(new_key) != 32
                or type(old_envelope) is not bytes or not old_envelope
                or type(new_envelope) is not bytes or not new_envelope
                or type(old_version) is not str or type(new_version) is not str or old_version == new_version
                or type(modes) is not tuple or not modes or type(migration_ids) is not tuple):
            raise UpdatePrepareError('Некорректные параметры')
        root, work = checked_path(install_root), checked_path(work_dir)
        old_path, new_path = checked_path(old_package), checked_path(new_package)
        if contains(root, work) or contains(work, root):
            raise UpdatePrepareError('Рабочий каталог пересекает установку')
        info = work.lstat()
        if (native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise UpdatePrepareError('Среда не подтверждена')
        old_options = {'trusted_public_key': old_key, 'expected_product_id': product_id,
                       'expected_version': old_version}
        new_options = {'trusted_public_key': new_key, 'expected_product_id': product_id,
                       'expected_version': new_version}
        old_metadata = verify_signed_package(old_path, old_envelope, **old_options)
        new_metadata = verify_signed_package(new_path, new_envelope, **new_options)
        if (not _previous_manifest_compatible(old_metadata['manifest'], new_metadata['manifest'])
                or any(check_target(meta['manifest'], native_target())['status'] != 'supported'
                       for meta in (old_metadata, new_metadata))):
            raise UpdatePrepareError('Выпуски несовместимы')
        ports = FilesystemPorts(new_metadata['manifest'], root, work_dir=work, payloads={},
                                effects=None, password_provider=lambda: None)
        plan = UpdatePlan(new_metadata['manifest'], root, ports, release=new_version,
                          modes=modes, migration_ids=migration_ids, guard_effects=True,
                          writer_lock_sha256=writer_lock_digest(writer_lock_path, forbidden_roots=(
                              root, work, ports.home.app, ports.home.releases, *ports.home.roots.values())))
        marker = checked_path(work / ('prepare-' + operation_id + '.json'))
        destination = checked_path(work / ('payload-' + operation_id))
        if _locks_held:
            lease = _UPDATE_LOCK_SCOPE.get()
            if (not isinstance(lease, _UpdateLease) or not lease.active
                    or lease.root != str(root) or lease.lock_path != str(plan.lock_path)):
                raise UpdatePrepareError('Блокировка не подтверждена')
        with nullcontext() if _locks_held else _locked_update_scope(root, plan.lock_path):
            fresh = status_fresh_native(old_path, old_envelope, str(root), **old_options)
            state = plan.status()
            if (fresh['reported_status'] != 'prepared' or state is None
                    or state['old_release'] != old_version
                    or (state['outcome'] != 'running' and not _allow_terminal)):
                raise UpdatePrepareError('Нет начатого обновления')
            digest = _binding(root=root, work=work, old_path=old_path, new_path=new_path,
                old_envelope=old_envelope, new_envelope=new_envelope, old_key=old_key, new_key=new_key,
                old_transaction=fresh['transaction'], old_metadata=old_metadata,
                new_metadata=new_metadata, plan=plan, operation_id=operation_id)
            done = {'version': 1, 'operation_id': operation_id, 'binding': digest, 'status': 'done'}
            receipt = read_effect_receipt(marker)
            current = ports.current_release()
            if (receipt is None or type(receipt.get('version')) is not int or receipt != done
                    or _tree(destination) != _expected_tree(new_metadata)
                    or _tree(ports.release_path(old_version)) != _expected_tree(old_metadata)
                    or current not in (old_version, new_version)):
                raise UpdatePrepareError('Подготовка или установка изменились')
            new_release = ports.release_path(new_version)
            if new_release.exists() and _tree(new_release) != _expected_tree(new_metadata):
                raise UpdatePrepareError('Новый выпуск изменился')
            if ((state['outcome'] == 'updated' and (current != new_version or not new_release.is_dir()))
                    or (state['outcome'] == 'rolled_back' and current != old_version)):
                raise UpdatePrepareError('Результат обновления не соответствует указателю')
            if (plan.status() != state or ports.current_release() != current
                    or read_effect_receipt(marker) != done
                    or status_fresh_native(old_path, old_envelope, str(root), **old_options) != fresh
                    or verify_signed_package(new_path, new_envelope, **new_options) != new_metadata):
                raise UpdatePrepareError('Состояние изменилось во время проверки')
        return {'state': state, 'payload_name': destination.name, 'plan_sha256': plan.digest}
    except Exception:
        raise UpdatePrepareError('Не удалось подтвердить начатое обновление') from None
