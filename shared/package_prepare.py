# HomenS.AI · © 2026 Serhii Khomenko
"""Изолированное извлечение подписанного пакета в новый каталог.

Это подготовка тестового payload, не установка или защита от изменений другим
процессом с теми же правами. Ключ и ожидаемые данные задаёт доверенная сторона.
"""

from __future__ import annotations

import os
from pathlib import Path
from hashlib import sha256
import stat
import tempfile
import zipfile

from shared.archive import ArchiveError, CHUNK, _digest, _signature, _space
from shared.adapters_fs import _tree
from shared.datahome import DataHomeError, checked_path
from shared.package import MAX_METADATA_BYTES, MAX_TOTAL_BYTES, PackageError
from shared.package_signing import verify_signed_package
from shared.update_plan import UpdateError

INCOMPLETE = ".homensai-prepare-incomplete"
MAX_ARCHIVE = MAX_TOTAL_BYTES + MAX_METADATA_BYTES + 64 * 1024 * 1024


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def prepare_signed_package(package_path: str | Path, envelope: bytes, destination: str | Path, *,
                           trusted_public_key: bytes, expected_product_id: str,
                           expected_version: str) -> dict:
    """Проверить, извлечь в изоляции и опубликовать только в новый каталог.

    При сбое после создания destination каталог может остаться частичным;
    маркер обычно сохраняется, но его отсутствие не подтверждает успех после
    аварии. Каталог не очищается автоматически; файлы не запускаются.
    """
    try:
        source = checked_path(package_path)
        destination = checked_path(destination)
        signature = _signature(source)
        if (not stat.S_ISREG(signature[2]) or signature[3] > MAX_ARCHIVE
                or destination.exists() or not destination.parent.is_dir()):
            raise PackageError("Недопустимые пути или размер пакета")
        _space(destination.parent, signature[3] * 2 + 4 * 1024 * 1024)
        with tempfile.TemporaryDirectory(prefix=".homensai-prepare-", dir=destination.parent) as temporary:
            work = checked_path(temporary)
            snapshot, staged = work / "package.zip", work / "payload"
            with source.open("rb") as incoming, snapshot.open("xb") as outgoing:
                count = 0
                while block := incoming.read(CHUNK):
                    count += len(block)
                    if count > MAX_ARCHIVE:
                        raise PackageError("Пакет слишком велик")
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if signature != _signature(source) or count != signature[3]:
                raise PackageError("Исходный пакет изменился при копировании")
            kwargs = {"trusted_public_key": trusted_public_key, "expected_product_id": expected_product_id,
                      "expected_version": expected_version}
            metadata = verify_signed_package(snapshot, envelope, **kwargs)
            if any(item["path"].split("/")[0].casefold() == INCOMPLETE.casefold() for item in metadata["files"]):
                raise PackageError("Payload содержит зарезервированное имя")
            staged.mkdir(mode=0o700)
            with zipfile.ZipFile(snapshot) as archive:
                for item in metadata["files"]:
                    output = checked_path(staged.joinpath(*item["path"].split("/")))
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.open("payload/" + item["path"]) as incoming:
                        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as outgoing:
                            size, digest = _digest(incoming, outgoing)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                    with output.open("rb") as restored:
                        stored = _digest(restored)
                    if (size, digest) != (item["size"], item["sha256"]) or stored != (size, digest):
                        raise PackageError("Извлечённый файл повреждён")
            if verify_signed_package(snapshot, envelope, **kwargs) != metadata:
                raise PackageError("Пакет изменился при извлечении")
            # mkdir и link атомарно отказывают при существующей цели: ничего
            # не перезаписываем даже при конкурентном создании назначения.
            destination.mkdir(mode=0o700)
            marker = destination / INCOMPLETE
            with marker.open("xb") as stream:
                stream.write(b"incomplete\n")
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(destination)
            for item in metadata["files"]:
                parts = item["path"].split("/")
                output = checked_path(destination.joinpath(*parts))
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.link(staged.joinpath(*parts), output)
            for item in metadata["files"]:
                output = checked_path(destination.joinpath(*item["path"].split("/")))
                with output.open("rb") as stream:
                    if _digest(stream) != (item["size"], item["sha256"]):
                        raise PackageError("Опубликованный файл повреждён")
            directories, actual = _tree(destination)
            expected = {item["path"]: item["sha256"] for item in metadata["files"]}
            expected[INCOMPLETE] = sha256(b"incomplete\n").hexdigest()
            expected_dirs = {"."}
            for name in expected:
                parts = name.split("/")
                expected_dirs.update("/".join(parts[:index]) for index in range(1, len(parts)))
            if directories != expected_dirs or actual != expected:
                raise PackageError("Опубликованный состав отличается от пакета")
            for directory in sorted(directories, key=lambda name: (name.count("/"), name), reverse=True):
                _sync_directory(checked_path(destination / directory))
            marker.unlink()
            _sync_directory(destination)
            _sync_directory(destination.parent)
            return {"product_id": metadata["product_id"], "version": metadata["version"],
                    "files": {item["path"]: item["sha256"] for item in metadata["files"]}}
    except (OSError, ValueError, RuntimeError, EOFError, NotImplementedError,
            DataHomeError, ArchiveError, UpdateError, zipfile.BadZipFile) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError("Не удалось подготовить подписанный пакет") from None
