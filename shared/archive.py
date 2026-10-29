# HomenS.AI · © 2026 Serhii Khomenko
"""Полный ZIP постоянных корней: SHA-256, проверка и изолированное восстановление.

Только stdlib. Нет шифрования, исполнения хуков, журналирования содержимого или
доступа к окружению. Вызывающий код обязан остановить всех писателей данных.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
import zipfile

from shared.datahome import (DataHome, DataHomeError, PERSISTENT_ROOTS,
                             checked_path, check_invariants, contains)


CHUNK = 1024 * 1024
MANIFEST = "manifest.json"
MAX_MANIFEST = 16 * CHUNK
RESERVE = CHUNK


class ArchiveError(ValueError):
    """Операция не завершена; исключение не содержит содержимое файлов."""


class InsufficientSpaceError(ArchiveError):
    """Known space precondition failed; distinct from invalid archive bytes."""


def _safe_name(name: str, *, directory: bool = False) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ArchiveError("Недопустимый путь ZIP")
    text = name[:-1] if directory and name.endswith("/") else name
    parts = text.split("/")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((".", " "))
                or any(ord(c) < 32 or ord(c) == 127 or c in ':<>"|?*' for c in part)
                or part.split(".")[0].upper() in {
                    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                    *(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"),
                }):
            raise ArchiveError("Недопустимый путь ZIP")
    return text


def _signature(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ArchiveError("Ссылки и reparse points не архивируются")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ArchiveError("Поддерживаются только обычные файлы и каталоги")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _digest(stream, output=None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(CHUNK):
        size += len(block)
        digest.update(block)
        if output is not None:
            output.write(block)
    return size, digest.hexdigest()


def _inventory(home: DataHome):
    directories, files, signatures = [], [], {}
    for name, root in sorted(home.roots.items()):
        if not root.is_dir():
            raise ArchiveError("Отсутствует обязательный постоянный каталог")
        pending = [(root, f"roots/{name}")]
        while pending:
            path, member = pending.pop()
            _safe_name(member)
            signature = signatures[member] = _signature(path)
            if stat.S_ISDIR(signature[2]):
                directories.append(member)
                # scandir не пропускает ошибки доступа (в отличие от некоторых rglob).
                with os.scandir(path) as entries:
                    children = sorted(entries, key=lambda entry: entry.name)
                pending.extend((Path(entry.path), member + "/" + entry.name) for entry in reversed(children))
            else:
                with path.open("rb") as stream:
                    size, digest = _digest(stream)
                if signature != _signature(path) or size != signature[3]:
                    raise ArchiveError("Данные изменились во время чтения")
                files.append({"path": member, "size": size, "sha256": digest})
    directories.sort()
    files.sort(key=lambda item: item["path"])
    names = directories + [item["path"] for item in files]
    if len({name.casefold() for name in names}) != len(names):
        raise ArchiveError("Пути конфликтуют без учёта регистра")
    return directories, files, signatures


def _space(parent: Path, required: int) -> None:
    if shutil.disk_usage(parent).free < required + RESERVE:
        raise InsufficientSpaceError("Недостаточно свободного места")


def _info(name: str, directory: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name + ("/" if directory else ""), (1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = ((stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o600)) << 16
    if directory:
        info.external_attr |= 0x10
    return info


def build_archive(home: DataHome, destination: str | Path) -> dict:
    """Полный архив всех корней; никаких исключений файлов и перезаписи результата.

    Публикация через hard link выполняется только после проверки временного ZIP.
    Файловая система назначения должна поддерживать hard links.
    """
    temporary = None
    temporary_created = False
    try:
        check_invariants(home)
        destination = checked_path(destination)
        if any(contains(root, destination) for root in (home.app, home.releases, *home.roots.values())):
            raise ArchiveError("Архив должен быть вне app/releases и архивируемых корней")
        if destination.exists() or not destination.parent.is_dir():
            raise ArchiveError("Архив уже существует или отсутствует каталог назначения")
        directories, files, signatures = _inventory(home)
        manifest = {"format": 1, "product_id": home.product_id, "roots": sorted(home.roots),
                    "directories": directories, "files": files}
        body = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_MANIFEST:
            raise ArchiveError("Манифест архива превышает ограничение размера")
        _validate_manifest(manifest)
        _space(destination.parent, sum(item["size"] for item in files) + len(body)
               + sum(1024 + 4 * len(name.encode("utf-8")) for name in signatures))
        temporary = destination.parent / (".archive-" + uuid.uuid4().hex + ".tmp")
        with temporary.open("xb") as output:
            temporary_created = True
            with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
                by_name = {item["path"]: item for item in files}
                for member in sorted(signatures):
                    if member not in by_name:
                        archive.writestr(_info(member, True), b"")
                        continue
                    relative = member.split("/")
                    path = home.roots[relative[1]].joinpath(*relative[2:])
                    if signatures[member] != _signature(path):
                        raise ArchiveError("Данные изменились перед архивированием")
                    with path.open("rb") as source, archive.open(_info(member), "w", force_zip64=True) as target:
                        size, digest = _digest(source, target)
                    if (size, digest) != (by_name[member]["size"], by_name[member]["sha256"]):
                        raise ArchiveError("Данные изменились при архивировании")
                archive.writestr(_info(MANIFEST), body)
            output.flush()
            os.fsync(output.fileno())
        if (directories, files, signatures) != _inventory(home):
            raise ArchiveError("Дерево данных изменилось при архивировании")
        verify_archive(temporary, expected_product_id=home.product_id)
        os.link(temporary, destination)  # Атомарно, без замены существующего файла.
        return manifest
    except (OSError, DataHomeError, zipfile.BadZipFile, UnicodeError):
        raise ArchiveError("Не удалось создать полный проверенный архив") from None
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError("Повторяющиеся поля манифеста архива")
        result[key] = value
    return result


def _validate_manifest(value: dict) -> None:
    if (type(value) is not dict or set(value) != {"format", "product_id", "roots", "directories", "files"}
            or type(value["format"]) is not int or value["format"] != 1
            or not isinstance(value["product_id"], str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value["product_id"]) is None):
        raise ArchiveError("Недопустимый манифест архива")
    roots, directories, files = value["roots"], value["directories"], value["files"]
    if (type(roots) is not list or not roots or any(type(name) is not str for name in roots)
            or roots != sorted(set(roots)) or set(roots) - set(PERSISTENT_ROOTS)
            or type(directories) is not list or type(files) is not list):
        raise ArchiveError("Недопустимые корни архива")
    if any(type(name) is not str for name in directories) or directories != sorted(set(directories)):
        raise ArchiveError("Недопустимый список каталогов")
    names = list(directories)
    file_names = []
    for item in files:
        if (type(item) is not dict or set(item) != {"path", "size", "sha256"}
                or type(item["path"]) is not str or type(item["size"]) is not int or item["size"] < 0
                or type(item["sha256"]) is not str or re.fullmatch("[0-9a-f]{64}", item["sha256"]) is None):
            raise ArchiveError("Недопустимая запись файла")
        file_names.append(item["path"])
    if file_names != sorted(set(file_names)):
        raise ArchiveError("Недопустимый порядок файлов")
    names.extend(file_names)
    if len({name.casefold() for name in names}) != len(names):
        raise ArchiveError("Конфликт путей архива")
    directory_set = set(directories)
    for root in roots:
        if f"roots/{root}" not in directory_set:
            raise ArchiveError("Отсутствует корневой каталог")
    for name in names:
        _safe_name(name)
        parts = name.split("/")
        if len(parts) < 2 or parts[0] != "roots" or parts[1] not in roots:
            raise ArchiveError("Путь вне постоянных корней")
        if len(parts) > 2 and name.rsplit("/", 1)[0] not in directory_set:
            raise ArchiveError("Отсутствует родительский каталог")
    if any(name in {f"roots/{root}" for root in roots} for name in file_names):
        raise ArchiveError("Файл вместо корня")


def _verify(archive: zipfile.ZipFile, expected_product_id: str | None) -> dict:
    entries = archive.infolist()
    names = set()
    for info in entries:
        if info.filename != info.orig_filename:
            raise ArchiveError("Неоднозначное имя ZIP")
        name = _safe_name(info.filename, directory=info.is_dir())
        key = name.casefold()
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if (key in names or info.flag_bits & 1 or info.compress_type != zipfile.ZIP_STORED
                or kind not in (0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG)
                or (info.is_dir() and info.file_size != 0)):
            raise ArchiveError("Неоднозначная или неподдерживаемая запись ZIP")
        names.add(key)
    try:
        manifest_info = archive.getinfo(MANIFEST)
    except KeyError:
        raise ArchiveError("Отсутствует манифест архива") from None
    if manifest_info.file_size > MAX_MANIFEST:
        raise ArchiveError("Манифест архива превышает ограничение размера")
    manifest = json.loads(archive.read(manifest_info), object_pairs_hook=_object)
    _validate_manifest(manifest)
    if expected_product_id is not None and manifest["product_id"] != expected_product_id:
        raise ArchiveError("Архив другого продукта")
    expected = {MANIFEST, *(name + "/" for name in manifest["directories"]),
                *(item["path"] for item in manifest["files"])}
    if {info.filename for info in entries} != expected:
        raise ArchiveError("Состав ZIP не совпадает с манифестом")
    # Проверяем CRC даже пустых каталогов и SHA-256 каждого файла.
    for name in manifest["directories"]:
        if archive.read(name + "/") != b"":
            raise ArchiveError("Недопустимое содержимое каталога")
    for item in manifest["files"]:
        info = archive.getinfo(item["path"])
        if info.file_size != item["size"]:
            raise ArchiveError("Размер файла не совпадает с манифестом")
        with archive.open(info) as stream:
            actual = _digest(stream)
        if actual != (item["size"], item["sha256"]):
            raise ArchiveError("SHA-256 файла не совпадает с манифестом")
    return manifest


def verify_archive(path: str | Path, *, expected_product_id: str | None = None) -> dict:
    """Проверить весь ZIP без извлечения; вернуть манифест без содержимого файлов."""
    try:
        with zipfile.ZipFile(checked_path(path), "r") as archive:
            return _verify(archive, expected_product_id)
    except (OSError, DataHomeError, zipfile.BadZipFile, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError("Архив повреждён или недоступен") from None


def restore_archive(path: str | Path, target: str | Path, *, expected_product_id: str | None = None) -> dict:
    """Проверить и восстановить в существующий пустой каталог: target/roots/<имя>.

    При ошибке после начала записи остаётся частичный результат для анализа;
    автоматической очистки или записи в живые продуктовые пути нет.
    """
    try:
        target = checked_path(target)
        if not target.is_dir() or any(target.iterdir()):
            raise ArchiveError("Восстановление требует существующий пустой каталог")
        with zipfile.ZipFile(checked_path(path), "r") as archive:
            manifest = _verify(archive, expected_product_id)
            _space(target, sum(item["size"] for item in manifest["files"])
                   + 4096 * (len(manifest["directories"]) + len(manifest["files"]) + 1))
            for name in manifest["directories"]:
                output = checked_path(target.joinpath(*name.split("/")))
                output.mkdir(parents=True, exist_ok=True)
            for item in manifest["files"]:
                output = checked_path(target.joinpath(*item["path"].split("/")))
                with archive.open(item["path"]) as source, output.open("xb") as destination:
                    actual = _digest(source, destination)
                with output.open("rb") as restored:
                    on_disk = _digest(restored)
                if actual != on_disk or on_disk != (item["size"], item["sha256"]):
                    raise ArchiveError("Проверка восстановленного файла не пройдена")
            return manifest
    except (OSError, DataHomeError, zipfile.BadZipFile, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError("Восстановление не завершено") from None
