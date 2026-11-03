# HomenS.AI · © 2026 Serhii Khomenko
"""Офлайн-подготовка Windows runtime из install_only архива Python Build Standalone.

Архив проверяется по внешнему SHA-256, распаковываются только обычные файлы
внутри `python/`; код не исполняется. Результат — каталог с `python.exe` и
описанием `homensai-runtime.json`. Хэш не доказывает происхождение и
безопасность файлов; лицензии компонентов требуют отдельной проверки.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tarfile
from urllib.parse import urlsplit

from shared.archive import _digest, _safe_name
from shared.runtime_block import RuntimeBlockError
from shared.runtime_pbs import MAX_ARCHIVE, _check_tar_stream

DESCRIPTION = 'homensai-runtime.json'
# Не нужны службам: заголовки, библиотеки сборки, Tcl/Tk, IDLE, pip и тестовые модули.
_MINIMAL_EXCLUDED = re.compile(r'(?:include/.*|libs/.*|Scripts/.*|tcl/.*'
                               r'|Lib/(?:idlelib|turtledemo|tkinter|ensurepip|site-packages|test)/.*'
                               r'|DLLs/(?:_tkinter\.pyd|tcl[^/]*\.dll|_test[^/]*\.pyd|_ctypes_test\.pyd))\Z',
                               re.IGNORECASE)


def prepare_windows_runtime(archive_path, destination, *, expected_sha256, origin, python_version,
                            minimal=True) -> dict:
    """Распаковать проверенный архив в новый каталог destination."""
    archive_path, destination = Path(archive_path), Path(destination)
    parsed = urlsplit(origin) if type(origin) is str else None
    if (type(expected_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', expected_sha256) is None
            or type(python_version) is not str or re.fullmatch(r'3\.14\.\d{1,4}', python_version) is None
            or parsed is None or parsed.scheme != 'https' or not parsed.hostname or parsed.query
            or parsed.fragment or type(minimal) is not bool
            or destination.exists() or not destination.parent.is_dir()):
        raise RuntimeBlockError('Недопустимые параметры Windows runtime')
    with archive_path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_size > MAX_ARCHIVE:
            raise RuntimeBlockError('Архив Windows runtime слишком велик')
        size, digest = _digest(stream)
        if size != info.st_size or digest != expected_sha256:
            raise RuntimeBlockError('Хэш архива Windows runtime не совпадает')
        _check_tar_stream(stream)
        stream.seek(0)
        files, folded = [], set()
        destination.mkdir()
        with tarfile.open(fileobj=stream, mode='r:gz') as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isfile() or not member.name.startswith('python/'):
                    raise RuntimeBlockError('Недопустимая запись архива Windows runtime')
                try:
                    name = _safe_name(member.name.removeprefix('python/'))
                except ValueError:
                    raise RuntimeBlockError('Недопустимое имя в архиве Windows runtime') from None
                if name.casefold() in folded:
                    raise RuntimeBlockError('Конфликт имён в архиве Windows runtime')
                folded.add(name.casefold())
                if minimal and _MINIMAL_EXCLUDED.fullmatch(name):
                    continue
                target = destination.joinpath(*name.split('/'))
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                with source, target.open('xb') as output:
                    count, file_digest = _digest(source, output)
                if count != member.size:
                    raise RuntimeBlockError('Файл Windows runtime неполон')
                files.append({'path': name, 'size': count, 'sha256': file_digest})
    if not (destination / 'python.exe').is_file() or not (destination / 'LICENSE.txt').is_file():
        raise RuntimeBlockError('Архив Windows runtime неполон')
    description = {'format': 1, 'target': {'os': 'windows', 'architecture': 'x86_64'},
                   'python_version': python_version, 'origin': origin, 'artifact_sha256': expected_sha256,
                   'license': 'LicenseRef-Composite-Unreviewed', 'minimal': minimal,
                   'files': sorted(files, key=lambda item: item['path'])}
    (destination / DESCRIPTION).write_text(json.dumps(description, ensure_ascii=True, sort_keys=True) + '\n',
                                           encoding='utf-8')
    return {'file_count': len(files), 'python_version_declared': python_version, 'origin_verified': False,
            'runtime_executed': False, 'license_review_complete': False}
