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
import tempfile
from typing import Mapping, Protocol
import uuid

from shared.archive import _open_regular, _safe_name, build_archive, restore_archive, verify_archive
from shared import archive_crypto
from shared.datahome import checked_path, contains, resolve_datahome
from shared.effect_receipt import read_effect_receipt
from shared.manifest import validate_manifest
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _atomic_write, _identifier, operation_lock


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
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            while block := incoming.read(1024 * 1024):
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
        _sync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


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

    def recover_archive_binding(self, plan: UpdatePlan, key: str, expected_sha256: str) -> None:
        """Подтвердить байты незавершённой копии по независимо полученному SHA-256.

        Это не подтверждает остановку писателей и не разрешает повтор эффекта.
        Вызывающий обязан проверить происхождение и свежесть контрольной суммы.
        """
        if (self.password_provider is None or type(plan) is not UpdatePlan
                or plan.ports is not self or plan.guard_effects is not True
                or plan.manifest != self.manifest or plan.state_path != self.state
                or plan.lock_path != self.lock or type(key) is not str
                or re.fullmatch(r'[0-9a-f]{32}/(?:archive|backup:(?:0|[1-9][0-9]*)|before_rollback)', key) is None
                or type(expected_sha256) is not str
                or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None):
            raise UpdateError('Недопустимые параметры подтверждения копии')
        path = self.archive_path(key)
        binding_path = checked_path(path.with_suffix('.archive.json'))
        pending = {'version': 1, 'key': key, 'product_id': self.home.product_id,
                   'sha256': None, 'status': 'pending'}
        done = {**pending, 'sha256': expected_sha256, 'status': 'done'}
        with operation_lock(plan.lock_path):
            state = plan.status()
            if state is None or state['outcome'] != 'running' or state.get('indeterminate') is not True:
                raise UpdateError('Нет неопределённого этапа копирования')
            phases = plan.forward if state['direction'] == 'forward' else plan.rollback
            if (state['index'] >= len(phases) or key != state['transaction'] + '/' + phases[state['index']]
                    or (state['direction'] == 'rollback') != key.endswith('/before_rollback')):
                raise UpdateError('Копия не соответствует текущему этапу')
            record = read_effect_receipt(binding_path)
            if record is not None and record != pending and record != done:
                raise EffectIndeterminate('Отметка копии не соответствует операции')
            try:
                receipt = archive_crypto.verify_archive(
                    path, password_provider=self.password_provider, expected_product_id=self.home.product_id,
                    require_encrypted=True, expected_ciphertext_sha256=expected_sha256)
                if set(receipt['roots']) != set(self.home.roots):
                    raise UpdateError('Корни архива отличаются от установки')
                if plan.status() != state or read_effect_receipt(binding_path) != record:
                    raise EffectIndeterminate('Состояние операции изменилось')
                if record != done:
                    _atomic_write(binding_path, done)
                if read_effect_receipt(binding_path) != done:
                    raise EffectIndeterminate('Отметка копии не подтверждена')
            except Exception:
                raise EffectIndeterminate('Подтверждение копии не удалось') from None

    def _archive_binding(self, path: Path) -> dict:
        path = checked_path(path)
        record = read_effect_receipt(checked_path(path.with_suffix('.archive.json')))
        if (type(record) is not dict or set(record) != {'version', 'key', 'product_id', 'sha256', 'status'}
                or type(record['version']) is not int or record['version'] != 1
                or type(record['key']) is not str or record['product_id'] != self.home.product_id
                or record['status'] != 'done' or type(record['sha256']) is not str
                or re.fullmatch(r'[a-f0-9]{64}', record['sha256']) is None
                or self.archive_path(record['key']) != path):
            raise EffectIndeterminate('Отметка архивной копии не подтверждена')
        return record

    def _verify_archive(self, path):
        if self.password_provider is None:
            return verify_archive(path, expected_product_id=self.home.product_id)
        binding = self._archive_binding(path)
        if not archive_crypto.is_encrypted(path):
            raise UpdateError("Ожидался шифрованный архив; обычный ZIP запрещён")
        return archive_crypto.verify_archive(path, password_provider=self.password_provider,
                                             expected_product_id=self.home.product_id, require_encrypted=True,
                                             expected_ciphertext_sha256=binding['sha256'])

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
        guarded = getattr(self.effects, 'guarded_stage_release', None)
        if guarded is not None:
            if not callable(guarded):
                raise UpdateError('Недопустимая проверка выпуска')
            return guarded(key, release, self._stage_release)
        return self._stage_release(key, release)

    def _stage_release(self, key: str, release: str) -> None:
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
        guarded = getattr(self.effects, 'guarded_switch_pointer', None)
        if guarded is not None:
            if not callable(guarded):
                raise UpdateError('Недопустимая проверка переключения')
            return guarded(key, release, self._switch_pointer)
        return self._switch_pointer(key, release)

    def _switch_pointer(self, key: str, release: str) -> None:
        value = {"release": release}
        if self._receipt(key, value):
            return
        if not self.release_path(release).is_dir():
            raise UpdateError("Выпуск отсутствует")
        checked_path(self.pointer.parent).mkdir(parents=True, exist_ok=True)
        _atomic_write(self.pointer, value)
        self._record(key, value)

    def backup(self, key: str, purpose: str, command: tuple[str, ...]) -> None:
        guarded = getattr(self.effects, 'guarded_backup', None)
        if guarded is not None:
            if not callable(guarded):
                raise UpdateError('Недопустимая проверка остановки')
            return guarded(key, purpose, command, self._backup)
        return self._backup(key, purpose, command)

    def _backup(self, key: str, purpose: str, command: tuple[str, ...]) -> None:
        expected = key.split("/", 1)[-1].replace("before_rollback", "before-rollback")
        if purpose != expected:
            raise UpdateError("Назначение копии не соответствует ключу")
        path = self.archive_path(key)
        checked_path(self.work).mkdir(parents=True, exist_ok=True)
        if self.password_provider is not None:
            binding_path = checked_path(path.with_suffix('.archive.json'))
            existing = read_effect_receipt(binding_path)
            if existing is not None:
                try:
                    self._verify_archive(path)
                except Exception:
                    raise EffectIndeterminate('Сохранённая копия не подтверждена') from None
                return
            if path.exists():
                if not archive_crypto.is_encrypted(path):
                    raise UpdateError('Ожидался шифрованный архив; обычный ZIP запрещён')
                raise EffectIndeterminate('Архив без отметки операции требует проверки')
            pending = {'version': 1, 'key': key, 'product_id': self.home.product_id,
                       'sha256': None, 'status': 'pending'}
            _atomic_write(binding_path, pending)
            try:
                archive_crypto.build_encrypted_archive(self.home, path, password_provider=self.password_provider)
                with _open_regular(path) as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                archive_crypto.verify_archive(path, password_provider=self.password_provider,
                    expected_product_id=self.home.product_id, require_encrypted=True,
                    expected_ciphertext_sha256=digest)
                _sync_directory(self.work)
                if read_effect_receipt(binding_path) != pending:
                    raise EffectIndeterminate('Отметка архивной копии изменилась')
                _atomic_write(binding_path, {**pending, 'sha256': digest, 'status': 'done'})
                self._verify_archive(path)
            except Exception:
                raise EffectIndeterminate('Результат архивной копии требует проверки') from None
            return
        if not path.exists():
            build_archive(self.home, path)
        self._verify_archive(path)
        _sync_directory(self.work)

    def restore(self, key: str, archive_key: str, command: tuple[str, ...]) -> None:
        guarded = getattr(self.effects, 'guarded_restore', None)
        if guarded is not None:
            if not callable(guarded):
                raise UpdateError('Недопустимая проверка восстановления')
            return guarded(key, archive_key, command, self._restore)
        return self._restore(key, archive_key, command)

    def _restore(self, key: str, archive_key: str, command: tuple[str, ...]) -> None:
        transaction = key.split("/", 1)[0]
        if key != transaction + "/restore" or archive_key != transaction + "/archive":
            raise UpdateError("Недопустимые ключи восстановления")
        # Независимая защита: без проверенной отдельной копии нет живых записей.
        self._verify_archive(self.archive_path(transaction + "/before_rollback"))
        value = {"archive": archive_key}
        if self._receipt(key, value):
            return
        archive = self.archive_path(archive_key)
        binding = self._archive_binding(archive) if self.password_provider is not None else None
        receipt = self._verify_archive(archive)
        if set(receipt["roots"]) != set(self.home.roots):
            raise UpdateError("Корни архива отличаются от установки")
        # Расшифрованное дерево не должно оставаться после обычного завершения
        # или исключения. После аварийного завершения нужна отдельная очистка.
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=self.work) as directory:
            scratch = checked_path(directory)
            if self.password_provider is None:
                restore_archive(archive, scratch, expected_product_id=self.home.product_id)
            else:
                archive_crypto.restore_archive(archive, scratch, password_provider=self.password_provider,
                                                expected_product_id=self.home.product_id, require_encrypted=True,
                                                expected_ciphertext_sha256=binding['sha256'])
            for name, target in sorted(self.home.roots.items()):
                preserved = set()
                if name == "services":
                    for path in (self.pointer, self.state, self.lock):
                        preserved.add(path.relative_to(target).as_posix())
                        preserved.update(parent.relative_to(target).as_posix()
                                         for parent in path.parents if contains(target, parent))
                self._reconcile(scratch / "roots" / name, target, preserved)
        # Не подтверждать завершение, пока не удалось удалить временное дерево.
        self._record(key, value)

    @staticmethod
    def _reconcile(source: Path, target: Path, preserved: set[str]) -> None:
        """Повторяемое восстановление: управляющие файлы не заменяются/не удаляются."""
        desired_dirs, desired_files = _tree(source)
        checked_path(target).mkdir(parents=True, exist_ok=True)
        actual_dirs, actual_files = _tree(target)
        for name in sorted(actual_files):
            if name not in preserved and (name not in desired_files or name in desired_dirs):
                checked_path(target / name).unlink()
        for name in sorted(actual_dirs - {"."}, key=lambda name: (name.count("/"), name), reverse=True):
            if name not in preserved and name not in desired_dirs:
                checked_path(target / name).rmdir()
        for name in sorted(desired_dirs - preserved, key=lambda name: (name.count("/"), name)):
            checked_path(target / name).mkdir(parents=True, exist_ok=True)
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
