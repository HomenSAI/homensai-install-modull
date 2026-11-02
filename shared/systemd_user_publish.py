# HomenS.AI · © 2026 Serhii Khomenko
"""Публикация подписанных пользовательских systemd units без их запуска.

Предварительный шаг: не выполняет daemon-reload, enable, start или удаление.
Самостоятельной установкой продукта и проверкой безопасности unit не является.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from contextlib import contextmanager

from shared.datahome import DataHomeError, checked_path, resolve_datahome
from shared.fresh_prepare import FreshPrepareError, verify_fresh_release
from shared.manifest import ManifestError
from shared.package import PackageError
from shared.package_signing import verify_signed_package
from shared.services import component_order, render_fixed_user_services
from shared.systemd_user import SystemdUserError, _PROPERTIES, _run, _trusted_file
from shared.update_plan import UpdateError, _atomic_write, _object

_MAX_UNIT = 1024 * 1024


class UnitPublicationError(RuntimeError):
    """Публикация не подтверждена; частичные файлы сохраняются."""


def _unit_directory(value):
    if os.name != 'posix' or not hasattr(os, 'getuid') or not hasattr(os, 'O_NOFOLLOW'):
        raise UnitPublicationError('Пользовательские службы недоступны')
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise UnitPublicationError('Требуется непривилегированный пользователь')
    path = checked_path(value)
    if any(char.isspace() for char in str(path)):
        raise UnitPublicationError('Путь каталога служб не поддерживается')
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid not in (0, uid) or info.st_mode & 0o022):
            raise UnitPublicationError('Небезопасный каталог служб')
    if path.stat().st_uid != uid:
        raise UnitPublicationError('Каталог служб принадлежит другому пользователю')
    return path


def _read_unit(path, expected):
    path = checked_path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_UNIT:
            raise UnitPublicationError('Файл службы недопустим')
        data = stream.read(_MAX_UNIT + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    if (len(data) != before.st_size or len(data) > _MAX_UNIT
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected)):
        raise UnitPublicationError('Файл службы не соответствует подписи')
    return data


def _manager_environment():
    runtime = '/run/user/' + str(os.getuid())
    return {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
            'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}


def _confirm_manager(runner, directory, names, timeout):
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    env = _manager_environment()
    unit_paths = runner(base + ['show', '--property=UnitPath', '--value'], timeout=timeout, env=env)
    if type(unit_paths) is not str or str(directory) not in unit_paths.split():
        raise UnitPublicationError('Каталог не принадлежит текущему менеджеру')
    for name in names:
        output = runner(base + ['show', '--property=' + ','.join(_PROPERTIES), '--', name],
                        timeout=timeout, env=env)
        if type(output) is not str:
            raise UnitPublicationError('Некорректный ответ менеджера')
        fields = {}
        for line in output.splitlines():
            key, sep, value = line.partition('=')
            if not sep or key in fields or key not in _PROPERTIES:
                raise UnitPublicationError('Некорректный ответ менеджера')
            fields[key] = value
        if fields != {'Id': name, 'LoadState': 'not-found', 'ActiveState': 'inactive',
                      'SubState': 'dead', 'FragmentPath': '', 'DropInPaths': '', 'NeedDaemonReload': 'no'}:
            raise UnitPublicationError('Имя службы уже занято или состояние не подтверждено')


def _write_exclusive(directory_fd, name, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _publication_receipt(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        current = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 8192
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise UnitPublicationError('Недопустимая отметка публикации')
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise UnitPublicationError('Недопустимая отметка публикации')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    if (type(value) is not dict or set(value) != {'version', 'operation_id', 'binding', 'status'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['operation_id']) is not str or re.fullmatch(r'[a-f0-9]{32}', value['operation_id']) is None
            or type(value['binding']) is not str or re.fullmatch(r'[a-f0-9]{64}', value['binding']) is None
            or value['status'] not in ('pending', 'done')):
        raise UnitPublicationError('Недопустимая отметка публикации')
    return value


def _new_publication_receipt(path, value):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _require_absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise UnitPublicationError('Обнаружено существующее состояние')


def _confirm_published(runner, directory, units, product_id, timeout):
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    paths = runner(base + ['show', '--property=UnitPath', '--value'],
                   timeout=timeout, env=_manager_environment())
    if type(paths) is not str or len(paths) > 16384 or str(directory) not in paths.split():
        raise UnitPublicationError('Каталог не принадлежит менеджеру')
    _require_absent(directory / ('.' + product_id + '.homensai-units.pending'))
    for name, digest, _ in units:
        _require_absent(directory / (name + '.d'))
        _trusted_file(directory / name, digest)


@contextmanager
def _existing_lock(path):
    """Захватить существующий протокол flock, не стирая признак прежнего crash."""
    import fcntl

    path = checked_path(path)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    acquired = False
    try:
        info = os.fstat(fd)
        current = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                or info.st_nlink != 1 or info.st_size > 8192
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise UnitPublicationError('Недопустимая блокировка')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        data = os.read(fd, 8193)
        if len(data) > 8192 or data[:1] != b'\0':
            raise UnitPublicationError('Недопустимая блокировка')
        state = json.loads(data[1:].decode('utf-8'), object_pairs_hook=_object)
        if (type(state) is not dict or set(state) != {'version', 'status', 'pid'}
                or type(state['version']) is not int or state['version'] != 1
                or state['status'] != 'released' or type(state['pid']) is not int or state['pid'] <= 0):
            raise UnitPublicationError('Предыдущая операция требует восстановления')
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _publish_files(directory, names, units, public, product_id, version, verify_again):
    marker = '.' + product_id + '.homensai-units.pending'
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise UnitPublicationError('Каталог служб изменился')
        # A sibling drop-in could change the unit after a manager reload;
        # broader manager overrides still need inspection before execution.
        for name in [marker, *names, *(name + '.d' for name in names)]:
            try:
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise UnitPublicationError('Файл службы или маркер уже существует')
        body = (json.dumps({'product_id': product_id, 'version': version,
                            'units': public['units']}, sort_keys=True, separators=(',', ':')) + '\n').encode()
        _write_exclusive(dir_fd, marker, body)
        os.fsync(dir_fd)
        for name, digest, data in units:
            _write_exclusive(dir_fd, name, data)
            _trusted_file(directory / name, digest)
        os.fsync(dir_fd)
        verify_again()
        os.unlink(marker, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def publish_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                              expected_product_id, expected_version, modes, unit_dir,
                              execute=False, operation_id=None, verify_generated_units=False, runner=None, timeout=30):
    """Опубликовать подписанные units в каталоге user-manager без перезаписи.

    Пакет содержит systemd-user/<product>-<component>.service. Dry-run ничего
    не пишет и не обращается к systemd. При ошибке после начала записи частичные
    файлы и маркер остаются для ручной проверки. Этого шага недостаточно для
    запуска или полной установки; доверенная подпись не доказывает безопасность.
    """
    if (type(execute) is not bool or type(verify_generated_units) is not bool or type(modes) is not tuple or not modes
            or type(timeout) not in (int, float) or not 0 < timeout <= 120
            or (runner is not None and not callable(runner))
            or (operation_id is not None and (type(operation_id) is not str
                or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None))):
        raise UnitPublicationError('Недопустимые параметры публикации')
    try:
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        metadata = verify_signed_package(package_path, envelope, **options)
        verified = verify_fresh_release(package_path, envelope, install_root, **options)
        manifest = metadata['manifest']
        order = component_order(manifest, modes)
        inventory = {item['path']: item for item in metadata['files']}
        if verified['process_target'] != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}:
            raise UnitPublicationError('Требуется нативный Linux x86-64')
        home = resolve_datahome(manifest, install_root)
        release = home.releases / ('app-' + expected_version)
        names = [expected_product_id + '-' + component + '.service' for component in order]
        units = []
        for name in names:
            entry = inventory.get('systemd-user/' + name)
            if entry is None or entry['size'] > _MAX_UNIT:
                raise UnitPublicationError('Подписанный unit отсутствует')
            data = _read_unit(release / 'systemd-user' / name, entry['sha256'])
            if len(data) != entry['size']:
                raise UnitPublicationError('Размер unit не совпадает')
            units.append((name, entry['sha256'], data))
        if verify_generated_units:
            generated = render_fixed_user_services(manifest, modes=modes, install_root=str(checked_path(install_root)))
            if set(generated) != set(names) or any(generated[name].encode('utf-8') != data for name, _, data in units):
                raise UnitPublicationError('Подписанные units не соответствуют корню и манифесту')
        if (verify_signed_package(package_path, envelope, **options) != metadata
                or verify_fresh_release(package_path, envelope, install_root, **options) != verified):
            raise UnitPublicationError('Выпуск изменился во время подготовки')
        public = {'units': [{'name': name, 'sha256': digest} for name, digest, _ in units],
                  'state': 'planned' if not execute else 'published', 'services_started': False}
        if not execute:
            return public
        bootstrap = checked_path(checked_path(install_root) / '.homensai-bootstrap.lock')
        service_lock = checked_path(home.roots['services'] / 'operation.lock')
        def verify_again():
            if (verify_signed_package(package_path, envelope, **options) != metadata
                    or verify_fresh_release(package_path, envelope, install_root, **options) != verified):
                raise UnitPublicationError('Выпуск изменился при публикации')
        with _existing_lock(bootstrap):
            with _existing_lock(service_lock):
                verify_again()
                update_state = checked_path(home.roots['services'] / manifest['update']['state_file'].replace('\\', '/'))
                _require_absent(update_state)
                directory = _unit_directory(unit_dir)
                runner = _run if runner is None else runner
                receipt = None
                if operation_id is None:
                    prior = checked_path(checked_path(install_root) / '.homensai-control/user-systemd-publication.json')
                    if _publication_receipt(prior) is not None:
                        raise UnitPublicationError('Требуется исходный ключ операции публикации')
                if operation_id is not None:
                    root = checked_path(install_root)
                    control = checked_path(root / '.homensai-control')
                    for path in (root, control):
                        info = path.lstat()
                        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                            raise UnitPublicationError('Каталог управления не защищён')
                    context = {'product_id': expected_product_id, 'version': expected_version,
                               'transaction': verified['transaction'], 'units': public['units'],
                               'modes': modes, 'install_sha256': hashlib.sha256(str(root).encode()).hexdigest(),
                               'directory_sha256': hashlib.sha256(str(directory).encode()).hexdigest(),
                               'publisher_sha256': hashlib.sha256(trusted_public_key).hexdigest()}
                    if verify_generated_units:
                        context['verify_generated_units'] = True
                    binding = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    receipt = checked_path(control / 'user-systemd-publication.json')
                    pending = {'version': 1, 'operation_id': operation_id, 'binding': binding, 'status': 'pending'}
                    done = {**pending, 'status': 'done'}
                    old = _publication_receipt(receipt)
                    if old == done:
                        try:
                            _confirm_published(runner, directory, units, expected_product_id, timeout)
                        except Exception:
                            raise UnitPublicationError('Публикация не подтверждена') from None
                        verify_again()
                        if _publication_receipt(receipt) != done:
                            raise UnitPublicationError('Отметка публикации изменилась')
                        return public
                    if old is not None:
                        raise UnitPublicationError('Публикация требует восстановления')
                try:
                    _confirm_manager(runner, directory, names, timeout)
                except Exception:
                    raise UnitPublicationError('Менеджер служб не подтвердил публикацию') from None
                if receipt is not None:
                    _new_publication_receipt(receipt, pending)
                _publish_files(directory, names, units, public, expected_product_id, expected_version, verify_again)
                if receipt is not None:
                    if _publication_receipt(receipt) != pending:
                        raise UnitPublicationError('Отметка публикации изменилась')
                    _atomic_write(receipt, done)
                    if _publication_receipt(receipt) != done:
                        raise UnitPublicationError('Отметка публикации не подтверждена')
        return public
    except (OSError, ValueError, TypeError, KeyError, RecursionError, DataHomeError,
            ManifestError, PackageError, FreshPrepareError, SystemdUserError, UpdateError):
        raise UnitPublicationError('Не удалось опубликовать службы') from None
