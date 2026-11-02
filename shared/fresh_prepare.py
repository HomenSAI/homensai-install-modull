# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальная подготовка первой нативной установки без запуска продукта.

Корень должен отсутствовать. При любой ошибке частичный корень сохраняется;
автоматического удаления, восстановления или исполнения продуктового кода нет.
"""
from __future__ import annotations

from pathlib import Path
import uuid

from shared.adapters_fs import FilesystemPorts, ReleaseSource
from shared.datahome import DataHomeError, checked_path, contains, resolve_datahome
from shared.host_target import HostTargetError, native_target
from shared.manifest import ManifestError, check_target
from shared.package import PackageError
from shared.package_prepare import prepare_native_signed_package
from shared.package_signing import verify_signed_package
from shared.preflight import PreflightError, preflight_fresh
from shared.update_plan import UpdateError, _atomic_write, operation_lock


class FreshPrepareError(RuntimeError):
    """Свежий выпуск не подготовлен; подробности входа не раскрываются."""


def prepare_fresh_native(package_path, envelope, install_root, *, trusted_public_key,
                         expected_product_id, expected_version, required_bytes, reserve_bytes):
    """Подготовить каталоги и указатель из подписанного пакета; код не запускать.

    Не создаёт службы, runtime или БД и не подтверждает пригодность продукта.
    Частичные каталоги не удаляются и не используются повторно автоматически.
    """
    try:
        root = checked_path(install_root)
        if root == Path(root.anchor) or root.exists() or not root.parent.is_dir():
            raise FreshPrepareError('Требуется новый корень установки')
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        metadata = verify_signed_package(package_path, envelope, **options)
        manifest = metadata['manifest']
        if manifest['schema_version'] != 2 or check_target(manifest, native_target())['status'] != 'supported':
            raise FreshPrepareError('Платформа не подтверждена')
        preflight_fresh(manifest, root, required_bytes=required_bytes, reserve_bytes=reserve_bytes)
        home = resolve_datahome(manifest, root)
        paths = (home.app, home.releases, *home.roots.values())
        control = checked_path(root / '.homensai-control')
        marker = checked_path(root / '.homensai-fresh.json')
        if any(path == root or not contains(root, path)
               or contains(path, control) or contains(control, path)
               or contains(path, marker) or contains(marker, path) for path in paths):
            raise FreshPrepareError('Пути выходят за пределы нового корня или пересекают управление')
        root.mkdir(mode=0o700)
        transaction = uuid.uuid4().hex
        state = {'format': 1, 'transaction': transaction, 'product_id': expected_product_id,
                 'version': expected_version, 'status': 'preparing'}
        _atomic_write(marker, state)
        control.mkdir(mode=0o700)
        prepared = control / 'prepared'
        receipt = prepare_native_signed_package(package_path, envelope, prepared, **options)
        inventory = {item['path']: item['sha256'] for item in metadata['files']}
        if receipt['manifest'] != manifest or receipt['files'] != inventory:
            raise FreshPrepareError('Пакет изменился во время подготовки')
        # После создания корня повторяем проверку до создания путей продукта.
        preflight_fresh(manifest, root, required_bytes=required_bytes, reserve_bytes=reserve_bytes)
        for path in paths:
            checked_path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Никакое резервное копирование здесь не вызывается; provider не выдаёт пароль.
        ports = FilesystemPorts(manifest, root, work_dir=control / 'work',
                                payloads={expected_version: ReleaseSource(prepared, inventory)},
                                effects=None, password_provider=lambda: None)
        with operation_lock(home.roots['services'] / 'operation.lock'):
            ports.stage_release(transaction + '/stage', expected_version)
            ports.switch_pointer(transaction + '/switch', expected_version)
            if ports.current_release() != expected_version:
                raise FreshPrepareError('Указатель выпуска не подтверждён')
            state['status'] = 'prepared'
            _atomic_write(marker, state)
        return {**state, 'process_target': receipt['process_target']}
    except (PackageError, DataHomeError, HostTargetError, ManifestError, PreflightError,
            UpdateError, OSError, ValueError, KeyError, TypeError, RecursionError):
        raise FreshPrepareError('Не удалось подготовить свежий выпуск') from None
