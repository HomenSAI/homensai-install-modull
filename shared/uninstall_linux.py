# HomenS.AI · © 2026 Serhii Khomenko
"""Удаление Linux user-systemd установки; данные сохраняются по умолчанию.

Владение подтверждается подписанным пакетом первой установки: журнал первой
установки должен соответствовать пакету, а unit-файлы на диске — побайтно
совпадать с отрисованными из него. Удаляются только объявленные каталоги
программы, служебные файлы установщика и собственные units. Безопасное
стирание носителя не выполняется и не заявляется.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
import re
import shutil
import stat

from shared.datahome import checked_path, contains, resolve_datahome
from shared.fresh_prepare import status_fresh_native
from shared.host_target import native_target
from shared.manifest import check_target
from shared.package_signing import verify_signed_package
from shared.services import component_order, render_pointer_user_services_v2, render_portable_user_services_v1
from shared.systemd_pointer import RECIPE_PATH as POINTER_RECIPE, RECIPE_V2
from shared.systemd_portable import RECIPE_PATH as PORTABLE_RECIPE, RECIPE_V1
from shared.systemd_user import _PROPERTIES, _run
from shared.systemd_user_publish import _manager_environment, _read_unit, _unit_directory
from shared.update_plan import _atomic_write, _object, operation_lock

RECEIPT_NAME = '.homensai-uninstall.json'
_MAX_RECEIPT = 8192
_KEY = re.compile(r'[a-f0-9]{32}\Z')
_PROGRAM = ('runtime', 'services')
_KEPT = ('data', 'backup', 'config')
_SYSTEMCTL = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']


class UninstallError(RuntimeError):
    """Удаление не подтверждено; квитанция сохраняется для повторного вызова."""

    def __init__(self, code='uninstall_failed'):
        super().__init__('Не удалось подтвердить удаление')
        self.code = code


def _expected_units(metadata, root, modes, version):
    entries = {item['path']: item for item in metadata['files']}
    recipes = [name for name in entries if name.startswith('systemd-user/')]
    manifest = metadata['manifest']
    if recipes == [POINTER_RECIPE] and entries[POINTER_RECIPE]['sha256'] == hashlib.sha256(RECIPE_V2).hexdigest():
        generator = 'homensai-systemd-user-pointer-v2'
        units = render_pointer_user_services_v2(manifest, modes=modes, install_root=str(root))
    elif recipes == [PORTABLE_RECIPE] and entries[PORTABLE_RECIPE]['sha256'] == hashlib.sha256(RECIPE_V1).hexdigest():
        generator = 'homensai-systemd-user-portable-v1'
        units = render_portable_user_services_v1(manifest, modes=modes, install_root=str(root), version=version)
    else:
        raise UninstallError()
    names = [manifest['product_id'] + '-' + name + '.service' for name in component_order(manifest, modes)]
    if list(units) != names:
        raise UninstallError()
    return generator, {name: units[name].encode('utf-8') for name in names}


def _layout(manifest, root, data_policy):
    home = resolve_datahome(manifest, root)
    program = [home.app, home.releases, *(home.roots[name] for name in _PROGRAM if name in home.roots)]
    kept = [home.roots[name] for name in _KEPT if name in home.roots]
    removed = program + [root / '.homensai-control', root / '.homensai-fresh.json']
    if data_policy == 'purge':
        removed, kept = removed + kept, []
    special = (root / RECEIPT_NAME, root / '.homensai-bootstrap.lock')
    for path in (*removed, *kept):
        if (path == root or not contains(root, path)
                or any(contains(path, item) or contains(item, path) for item in special)):
            raise UninstallError()
    if any(contains(a, b) or contains(b, a) for a in removed for b in kept):
        raise UninstallError()
    return home, removed, kept


def _binding(**value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def _read_receipt(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > _MAX_RECEIPT):
            raise UninstallError()
        data = stream.read(_MAX_RECEIPT + 1)
    value = json.loads(data.decode('utf-8'), object_pairs_hook=_object)
    if (type(value) is not dict
            or set(value) != {'version', 'operation_id', 'product_id', 'binding', 'status', 'preserved', 'release'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['operation_id']) is not str or _KEY.fullmatch(value['operation_id']) is None
            or type(value['product_id']) is not str
            or type(value['binding']) is not str or re.fullmatch(r'[a-f0-9]{64}', value['binding']) is None
            or value['status'] not in ('pending', 'done') or type(value['preserved']) is not list
            or any(type(item) is not str for item in value['preserved'])
            or (value['release'] is not None and (type(value['release']) is not str
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value['release']) is None))):
        raise UninstallError()
    return value


def _active_release(home, manifest):
    """Версия из указателя; при повреждённом указателе переустановка не разрешается."""
    pointer = checked_path(home.roots['services'] / manifest['update']['pointer_file'].replace('\\', '/'))
    try:
        descriptor = os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            value = json.loads(stream.read(8193).decode('utf-8'), object_pairs_hook=_object)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    release = value.get('release') if type(value) is dict and set(value) == {'release'} else None
    if (type(release) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', release) is None
            or not (home.releases / ('app-' + release)).is_dir()):
        return None
    return release


def _write_receipt(path, value):
    _atomic_write(path, value)
    path.chmod(0o600)
    if _read_receipt(path) != value:
        raise UninstallError()


def _show(runner, name, timeout):
    output = runner(_SYSTEMCTL + ['show', '--property=' + ','.join(_PROPERTIES), '--', name],
                    timeout=timeout, env=_manager_environment())
    if type(output) is not str:
        raise UninstallError()
    values = {}
    for line in output.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key not in _PROPERTIES or key in values:
            raise UninstallError()
        values[key] = value
    if set(values) != set(_PROPERTIES) or values['Id'] != name or values['DropInPaths']:
        raise UninstallError()
    return values


def _update_idle(home, manifest):
    """Незавершённое обновление запрещает удаление: его откат требует файлов программы."""
    state = checked_path(home.roots['services'] / manifest['update']['state_file'].replace('\\', '/'))
    try:
        descriptor = os.open(state, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UninstallError('update_in_progress')
        data = stream.read(1024 * 1024 + 1)
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise UninstallError('update_in_progress') from None
    if (type(value) is not dict or value.get('outcome') not in ('updated', 'rolled_back')
            or 'indeterminate' in value):
        raise UninstallError('update_in_progress')


def _remove(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if info.st_uid != os.getuid():
        raise UninstallError()
    if stat.S_ISDIR(info.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise UninstallError()
        shutil.rmtree(path)
    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        path.unlink()
    else:
        raise UninstallError()


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def uninstall_portable_linux(package_path, envelope, install_root, *, trusted_public_key, expected_product_id,
                             expected_version, modes, unit_dir, operation_id, data_policy='keep',
                             confirm_product_id=None, execute=False, runner=None, timeout=30):
    """Остановить службы и удалить программу; данные, копии и настройки — только при purge.

    Пакет — тот, которым выполнена первая установка в этот корень: после
    pointer-обновлений units по-прежнему отрисованы из него. Повтор с тем же
    operation_id продолжает прерванное удаление. Писатели вне собственных
    units, ключ издателя и локальный код установщика должны быть доверены
    вызывающей стороной; процессы вне systemd этим вызовом не останавливаются.
    """
    try:
        if (type(execute) is not bool or type(timeout) not in (int, float) or not 0 < timeout <= 120
                or (runner is not None and not callable(runner))
                or type(operation_id) is not str or _KEY.fullmatch(operation_id) is None
                or data_policy not in ('keep', 'purge') or type(data_policy) is not str
                or type(modes) is not tuple or not modes or any(type(item) is not str for item in modes)
                or len(set(modes)) != len(modes)
                or (data_policy == 'purge') != (confirm_product_id is not None)
                or (confirm_product_id is not None and confirm_product_id != expected_product_id)):
            raise UninstallError('invalid_request')
        if native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}:
            raise UninstallError()
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        metadata = verify_signed_package(package_path, envelope, **options)
        manifest = metadata['manifest']
        if manifest['schema_version'] != 2 or check_target(manifest, native_target())['status'] != 'supported':
            raise UninstallError()
        root, directory = checked_path(install_root), _unit_directory(unit_dir)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UninstallError()
        home, removed, kept = _layout(manifest, root, data_policy)
        generator, units = _expected_units(metadata, root, modes, expected_version)
        digests = {name: hashlib.sha256(body).hexdigest() for name, body in units.items()}
        binding = _binding(product_id=expected_product_id, version=expected_version,
                           envelope=hashlib.sha256(envelope).hexdigest(),
                           key=hashlib.sha256(trusted_public_key).hexdigest(), root=str(root),
                           unit_dir=str(directory), modes=list(modes), data_policy=data_policy, units=digests)
        receipt_path = checked_path(root / RECEIPT_NAME)
        preserved = [path.relative_to(root).as_posix() for path in kept]
        identity = {'version': 1, 'operation_id': operation_id, 'product_id': expected_product_id,
                    'binding': binding, 'preserved': preserved}

        def check_ownership():
            receipt = _read_receipt(receipt_path)
            if receipt is None:
                status_fresh_native(package_path, envelope, str(root), **options)
                _update_idle(home, manifest)
            elif {key: value for key, value in receipt.items() if key not in ('status', 'release')} != identity:
                raise UninstallError('uninstall_conflict')
            for name, digest in digests.items():
                path = directory / name
                if os.path.lexists(path):
                    _read_unit(path, digest)
            return receipt

        receipt = check_ownership()
        public = {'state': 'planned', 'generator': generator, 'units': list(units),
                  'removed_paths': [path.relative_to(root).as_posix() for path in removed],
                  'preserved_paths': preserved, 'data_policy': data_policy,
                  'services_stopped': False, 'process_identity_verified': False, 'secure_erase': False}
        if not execute:
            return public
        if receipt is not None and receipt['status'] == 'done' and not any(os.path.lexists(path) for path in removed) \
                and not any(os.path.lexists(directory / name) for name in units):
            return {**public, 'state': 'uninstalled', 'services_stopped': True}
        runner = _run if runner is None else runner
        with ExitStack() as stack:
            stale = stack.enter_context(operation_lock(root / '.homensai-bootstrap.lock'))
            services = home.roots['services']
            if services.is_dir():
                stale = stack.enter_context(operation_lock(services / 'operation.lock')) or stale
            receipt = check_ownership()
            if receipt is None:
                if stale:
                    # Прерванная чужая операция требует собственного восстановления.
                    raise UninstallError('update_in_progress')
                receipt = {**identity, 'status': 'pending', 'release': _active_release(home, manifest)}
                _write_receipt(receipt_path, receipt)
            unit_paths = runner(_SYSTEMCTL + ['show', '--property=UnitPath', '--value'],
                                timeout=timeout, env=_manager_environment())
            if type(unit_paths) is not str or str(directory) not in unit_paths.split():
                raise UninstallError()
            for name in reversed(units):
                values = _show(runner, name, timeout)
                path = directory / name
                if values['LoadState'] != 'not-found':
                    if values['FragmentPath'] not in ('', str(path)):
                        raise UninstallError()
                    if values['ActiveState'] not in ('inactive', 'failed'):
                        runner(_SYSTEMCTL + ['stop', '--', name], timeout=timeout, env=_manager_environment())
                    values = _show(runner, name, timeout)
                    if values['ActiveState'] == 'failed':
                        runner(_SYSTEMCTL + ['reset-failed', '--', name], timeout=timeout,
                               env=_manager_environment())
                        values = _show(runner, name, timeout)
                if values['ActiveState'] != 'inactive':
                    raise UninstallError()
            public['services_stopped'] = True
            for name, digest in digests.items():
                path = directory / name
                if os.path.lexists(path):
                    _read_unit(path, digest)
                    path.unlink()
            _fsync_directory(directory)
            runner(_SYSTEMCTL + ['daemon-reload'], timeout=timeout, env=_manager_environment())
            for name in units:
                values = _show(runner, name, timeout)
                if (values['LoadState'] != 'not-found' or values['ActiveState'] != 'inactive'
                        or values['FragmentPath'] or values['NeedDaemonReload'] != 'no'):
                    raise UninstallError()
            for path in removed:
                _remove(path)
            _fsync_directory(root)
            if any(os.path.lexists(path) for path in removed):
                raise UninstallError()
            _write_receipt(receipt_path, {**receipt, 'status': 'done'})
        return {**public, 'state': 'uninstalled'}
    except UninstallError:
        raise
    except Exception:
        raise UninstallError() from None


def preserved_installation(install_root, *, expected_product_id):
    """Прочитать квитанцию завершённого удаления с сохранением данных; ничего не изменяет."""
    try:
        root = checked_path(install_root)
        receipt = _read_receipt(root / RECEIPT_NAME)
        if (receipt is None or receipt['status'] != 'done' or not receipt['preserved']
                or receipt['product_id'] != expected_product_id):
            raise UninstallError()
        return {'operation_id': receipt['operation_id'], 'binding': receipt['binding'],
                'preserved': list(receipt['preserved']), 'product_id': receipt['product_id'],
                'release': receipt['release']}
    except UninstallError:
        raise
    except Exception:
        raise UninstallError() from None
