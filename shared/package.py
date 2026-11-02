# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальный единый ZIP-пакет и проверка ожидаемого SHA-256.

Не исполняет и не извлекает файлы. Ожидаемый hash должен быть получен по
независимому доверенному каналу; это пока не готовая система выпуска.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import uuid
import zipfile

from shared.archive import ArchiveError, _digest, _info, _safe_name, _signature, _space
from shared.datahome import DataHomeError, checked_path, contains
from shared.manifest import ManifestError, validate_manifest

MAX_FILES = 10000
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class PackageError(ValueError):
    """Фиксированная ошибка без содержимого пользовательских файлов."""


def _files(root: Path) -> tuple[list[dict], dict]:
    if not root.is_dir():
        raise PackageError("Требуется каталог payload")
    pending = [(root, "")]
    result, signatures, seen = [], {}, set()
    total = 0
    while pending:
        path, name = pending.pop()
        if len(signatures) >= MAX_FILES * 2 or len(name) > 1024:
            raise PackageError("Превышен лимит состава payload")
        signature = _signature(path)
        if name:
            _safe_name(name)
            folded = name.casefold()
            if folded in seen:
                raise PackageError("Конфликт имён payload")
            seen.add(folded)
        signatures[name] = signature
        if stat.S_ISDIR(signature[2]):
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda item: item.name)
            pending.extend((Path(item.path), (name + "/" if name else "") + item.name)
                           for item in reversed(children))
        else:
            if len(result) >= MAX_FILES or signature[3] > MAX_FILE_BYTES:
                raise PackageError("Превышен лимит файлов payload")
            with path.open("rb") as stream:
                size, digest = _digest(stream)
            if size != signature[3] or signature != _signature(path):
                raise PackageError("Payload изменился во время чтения")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise PackageError("Превышен общий размер payload")
            result.append({"path": name, "size": size, "sha256": digest})
    if not result:
        raise PackageError("Пустой payload запрещён")
    return sorted(result, key=lambda item: item["path"]), signatures


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("Повтор поля пакета")
        result[key] = value
    return result


def _reject_constant(_):
    raise PackageError("Недопустимое значение пакета")


def _metadata(value):
    if type(value) is not dict or set(value) != {"format", "product_id", "version", "manifest", "files"}:
        raise PackageError("Некорректное описание пакета")
    if type(value["format"]) is not int or value["format"] != 1:
        raise PackageError("Неизвестная версия пакета")
    if type(value["version"]) is not str or _VERSION.fullmatch(value["version"]) is None:
        raise PackageError("Некорректная версия выпуска")
    manifest = validate_manifest(value["manifest"])
    if value["product_id"] != manifest["product_id"]:
        raise PackageError("Продукт пакета не соответствует манифесту")
    files = value["files"]
    if type(files) is not list or not 0 < len(files) <= MAX_FILES:
        raise PackageError("Некорректный список файлов")
    names, folded, total = [], set(), 0
    for item in files:
        if type(item) is not dict or set(item) != {"path", "size", "sha256"}:
            raise PackageError("Некорректный файл пакета")
        name, size, digest = item["path"], item["size"], item["sha256"]
        if type(name) is not str or len(name) > 1024:
            raise PackageError("Некорректный путь пакета")
        _safe_name(name)
        if (name.casefold() in folded or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
                or type(digest) is not str or _HASH.fullmatch(digest) is None):
            raise PackageError("Некорректный файл пакета")
        folded.add(name.casefold())
        names.append(name)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise PackageError("Превышен общий размер payload")
    if names != sorted(names):
        raise PackageError("Файлы пакета расположены не по порядку")
    for name in folded:
        parts = name.split("/")
        if any("/".join(parts[:index]) in folded for index in range(1, len(parts))):
            raise PackageError("Пересечение файлов пакета")
    if manifest["schema_version"] == 2:
        inventory = {item["path"]: item["sha256"] for item in files}
        runtime = manifest["runtime"]
        required = [*runtime["lock_files"].values(), *runtime["vendor_wheels"]]
        if any(inventory.get(item["file"].replace("\\", "/")) != item["sha256"] for item in required):
            raise PackageError("Отсутствуют закреплённые runtime-артефакты")
    return value


def _sha256(stream) -> str:
    stream.seek(0)
    return _digest(stream)[1]


def verify_package(path: str | Path, *, expected_sha256: str) -> dict:
    """Проверить доверенный внешний hash и весь ZIP до любого извлечения."""
    if type(expected_sha256) is not str or _HASH.fullmatch(expected_sha256) is None:
        raise PackageError("Требуется доверенный SHA-256 пакета")
    try:
        path = checked_path(path)
        signature = _signature(path)
        if not stat.S_ISREG(signature[2]):
            raise PackageError("Требуется обычный файл пакета")
        if signature[3] > MAX_TOTAL_BYTES + MAX_METADATA_BYTES + 64 * 1024 * 1024:
            raise PackageError("Пакет слишком велик")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not hmac.compare_digest(_sha256(stream), expected_sha256):
                raise PackageError("SHA-256 пакета не совпадает")
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                if not 2 <= len(infos) <= MAX_FILES + 1:
                    raise PackageError("Некорректный состав пакета")
                names = [info.filename for info in infos]
                if (len(set(names)) != len(names) or names.count("package.json") != 1
                        or any(info.is_dir() or info.orig_filename != info.filename or info.flag_bits & 1
                               or info.compress_type != zipfile.ZIP_STORED
                               or info.file_size != info.compress_size for info in infos)):
                    raise PackageError("Недопустимый формат ZIP")
                header = archive.getinfo("package.json")
                if (header.file_size > MAX_METADATA_BYTES or header.create_system != 3
                        or not stat.S_ISREG(header.external_attr >> 16)):
                    raise PackageError("Описание пакета слишком велико")
                metadata = _metadata(json.loads(archive.read(header).decode("utf-8"),
                                                object_pairs_hook=_object,
                                                parse_constant=_reject_constant))
                expected = {"package.json", *("payload/" + item["path"] for item in metadata["files"])}
                if set(names) != expected:
                    raise PackageError("Состав пакета отличается от описания")
                for item in metadata["files"]:
                    info = archive.getinfo("payload/" + item["path"])
                    mode = info.external_attr >> 16
                    if (info.create_system != 3 or not stat.S_ISREG(mode)
                            or info.file_size != item["size"]):
                        raise PackageError("Недопустимый тип файла пакета")
                    with archive.open(info) as member:
                        size, digest = _digest(member)
                    if size != item["size"] or not hmac.compare_digest(digest, item["sha256"]):
                        raise PackageError("Содержимое файла пакета повреждено")
            if not hmac.compare_digest(_sha256(stream), expected_sha256):
                raise PackageError("Пакет изменился во время проверки")
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise PackageError("Пакет изменился во время проверки")
        if signature != _signature(path):
            raise PackageError("Пакет изменился во время проверки")
        return metadata
    except (OSError, UnicodeError, ValueError, RecursionError, KeyError, zipfile.BadZipFile,
            DataHomeError, ArchiveError, ManifestError) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError("Не удалось проверить пакет") from None


def build_package(manifest: dict, version: str, source: str | Path, destination: str | Path) -> dict:
    """Собрать новый пакет из явно выбранного payload, не заменяя файлы."""
    temporary = None
    temporary_created = False
    try:
        manifest = validate_manifest(manifest)
        source, destination = checked_path(source), checked_path(destination)
        if contains(source, destination) or destination.exists() or not destination.parent.is_dir():
            raise PackageError("Недопустимое назначение пакета")
        files, signatures = _files(source)
        metadata = _metadata({"format": 1, "product_id": manifest["product_id"],
                              "version": version, "manifest": manifest, "files": files})
        body = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_METADATA_BYTES:
            raise PackageError("Описание пакета слишком велико")
        _space(destination.parent, sum(item["size"] for item in files) + len(body)
               + sum(1024 + 4 * len(item["path"].encode("utf-8")) for item in files))
        temporary = destination.parent / (".package-" + uuid.uuid4().hex + ".tmp")
        with temporary.open("xb") as output:
            temporary_created = True
            with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
                archive.writestr(_info("package.json"), body)
                for item in files:
                    path = source.joinpath(*item["path"].split("/"))
                    if _signature(path) != signatures[item["path"]]:
                        raise PackageError("Payload изменился во время сборки")
                    with path.open("rb") as src, archive.open(_info("payload/" + item["path"]), "w", force_zip64=True) as target:
                        size, digest = _digest(src, target)
                    if size != item["size"] or digest != item["sha256"]:
                        raise PackageError("Payload изменился во время сборки")
            output.flush()
            os.fsync(output.fileno())
        if _files(source) != (files, signatures):
            raise PackageError("Payload изменился во время сборки")
        with temporary.open("rb") as stream:
            digest = _sha256(stream)
        verify_package(temporary, expected_sha256=digest)
        os.link(temporary, destination)
        return {"sha256": digest, "product_id": manifest["product_id"], "version": version,
                "file_count": len(files)}
    except (OSError, UnicodeError, ValueError, RecursionError, zipfile.BadZipFile,
            DataHomeError, ArchiveError, ManifestError) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError("Не удалось собрать пакет") from None
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)
