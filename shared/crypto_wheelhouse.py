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


def _licenses(data: bytes, package: str, names: tuple[str, ...]) -> dict[str, bytes]:
    prefix = f'{package}-{LOCKED_VERSIONS[package]}.dist-info/licenses/'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        result = {}
        for name in names:
            matches = [item for item in infos if item.filename == prefix + name]
            if len(matches) != 1 or matches[0].is_dir() or matches[0].file_size > _MAX_LICENSE:
                raise WheelhouseError('Отсутствует или неоднозначна лицензия wheel')
            result[name] = archive.read(matches[0])
        return result


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
