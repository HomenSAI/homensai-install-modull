# HomenS.AI · © 2026 Serhii Khomenko
"""Выбор wheels по lock-файлу и офлайн-распаковка в каталог зависимостей выпуска.

Сеть не используется, код из wheels не исполняется. Хэши lock-файла задаёт
издатель; совпадение хэша не доказывает отсутствие вредоносного кода.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import zipfile

from shared.crypto_wheelhouse import WheelhouseError, _validate_wheel
from shared.release_inventory import _pinned

MAX_WHEEL_BYTES = 64 * 1024 * 1024
# Цели комплекта: имя каталога зависимостей и допустимые платформенные теги.
TARGETS = ('linux-x86_64', 'windows-x86_64')
_WHEEL_NAME = re.compile(r'(?P<name>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)(?:-[0-9][A-Za-z0-9_.]*)?'
                         r'-(?P<python>[A-Za-z0-9_.]+)-(?P<abi>[A-Za-z0-9_.]+)-(?P<platform>[A-Za-z0-9_.]+)\.whl\Z')
_MANYLINUX = re.compile(r'manylinux_(\d+)_(\d+)_x86_64\Z')
_LEGACY = {'manylinux1_x86_64': (2, 5), 'manylinux2010_x86_64': (2, 12), 'manylinux2014_x86_64': (2, 17)}


class KitWheelError(ValueError):
    """Зависимости не подготовлены; сообщение не содержит содержимого файлов."""


def _normalized(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def wheel_tags(filename: str) -> dict:
    """Разобрать имя wheel по PEP 427; сжатые наборы тегов раскрываются."""
    match = _WHEEL_NAME.fullmatch(filename)
    if match is None:
        raise KitWheelError('Недопустимое имя wheel')
    return {'name': _normalized(match['name']), 'version': match['version'],
            'python': set(match['python'].split('.')), 'abi': set(match['abi'].split('.')),
            'platform': set(match['platform'].split('.'))}


def compatible(filename: str, target: str, *, glibc: tuple[int, int] = (2, 28)) -> int:
    """Вернуть приоритет совместимости с CPython 3.14 цели (0 — несовместим)."""
    tags = wheel_tags(filename)
    if target not in TARGETS:
        raise KitWheelError('Неизвестная цель')

    def interpreter_ok(python, abi):
        # Полные сочетания тегов CPython 3.14 (как packaging.tags): чистый Python —
        # py3/cp314 без ABI; abi3 — cp32…cp314; собственный ABI — только cp314.
        if abi == 'none':
            found = re.fullmatch(r'py3(\d{0,2})', python)
            return python == 'cp314' or (found is not None and (not found[1] or int(found[1]) <= 14))
        if abi == 'abi3':
            found = re.fullmatch(r'cp3(\d{1,2})', python)
            return found is not None and 2 <= int(found[1]) <= 14
        return abi == 'cp314' and python == 'cp314'
    if not any(interpreter_ok(python, abi) for python in tags['python'] for abi in tags['abi']):
        return 0
    if 'any' in tags['platform']:
        return 1
    if target == 'windows-x86_64':
        return 2 if 'win_amd64' in tags['platform'] else 0
    for platform in tags['platform']:
        legacy = _LEGACY.get(platform)
        found = _MANYLINUX.fullmatch(platform)
        version = legacy or ((int(found[1]), int(found[2])) if found else None)
        if version is not None and version <= glibc:
            return 2
    return 0


def read_lock(path: Path) -> list[dict]:
    """Прочитать строгий lock: name==version и хотя бы один --hash=sha256."""
    raw = Path(path).read_bytes()
    if len(raw) > 1024 * 1024:
        raise KitWheelError('Lock-файл слишком велик')
    packages = _pinned(raw)
    if packages is None:
        raise KitWheelError('Lock-файл должен содержать name==version и --hash=sha256 для каждой строки')
    return packages


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def select_wheels(lock: list[dict], wheelhouse: Path, target: str, *, glibc=(2, 28)) -> list[Path]:
    """Для каждой строки lock выбрать ровно один подходящий wheel из каталога."""
    wheelhouse = Path(wheelhouse)
    candidates = {}
    for path in sorted(wheelhouse.iterdir()):
        if path.name.endswith('.whl') and path.is_file() and not path.is_symlink():
            try:
                tags = wheel_tags(path.name)
            except KitWheelError:
                continue
            candidates.setdefault((tags['name'], tags['version']), []).append(path)
    selected = []
    for item in lock:
        options = []
        for path in candidates.get((item['name'], item['version']), []):
            priority = compatible(path.name, target, glibc=glibc)
            if priority and _digest(path) in item['sha256']:
                options.append((priority, path.name, path))
        if not options:
            raise KitWheelError(f'Нет wheel для {item["name"]}=={item["version"]} ({target}) с хэшем из lock')
        options.sort(key=lambda option: (-option[0], option[1]))
        selected.append(options[0][2])
    return selected


def install_wheel(data: bytes, destination: Path, owned: dict[str, str]) -> dict:
    """Распаковать purelib/platlib wheel в destination без исполнения кода.

    owned отображает уже записанные пути (в нижнем регистре) на имя wheel;
    пересечение файлов разных wheel отклоняется. Каталоги .data/scripts,
    .data/headers и .data/data не нужны для импорта и пропускаются.
    """
    if len(data) > MAX_WHEEL_BYTES:
        raise KitWheelError('Wheel слишком велик')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        _validate_wheel(archive)
    except (zipfile.BadZipFile, WheelhouseError):
        raise KitWheelError('Недопустимый архив wheel') from None
    destination = Path(destination)
    written, skipped = 0, 0
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = {info.filename.split('/', 1)[0] for info in infos}
        dist_info = [name for name in names if name.endswith('.dist-info')]
        if len(dist_info) != 1:
            raise KitWheelError('Wheel должен содержать ровно один .dist-info')
        data_dir = dist_info[0][:-len('.dist-info')] + '.data'
        for info in infos:
            name = info.filename
            if name.startswith(data_dir + '/'):
                parts = name.split('/', 2)
                if len(parts) < 3 or not parts[2]:
                    raise KitWheelError('Недопустимая запись .data')
                if parts[1] in ('purelib', 'platlib'):
                    name = parts[2]
                elif parts[1] in ('scripts', 'headers', 'data'):
                    # Сценарии, заголовки и данные префикса (например, man-страницы) импорту не нужны.
                    skipped += 1
                    continue
                else:
                    raise KitWheelError('Wheel содержит неизвестный каталог .data')
            folded = name.casefold()
            if folded in owned:
                raise KitWheelError('Файлы разных wheel пересекаются')
            target = destination.joinpath(*name.split('/'))
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
            with os.fdopen(fd, 'wb') as output, archive.open(info) as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    output.write(block)
            owned[folded] = dist_info[0]
            written += 1
    return {'files': written, 'skipped': skipped}
