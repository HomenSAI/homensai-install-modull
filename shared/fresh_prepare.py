# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальная подготовка первой нативной установки без запуска продукта.

Начало требует отсутствующего корня. Частичный корень сохраняется; отдельное
возобновление допускает только проверенное состояние. Код продукта не запускается.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import uuid

from shared.adapters_fs import FilesystemPorts, ReleaseSource, _tree
from shared.archive import ArchiveError
from shared.datahome import DataHomeError, checked_path, contains, resolve_datahome
from shared.host_target import HostTargetError, native_target
from shared.manifest import ManifestError, check_target
from shared.package import PackageError, _files
from shared.package_prepare import prepare_native_signed_package
from shared.package_signing import verify_signed_package
from shared.preflight import MAX_BYTES, NAMES, PreflightError, _nearest_directory, preflight_fresh
from shared.update_plan import UpdateError, _atomic_write, _object, operation_lock


class FreshPrepareError(RuntimeError):
    """Свежий выпуск не подготовлен; подробности входа не раскрываются."""


class FreshConflict(FreshPrepareError):
    """Идентификатор операции уже связан с другим запросом."""


def _plan(root, metadata, key, target):
    value = {'root': str(root), 'key': hashlib.sha256(key).hexdigest(),
             'metadata': metadata, 'target': target}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def _request_hash(package_path, envelope, required_bytes, reserve_bytes):
    if (type(envelope) is not bytes or type(required_bytes) is not dict
            or set(required_bytes) != set(NAMES)
            or any(type(value) is not int or not 0 <= value <= MAX_BYTES for value in required_bytes.values())
            or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= MAX_BYTES):
        raise FreshPrepareError('Некорректный запрос подготовки')
    value = {'package_path': str(checked_path(package_path)), 'envelope': hashlib.sha256(envelope).hexdigest(),
             'required_bytes': required_bytes, 'reserve_bytes': reserve_bytes}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def _layout(manifest, root):
    home = resolve_datahome(manifest, root)
    paths = (home.app, home.releases, *home.roots.values())
    control = checked_path(root / '.homensai-control')
    marker = checked_path(root / '.homensai-fresh.json')
    bootstrap = checked_path(root / '.homensai-bootstrap.lock')
    if any(path == root or not contains(root, path)
           or contains(path, control) or contains(control, path)
           or contains(path, marker) or contains(marker, path)
           or contains(path, bootstrap) or contains(bootstrap, path) for path in paths):
        raise FreshPrepareError('Пути выходят за пределы нового корня или пересекают управление')
    return home, paths, control, marker


def _check_fresh_storage(package_path, metadata, root, reserve_bytes):
    """Нижняя оценка для snapshot и двух файловых копий до новых записей."""
    package = checked_path(package_path)
    info = package.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise FreshPrepareError('Пакет недоступен')
    payload_bytes = sum(item['size'] for item in metadata['files'])
    # package_prepare reserves twice the ZIP size plus 4 MiB, while staging
    # temporarily holds both the prepared copy and the installed release.
    minimum = max(info.st_size * 2 + 4 * 1024 * 1024, payload_bytes * 2)
    needed = minimum + reserve_bytes
    if needed > MAX_BYTES:
        raise FreshPrepareError('Недопустимая оценка места')
    free = shutil.disk_usage(_nearest_directory(root)).free
    if type(free) is not int or free < needed:
        raise FreshPrepareError('Недостаточно места для подготовки')
    return {'minimum_bytes': minimum, 'reserve_bytes': reserve_bytes, 'free_bytes': free}


def prepare_fresh_native(package_path, envelope, install_root, *, trusted_public_key,
                         expected_product_id, expected_version, required_bytes, reserve_bytes,
                         transaction_id=None, request_hash=None):
    """Подготовить каталоги и указатель из подписанного пакета; код не запускать.

    Не создаёт службы, runtime или БД и не подтверждает пригодность продукта.
    Частичные каталоги не удаляются и не используются повторно автоматически.
    """
    try:
        if ((transaction_id is None) != (request_hash is None)
                or (transaction_id is not None and (type(transaction_id) is not str
                    or re.fullmatch(r'[a-f0-9]{32}', transaction_id) is None
                    or type(request_hash) is not str or re.fullmatch(r'[a-f0-9]{64}', request_hash) is None))):
            raise FreshPrepareError('Некорректный идентификатор запроса')
        root = checked_path(install_root)
        if root == Path(root.anchor) or root.exists() or not root.parent.is_dir():
            raise FreshPrepareError('Требуется новый корень установки')
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        metadata = verify_signed_package(package_path, envelope, **options)
        manifest = metadata['manifest']
        target = native_target()
        if manifest['schema_version'] != 2 or check_target(manifest, target)['status'] != 'supported':
            raise FreshPrepareError('Платформа не подтверждена')
        preflight_fresh(manifest, root, required_bytes=required_bytes, reserve_bytes=reserve_bytes)
        _check_fresh_storage(package_path, metadata, root, reserve_bytes)
        home, paths, control, marker = _layout(manifest, root)
        root.mkdir(mode=0o700)
        with operation_lock(root / '.homensai-bootstrap.lock'):
            _check_fresh_storage(package_path, metadata, root, reserve_bytes)
            transaction = transaction_id or uuid.uuid4().hex
            state = {'format': 4 if transaction_id else 3, 'transaction': transaction, 'product_id': expected_product_id,
                     'version': expected_version, 'status': 'preparing',
                     'plan': _plan(root, metadata, trusted_public_key, target)}
            if transaction_id:
                state['request_hash'] = request_hash
            _atomic_write(marker, state)
            control.mkdir(mode=0o700)
            prepared = control / 'prepared'
            receipt = prepare_native_signed_package(package_path, envelope, prepared, **options)
            inventory = {item['path']: item['sha256'] for item in metadata['files']}
            if receipt['manifest'] != manifest or receipt['files'] != inventory or receipt['process_target'] != target:
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


def _read_state(marker, plan, product_id, version, *, statuses=('preparing',)):
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192:
        raise FreshPrepareError('Журнал операции недоступен')
    with marker.open('rb') as stream:
        state = json.loads(stream.read(8193).decode('utf-8'), object_pairs_hook=_object)
    if (type(state) is not dict or type(state.get('format')) is not int
            or state['format'] not in (3, 4)
            or set(state) != ({'format', 'transaction', 'product_id', 'version', 'status', 'plan'}
                              | ({'request_hash'} if state['format'] == 4 else set()))
            or (state['format'] == 4 and (type(state['request_hash']) is not str
                or re.fullmatch(r'[a-f0-9]{64}', state['request_hash']) is None))
            or type(state['transaction']) is not str
            or re.fullmatch(r'[a-f0-9]{32}', state['transaction']) is None
            or state['product_id'] != product_id or state['version'] != version
            or state['status'] not in statuses or state['plan'] != plan):
        raise FreshPrepareError('Журнал не соответствует проверенному выпуску')
    return state


def status_fresh_native(package_path, envelope, install_root, *, trusted_public_key,
                        expected_product_id, expected_version):
    """Прочитать журнал после проверки пакета; не подтверждает целостность установки."""
    try:
        root = checked_path(install_root)
        if root == Path(root.anchor) or not root.is_dir():
            raise FreshPrepareError('Корень недоступен')
        metadata = verify_signed_package(package_path, envelope, trusted_public_key=trusted_public_key,
                                         expected_product_id=expected_product_id,
                                         expected_version=expected_version)
        manifest, target = metadata['manifest'], native_target()
        if manifest['schema_version'] != 2 or check_target(manifest, target)['status'] != 'supported':
            raise FreshPrepareError('Платформа не подтверждена')
        _, _, _, marker = _layout(manifest, root)
        plan = _plan(root, metadata, trusted_public_key, target)
        state = _read_state(marker, plan, expected_product_id, expected_version,
                            statuses=('preparing', 'prepared'))
        return {'transaction': state['transaction'], 'product_id': state['product_id'],
                'version': state['version'], 'reported_status': state['status'],
                'completion_verified': False}
    except (PackageError, DataHomeError, HostTargetError, ManifestError, UpdateError,
            OSError, ValueError, KeyError, TypeError, RecursionError):
        raise FreshPrepareError('Не удалось прочитать состояние подготовки') from None


def verify_fresh_release(package_path, envelope, install_root, *, trusted_public_key,
                         expected_product_id, expected_version):
    """Сверить файлы и указатель с пакетом; не проверяет работу приложения."""
    try:
        root = checked_path(install_root)
        if root == Path(root.anchor) or not root.is_dir():
            raise FreshPrepareError('Корень недоступен')
        metadata = verify_signed_package(package_path, envelope, trusted_public_key=trusted_public_key,
                                         expected_product_id=expected_product_id,
                                         expected_version=expected_version)
        manifest, target = metadata['manifest'], native_target()
        if manifest['schema_version'] != 2 or check_target(manifest, target)['status'] != 'supported':
            raise FreshPrepareError('Платформа не подтверждена')
        home, _, control, marker = _layout(manifest, root)
        plan = _plan(root, metadata, trusted_public_key, target)
        state = _read_state(marker, plan, expected_product_id, expected_version, statuses=('prepared',))
        ports = FilesystemPorts(manifest, root, work_dir=control / 'work', payloads={},
                                effects=None, password_provider=lambda: None)
        pointer = checked_path(ports.pointer)
        info = pointer.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192:
            raise FreshPrepareError('Указатель выпуска недоступен')
        with pointer.open('rb') as stream:
            raw_pointer = stream.read(8193)
        if json.loads(raw_pointer.decode('utf-8'), object_pairs_hook=_object) != {'release': expected_version}:
            raise FreshPrepareError('Указатель выпуска не совпадает')
        release = ports.release_path(expected_version)
        # The signed inventory is authoritative for this release, including
        # older crypto versions; today's builder lock is not its policy.
        entries, signatures = _files(release, verify_crypto_block=False)
        if entries != metadata['files']:
            raise FreshPrepareError('Файлы выпуска не совпадают')
        expected_dirs = {''}
        for item in metadata['files']:
            parts = item['path'].split('/')
            expected_dirs.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        if {name for name, signature in signatures.items() if stat.S_ISDIR(signature[2])} != expected_dirs:
            raise FreshPrepareError('Каталоги выпуска не совпадают')
        with pointer.open('rb') as stream:
            final_pointer = stream.read(8193)
        if (_read_state(marker, plan, expected_product_id, expected_version, statuses=('prepared',)) != state
                or final_pointer != raw_pointer):
            raise FreshPrepareError('Состояние изменилось во время проверки')
        return {'transaction': state['transaction'], 'product_id': state['product_id'],
                'version': state['version'], 'release_files_match': True,
                'installation_complete': False, 'process_target': target}
    except (PackageError, ArchiveError, DataHomeError, HostTargetError, ManifestError, UpdateError,
            OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError):
        raise FreshPrepareError('Не удалось проверить подготовленный выпуск') from None


def _check_lock(path):
    """Не открывать на запись чужой, связанный или повреждённый файл блокировки."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192:
        raise FreshPrepareError('Служебная блокировка недоступна')
    with path.open('rb') as stream:
        raw = stream.read(8193)
    if raw[:1] != b'\0':
        raise FreshPrepareError('Неизвестный формат блокировки')
    lock = json.loads(raw[1:].decode('utf-8'), object_pairs_hook=_object)
    if (type(lock) is not dict or set(lock) != {'version', 'status', 'pid'}
            or type(lock['version']) is not int or lock['version'] != 1
            or lock['status'] not in ('held', 'released')
            or type(lock['pid']) is not int or lock['pid'] <= 0):
        raise FreshPrepareError('Неизвестный формат блокировки')


def _check_resume_files(home, ports, prepared, inventory, version):
    expected_dirs = {'.'}
    for name in inventory:
        parts = name.split('/')
        expected_dirs.update('/'.join(parts[:index]) for index in range(1, len(parts)))
    if not prepared.is_dir() or _tree(prepared) != (expected_dirs, inventory):
        raise FreshPrepareError('Подготовленная копия повреждена или неполна')
    for root in (home.app, home.roots['data'], home.roots['backup'],
                 home.roots['config'], home.roots['runtime']):
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise FreshPrepareError('Каталоги продукта не пусты')
    release = ports.release_path(version)
    if home.releases.exists():
        if not home.releases.is_dir() or any(p != release for p in home.releases.iterdir()):
            raise FreshPrepareError('Обнаружены посторонние или неполные выпуски')
        if release.exists() and _tree(release) != (expected_dirs, inventory):
            raise FreshPrepareError('Размещённый выпуск повреждён')
    services = home.roots['services']
    if services.exists():
        dirs, files = _tree(services)
        allowed_files = {ports.lock.relative_to(services).as_posix(), ports.pointer.relative_to(services).as_posix()}
        allowed_dirs = {'.'}
        for name in allowed_files:
            parts = name.split('/')
            allowed_dirs.update('/'.join(parts[:index]) for index in range(1, len(parts)))
        if set(files) - allowed_files or dirs - allowed_dirs:
            raise FreshPrepareError('Обнаружены посторонние служебные данные')
        for item in (ports.lock, ports.pointer):
            if item.exists():
                info = item.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192:
                    raise FreshPrepareError('Служебный файл недоступен')
        if ports.lock.exists():
            _check_lock(ports.lock)
        if ports.pointer.exists():
            with ports.pointer.open('rb') as stream:
                pointer = json.loads(stream.read(8193).decode('utf-8'), object_pairs_hook=_object)
            if pointer != {'release': version} or not release.is_dir():
                raise FreshPrepareError('Указатель выпуска не соответствует операции')


def resume_fresh_native(package_path, envelope, install_root, *, trusted_public_key,
                        expected_product_id, expected_version, reserve_bytes=64 * 1024 * 1024,
                        expected_transaction=None, expected_request_hash=None):
    """Продолжить после проверки журнала и полного подписанного payload.

    Не удаляет неполные staging-каталоги, не запускает код и не повторяет
    готовую операцию. Старые журналы форматов 1 и 2 не принимаются.
    """
    try:
        if type(reserve_bytes) is not int or not 0 <= reserve_bytes <= MAX_BYTES:
            raise FreshPrepareError('Недопустимый резерв места')
        root = checked_path(install_root)
        if root == Path(root.anchor) or not root.is_dir():
            raise FreshPrepareError('Корень недоступен')
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        metadata = verify_signed_package(package_path, envelope, **options)
        manifest, target = metadata['manifest'], native_target()
        if manifest['schema_version'] != 2 or check_target(manifest, target)['status'] != 'supported':
            raise FreshPrepareError('Платформа не подтверждена')
        home, paths, control, marker = _layout(manifest, root)
        plan = _plan(root, metadata, trusted_public_key, target)
        state = _read_state(marker, plan, expected_product_id, expected_version)
        if expected_transaction is not None and (state.get('format') != 4
                or state['transaction'] != expected_transaction or state['request_hash'] != expected_request_hash):
            raise FreshPrepareError('Запрос не соответствует операции')
        bootstrap = checked_path(root / '.homensai-bootstrap.lock')
        _check_lock(bootstrap)
        with operation_lock(bootstrap):
            if _read_state(marker, plan, expected_product_id, expected_version) != state:
                raise FreshPrepareError('Состояние изменилось')
            prepared = checked_path(control / 'prepared')
            inventory = {item['path']: item['sha256'] for item in metadata['files']}
            if not prepared.exists():
                # Продуктовые пути обязаны оставаться свежими; посторонние данные не трогаем.
                preflight_fresh(manifest, root, required_bytes={name: 0 for name in NAMES},
                                reserve_bytes=reserve_bytes)
                control.mkdir(mode=0o700, exist_ok=True)
                receipt = prepare_native_signed_package(package_path, envelope, prepared, **options)
                if (receipt['manifest'] != manifest or receipt['files'] != inventory
                        or receipt['process_target'] != target):
                    raise FreshPrepareError('Пакет изменился во время подготовки')
            ports = FilesystemPorts(manifest, root, work_dir=control / 'work',
                                    payloads={expected_version: ReleaseSource(prepared, inventory)},
                                    effects=None, password_provider=lambda: None)
            _check_resume_files(home, ports, prepared, inventory, expected_version)
            checked_path(home.roots['services']).mkdir(parents=True, exist_ok=True, mode=0o700)
            with operation_lock(ports.lock):
                if _read_state(marker, plan, expected_product_id, expected_version) != state:
                    raise FreshPrepareError('Состояние изменилось')
                _check_resume_files(home, ports, prepared, inventory, expected_version)
                release = ports.release_path(expected_version)
                needed = 0 if release.exists() else sum(item['size'] for item in metadata['files'])
                if needed + reserve_bytes > MAX_BYTES or shutil.disk_usage(_nearest_directory(home.releases)).free < needed + reserve_bytes:
                    raise FreshPrepareError('Недостаточно места для продолжения')
                for path in paths:
                    checked_path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
                transaction = state['transaction']
                ports.stage_release(transaction + '/stage', expected_version)
                ports.switch_pointer(transaction + '/switch', expected_version)
                if ports.current_release() != expected_version:
                    raise FreshPrepareError('Указатель выпуска не подтверждён')
                state['status'] = 'prepared'
                _atomic_write(marker, state)
        return {**state, 'process_target': target}
    except (PackageError, DataHomeError, HostTargetError, ManifestError, PreflightError,
            UpdateError, OSError, ValueError, KeyError, TypeError, RecursionError):
        raise FreshPrepareError('Не удалось продолжить подготовку') from None


def prepare_idempotent_native(package_path, envelope, install_root, *, trusted_public_key,
                              expected_product_id, expected_version, required_bytes, reserve_bytes,
                              operation_id):
    """Начать или повторить ту же подготовку; другие запросы с ключом запрещены."""
    try:
        if type(operation_id) is not str or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None:
            raise FreshPrepareError('Некорректный идентификатор операции')
        request_hash = _request_hash(package_path, envelope, required_bytes, reserve_bytes)
        root = checked_path(install_root)
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        if not root.exists():
            return prepare_fresh_native(package_path, envelope, root, **options,
                                        required_bytes=required_bytes, reserve_bytes=reserve_bytes,
                                        transaction_id=operation_id, request_hash=request_hash)
        if root == Path(root.anchor) or not root.is_dir():
            raise FreshPrepareError('Корень недоступен')
        metadata = verify_signed_package(package_path, envelope, **options)
        manifest, target = metadata['manifest'], native_target()
        if manifest['schema_version'] != 2 or check_target(manifest, target)['status'] != 'supported':
            raise FreshPrepareError('Платформа не подтверждена')
        _, _, _, marker = _layout(manifest, root)
        plan = _plan(root, metadata, trusted_public_key, target)
        state = _read_state(marker, plan, expected_product_id, expected_version,
                            statuses=('preparing', 'prepared'))
        if (state['format'] != 4 or state['transaction'] != operation_id
                or state['request_hash'] != request_hash):
            raise FreshConflict('Запрос не соответствует операции')
        if state['status'] == 'preparing':
            return resume_fresh_native(package_path, envelope, root, **options, reserve_bytes=reserve_bytes,
                                       expected_transaction=operation_id, expected_request_hash=request_hash)
        bootstrap = checked_path(root / '.homensai-bootstrap.lock')
        _check_lock(bootstrap)
        with operation_lock(bootstrap):
            current = _read_state(marker, plan, expected_product_id, expected_version,
                                  statuses=('prepared',))
            if current != state:
                raise FreshPrepareError('Состояние изменилось')
            verified = verify_fresh_release(package_path, envelope, root, **options)
            if verified['transaction'] != state['transaction'] or verified['process_target'] != target:
                raise FreshPrepareError('Выпуск не соответствует операции')
        return {**state, 'process_target': target}
    except (PackageError, DataHomeError, HostTargetError, ManifestError, PreflightError,
            UpdateError, OSError, ValueError, KeyError, TypeError, RecursionError):
        raise FreshPrepareError('Не удалось подготовить выпуск') from None
