# HomenS.AI · © 2026 Serhii Khomenko
"""Офлайн-преобразование проверенного install_only архива Python Build Standalone."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import tarfile
import tempfile
from urllib.parse import urlsplit

from shared.archive import _digest, _safe_name
from shared.datahome import checked_path, contains
from shared.runtime_block import RuntimeBlockError, build_runtime_block

MAX_ARCHIVE = 128 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_EXPANDED = 1024 * 1024 * 1024


def _check_tar_stream(stream):
    """Ограничить распаковку и отклонить расширенные TAR-заголовки до tarfile."""
    stream.seek(0)
    count, total = 0, 0
    with gzip.GzipFile(fileobj=stream, mode='rb') as data:
        while True:
            block = data.read(512)
            if len(block) != 512:
                raise RuntimeBlockError('Неполный архив Python')
            if block == bytes(512):
                padding = 512
                while tail := data.read(1024 * 1024):
                    padding += len(tail)
                    if padding > 1024 * 1024 or any(tail):
                        raise RuntimeBlockError('Недопустимые данные после архива')
                break
            item = tarfile.TarInfo.frombuf(block, encoding='utf-8', errors='strict')
            count += 1
            if (count > MAX_ENTRIES or item.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.SYMTYPE)
                    or item.size < 0 or item.size > MAX_EXPANDED or (item.issym() and item.size != 0)):
                raise RuntimeBlockError('Недопустимый заголовок архива Python')
            total += item.size
            if total > MAX_EXPANDED:
                raise RuntimeBlockError('Архив Python слишком велик')
            remaining = ((item.size + 511) // 512) * 512
            while remaining:
                chunk = data.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeBlockError('Неполный файл архива Python')
                remaining -= len(chunk)
    stream.seek(0)


def _members(archive):
    table, folded, total, count = {}, set(), 0, 0
    for item in archive:
        count += 1
        if count > MAX_ENTRIES or not (item.isfile() or item.issym()):
            raise RuntimeBlockError('Недопустимый состав архива Python')
        name = _safe_name(item.name)
        if (not name.startswith('python/') or len(name) > 1000
                or item.size < 0 or item.size > MAX_EXPANDED):
            raise RuntimeBlockError('Недопустимое имя архива Python')
        # terminfo содержит регистрозависимые имена, которые запрещены в общем ZIP.
        if name.casefold().startswith('python/share/terminfo/'):
            continue
        if name.casefold() in folded:
            raise RuntimeBlockError('Конфликт имён архива Python')
        folded.add(name.casefold())
        total += item.size if item.isfile() else 0
        if total > MAX_EXPANDED:
            raise RuntimeBlockError('Архив Python слишком велик')
        table[name] = item
    if 'python/bin/python' not in table or 'python/lib/python3.14/LICENSE.txt' not in table:
        raise RuntimeBlockError('Архив Python неполон')
    return table


def _resolve(name, table):
    current, visited = name, set()
    while current in table and table[current].issym():
        if current in visited or len(visited) >= 40:
            raise RuntimeBlockError('Циклическая ссылка Python')
        visited.add(current)
        link = table[current].linkname
        if (type(link) is not str or not link or link.startswith('/') or '\\' in link
                or any(ord(char) < 32 or ord(char) == 127 for char in link)):
            raise RuntimeBlockError('Недопустимая ссылка Python')
        current = posixpath.normpath(posixpath.join(posixpath.dirname(current), link))
        if not current.startswith('python/'):
            raise RuntimeBlockError('Ссылка Python выходит за архив')
    if current not in table or not table[current].isfile():
        raise RuntimeBlockError('Цель ссылки Python отсутствует')
    return current


def _copy(input_stream, path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as output:
        size, digest = _digest(input_stream, output)
        output.flush()
        os.fsync(output.fileno())
    return size, digest


def prepare_pbs_archive(archive_path, destination, *, expected_sha256, origin, python_version,
                        full_archive_path=None, full_sha256=None):
    """Подготовить блок из заранее загруженного архива; доверие к хэшу внешнее.

    Ссылки материализуются как обычные файлы; terminfo исключается. Утверждений
    о полной лицензии, происхождении, ABI или безопасности результата нет.
    """
    try:
        archive_path, destination = checked_path(archive_path), checked_path(destination)
        if (type(expected_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', expected_sha256) is None
                or type(python_version) is not str or re.fullmatch(r'3\.14\.(0|[1-9][0-9]{0,3})', python_version) is None
                or type(origin) is not str or not destination.parent.is_dir() or destination.exists()
                or contains(destination, archive_path)):
            raise RuntimeBlockError('Недопустимые параметры архива Python')
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or len(origin) > 1024
                or any(ord(char) < 33 or ord(char) > 126 for char in origin)):
            raise RuntimeBlockError('Недопустимый источник Python')
        evidence = None
        if (full_archive_path is None) != (full_sha256 is None):
            raise RuntimeBlockError('Требуются архив свидетельств и его хэш')
        if full_archive_path is not None:
            from shared.pbs_evidence import read_pbs_evidence
            full_archive_path = checked_path(full_archive_path)
            if contains(destination, full_archive_path):
                raise RuntimeBlockError('Архив свидетельств находится внутри назначения')
            evidence = read_pbs_evidence(full_archive_path, expected_sha256=full_sha256,
                                         python_version=python_version)
        fd = os.open(archive_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_ARCHIVE:
                raise RuntimeBlockError('Недопустимый архив Python')
            size, digest = _digest(stream)
            if size != before.st_size or digest != expected_sha256:
                raise RuntimeBlockError('Хэш архива Python не совпадает')
            _check_tar_stream(stream)
            with tarfile.open(fileobj=stream, mode='r:gz') as tar, tempfile.TemporaryDirectory(prefix='homensai-pbs-') as temp:
                table = _members(tar)
                resolved = {name: _resolve(name, table) for name in table if table[name].issym()}
                projected = sum(item.size for item in table.values() if item.isfile()) + sum(table[name].size for name in resolved.values())
                if projected > MAX_EXPANDED:
                    raise RuntimeBlockError('Развёрнутый Python слишком велик')
                staging = Path(temp) / 'source'
                staging.mkdir(mode=0o700)
                entries = {}
                for name, item in table.items():
                    if not item.isfile():
                        continue
                    target = staging / name.removeprefix('python/')
                    incoming = tar.extractfile(item)
                    if incoming is None:
                        raise RuntimeBlockError('Файл Python недоступен')
                    with incoming:
                        count, digest = _copy(incoming, target)
                    if count != item.size:
                        raise RuntimeBlockError('Файл Python неполон')
                    entries[name.removeprefix('python/')] = {'path': name.removeprefix('python/'), 'size': count, 'sha256': digest}
                for name, target_name in sorted(resolved.items()):
                    relative, target = name.removeprefix('python/'), target_name.removeprefix('python/')
                    with (staging / target).open('rb') as incoming:
                        count, digest = _copy(incoming, staging / relative)
                    entries[relative] = {'path': relative, 'size': count, 'sha256': digest}
                licenses = []
                for name in sorted(entries):
                    basename = posixpath.basename(name).upper()
                    if not any(word in basename for word in ('LICENSE', 'COPYING', 'NOTICE', 'AUTHORS')):
                        continue
                    target = 'licenses/upstream/' + name
                    with (staging / name).open('rb') as incoming:
                        count, digest = _copy(incoming, staging / target)
                    entries[target] = {'path': target, 'size': count, 'sha256': digest}
                    licenses.append(target)
                if evidence is not None:
                    metadata, license_data, install_license_digest = evidence
                    if entries['lib/python3.14/LICENSE.txt']['sha256'] != install_license_digest:
                        raise RuntimeBlockError('Архивы Python не совпадают по лицензии установки')
                    for name, contents in sorted(license_data.items()):
                        target = 'licenses/upstream-full/' + name
                        count, digest = _copy(io.BytesIO(contents), staging / target)
                        entries[target] = {'path': target, 'size': count, 'sha256': digest}
                        licenses.append(target)
                    target = 'licenses/upstream-full/PYTHON.json'
                    count, digest = _copy(io.BytesIO(metadata), staging / target)
                    entries[target] = {'path': target, 'size': count, 'sha256': digest}
                if not licenses:
                    raise RuntimeBlockError('Лицензии Python отсутствуют')
                after = os.fstat(stream.fileno())
                current = archive_path.lstat()
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                    raise RuntimeBlockError('Архив Python изменился')
                lock = {'format': 1, 'target': {'os': 'linux', 'architecture': 'x86_64'},
                        'python_version': python_version, 'origin': origin, 'license': 'LicenseRef-Composite-Unreviewed',
                        'artifact_sha256': expected_sha256,
                        'license_files': sorted(licenses), 'executables': ['bin/python'],
                        'files': [entries[name] for name in sorted(entries)]}
                if evidence is not None:
                    lock['evidence_sha256'] = full_sha256
                lock_path = Path(temp) / 'runtime-lock.json'
                lock_path.write_text(json.dumps(lock, ensure_ascii=True, sort_keys=True), encoding='utf-8')
                result = build_runtime_block(staging, lock_path, destination)
                return {**result, 'archive_sha256': expected_sha256, 'materialized_links': len(resolved),
                        'terminfo_included': False, 'license_review_complete': False,
                        'supplemental_license_count': len(evidence[1]) if evidence is not None else 0,
                        'archive_pair_independently_verified': False}
    except RuntimeBlockError:
        raise
    except Exception:
        raise RuntimeBlockError('Не удалось подготовить архив Python') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--python-version', required=True)
    parser.add_argument('--full-archive')
    parser.add_argument('--full-sha256')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_pbs_archive(args.archive, args.destination, expected_sha256=args.sha256,
                                             origin=args.origin, python_version=args.python_version,
                                             full_archive_path=args.full_archive, full_sha256=args.full_sha256), sort_keys=True))
    except RuntimeBlockError as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
