"""Experimental durable start/stop cycle after a completed pointer update."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from shared.datahome import checked_path
from shared.pointer_terminal import _unit_binding, _verified_terminal_group
from shared.systemd_cycle import _write
from shared.systemd_user_publish import _publication_receipt
from shared.update_plan import UpdateError, _object
from shared.wheel_audit import audit_signed_wheels

_KEY = re.compile(r'[a-f0-9]{32}\Z')
_LIMIT = 256


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as source:
        info, current = os.fstat(source.fileno()), path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 65536
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise UpdateError('Журнал цикла недоступен')
        raw = source.read(65537)
    if len(raw) > 65536:
        raise UpdateError('Журнал цикла слишком велик')
    state = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    if (type(state) is not dict or set(state) != {'version', 'binding', 'events'}
            or type(state['version']) is not int or state['version'] != 1
            or type(state['binding']) is not str or re.fullmatch(r'[a-f0-9]{64}', state['binding']) is None
            or type(state['events']) is not list or not 1 <= len(state['events']) <= _LIMIT):
        raise UpdateError('Недопустимый журнал цикла')
    seen = set()
    for index, event in enumerate(state['events']):
        if (type(event) is not dict or set(event) != {'action', 'key', 'status'}
                or event['action'] != ('stop' if index % 2 == 0 else 'start')
                or type(event['key']) is not str or _KEY.fullmatch(event['key']) is None
                or event['key'] in seen or event['status'] not in ('pending', 'done', 'interrupted')
                or (event['status'] == 'pending' and index != len(state['events']) - 1)
                or (event['status'] == 'interrupted' and
                    (event['action'] != 'start' or index == len(state['events']) - 1))):
            raise UpdateError('Недопустимая последовательность цикла')
        seen.add(event['key'])
    return state


def _confirm(group, action):
    for name in group.order:
        adapter = group.adapters[name]
        status = adapter.confirm_stopped() if action == 'stop' else adapter.inspect()
        if status['active_state'] != ('inactive' if action == 'stop' else 'active'):
            raise UpdateError('Состояние службы не подтверждено')


def cycle_terminal_pointer(*, action, cycle_key, reconcile=False, **options):
    """Execute or inspect one durable service transition for a completed update.

    The first action is stop. Pending start is never replayed; a new stop may
    interrupt it. Only a pending stop can be reconciled by observation. Local
    keys, signed code, the manager and all other writers remain caller-trusted.
    """
    try:
        if (type(action) is not str or action not in ('start', 'stop') or type(cycle_key) is not str
                or _KEY.fullmatch(cycle_key) is None or type(reconcile) is not bool
                or (reconcile and action != 'stop')):
            raise UpdateError('Недопустимый запрос цикла')
        with _verified_terminal_group(**options) as (group, observation):
            update = observation['state']
            root = checked_path(options['install_root'])
            control = checked_path(root / '.homensai-control')
            publication = _publication_receipt(control / 'user-systemd-publication.json')
            reserved = {options['operation_id'], options['load_key'], options['runtime_key'],
                        update['transaction']}
            if publication is None or publication['status'] != 'done':
                raise UpdateError('Публикация не подтверждена')
            reserved.add(publication['operation_id'])
            if cycle_key in reserved:
                raise UpdateError('Ключ уже используется')
            context = {'root': str(root), 'update': update, 'units': _unit_binding(group),
                       'operation_id': options['operation_id'], 'load_key': options['load_key'],
                       'runtime_key': options['runtime_key']}
            binding = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':'),
                                                ensure_ascii=True).encode('ascii')).hexdigest()
            path = checked_path(control / 'pointer-terminal-cycle.json')
            state = _read(path)
            if state is None:
                if action != 'stop' or reconcile:
                    raise UpdateError('Цикл начинается с остановки')
                state = {'version': 1, 'binding': binding, 'events': []}
            elif (state['binding'] != binding or any(event['key'] in reserved for event in state['events'])):
                raise UpdateError('Журнал относится к другому обновлению')
            events = state['events']
            last = events[-1] if events else None
            same = last is not None and last['key'] == cycle_key and last['action'] == action
            if reconcile:
                if not same or last['status'] not in ('pending', 'done'):
                    raise UpdateError('Нет остановки для сверки')
                _confirm(group, 'stop')
                if last['status'] == 'pending':
                    if _read(path) != state:
                        raise UpdateError('Журнал изменился')
                    last['status'] = 'done'
                    _write(path, state)
                    _confirm(group, 'stop')
            elif same:
                if last['status'] != 'done':
                    raise UpdateError('Исход действия не подтверждён')
                _confirm(group, action)
            else:
                if (len(events) >= _LIMIT or any(event['key'] == cycle_key for event in events)
                        or (last is not None and (last['action'] == action or
                            (last['status'] != 'done' and not (last['action'] == 'start'
                             and last['status'] == 'pending' and action == 'stop'))))):
                    raise UpdateError('Действие конфликтует с журналом')
                if action == 'start':
                    _confirm(group, 'stop')
                    old = update['outcome'] == 'rolled_back'
                    audit_signed_wheels(options['old_package' if old else 'new_package'],
                        options['old_envelope' if old else 'new_envelope'],
                        trusted_public_key=options['old_key' if old else 'new_key'],
                        expected_product_id=options['product_id'],
                        expected_version=options['old_version' if old else 'new_version'])
                if last is not None and last['status'] == 'pending':
                    last['status'] = 'interrupted'
                events.append({'action': action, 'key': cycle_key, 'status': 'pending'})
                _write(path, state)
                group.command(action, execute=True)
                _confirm(group, action)
                if _read(path) != state:
                    raise UpdateError('Журнал изменился')
                events[-1]['status'] = 'done'
                _write(path, state)
                _confirm(group, action)
            if _read(path) != state:
                raise UpdateError('Журнал не подтверждён')
            return {'state': 'stopped' if action == 'stop' else 'started',
                    'health_verified': False, 'process_identity_verified': False,
                    'all_writers_stopped': False, 'prior_command_confirmed': False if reconcile else None}
    except Exception:
        raise UpdateError('Не удалось подтвердить цикл служб') from None
