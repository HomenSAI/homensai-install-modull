# HomenS.AI · © 2026 Serhii Khomenko
"""Ограниченная загрузка подписанных user-units без enable/start.

Это глобальный daemon-reload пользовательского менеджера, не полный установщик.
При неизвестном результате журнал pending запрещает автоматический повтор.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from shared.datahome import DataHomeError, checked_path, resolve_datahome
from shared.fresh_prepare import FreshPrepareError, verify_fresh_release
from shared.manifest import ManifestError
from shared.package import PackageError
from shared.package_signing import verify_signed_package
from shared.runtime_native import NativeRuntimeError
from shared.services import component_order
from shared.systemd_user import SystemdUserAdapter, SystemdUserError, _PROPERTIES, _run, _trusted_file
from shared.systemd_user_publish import (UnitPublicationError, _existing_lock, _manager_environment,
                                         _publication_receipt, _unit_directory, publish_signed_user_units)
from shared.systemd_portable import PortableUnitError, _publication_binding, plan_portable_signed_user_units
from shared.update_plan import UpdateError, _atomic_write, _object


class UnitLoadError(RuntimeError):
    """Состояние загрузки не подтверждено; детали ввода не раскрываются."""


def _read_receipt(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 8192):
            raise UnitLoadError('Недопустимая отметка загрузки')
        raw = source.read(8193)
    if len(raw) > 8192:
        raise UnitLoadError('Недопустимая отметка загрузки')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    if (type(value) is not dict or set(value) != {'version', 'binding', 'status'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['binding']) is not str or len(value['binding']) != 64
            or any(char not in '0123456789abcdef' for char in value['binding'])
            or value['status'] not in ('pending', 'done')):
        raise UnitLoadError('Недопустимая отметка загрузки')
    return value


def _new_receipt(path, value):
    body = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _manager_call(runner, args, timeout):
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    try:
        output = runner(base + args, timeout=timeout, env=_manager_environment())
    except Exception:
        raise UnitLoadError('Менеджер не подтвердил операцию') from None
    if type(output) is not str or len(output) > 16384:
        raise UnitLoadError('Некорректный ответ менеджера')
    return output


def _before_reload(runner, directory, name, timeout):
    output = _manager_call(runner, ['show', '--property=' + ','.join(_PROPERTIES), '--', name], timeout)
    fields = {}
    for line in output.splitlines():
        key, sep, value = line.partition('=')
        if not sep or key not in _PROPERTIES or key in fields:
            raise UnitLoadError('Некорректный ответ менеджера')
        fields[key] = value
    if (set(fields) != set(_PROPERTIES) or fields['Id'] != name
            or fields['ActiveState'] != 'inactive' or fields['SubState'] != 'dead'
            or fields['DropInPaths'] or fields['NeedDaemonReload'] not in ('yes', 'no')):
        raise UnitLoadError('Служба занята или конфигурация не подтверждена')
    if not ((fields['LoadState'] == 'not-found' and fields['FragmentPath'] == '')
            or (fields['LoadState'] == 'loaded' and fields['FragmentPath'] == str(directory / name))):
        raise UnitLoadError('Имя службы занято')


def _load_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                            expected_product_id, expected_version, modes, unit_dir,
                            execute=False, inspect_pending=False, require_done=False, operation_id=None, portable=False,
                            runner=None, timeout=30, require_active=False, require_stable=False,
                            pointer=False, runtime_operation_id=None, reserve_bytes=None):
    if (type(execute) is not bool or type(portable) is not bool or type(pointer) is not bool
            or (pointer and portable) or type(modes) is not tuple or not modes
            or type(inspect_pending) is not bool or (inspect_pending and not execute)
            or type(require_done) is not bool or (require_done and (not execute or inspect_pending))
            or type(require_active) is not bool or (require_active and not require_done)
            or type(require_stable) is not bool or (require_stable and (not require_done or require_active))
            or type(timeout) not in (int, float) or not 0 < timeout <= 120
            or (runner is not None and not callable(runner))
            or (operation_id is not None and (type(operation_id) is not str
                or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None))
            or ((portable or pointer) and execute and operation_id is None)
            or (pointer and (type(runtime_operation_id) is not str
                or re.fullmatch(r'[a-f0-9]{32}', runtime_operation_id) is None
                or operation_id == runtime_operation_id or type(reserve_bytes) is not int
                or not 0 <= reserve_bytes <= 2**63 - 1))):
        raise UnitLoadError('Недопустимые параметры загрузки')
    try:
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        if pointer:
            from shared.systemd_pointer import plan_pointer_signed_user_units, _pointer_publication_binding
            plan_function = plan_pointer_signed_user_units
        elif portable:
            plan_function = plan_portable_signed_user_units
        else:
            plan_function = None
        plan = (plan_function(package_path, envelope, install_root, **options, modes=modes)
                if plan_function else publish_signed_user_units(package_path, envelope, install_root, **options,
                                                               modes=modes, unit_dir=unit_dir))
        metadata = verify_signed_package(package_path, envelope, **options)
        verified = verify_fresh_release(package_path, envelope, install_root, **options)
        manifest = metadata['manifest']
        expected_names = [expected_product_id + '-' + item + '.service' for item in component_order(manifest, modes)]
        inventory = {item['path']: item['sha256'] for item in metadata['files']}
        if ([item['name'] for item in plan['units']] != expected_names
                or (not portable and not pointer and any(inventory.get('systemd-user/' + item['name']) != item['sha256']
                                         for item in plan['units']))):
            raise UnitLoadError('Состав служб изменился')
        root = checked_path(install_root)
        home = resolve_datahome(manifest, root)
        public = {'units': plan['units'], 'state': 'pending_observed' if inspect_pending else
                  ('planned' if not execute else 'loaded'),
                  'services_started': False}
        if inspect_pending:
            public['reload_completion_verified'] = False
        if not execute:
            return public
        control = checked_path(root / '.homensai-control')
        for path in (root, control):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise UnitLoadError('Каталог управления не защищён')
        bootstrap = checked_path(root / '.homensai-bootstrap.lock')
        service_lock = checked_path(home.roots['services'] / 'operation.lock')
        receipt = checked_path(control / 'user-systemd-reload.json')
        runner = _run if runner is None else runner
        directory = _unit_directory(unit_dir)
        runtime_receipt_path = None
        runtime_definition = None
        if pointer and not inspect_pending:
            from shared.runtime_native import _definition, _inspect_runtime, verify_signed_linux_runtime
            runtime_definition = _definition(package_path, envelope, install_root, options)
            runtime_receipt_path = checked_path(control / 'linux-runtime.json')
            runtime_receipt = _publication_receipt(runtime_receipt_path)
            if (runtime_receipt is None or runtime_receipt['status'] != 'done'
                    or runtime_receipt['operation_id'] != runtime_operation_id):
                raise UnitLoadError('Runtime не подтверждён')
            verify_signed_linux_runtime(package_path, envelope, install_root, **options,
                                        operation_id=runtime_operation_id, reserve_bytes=reserve_bytes)
            if _publication_receipt(runtime_receipt_path) != runtime_receipt:
                raise UnitLoadError('Отметка runtime изменилась')
        context = {'product_id': expected_product_id, 'version': expected_version,
                   'transaction': verified['transaction'], 'units': plan['units'],
                   'install_sha256': hashlib.sha256(str(root).encode()).hexdigest(),
                   'directory_sha256': hashlib.sha256(str(directory).encode()).hexdigest(),
                   'publisher_sha256': hashlib.sha256(trusted_public_key).hexdigest()}
        if operation_id is not None:
            context['operation_id'] = operation_id
            context['modes'] = modes
        if portable or pointer:
            context['generator'] = plan['generator']
        if pointer:
            context['runtime_operation_id'] = runtime_operation_id
            context['reserve_bytes'] = reserve_bytes
        binding = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        pending, done = {'version': 1, 'binding': binding, 'status': 'pending'}, {'version': 1, 'binding': binding, 'status': 'done'}
        with _existing_lock(bootstrap):
            with _existing_lock(service_lock):
                if (verify_signed_package(package_path, envelope, **options) != metadata
                        or verify_fresh_release(package_path, envelope, install_root, **options) != verified
                        or (plan_function and plan_function(package_path, envelope, install_root,
                                                            **options, modes=modes) != plan)):
                    raise UnitLoadError('Выпуск изменился')
                if runtime_definition is not None:
                    if (_definition(package_path, envelope, install_root, options) != runtime_definition
                            or _publication_receipt(runtime_receipt_path) != runtime_receipt):
                        raise UnitLoadError('Runtime изменился')
                    _inspect_runtime(checked_path(home.roots['runtime']), runtime_definition[6],
                                     set(runtime_definition[5]['executables']))
                publication = _publication_receipt(control / 'user-systemd-publication.json')
                if publication is not None and publication['status'] != 'done':
                    raise UnitLoadError('Публикация служб не подтверждена')
                if publication is not None and operation_id is not None and publication['operation_id'] == operation_id:
                    raise UnitLoadError('Ключ уже использован для публикации')
                if portable or pointer:
                    binding_function = _pointer_publication_binding if pointer else _publication_binding
                    extra = {'runtime_operation_id': runtime_operation_id, 'reserve_bytes': reserve_bytes} if pointer else {}
                    expected_publication = binding_function(product_id=expected_product_id,
                        version=expected_version, transaction=verified['transaction'], plan=plan,
                        modes=modes, root=root, directory=directory, trusted_public_key=trusted_public_key, **extra)
                    if publication is None or publication['status'] != 'done' or publication['binding'] != expected_publication:
                        raise UnitLoadError('Публикация переносимых служб не подтверждена')
                update_state = checked_path(home.roots['services'] / manifest['update']['state_file'].replace('\\', '/'))
                if not _absent(update_state):
                    raise UnitLoadError('Состояние обновления требует проверки')
                old = _read_receipt(receipt)
                if require_done and old != done:
                    raise UnitLoadError('Загрузка служб не подтверждена')
                if (inspect_pending and old != pending) or (not inspect_pending and old not in (None, done)):
                    raise UnitLoadError('Результат предыдущей загрузки требует проверки')
                paths = _manager_call(runner, ['show', '--property=UnitPath', '--value'], timeout)
                if str(directory) not in paths.split():
                    raise UnitLoadError('Каталог не принадлежит менеджеру')
                marker = directory / ('.' + expected_product_id + '.homensai-units.pending')
                if not _absent(marker):
                    raise UnitLoadError('Публикация служб не завершена')
                for item in plan['units']:
                    name, digest = item['name'], item['sha256']
                    if not _absent(directory / (name + '.d')):
                        raise UnitLoadError('Обнаружен drop-in службы')
                    _trusted_file(directory / name, digest)
                    if old is None:
                        _before_reload(runner, directory, name, timeout)
                if old is None:
                    _new_receipt(receipt, pending)
                    _manager_call(runner, ['daemon-reload'], timeout)
                for item in plan['units']:
                    component = item['name'][len(expected_product_id) + 1:-len('.service')]
                    adapter = SystemdUserAdapter(manifest, modes=modes, component=component,
                                                 fragment_path=str(directory / item['name']),
                                                 fragment_sha256=item['sha256'], runner=runner, timeout=timeout)
                    status = adapter.inspect()
                    if ((require_active and status['active_state'] != 'active') or
                            (require_stable and not (status['active_state'] == 'active' or
                              (status['active_state'] == 'inactive' and status['sub_state'] == 'dead'))) or
                            (not require_active and not require_stable and
                             (status['active_state'] != 'inactive' or status['sub_state'] != 'dead'))):
                        raise UnitLoadError('Служба активна или её состояние не подтверждено')
                if (verify_signed_package(package_path, envelope, **options) != metadata
                        or verify_fresh_release(package_path, envelope, install_root, **options) != verified
                        or (plan_function and plan_function(package_path, envelope, install_root,
                                                            **options, modes=modes) != plan)):
                    raise UnitLoadError('Выпуск изменился')
                if runtime_definition is not None:
                    if (_definition(package_path, envelope, install_root, options) != runtime_definition
                            or _publication_receipt(runtime_receipt_path) != runtime_receipt):
                        raise UnitLoadError('Runtime изменился')
                    _inspect_runtime(checked_path(home.roots['runtime']), runtime_definition[6],
                                     set(runtime_definition[5]['executables']))
                if old is None:
                    if _read_receipt(receipt) != pending:
                        raise UnitLoadError('Отметка изменилась')
                    _atomic_write(receipt, done)
                    if _read_receipt(receipt) != done:
                        raise UnitLoadError('Отметка не подтверждена')
                elif inspect_pending and _read_receipt(receipt) != pending:
                    raise UnitLoadError('Отметка изменилась')
        return public
    except (OSError, ValueError, TypeError, KeyError, RecursionError, DataHomeError, ManifestError,
            PackageError, FreshPrepareError, NativeRuntimeError, SystemdUserError, UnitPublicationError, PortableUnitError, UpdateError):
        raise UnitLoadError('Не удалось подтвердить загрузку служб') from None


def load_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                           expected_product_id, expected_version, modes, unit_dir,
                           execute=False, operation_id=None, runner=None, timeout=30):
    """Загрузить ожидаемые units без запуска; pending автоматически не повторяется."""
    return _load_signed_user_units(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir,
        execute=execute, operation_id=operation_id, runner=runner, timeout=timeout)


def inspect_pending_user_units(package_path, envelope, install_root, *, trusted_public_key,
                               expected_product_id, expected_version, modes, unit_dir,
                               operation_id=None, runner=None, timeout=30):
    """Проверка pending без reload/start и записи отметки; show может загрузить unit."""
    return _load_signed_user_units(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir,
        execute=True, inspect_pending=True, operation_id=operation_id, runner=runner, timeout=timeout)


def load_portable_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                    expected_product_id, expected_version, modes, unit_dir,
                                    execute=False, operation_id=None, runner=None, timeout=30):
    """Загрузить опубликованные units из подписанного переносимого рецепта."""
    return _load_signed_user_units(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir,
        execute=execute, operation_id=operation_id, portable=True, runner=runner, timeout=timeout)


def inspect_pending_portable_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                        expected_product_id, expected_version, modes, unit_dir,
                                        operation_id, runner=None, timeout=30):
    """Наблюдать pending без нового reload; результат reload не подтверждается."""
    return _load_signed_user_units(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir,
        execute=True, inspect_pending=True, operation_id=operation_id, portable=True, runner=runner, timeout=timeout)


def load_pointer_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                   expected_product_id, expected_version, modes, unit_dir,
                                   runtime_operation_id, reserve_bytes, operation_id, runner=None, timeout=30):
    """Load published pointer units after verifying runtime; never start services."""
    return _load_signed_user_units(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir, execute=True,
        operation_id=operation_id, pointer=True, runtime_operation_id=runtime_operation_id,
        reserve_bytes=reserve_bytes, runner=runner, timeout=timeout)


def inspect_pending_pointer_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                       expected_product_id, expected_version, modes, unit_dir,
                                       runtime_operation_id, reserve_bytes, operation_id, runner=None, timeout=30):
    """Observe pending reload without replay or installed-runtime verification."""
    return _load_signed_user_units(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir, execute=True, inspect_pending=True,
        operation_id=operation_id, pointer=True, runtime_operation_id=runtime_operation_id,
        reserve_bytes=reserve_bytes, runner=runner, timeout=timeout)


def signed_user_group(package_path, envelope, install_root, *, trusted_public_key,
                      expected_product_id, expected_version, modes, unit_dir,
                      operation_id=None, portable=False, runner=None, timeout=30, _require_active=False,
                      _require_stable=False):
    """Создать группу из подписанного выпуска после подтверждённой загрузки.

    Сам вызов не запускает службы и не вызывает daemon-reload. Статус менеджера
    может измениться после проверки; операции группы повторно проверяют units.
    """
    from shared.systemd_group import SystemdUserGroup, SystemdGroupError

    options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                   expected_version=expected_version)
    try:
        loaded = _load_signed_user_units(package_path, envelope, install_root, **options,
                    modes=modes, unit_dir=unit_dir, execute=True, require_done=True,
                    operation_id=operation_id, portable=portable, runner=runner, timeout=timeout,
                    require_active=_require_active, require_stable=_require_stable)
        metadata = verify_signed_package(package_path, envelope, **options)
        verified = verify_fresh_release(package_path, envelope, install_root, **options)
        manifest = metadata['manifest']
        order = component_order(manifest, modes)
        directory = _unit_directory(unit_dir)
        inventory = {item['path']: item['sha256'] for item in metadata['files']}
        units = loaded['units']
        names = [expected_product_id + '-' + name + '.service' for name in order]
        portable_plan = (plan_portable_signed_user_units(package_path, envelope, install_root, **options, modes=modes)
                         if portable else None)
        if ([item['name'] for item in units] != names or
                (portable and portable_plan['units'] != units) or
                (not portable and any(inventory.get('systemd-user/' + item['name']) != item['sha256'] for item in units))):
            raise UnitLoadError('Состав служб изменился')
        adapters = {component: SystemdUserAdapter(manifest, modes=modes, component=component,
                       fragment_path=str(directory / item['name']), fragment_sha256=item['sha256'],
                       runner=runner, timeout=timeout) for component, item in zip(order, units)}
        group = SystemdUserGroup(manifest, modes=modes, adapters=adapters)
        for adapter in adapters.values():
            status = adapter.inspect()
            if ((_require_active and status['active_state'] != 'active') or
                    (_require_stable and not (status['active_state'] == 'active' or
                      (status['active_state'] == 'inactive' and status['sub_state'] == 'dead'))) or
                    (not _require_active and not _require_stable and
                     (status['active_state'] != 'inactive' or status['sub_state'] != 'dead'))):
                raise UnitLoadError('Состояние службы изменилось')
        if (verify_signed_package(package_path, envelope, **options) != metadata
                or verify_fresh_release(package_path, envelope, install_root, **options) != verified
                or (portable and plan_portable_signed_user_units(package_path, envelope, install_root,
                                             **options, modes=modes) != portable_plan)):
            raise UnitLoadError('Выпуск изменился')
        return group
    except (OSError, ValueError, TypeError, KeyError, RecursionError, DataHomeError, ManifestError,
            PackageError, FreshPrepareError, SystemdUserError, SystemdGroupError, UnitPublicationError,
            PortableUnitError, UpdateError):
        raise UnitLoadError('Не удалось подтвердить группу служб') from None


def portable_signed_user_group(package_path, envelope, install_root, *, trusted_public_key,
                               expected_product_id, expected_version, modes, unit_dir,
                               operation_id, runner=None, timeout=30):
    """Построить группу из подтверждённого переносимого рецепта без запуска."""
    return signed_user_group(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir,
        operation_id=operation_id, portable=True, runner=runner, timeout=timeout)


def portable_signed_runtime_user_group(package_path, envelope, install_root, *, trusted_public_key,
                                       expected_product_id, expected_version, modes, unit_dir,
                                       operation_id, runtime_operation_id, runtime_reserve_bytes,
                                       runner=None, timeout=30):
    """Свежая группа: проверять runtime перед каждым явным start, но не stop."""
    from shared.systemd_runtime_group import RuntimeCheckedUserGroup

    group = portable_signed_user_group(package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, modes=modes, unit_dir=unit_dir,
        operation_id=operation_id, runner=runner, timeout=timeout)
    return RuntimeCheckedUserGroup(group, package_path, envelope, install_root,
        trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
        expected_version=expected_version, runtime_operation_id=runtime_operation_id,
        reserve_bytes=runtime_reserve_bytes)
