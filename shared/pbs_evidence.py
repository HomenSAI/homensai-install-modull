# HomenS.AI · © 2026 Serhii Khomenko
"""Извлечение лицензионных свидетельств из закреплённого полного архива PBS."""
from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import tarfile

from shared.archive import _digest, _safe_name
from shared.datahome import checked_path
from shared.runtime_block import RuntimeBlockError
from shared.update_plan import _object

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_ENTRIES = 20000
MAX_METADATA = 4 * 1024 * 1024
MAX_LICENSE = 1024 * 1024
MAX_LICENSES = 128
MAX_ZSTD_WINDOW_LOG = 27  # 128 МиБ; предел касается обоих проходов чтения.


def _zstd_reader(stream):
    from compression.zstd import DecompressionParameter, ZstdFile
    return ZstdFile(stream, mode='rb', options={DecompressionParameter.window_log_max: MAX_ZSTD_WINDOW_LOG})


def _check_stream(stream):
    stream.seek(0)
    total = count = 0
    with _zstd_reader(stream) as data:
        while True:
            header = data.read(512)
            if len(header) != 512:
                raise RuntimeBlockError('Неполный архив свидетельств')
            if header == bytes(512):
                padding = 512
                while tail := data.read(1024 * 1024):
                    padding += len(tail)
                    if padding > 1024 * 1024 or any(tail):
                        raise RuntimeBlockError('Недопустимые данные после архива')
                break
            info = tarfile.TarInfo.frombuf(header, encoding='utf-8', errors='strict')
            count += 1
            if (count > MAX_ENTRIES or info.type not in
                    (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.SYMTYPE, tarfile.XHDTYPE)
                    or info.size < 0 or info.size > MAX_EXPANDED
                    or (info.issym() and info.size != 0)
                    or (info.type == tarfile.XHDTYPE and info.size > 65536)):
                raise RuntimeBlockError('Недопустимый заголовок свидетельств')
            total += info.size
            if total > MAX_EXPANDED:
                raise RuntimeBlockError('Архив свидетельств слишком велик')
            remaining = ((info.size + 511) // 512) * 512
            while remaining:
                data_block = data.read(min(remaining, 1024 * 1024))
                if not data_block:
                    raise RuntimeBlockError('Неполный архив свидетельств')
                remaining -= len(data_block)
    stream.seek(0)


def read_pbs_evidence(archive_path, *, expected_sha256, python_version):
    """Вернуть метаданные и тексты лицензий; не подтверждать право выпуска."""
    try:
        if (type(expected_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', expected_sha256) is None
                or type(python_version) is not str
                or re.fullmatch(r'3\.14\.(0|[1-9][0-9]{0,3})', python_version) is None):
            raise RuntimeBlockError('Недопустимые параметры свидетельств')
        path = checked_path(archive_path)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_ARCHIVE:
                raise RuntimeBlockError('Недопустимый архив свидетельств')
            size, digest = _digest(stream)
            if size != before.st_size or digest != expected_sha256:
                raise RuntimeBlockError('Хэш архива свидетельств не совпадает')
            _check_stream(stream)
            metadata, install_license, licenses, seen, count = None, None, {}, set(), 0
            with _zstd_reader(stream) as data, tarfile.open(fileobj=data, mode='r|') as archive:
                for item in archive:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise RuntimeBlockError('Слишком много объектов свидетельств')
                    if item.name == 'python/PYTHON.json':
                        if metadata is not None or not item.isfile() or item.size > MAX_METADATA:
                            raise RuntimeBlockError('Метаданные неоднозначны')
                        source = archive.extractfile(item)
                        if source is None:
                            raise RuntimeBlockError('Метаданные недоступны')
                        with source:
                            metadata = source.read(MAX_METADATA + 1)
                        if len(metadata) != item.size:
                            raise RuntimeBlockError('Метаданные неполны')
                    elif item.name == 'python/install/lib/python3.14/LICENSE.txt':
                        if install_license is not None or not item.isfile() or not 0 < item.size <= MAX_LICENSE:
                            raise RuntimeBlockError('Лицензия установки неоднозначна')
                        source = archive.extractfile(item)
                        if source is None:
                            raise RuntimeBlockError('Лицензия установки недоступна')
                        with source:
                            body = source.read(MAX_LICENSE + 1)
                        if len(body) != item.size:
                            raise RuntimeBlockError('Лицензия установки неполна')
                        install_license = hashlib.sha256(body).hexdigest()
                    elif item.name.startswith('python/licenses/'):
                        name = _safe_name(item.name)
                        base = name.removeprefix('python/licenses/')
                        if (not item.isfile() or re.fullmatch(r'LICENSE\.[A-Za-z0-9-]+\.txt', base) is None
                                or name.casefold() in seen or item.size == 0 or item.size > MAX_LICENSE
                                or len(licenses) >= MAX_LICENSES):
                            raise RuntimeBlockError('Недопустимая лицензия свидетельств')
                        seen.add(name.casefold())
                        source = archive.extractfile(item)
                        if source is None:
                            raise RuntimeBlockError('Лицензия недоступна')
                        with source:
                            data = source.read(MAX_LICENSE + 1)
                        if len(data) != item.size:
                            raise RuntimeBlockError('Лицензия неполна')
                        licenses[base] = data
            if metadata is None or install_license is None or not licenses:
                raise RuntimeBlockError('Свидетельства отсутствуют')
            info = json.loads(metadata.decode('utf-8'), object_pairs_hook=_object)
            if (type(info) is not dict or type(info.get('version')) is not str or info['version'] != '8'
                    or info.get('python_version') != python_version
                    or info.get('target_triple') != 'x86_64-unknown-linux-gnu'
                    or info.get('python_implementation_name') != 'cpython'
                    or info.get('build_options') != 'pgo+lto'
                    or info.get('license_path') != 'licenses/LICENSE.cpython.txt'
                    or 'LICENSE.cpython.txt' not in licenses or type(info.get('licenses')) is not list
                    or not info['licenses'] or any(type(item) is not str for item in info['licenses'])
                    or type(info.get('build_info')) is not dict
                    or type(info['build_info'].get('extensions')) is not dict):
                raise RuntimeBlockError('Метаданные не соответствуют кандидату')
            after, current = os.fstat(stream.fileno()), path.lstat()
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                raise RuntimeBlockError('Архив свидетельств изменился')
            return metadata, licenses, install_license
    except RuntimeBlockError:
        raise
    except Exception:
        raise RuntimeBlockError('Не удалось проверить свидетельства Python') from None
