# HomenS.AI · © 2026 Serhii Khomenko
"""Повторяемый цикл пользовательских служб после первого подтверждённого stop."""
from __future__ import annotations

import json
import os
import re
import stat
import uuid

from shared.datahome import checked_path
from shared.runtime_native import verify_signed_linux_runtime
from shared.systemd_fresh_start import FreshStartError, _binding, _lifecycle_lock
from shared.systemd_user_load import signed_user_group, portable_signed_runtime_user_group
from shared.systemd_user_publish import _publication_receipt
from shared.update_plan import _object

_KEY = re.compile(r'[a-f0-9]{32}\Z')
_LIMIT = 512


def _write(path, state):
    temporary = checked_path(path.with_name('.cycle-' + uuid.uuid4().hex + '.tmp'))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write((json.dumps(state, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info, current = os.fstat(stream.fileno()), path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 65536
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise FreshStartError('Журнал цикла недоступен')
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise FreshStartError('Журнал цикла слишком велик')
    state = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    if (type(state) is not dict or set(state) != {'format', 'binding', 'initial_start', 'initial_stop', 'events'}
            or type(state['format']) is not int or state['format'] != 1
            or type(state['binding']) is not str or re.fullmatch(r'[a-f0-9]{64}', state['binding']) is None
            or any(type(state[key]) is not str or _KEY.fullmatch(state[key]) is None
                   for key in ('initial_start', 'initial_stop'))
            or type(state['events']) is not list or not 1 <= len(state['events']) <= _LIMIT):
        raise FreshStartError('Недопустимый журнал цикла')
    seen = {state['initial_start'], state['initial_stop']}
    if len(seen) != 2:
        raise FreshStartError('Ключи цикла совпадают')
    for index, event in enumerate(state['events']):
        if (type(event) is not dict or set(event) != {'action', 'key', 'status'}
                or event['action'] != ('start' if index % 2 == 0 else 'stop')
                or type(event['key']) is not str or _KEY.fullmatch(event['key']) is None
                or event['key'] in seen or event['status'] not in ('pending', 'done', 'interrupted')
                or (event['status'] == 'pending' and index != len(state['events']) - 1)
                or (event['status'] == 'interrupted' and (event['action'] != 'start'
                    or index == len(state['events']) - 1))):
            raise FreshStartError('Недопустимая последовательность цикла')
        seen.add(event['key'])
    return state


def cycle_portable(package_path, envelope, install_root, *, trusted_public_key, expected_product_id,
                   expected_version, modes, unit_dir, load_key, runtime_key, initial_start_key,
                   initial_stop_key, reserve_bytes, operation_id, action, reconcile=False, runner=None):
    """Выполнить следующий start/stop или подтвердить его по текущему состоянию."""
    try:
        keys = (load_key, runtime_key, initial_start_key, initial_stop_key, operation_id)
        if (any(type(key) is not str or _KEY.fullmatch(key) is None for key in keys)
                or len(set(keys)) != len(keys) or type(action) is not str or action not in ('start', 'stop')
                or type(reconcile) is not bool or type(reserve_bytes) is not int
                or not 0 <= reserve_bytes <= 2**63 - 1 or type(envelope) is not bytes
                or type(trusted_public_key) is not bytes or len(trusted_public_key) != 32
                or type(modes) is not tuple or not modes):
            raise FreshStartError('Недопустимые параметры цикла')
        with _lifecycle_lock(install_root):
            root, directory = checked_path(install_root), checked_path(unit_dir)
            control = checked_path(root / '.homensai-control')
            publication = _publication_receipt(control / 'user-systemd-publication.json')
            if publication is None or publication['status'] != 'done' or publication['operation_id'] in keys:
                raise FreshStartError('Публикация или ключи не подтверждены')
            binding = _binding(root, directory, envelope, trusted_public_key, expected_product_id,
                               expected_version, modes, load_key, runtime_key, reserve_bytes)
            first_start = _publication_receipt(control / 'user-systemd-start.json')
            first_stop = _publication_receipt(control / 'user-systemd-stop.json')
            import hashlib
            stop_binding = hashlib.sha256((binding + initial_start_key).encode('ascii')).hexdigest()
            if (first_start is None or first_start['operation_id'] != initial_start_key
                    or first_start['binding'] != binding or first_stop is None
                    or first_stop != {'version': 1, 'operation_id': initial_stop_key,
                                      'binding': stop_binding, 'status': 'done'}):
                raise FreshStartError('Первый цикл не подтверждён')
            path = checked_path(control / 'user-systemd-cycle-v2.json')
            state = _read(path)
            if state is None:
                if action != 'start' or reconcile:
                    raise FreshStartError('Следует начать новый цикл')
                state = {'format': 1, 'binding': binding, 'initial_start': initial_start_key,
                         'initial_stop': initial_stop_key, 'events': []}
            elif (state['binding'] != binding or state['initial_start'] != initial_start_key
                    or state['initial_stop'] != initial_stop_key
                    or any(event['key'] in (load_key, runtime_key, publication['operation_id'])
                           for event in state['events'])):
                raise FreshStartError('Журнал относится к другой установке')
            events = state['events']
            if operation_id == publication['operation_id']:
                raise FreshStartError('Ключ публикации уже использован')
            last = events[-1] if events else None
            same = last is not None and last['key'] == operation_id and last['action'] == action
            if reconcile:
                if not same or last['status'] not in ('pending', 'done'):
                    raise FreshStartError('Нет соответствующей неопределённой операции')
            elif same:
                if last['status'] != 'done':
                    raise FreshStartError('Результат операции требует проверки')
            else:
                if (len(events) >= _LIMIT or any(event['key'] == operation_id for event in events)
                        or (last is not None and (last['action'] == action or
                            (last['status'] != 'done' and not
                             (last['action'] == 'start' and last['status'] == 'pending' and action == 'stop'))))):
                    raise FreshStartError('Операция конфликтует с журналом')
            options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                           expected_version=expected_version)
            if action == 'start':
                verify_signed_linux_runtime(package_path, envelope, str(root), **options,
                                            operation_id=runtime_key, reserve_bytes=reserve_bytes)
            if same or reconcile:
                signed_user_group(package_path, envelope, str(root), **options, modes=modes,
                    unit_dir=str(directory), operation_id=load_key, portable=True, runner=runner,
                    _require_active=(action == 'start'))
                if reconcile and last['status'] == 'pending':
                    if _read(path) != state:
                        raise FreshStartError('Журнал цикла изменился')
                    last['status'] = 'done'
                    _write(path, state)
                    if action == 'start':
                        verify_signed_linux_runtime(package_path, envelope, str(root), **options,
                                                    operation_id=runtime_key, reserve_bytes=reserve_bytes)
                    signed_user_group(package_path, envelope, str(root), **options, modes=modes,
                        unit_dir=str(directory), operation_id=load_key, portable=True, runner=runner,
                        _require_active=(action == 'start'))
            else:
                if action == 'start':
                    group = portable_signed_runtime_user_group(package_path, envelope, str(root), **options,
                        modes=modes, unit_dir=str(directory), operation_id=load_key,
                        runtime_operation_id=runtime_key, runtime_reserve_bytes=reserve_bytes, runner=runner)
                else:
                    group = signed_user_group(package_path, envelope, str(root), **options, modes=modes,
                        unit_dir=str(directory), operation_id=load_key, portable=True, runner=runner,
                        _require_stable=True)
                if last is not None and last['status'] == 'pending':
                    last['status'] = 'interrupted'
                events.append({'action': action, 'key': operation_id, 'status': 'pending'})
                _write(path, state)
                group.command(action, execute=True)
                if _read(path) != state:
                    raise FreshStartError('Журнал цикла изменился')
                events[-1]['status'] = 'done'
                _write(path, state)
            if _read(path) != state:
                raise FreshStartError('Журнал цикла не подтверждён')
            result = {'state': 'started' if action == 'start' else 'stopped',
                      'installation_complete': False, 'health_verified': False,
                      'all_writers_stopped': False}
            if reconcile:
                result['prior_command_confirmed'] = False
            return result
    except FreshStartError:
        raise
    except Exception:
        raise FreshStartError('Не удалось подтвердить цикл служб') from None
