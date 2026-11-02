"""Защищённая подготовка Compose-файла по подписанному плану v2."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from shared.datahome import checked_path
from shared.docker_recipe import plan_signed_compose
from shared.effect_receipt import read_effect_receipt
from shared.update_plan import _atomic_write, operation_lock


class DockerPrepareError(RuntimeError):
    """Подготовка не подтверждена; подробности входа не раскрываются."""


def _read_compose(path, expected):
    path = checked_path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size != len(expected)):
            raise DockerPrepareError('Файл недоступен')
        body = stream.read(len(expected) + 1)
        after = os.fstat(stream.fileno())
    current = checked_path(path).lstat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
                              info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
    if body != expected or signature(before) != signature(after) or signature(after) != signature(current):
        raise DockerPrepareError('Файл изменился')


def _create_compose(path, content):
    descriptor = os.open(checked_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def prepare_signed_compose(package_path: str, envelope: bytes, *, trusted_public_key: bytes,
                           expected_product_id: str, expected_version: str, target: dict,
                           modes: tuple[str, ...], instance_id: str, work_dir: str,
                           operation_id: str, verify_only: bool = False) -> dict:
    """Подготовить файл и отметку, не исполняя Docker и не заменяя чужие файлы.

    Прерванный pending не повторяется автоматически. Родители work_dir и
    вызывающая сторона должны быть доверенными.
    """
    try:
        if (os.name != 'posix' or not hasattr(os, 'getuid') or not hasattr(os, 'O_NOFOLLOW')
                or not hasattr(os, 'O_DIRECTORY') or type(verify_only) is not bool or type(operation_id) is not str
                or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None):
            raise DockerPrepareError('Среда или ключ не поддерживаются')
        root = checked_path(work_dir)
        info = root.lstat()
        if (root == root.parent or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise DockerPrepareError('Рабочий каталог не защищён')
        package = checked_path(package_path)
        options = {'trusted_public_key': trusted_public_key, 'expected_product_id': expected_product_id,
                   'expected_version': expected_version, 'target': target, 'modes': modes,
                   'instance_id': instance_id}
        plan = plan_signed_compose(str(package), envelope, **options)
        content = plan['compose_json'].encode('ascii')
        context = {'package': str(package), 'work': str(root), 'plan': plan['compose_sha256'],
                   'envelope': hashlib.sha256(envelope).hexdigest(),
                   'key': hashlib.sha256(trusted_public_key).hexdigest(),
                   'product': expected_product_id, 'version': expected_version,
                   'target': target, 'modes': modes, 'instance': instance_id, 'operation': operation_id}
        binding = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=True,
                                           separators=(',', ':')).encode('ascii')).hexdigest()
        marker = checked_path(root / ('prepare-compose-' + operation_id + '.json'))
        lock = checked_path(root / ('prepare-compose-' + operation_id + '.lock'))
        destination = checked_path(root / ('compose-' + instance_id + '.json'))
        pending = {'version': 1, 'binding': binding, 'operation_id': operation_id, 'status': 'pending'}
        done = {**pending, 'status': 'done'}
        with operation_lock(lock):
            record = read_effect_receipt(marker)
            if verify_only and record is None:
                raise DockerPrepareError('Нет подтверждённой подготовки')
            if record is not None and (type(record.get('version')) is not int or record != done):
                raise DockerPrepareError('Предыдущая операция требует проверки')
            if record is None:
                if destination.exists():
                    raise DockerPrepareError('Существующий файл не заменяется')
                _atomic_write(marker, pending)
                _create_compose(destination, content)
            _read_compose(destination, content)
            if (plan_signed_compose(str(package), envelope, **options) != plan
                    or read_effect_receipt(marker) != (pending if record is None else done)):
                raise DockerPrepareError('Данные изменились')
            if record is None:
                _atomic_write(marker, done)
            final = read_effect_receipt(marker)
            if final is None or type(final.get('version')) is not int or final != done:
                raise DockerPrepareError('Отметка не подтверждена')
            _read_compose(destination, content)
        return {'state': 'verified' if verify_only else 'prepared',
                'compose_name': destination.name, 'compose_sha256': plan['compose_sha256'],
                'instance_id': instance_id, 'host_verified': False, 'images_verified': False,
                'containers_started': False, 'installation_complete': False}
    except Exception:
        raise DockerPrepareError('Не удалось подтвердить подготовку Compose') from None
