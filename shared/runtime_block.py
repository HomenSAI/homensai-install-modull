# HomenS.AI · © 2026 Serhii Khomenko
"""Офлайн-подготовка кандидата Linux runtime по независимому перечню файлов."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from shared.archive import _digest, _safe_name, _signature
from shared.datahome import checked_path, contains
from shared.package_markers import RUNTIME_INCOMPLETE
from shared.update_plan import _object

RECIPE = 'runtime-v1.json'
PROVENANCE = 'runtime-provenance-v1.json'
MAX_FILES = 10000
MAX_FILE = 1024 * 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024 * 1024
MAX_LOCK = 4 * 1024 * 1024


class RuntimeBlockError(ValueError):
    """Кандидат runtime или его перечень не подтверждены."""


def _parse(raw):
    if len(raw) > MAX_LOCK:
        raise RuntimeBlockError('Описание runtime слишком велико')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    expected = {'format', 'target', 'python_version', 'origin', 'license', 'license_files', 'executables', 'files'}
    if (type(value) is not dict or not expected <= set(value) or set(value) - expected - {'artifact_sha256'}
            or ('artifact_sha256' in value and (type(value['artifact_sha256']) is not str
                or re.fullmatch(r'[a-f0-9]{64}', value['artifact_sha256']) is None))
            or type(value['format']) is not int
            or value['format'] != 1 or value['target'] != {'os': 'linux', 'architecture': 'x86_64'}
            or type(value['python_version']) is not str
            or re.fullmatch(r'3\.14\.(0|[1-9][0-9]{0,3})', value['python_version']) is None
            or type(value['origin']) is not str or len(value['origin']) > 1024
            or type(value['license']) is not str
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+-]{0,127}', value['license']) is None
            or type(value['files']) is not list or not 1 <= len(value['files']) <= MAX_FILES):
        raise RuntimeBlockError('Недопустимое описание runtime')
    origin = urlsplit(value['origin'])
    if (origin.scheme != 'https' or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.query or origin.fragment
            or any(ord(c) < 33 or ord(c) > 126 for c in value['origin'])):
        raise RuntimeBlockError('Недопустимый источник runtime')
    names, folded, total = [], set(), 0
    for entry in value['files']:
        if (type(entry) is not dict or set(entry) != {'path', 'size', 'sha256'}
                or type(entry['path']) is not str or len(entry['path']) > 1024
                or type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_FILE
                or type(entry['sha256']) is not str or re.fullmatch(r'[a-f0-9]{64}', entry['sha256']) is None):
            raise RuntimeBlockError('Недопустимый файл runtime')
        name = _safe_name(entry['path'])
        if name.casefold() in folded or name.casefold() in {RECIPE.casefold(), PROVENANCE.casefold(), RUNTIME_INCOMPLETE.casefold()}:
            raise RuntimeBlockError('Конфликт имён runtime')
        names.append(name)
        folded.add(name.casefold())
        total += entry['size']
    if names != sorted(names) or total > MAX_TOTAL:
        raise RuntimeBlockError('Недопустимый состав runtime')
    for field in ('executables', 'license_files'):
        items = value[field]
        if (type(items) is not list or not items or len(items) > (512 if field == 'executables' else MAX_FILES)
                or any(type(item) is not str or item not in names for item in items)
                or items != sorted(items) or len(set(items)) != len(items)):
            raise RuntimeBlockError('Недопустимый перечень runtime')
    if 'bin/python' not in value['executables']:
        raise RuntimeBlockError('Нет интерпретатора runtime')
    if (any(not name.startswith('licenses/') for name in value['license_files'])
            or set(value['license_files']) & set(value['executables'])):
        raise RuntimeBlockError('Недопустимый путь лицензии runtime')
    if any(entry['size'] == 0 for entry in value['files'] if entry['path'] in value['license_files']):
        raise RuntimeBlockError('Пустой файл лицензии runtime')
    if len(_recipe(value)) > 16384:
        raise RuntimeBlockError('Рецепт runtime слишком велик')
    return value


def _scan(root, *, marker=False):
    pending = [(root, '')]
    files, names, total, nodes = [], set(), 0, 0
    while pending:
        path, name = pending.pop()
        nodes += 1
        if nodes > MAX_FILES * 2:
            raise RuntimeBlockError('Слишком много объектов runtime')
        signature = _signature(path)
        if name:
            _safe_name(name)
            if name.casefold() in names or len(name) > 1024:
                raise RuntimeBlockError('Конфликт имён runtime')
            names.add(name.casefold())
        if stat.S_ISDIR(signature[2]):
            with os.scandir(path) as entries:
                pending.extend((Path(item.path), f'{name}/{item.name}' if name else item.name) for item in entries)
        else:
            if path.lstat().st_nlink != 1 or signature[3] > MAX_FILE or len(files) >= MAX_FILES + 3:
                raise RuntimeBlockError('Недопустимый файл runtime')
            with path.open('rb') as stream:
                size, digest = _digest(stream)
            if signature != _signature(path) or size != signature[3]:
                raise RuntimeBlockError('Файл runtime изменился')
            if marker and name == RUNTIME_INCOMPLETE:
                if size != len(b'incomplete\n') or digest != hashlib.sha256(b'incomplete\n').hexdigest():
                    raise RuntimeBlockError('Маркер runtime изменился')
                continue
            total += size
            if total > MAX_TOTAL + MAX_LOCK + 16384:
                raise RuntimeBlockError('Runtime слишком велик')
            files.append({'path': name, 'size': size, 'sha256': digest})
    return sorted(files, key=lambda item: item['path'])


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _read_limited(path, limit):
    path = checked_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise RuntimeBlockError('Недопустимый файл описания')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) != before.st_size or len(data) > limit or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise RuntimeBlockError('Файл описания изменился')
    return data


def _recipe(value):
    return (json.dumps({'format': 1, 'target': value['target'], 'python_version': value['python_version'],
                        'executables': value['executables']}, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _verify(root, *, marker=False):
    provenance = root / PROVENANCE
    raw = _read_limited(provenance, MAX_LOCK)
    value = _parse(raw)
    recipe = _recipe(value)
    expected = value['files'] + [
        {'path': PROVENANCE, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()},
        {'path': RECIPE, 'size': len(recipe), 'sha256': hashlib.sha256(recipe).hexdigest()}]
    if _scan(root, marker=marker) != sorted(expected, key=lambda item: item['path']):
        raise RuntimeBlockError('Состав runtime не совпадает с перечнем')
    return value


def verify_runtime_block(source):
    """Проверить внутреннюю согласованность блока, не удостоверяя издателя."""
    try:
        return _verify(checked_path(source))
    except RuntimeBlockError:
        raise
    except Exception:
        raise RuntimeBlockError('Не удалось проверить блок runtime') from None


def build_runtime_block(source, lock_path, destination):
    """Скопировать заранее проверенные файлы в новый блок без исполнения."""
    try:
        source, lock_path, destination = map(checked_path, (source, lock_path, destination))
        if (not source.is_dir() or not destination.parent.is_dir() or destination.exists()
                or contains(source, destination) or contains(destination, source)
                or contains(source, lock_path) or contains(destination, lock_path)):
            raise RuntimeBlockError('Недопустимые пути runtime')
        raw = _read_limited(lock_path, MAX_LOCK)
        value = _parse(raw)
        if _scan(source) != value['files']:
            raise RuntimeBlockError('Исходные файлы runtime не совпадают')
        destination.mkdir(mode=0o700)
        marker = destination / RUNTIME_INCOMPLETE
        _write(marker, b'incomplete\n')
        for item in value['files']:
            source_file = checked_path(source / item['path'])
            target = checked_path(destination / item['path'])
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(source_file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'wb') as output:
                    size, digest = _digest(stream, output)
                    output.flush()
                    os.fsync(output.fileno())
            if (size, digest) != (item['size'], item['sha256']):
                raise RuntimeBlockError('Исходный файл runtime изменился')
        _write(destination / RECIPE, _recipe(value))
        _write(destination / PROVENANCE, raw)
        if _scan(source) != value['files'] or _verify(destination, marker=True) != value:
            raise RuntimeBlockError('Runtime изменился при подготовке')
        marker.unlink()
        return {'file_count': len(value['files']), 'python_version_declared': value['python_version'],
                'license_declared': value['license'], 'origin_verified': False, 'runtime_executed': False}
    except RuntimeBlockError:
        raise
    except Exception:
        raise RuntimeBlockError('Не удалось подготовить блок runtime') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--lock', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build_runtime_block(args.source, args.lock, args.destination), sort_keys=True))
    except RuntimeBlockError as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
