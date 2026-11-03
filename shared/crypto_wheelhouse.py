# HomenS.AI · © 2026 Serhii Khomenko
"""Подготовка кандидата офлайн-зависимостей из заранее загруженных wheels.

Не скачивает, не устанавливает и не исполняет сторонний код. Этот блок не
является самостоятельно доверенным или готовым к распространению выпуском.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import zipfile

from shared.crypto_policy import LOCKED_VERSIONS
from shared.archive import _safe_name
from shared.datahome import DataHomeError, checked_path, contains
from shared.package_markers import CRYPTO_INCOMPLETE

# Имена и хэши относятся только к CPython 3.14 x86-64 (Windows / glibc Linux).
_ARTIFACTS = {
    'cryptography-50.0.2-cp311-abi3-manylinux_2_28_x86_64.whl': ('cryptography', '4061c0079120205fb760c58acab6443e217307dcf05e3702cf970e0689972856', ('LICENSE', 'LICENSE.APACHE', 'LICENSE.BSD')),
    'cryptography-50.0.2-cp311-abi3-win_amd64.whl': ('cryptography', '7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c', ('LICENSE', 'LICENSE.APACHE', 'LICENSE.BSD')),
    'cffi-2.1.1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl': ('cffi', 'b0431303acaea1089ad4b3e9ce4e6518193def1118d4073ca848635ee4ea2e96', ('LICENSE',)),
    'cffi-2.1.1-cp314-cp314-win_amd64.whl': ('cffi', '3222ba5d678f80a030e6afbcc33dc1ae5cb45facabb61cee2c7016b8432fde48', ('LICENSE',)),
    'pycparser-3.0-py3-none-any.whl': ('pycparser', 'b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992', ('LICENSE',)),
}
_LOCK = Path(__file__).resolve().parent.parent / 'requirements-crypto.txt'
_MAX_WHEEL = 64 * 1024 * 1024
_MAX_LICENSE = 128 * 1024
_MAX_WHEEL_ENTRIES = 50000
_MAX_WHEEL_EXPANDED = 512 * 1024 * 1024
_METADATA = 'crypto-wheelhouse.json'


class WheelhouseError(ValueError):
    """Не удалось безопасно подготовить кандидата."""


def _lock_check(data: bytes) -> None:
    """Разрешить только точно известные зависимости и полный набор хэшей."""
    lines = [line.strip() for line in data.decode('utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if lines[:2] != ['--require-hashes', '--only-binary=:all:']:
        raise WheelhouseError('Некорректные параметры lock')
    groups = {}
    name = None
    continued = False
    for line in lines[2:]:
        match = re.fullmatch(r'([a-z]+)==([0-9.]+)\s*\\', line)
        if match:
            if continued:
                raise WheelhouseError('Некорректное продолжение lock')
            name, version = match.groups()
            if name in groups or LOCKED_VERSIONS.get(name) != version:
                raise WheelhouseError('Версии lock не совпадают')
            groups[name] = []
            continued = True
            continue
        match = re.fullmatch(r'--hash=sha256:([a-f0-9]{64})(\s*\\)?', line)
        if not match or name is None or not continued:
            raise WheelhouseError('Недопустимая строка lock')
        groups[name].append(match.group(1))
        continued = bool(match.group(2))
    expected = {name: [] for name in LOCKED_VERSIONS}
    for package, digest, _ in _ARTIFACTS.values():
        expected[package].append(digest)
    if continued or set(groups) != set(expected) or any(sorted(groups[n]) != sorted(expected[n]) for n in expected):
        raise WheelhouseError('Хэши lock не совпадают')


def _read_file(path: Path, limit: int) -> bytes:
    checked_path(path)
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
             | getattr(os, 'O_BINARY', 0))
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise WheelhouseError('Недопустимый файл блока')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) != before.st_size or len(data) > limit or
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise WheelhouseError('Файл блока изменился во время чтения')
    return data


def _read_wheel(path: Path, expected: str) -> bytes:
    data = _read_file(path, _MAX_WHEEL)
    if hashlib.sha256(data).hexdigest() != expected:
        raise WheelhouseError('SHA-256 wheel не совпадает')
    return data


def _validate_wheel(archive: zipfile.ZipFile) -> None:
    """Отклонить опасные и неоднозначные ZIP-записи до публикации wheel."""
    infos = archive.infolist()
    if not infos or len(infos) > _MAX_WHEEL_ENTRIES:
        raise WheelhouseError('Недопустимый состав wheel')
    names = {}
    total = 0
    for info in infos:
        if (info.filename != info.orig_filename or info.flag_bits & 1
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise WheelhouseError('Недопустимая запись wheel')
        try:
            name = _safe_name(info.filename, directory=info.is_dir())
        except ValueError:
            raise WheelhouseError('Недопустимый путь wheel') from None
        folded = name.casefold()
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode) if info.create_system == 3 else 0
        allowed = (0, stat.S_IFDIR) if info.is_dir() else (0, stat.S_IFREG)
        if (folded in names or kind not in allowed or (info.is_dir() and info.file_size != 0)
                or info.file_size > _MAX_WHEEL_EXPANDED):
            raise WheelhouseError('Конфликт или недопустимый тип записи wheel')
        names[folded] = info.is_dir()
        total += info.file_size
        if total > _MAX_WHEEL_EXPANDED:
            raise WheelhouseError('Распакованный wheel слишком велик')
    for name in names:
        parts = name.split('/')
        if any(names.get('/'.join(parts[:index])) is False for index in range(1, len(parts))):
            raise WheelhouseError('Файл wheel перекрывает каталог')


def _licenses(data: bytes, package: str, names: tuple[str, ...]) -> dict[str, bytes]:
    prefix = f'{package}-{LOCKED_VERSIONS[package]}.dist-info/licenses/'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        _validate_wheel(archive)
        infos = archive.infolist()
        result = {}
        for name in names:
            matches = [item for item in infos if item.filename == prefix + name]
            if len(matches) != 1 or matches[0].is_dir() or matches[0].file_size > _MAX_LICENSE:
                raise WheelhouseError('Отсутствует или неоднозначна лицензия wheel')
            result[name] = archive.read(matches[0])
        return result


def verify_archived_wheelhouse(archive, files, marker: str) -> None:
    """Проверить внутреннюю согласованность блока в ZIP без lock сборщика."""
    parent = marker.rpartition('/')[0]
    prefix = parent + '/' if parent else ''
    inventory = {item['path']: item for item in files}

    def read(name, limit):
        item = inventory.get(prefix + name)
        if item is None or item['size'] > limit:
            raise WheelhouseError('Недопустимый файл криптоблока')
        return archive.read('payload/' + prefix + name)

    raw = read(_METADATA, 1024 * 1024)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise WheelhouseError('Повтор поля криптоблока')
            result[key] = value
        return result
    metadata = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
    if (type(metadata) is not dict or set(metadata) != {'lock_files', 'vendor_wheels', 'license_files'}
            or raw != (json.dumps(metadata, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode()
            or type(metadata['lock_files']) is not dict or set(metadata['lock_files']) != {'linux', 'windows'}
            or type(metadata['vendor_wheels']) is not list or not 1 <= len(metadata['vendor_wheels']) <= 128
            or type(metadata['license_files']) is not list or not 1 <= len(metadata['license_files']) <= 1024):
        raise WheelhouseError('Недопустимое описание криптоблока')
    expected = {_METADATA}
    def entry(value, path):
        if (type(value) is not dict or set(value) != {'file', 'sha256'}
                or value['file'] != path or type(value['sha256']) is not str
                or re.fullmatch(r'[a-f0-9]{64}', value['sha256']) is None
                or path in expected):
            raise WheelhouseError('Недопустимая запись криптоблока')
        expected.add(path)
        item = inventory.get(prefix + path)
        if item is None or item['sha256'] != value['sha256']:
            raise WheelhouseError('Хэш криптоблока не совпадает')

    lock = None
    for system in ('linux', 'windows'):
        path = f'locks/{system}.lock'
        entry(metadata['lock_files'][system], path)
        data = read(path, 1024 * 1024)
        if lock is not None and lock != data:
            raise WheelhouseError('Lock криптоблока не совпадают')
        lock = data
    wheels = {}
    for item in metadata['vendor_wheels']:
        if type(item) is not dict or type(item.get('file')) is not str:
            raise WheelhouseError('Недопустимый wheel')
        path = item['file']
        match = re.fullmatch(r'vendor/([A-Za-z0-9_]+)-([0-9]+(?:\.[0-9]+)*)-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl', path)
        if not match:
            raise WheelhouseError('Недопустимое имя wheel')
        entry(item, path)
        if inventory[prefix + path]['size'] > _MAX_WHEEL:
            raise WheelhouseError('Wheel слишком велик')
        wheels[path[7:]] = (match.group(1), match.group(2))
    locks = {}
    lines = [line.strip() for line in lock.decode('utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if lines[:2] != ['--require-hashes', '--only-binary=:all:']:
        raise WheelhouseError('Недопустимый lock криптоблока')
    current, continued = None, False
    for line in lines[2:]:
        match = re.fullmatch(r'([a-z]+)==([0-9.]+)\s*\\', line)
        if match:
            if continued or match.group(1) in locks:
                raise WheelhouseError('Недопустимый lock криптоблока')
            current = match.group(1)
            locks[current] = (match.group(2), [])
            continued = True
            continue
        match = re.fullmatch(r'--hash=sha256:([a-f0-9]{64})(\s*\\)?', line)
        if not match or not continued or current is None:
            raise WheelhouseError('Недопустимый lock криптоблока')
        locks[current][1].append(match.group(1))
        continued = bool(match.group(2))
    wheel_locks = {}
    for item in metadata['vendor_wheels']:
        name, version = wheels[item['file'][7:]]
        if name in wheel_locks and wheel_locks[name][0] != version:
            raise WheelhouseError('Версии wheel не совпадают')
        wheel_locks.setdefault(name, (version, []))[1].append(item['sha256'])
    if continued or set(locks) != set(wheel_locks) or any(
            locks[name][0] != wheel_locks[name][0] or sorted(locks[name][1]) != sorted(wheel_locks[name][1])
            for name in locks):
        raise WheelhouseError('Lock и wheels не совпадают')
    licenses = {}
    for item in metadata['license_files']:
        if type(item) is not dict or type(item.get('file')) is not str:
            raise WheelhouseError('Недопустимая лицензия')
        path = item['file']
        parts = path.split('/')
        if len(parts) != 3 or parts[0] != 'licenses' or parts[1] not in wheels or not re.fullmatch(r'[A-Za-z0-9_.-]+', parts[2]):
            raise WheelhouseError('Недопустимый путь лицензии')
        entry(item, path)
        licenses.setdefault(parts[1], {})[parts[2]] = read(path, _MAX_LICENSE)
    if set(licenses) != set(wheels):
        raise WheelhouseError('Не хватает лицензии wheel')
    for filename, (name, version) in wheels.items():
        data = read('vendor/' + filename, _MAX_WHEEL)
        with zipfile.ZipFile(io.BytesIO(data)) as wheel:
            _validate_wheel(wheel)
            for license_name, contents in licenses[filename].items():
                member = f'{name}-{version}.dist-info/licenses/{license_name}'
                matches = [info for info in wheel.infolist() if info.filename == member]
                if (len(matches) != 1 or matches[0].is_dir() or matches[0].file_size > _MAX_LICENSE
                        or wheel.read(matches[0]) != contents):
                    raise WheelhouseError('Лицензия wheel не совпадает')
    if {name[len(prefix):] for name in inventory if name.startswith(prefix)} != expected:
        raise WheelhouseError('Состав криптоблока не совпадает')


def _write(path: Path, data: bytes) -> None:
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_wheelhouse(root: Path, *, allow_marker: bool) -> dict:
    lock = _LOCK.read_bytes()
    _lock_check(lock)
    lock_hash = hashlib.sha256(lock).hexdigest()
    expected = {'locks', 'vendor', 'licenses', _METADATA}
    if allow_marker:
        expected.add(CRYPTO_INCOMPLETE)
    lock_files = {}
    for system in ('linux', 'windows'):
        name = f'locks/{system}.lock'
        expected.add(name)
        if _read_file(root / name, 1024 * 1024) != lock:
            raise WheelhouseError('Lock блока не совпадает')
        lock_files[system] = {'file': name, 'sha256': lock_hash}
    vendor, license_files = [], []
    for filename, (package, digest, names) in sorted(_ARTIFACTS.items()):
        name = f'vendor/{filename}'
        expected.update((name, f'licenses/{filename}'))
        data = _read_wheel(root / name, digest)
        vendor.append({'file': name, 'sha256': digest})
        for license_name, contents in _licenses(data, package, names).items():
            license_path = f'licenses/{filename}/{license_name}'
            expected.add(license_path)
            if _read_file(root / license_path, _MAX_LICENSE) != contents:
                raise WheelhouseError('Лицензия блока не совпадает')
            license_files.append({'file': license_path, 'sha256': hashlib.sha256(contents).hexdigest()})
    result = {'lock_files': lock_files, 'vendor_wheels': vendor, 'license_files': license_files}
    metadata = (json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode()
    if _read_file(root / _METADATA, 1024 * 1024) != metadata:
        raise WheelhouseError('Описание блока не совпадает')
    if allow_marker and _read_file(root / CRYPTO_INCOMPLETE, 64) != b'incomplete\n':
        raise WheelhouseError('Маркер блока изменился')
    actual = set()
    pending = [(root, '')]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                name = f'{prefix}/{entry.name}' if prefix else entry.name
                if name not in expected or name in actual:
                    raise WheelhouseError('Лишний файл блока')
                actual.add(name)
                info = entry.stat(follow_symlinks=False)
                if getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise WheelhouseError('Недопустимый объект блока')
                if stat.S_ISDIR(info.st_mode):
                    pending.append((Path(entry.path), name))
                elif not stat.S_ISREG(info.st_mode):
                    raise WheelhouseError('Недопустимый объект блока')
    if actual != expected:
        raise WheelhouseError('Неполный блок')
    return result


def verify_wheelhouse(source: str | Path) -> dict:
    """Проверить точный состав кандидата, даже если маркер отсутствует."""
    try:
        root = checked_path(source)
        if not root.is_dir():
            raise WheelhouseError('Требуется каталог блока')
        return _verify_wheelhouse(root, allow_marker=False)
    except (OSError, UnicodeError, KeyError, ValueError, RuntimeError, zipfile.BadZipFile, DataHomeError) as error:
        if isinstance(error, WheelhouseError):
            raise
        raise WheelhouseError('Не удалось проверить зависимости') from None


def build_wheelhouse(source: str | Path, destination: str | Path) -> dict:
    """Создать новый каталог кандидата; незавершённый каталог имеет маркер."""
    try:
        source, destination = checked_path(source), checked_path(destination)
        if (not source.is_dir() or not destination.parent.is_dir() or destination.exists()
                or contains(source, destination) or contains(destination, source)):
            raise WheelhouseError('Недопустимый источник или назначение')
        lock = _LOCK.read_bytes()
        _lock_check(lock)
        # Все входы проверяются до создания выходного каталога.
        wheels = {}
        licenses = {}
        for filename, (package, digest, names) in sorted(_ARTIFACTS.items()):
            data = _read_wheel(source / filename, digest)
            wheels[filename] = data
            licenses[filename] = _licenses(data, package, names)
        destination.mkdir(mode=0o700)
        marker = destination / CRYPTO_INCOMPLETE
        _write(marker, b'incomplete\n')
        for directory in ('vendor', 'locks', 'licenses'):
            (destination / directory).mkdir()
        lock_hash = hashlib.sha256(lock).hexdigest()
        lock_files = {}
        for system in ('linux', 'windows'):
            relative = f'locks/{system}.lock'
            _write(destination / relative, lock)
            lock_files[system] = {'file': relative, 'sha256': lock_hash}
        vendor = []
        license_files = []
        for filename, data in wheels.items():
            relative = f'vendor/{filename}'
            _write(destination / relative, data)
            vendor.append({'file': relative, 'sha256': _ARTIFACTS[filename][1]})
            license_dir = destination / 'licenses' / filename
            license_dir.mkdir()
            for name, contents in licenses[filename].items():
                _write(license_dir / name, contents)
                license_files.append({'file': f'licenses/{filename}/{name}',
                                      'sha256': hashlib.sha256(contents).hexdigest()})
        result = {'lock_files': lock_files, 'vendor_wheels': vendor, 'license_files': license_files}
        _write(destination / _METADATA,
               (json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode())
        if _verify_wheelhouse(destination, allow_marker=True) != result:
            raise WheelhouseError('Описание проверенного блока не совпадает')
        marker.unlink()
        return result
    except (OSError, UnicodeError, KeyError, ValueError, RuntimeError, zipfile.BadZipFile, DataHomeError) as error:
        if isinstance(error, WheelhouseError):
            raise
        raise WheelhouseError('Не удалось подготовить зависимости') from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build_wheelhouse(args.source, args.destination), ensure_ascii=True, sort_keys=True))
    except WheelhouseError as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
