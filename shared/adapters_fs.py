# HomenS.AI · © 2026 Serhii Khomenko
"""Локальные файловые порты UpdatePlan; службы/миграции/health внедряются.

Для шифрования нужен внедрённый password_provider; обычный ZIP требует
явный allow_unencrypted=True. Все вызовы портов
выполняются под operation_lock UpdatePlan при остановленных писателях.
Рабочий каталог с архивами/receipts расположен вне всех корней установки.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping, Protocol
import uuid

from shared.archive import _safe_name, build_archive, restore_archive, verify_archive
from shared import archive_crypto
from shared.datahome import checked_path, contains, resolve_datahome
from shared.manifest import validate_manifest
from shared.update_plan import UpdateError, _atomic_write, _identifier, operation_lock


class InjectedEffects(Protocol):
    """Только внедрённые эффекты; этот модуль не исполняет argv/hooks."""

    def stop(self, key, components): ...
    def migrate(self, key, migration, command): ...
    def start(self, key, components, pre_start): ...
    def health(self, key, url, timeout, checks): ...
    def post_update(self, key, hooks): ...


@dataclass(frozen=True)
class ReleaseSource:
    directory: Path
    # Полный доверенный список файлов payload, а не только хэши lock/wheels.
    sha256: Mapping[str, str]


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _tree(root: Path) -> tuple[set[str], dict[str, str]]:
    """Полный обход с отказом от ссылок, спецфайлов и конфликтов регистра."""
    root = checked_path(root)
    directories, files, names = set(), {}, set()
    pending = [root]
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        if relative != ".":
            _safe_name(relative)
        folded = relative.casefold()
        if folded in names:
            raise UpdateError("Конфликт путей дерева")
        names.add(folded)
        info = path.lstat()
        mode = info.st_mode
        if stat.S_ISLNK(mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UpdateError("Ссылки и reparse points запрещены")
        if stat.S_ISDIR(mode):
            directories.add(relative)
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        elif stat.S_ISREG(mode):
            with path.open("rb") as stream:
                files[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            raise UpdateError("Поддерживаются только обычные файлы и каталоги")
    return directories, files


def _copy_file(source: Path, destination: Path) -> None:
    checked_path(source)
    destination = checked_path(destination)
    temporary = destination.with_name(".copy-" + uuid.uuid4().hex + ".tmp")
    def private_file(path, flags):
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        # Protect the unpublished inode before any credential/database bytes
        # enter it; chmod after replace would expose a public temporary copy.
        with source.open("rb") as incoming, open(temporary, "xb", opener=private_file) as outgoing:
            if os.name != "nt":
                os.fchmod(outgoing.fileno(), 0o600)
            while block := incoming.read(1024 * 1024):
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
        _sync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _private_recovery_directory(path: Path) -> None:
    """Owned target directories only; unrelated parents keep their permissions."""
    path = checked_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise UpdateError("Recovery target должен быть обычным каталогом")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            raise UpdateError("Recovery target принадлежит другому пользователю")
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
            captured = os.fstat(descriptor)
            def identity(value):
                return (value.st_dev, value.st_ino, value.st_uid, value.st_gid, stat.S_IFMT(value.st_mode))
            if not stat.S_ISDIR(captured.st_mode) or identity(captured) != identity(info):
                raise UpdateError("Recovery target изменился до проверки прав")
            os.fchmod(descriptor, 0o700)
            named = path.lstat()
            current = os.fstat(descriptor)
            if (identity(named) != identity(captured) or identity(current) != identity(captured)
                    or stat.S_IMODE(named.st_mode) != 0o700 or stat.S_IMODE(current.st_mode) != 0o700):
                raise UpdateError("Recovery target изменился при проверке прав")
        except OSError:
            raise UpdateError("Не удалось подтвердить права recovery target") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)


class FilesystemPorts:
    """Файловые эффекты с durable ключами; явные фейки остальных портов.

    Создание объекта ничего не пишет. Старые releases/runtime/архивы не удаляются.
    Payload задаётся вызывающим кодом вместе с полным доверенным SHA-256 inventory.
    Это проверка байтов, не проверка совместимости Python/wheels или подписи.
    """

    def __init__(self, manifest, install_root: Path, *, work_dir: Path,
                 payloads: Mapping[str, ReleaseSource], effects: InjectedEffects,
                 allow_unencrypted: bool = False, password_provider=None):
        self.manifest = validate_manifest(manifest)
        if self.manifest["schema_version"] != 2:
            raise UpdateError("FS-порты требуют явный манифест v2")
        if password_provider is None and allow_unencrypted is not True:
            raise UpdateError("ZIP не зашифрован; требуется явный allow_unencrypted=True")
        self.password_provider = password_provider
        self.home = resolve_datahome(self.manifest, install_root)
        self.work = checked_path(work_dir)
        if self.work == Path(self.work.anchor) or any(
            contains(root, self.work) or contains(self.work, root)
            for root in (self.home.app, self.home.releases, *self.home.roots.values())
        ):
            raise UpdateError("Рабочий каталог пересекается с установкой")
        services = self.home.roots["services"]
        self.pointer = checked_path(services / self.manifest["update"]["pointer_file"].replace("\\", "/"))
        self.state = checked_path(services / self.manifest["update"]["state_file"].replace("\\", "/"))
        self.lock = checked_path(services / "operation.lock")
        control = (self.pointer, self.state, self.lock)
        if any(contains(a, b) or contains(b, a)
               for i, a in enumerate(control) for b in control[i + 1:]):
            raise UpdateError("Управляющие пути пересекаются")
        self.payloads = dict(payloads)
        self.effects = effects

    def _path(self, key: str, suffix: str) -> Path:
        if type(key) is not str or re.fullmatch(
            r"[0-9a-f]{32}/[a-z_]+(?::[0-9]+)?", key
        ) is None:
            raise UpdateError("Недопустимый ключ операции")
        return checked_path(self.work / (hashlib.sha256(key.encode()).hexdigest() + suffix))

    def archive_path(self, key: str) -> Path:
        return self._path(key, ".hsac" if self.password_provider is not None else ".zip")

    def _verify_archive(self, path):
        if self.password_provider is None:
            return verify_archive(path, expected_product_id=self.home.product_id)
        if not archive_crypto.is_encrypted(path):
            raise UpdateError("Ожидался шифрованный архив; обычный ZIP запрещён")
        return archive_crypto.verify_archive(path, password_provider=self.password_provider,
                                             expected_product_id=self.home.product_id)

    def release_path(self, release: str) -> Path:
        if not _identifier(release):
            raise UpdateError("Недопустимый выпуск")
        return checked_path(self.home.releases / ("app-" + release))

    def _receipt(self, key, value):
        path = self._path(key, ".json")
        if path.exists():
            try:
                if json.loads(path.read_text(encoding="utf-8")) == value:
                    return True
            except (ValueError, UnicodeError):
                pass
            raise UpdateError("Результат операции не соответствует ключу")
        return False

    def _record(self, key, value):
        checked_path(self.work).mkdir(parents=True, exist_ok=True)
        _atomic_write(self._path(key, ".json"), value)

    def current_release(self) -> str:
        try:
            value = json.loads(checked_path(self.pointer).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise UpdateError("Указатель выпуска недоступен") from None
        if (type(value) is not dict or set(value) != {"release"}
                or not _identifier(value["release"])
                or not self.release_path(value["release"]).is_dir()):
            raise UpdateError("Указатель выпуска повреждён")
        return value["release"]

    def stage_release(self, key: str, release: str) -> None:
        destination = self.release_path(release)
        source = self.payloads.get(release)
        if source is None:
            raise UpdateError("Отсутствует доверенный payload")
        directories, files = _tree(source.directory)
        if not files or files != dict(source.sha256):
            raise UpdateError("Payload не соответствует полному SHA-256 inventory")
        artifacts = [*self.manifest["runtime"]["lock_files"].values(),
                     *self.manifest["runtime"]["vendor_wheels"]]
        if any(files.get(item["file"].replace("\\", "/")) != item["sha256"] for item in artifacts):
            raise UpdateError("Payload не соответствует runtime pins")
        value = {"release": release, "inventory": hashlib.sha256(
            json.dumps([sorted(directories), files], sort_keys=True).encode()).hexdigest()}
        self._receipt(key, value)  # Также запрещает повтор key с другим payload.
        if destination.exists():
            if _tree(destination) != (directories, files):
                raise UpdateError("Опубликованный выпуск отличается от payload")
        else:
            checked_path(self.home.releases).mkdir(parents=True, exist_ok=True)
            staging = checked_path(self.home.releases / (".stage-" + uuid.uuid4().hex))
            staging.mkdir()
            for name in sorted(directories, key=lambda name: (name.count("/"), name)):
                checked_path(staging / name).mkdir(parents=True, exist_ok=True)
            for name in sorted(files):
                _copy_file(source.directory / name, staging / name)
            if _tree(staging) != (directories, files):
                raise UpdateError("Проверка staged payload не пройдена")
            for name in sorted(directories, key=len, reverse=True):
                _sync_directory(staging / name)
            os.rename(staging, destination)
            _sync_directory(self.home.releases)
        self._record(key, value)

    def switch_pointer(self, key: str, release: str) -> None:
        value = {"release": release}
        if self._receipt(key, value):
            return
        if not self.release_path(release).is_dir():
            raise UpdateError("Выпуск отсутствует")
        checked_path(self.pointer.parent).mkdir(parents=True, exist_ok=True)
        _atomic_write(self.pointer, value)
        self._record(key, value)

    def backup(self, key: str, purpose: str, command: tuple[str, ...]) -> None:
        expected = key.split("/", 1)[-1].replace("before_rollback", "before-rollback")
        if purpose != expected:
            raise UpdateError("Назначение копии не соответствует ключу")
        path = self.archive_path(key)
        checked_path(self.work).mkdir(parents=True, exist_ok=True)
        if not path.exists():
            if self.password_provider is None:
                build_archive(self.home, path)
            else:
                archive_crypto.build_encrypted_archive(self.home, path,
                                                        password_provider=self.password_provider)
        self._verify_archive(path)
        _sync_directory(self.work)

    def restore(self, key: str, archive_key: str, command: tuple[str, ...]) -> None:
        transaction = key.split("/", 1)[0]
        if key != transaction + "/restore" or archive_key != transaction + "/archive":
            raise UpdateError("Недопустимые ключи восстановления")
        # Независимая защита: без проверенной отдельной копии нет живых записей.
        self._verify_archive(self.archive_path(transaction + "/before_rollback"))
        value = {"archive": archive_key}
        if self._receipt(key, value):
            return
        archive = self.archive_path(archive_key)
        receipt = self._verify_archive(archive)
        if set(receipt["roots"]) != set(self.home.roots):
            raise UpdateError("Корни архива отличаются от установки")
        scratch = checked_path(self.work / ("restore-" + uuid.uuid4().hex))
        scratch.mkdir()
        if self.password_provider is None:
            restore_archive(archive, scratch, expected_product_id=self.home.product_id)
        else:
            archive_crypto.restore_archive(archive, scratch, password_provider=self.password_provider,
                                            expected_product_id=self.home.product_id)
        for name, target in sorted(self.home.roots.items()):
            preserved = set()
            if name == "services":
                for path in (self.pointer, self.state, self.lock):
                    preserved.add(path.relative_to(target).as_posix())
                    preserved.update(parent.relative_to(target).as_posix()
                                     for parent in path.parents if contains(target, parent))
            self._reconcile(scratch / "roots" / name, target, preserved)
        self._record(key, value)

    @staticmethod
    def _reconcile(source: Path, target: Path, preserved: set[str]) -> None:
        """Повторяемое восстановление: управляющие файлы не заменяются/не удаляются."""
        desired_dirs, desired_files = _tree(source)
        _private_recovery_directory(target)
        actual_dirs, actual_files = _tree(target)
        for name in sorted(actual_files):
            if name not in preserved and (name not in desired_files or name in desired_dirs):
                checked_path(target / name).unlink()
        for name in sorted(actual_dirs - {"."}, key=lambda name: (name.count("/"), name), reverse=True):
            if name not in preserved and name not in desired_dirs:
                checked_path(target / name).rmdir()
        for name in sorted(desired_dirs - preserved, key=lambda name: (name.count("/"), name)):
            _private_recovery_directory(target / name)
        for name in sorted(desired_files.keys() - preserved):
            _copy_file(source / name, checked_path(target / name))
        for name in sorted(desired_dirs, key=len, reverse=True):
            _sync_directory(checked_path(target / name))
        final_dirs, final_files = _tree(target)
        if (final_dirs - preserved != desired_dirs - preserved
                or {k: v for k, v in final_files.items() if k not in preserved}
                != {k: v for k, v in desired_files.items() if k not in preserved}):
            raise UpdateError("Проверка живого восстановления не пройдена")

    def stop(self, key, components):
        return self.effects.stop(key, components)

    def migrate(self, key, migration, command):
        return self.effects.migrate(key, migration, command)

    def start(self, key, components, pre_start):
        return self.effects.start(key, components, pre_start)

    def health(self, key, url, timeout, checks):
        return self.effects.health(key, url, timeout, checks)

    def post_update(self, key, hooks):
        return self.effects.post_update(key, hooks)
