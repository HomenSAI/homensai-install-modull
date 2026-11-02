# HomenS.AI · © 2026 Serhii Khomenko
"""Ограниченный адаптер заранее установленных пользовательских systemd units.

Не устанавливает units, не повышает права и не выполняет произвольные команды.
Хэш и путь unit должны поступать из независимого доверенного источника.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

from shared.manifest import ManifestError, validate_manifest
from shared.services import component_order

_SYSTEMCTL = '/usr/bin/systemctl'
_MAX_UNIT = 1024 * 1024
_MAX_OUTPUT = 16384
_PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'FragmentPath', 'DropInPaths', 'NeedDaemonReload')
_STATE = re.compile(r'[a-z][a-z0-9-]{0,31}\Z')


class SystemdUserError(RuntimeError):
    """Служба не подтверждена или операция не завершилась; без вывода процесса."""


def _trusted_file(path: Path, expected: str) -> None:
    if os.name != 'posix' or not hasattr(os, 'getuid') or not hasattr(os, 'O_NOFOLLOW'):
        raise SystemdUserError('Пользовательский systemd недоступен')
    uid = os.getuid()
    # Каждый существующий каталог контролируется пользователем или root и не
    # открыт на запись группе/остальным. Ссылки, включая конечную, запрещены.
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid not in (0, uid) or info.st_mode & 0o022):
            raise SystemdUserError('Небезопасный каталог unit')
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                or before.st_nlink != 1 or before.st_mode & 0o022 or before.st_size > _MAX_UNIT):
            raise SystemdUserError('Небезопасный файл unit')
        data = stream.read(_MAX_UNIT + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    if (len(data) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected)):
        raise SystemdUserError('Файл unit изменён или не соответствует хэшу')


def _run(argv, *, timeout, env):
    # Ограничиваем stdout и время до чтения; stderr не сохраняется и не раскрывается.
    process = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env)
    deadline = time.monotonic() + timeout
    data = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise SystemdUserError('Превышен timeout systemctl')
                block = os.read(process.stdout.fileno(), min(4096, _MAX_OUTPUT + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > _MAX_OUTPUT:
                    raise SystemdUserError('Ответ systemctl слишком велик')
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise SystemdUserError('systemctl не подтвердил операцию')
        return data.decode('utf-8')
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


class SystemdUserAdapter:
    """Управлять ровно одним пользовательским unit с проверенным файлом.

    execute=False по умолчанию. Вызовы не доказывают здоровье продукта, полной
    остановки писателей и не реализуют durable-идемпотентность UpdatePorts.
    """
    def __init__(self, manifest, *, modes, component, fragment_path, fragment_sha256, runner=None, timeout=30):
        try:
            manifest = validate_manifest(manifest)
            order = component_order(manifest, modes)
            if type(component) is not str or component not in order:
                raise SystemdUserError('Компонент не выбран')
            if (type(fragment_path) is not str or not fragment_path.startswith('/')
                    or '\\' in fragment_path or any(x in ('', '.', '..') for x in fragment_path.split('/')[1:])
                    or any(ord(c) < 32 or ord(c) == 127 for c in fragment_path)
                    or type(fragment_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', fragment_sha256) is None):
                raise SystemdUserError('Недопустимый файл unit или хэш')
            if type(timeout) not in (int, float) or not 0 < timeout <= 120:
                raise SystemdUserError('Недопустимый timeout')
            if runner is not None and not callable(runner):
                raise SystemdUserError('Недопустимый runner')
            self.unit = manifest['product_id'] + '-' + component + '.service'
            self.path = Path(fragment_path)
            if self.path.name != self.unit:
                raise SystemdUserError('Имя файла не соответствует компоненту')
            self.digest = fragment_sha256
            self.runner = _run if runner is None else runner
            self.timeout = timeout
        except (ManifestError, TypeError, ValueError):
            raise SystemdUserError('Недопустимый манифест службы') from None

    def _call(self, action):
        uid = os.getuid()
        runtime = '/run/user/' + str(uid)
        env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
               'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
        base = [_SYSTEMCTL, '--user', '--no-pager', '--no-ask-password']
        if action == 'show':
            argv = base + ['show', '--property=' + ','.join(_PROPERTIES), '--', self.unit]
        else:
            argv = base + [action, '--', self.unit]
        try:
            return self.runner(argv, timeout=self.timeout, env=env)
        except Exception:
            raise SystemdUserError('Вызов systemctl не завершён') from None

    def _show(self):
        output = self._call('show')
        if type(output) is not str or len(output) > _MAX_OUTPUT:
            raise SystemdUserError('Некорректный ответ systemd')
        values = {}
        for line in output.splitlines():
            key, separator, value = line.partition('=')
            if not separator or key not in _PROPERTIES or key in values:
                raise SystemdUserError('Некорректный ответ systemd')
            values[key] = value
        if set(values) != set(_PROPERTIES):
            raise SystemdUserError('Некорректный ответ systemd')
        return values

    def inspect(self):
        try:
            _trusted_file(self.path, self.digest)
            values = self._show()
            if (values['Id'] != self.unit
                    or values['LoadState'] != 'loaded' or values['FragmentPath'] != str(self.path)
                    or values['DropInPaths'] or values['NeedDaemonReload'] != 'no'
                    or _STATE.fullmatch(values['ActiveState']) is None
                    or _STATE.fullmatch(values['SubState']) is None):
                raise SystemdUserError('Идентичность или состояние unit не подтверждены')
            _trusted_file(self.path, self.digest)
            return {'unit': self.unit, 'active_state': values['ActiveState'], 'sub_state': values['SubState']}
        except (OSError, UnicodeError, ValueError):
            raise SystemdUserError('Не удалось проверить unit') from None

    def command(self, action, *, execute=False):
        if type(execute) is not bool or type(action) is not str or action not in ('start', 'stop'):
            raise SystemdUserError('Недопустимая операция')
        if not execute:
            return {'action': action, 'unit': self.unit, 'scope': 'user'}
        before = self.inspect()
        wanted = 'active' if action == 'start' else 'inactive'
        if before['active_state'] != wanted:
            self._call(action)
        try:
            after = self.inspect()
        except SystemdUserError:
            # Временные user-units иногда выгружаются сразу после stop. Для
            # обычных файлов исчезновение не считается подтверждённой остановкой.
            transient = Path('/run/user') / str(os.getuid()) / 'systemd/transient' / self.unit
            if action != 'stop' or self.path != transient or self.path.exists():
                raise
            values = self._show()
            if values != {'Id': self.unit, 'LoadState': 'not-found', 'ActiveState': 'inactive',
                          'SubState': 'dead', 'FragmentPath': '', 'DropInPaths': '', 'NeedDaemonReload': 'no'}:
                raise SystemdUserError('Остановка временного unit не подтверждена')
            return {'unit': self.unit, 'active_state': 'inactive', 'sub_state': 'dead', 'unit_unloaded': True}
        if after['active_state'] != wanted:
            raise SystemdUserError('Состояние unit не подтверждено')
        return after
