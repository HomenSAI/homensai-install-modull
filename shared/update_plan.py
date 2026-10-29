# HomenS.AI · © 2026 Serhii Khomenko
"""Возобновляемая транзакция v2 через идемпотентные порты, без subprocess.

Успешный возврат порта подтверждает проверенный durable результат по key.
Повтор key после аварии обязан вернуть тот же результат без нового эффекта.
Здесь нет адаптеров БД, шифрования, служб или удаления выпусков.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator, Protocol
import uuid

from shared.datahome import checked_path, contains, resolve_datahome
from shared.manifest import validate_manifest
from shared.services import component_order


class UpdateError(RuntimeError):
    """Обобщённая ошибка без строк/исключений адаптеров и секретов."""


class LockBusy(UpdateError):
    """Операция уже занята или блокировка недоступна."""


class UpdatePorts(Protocol):
    """Все эффекты принадлежат адаптеру; key должен переживать смерть процесса.

    backup: отдельная полная проверенная зашифрованная копия под key.
    stage_release: неизменяемый полный payload с проверенными lock/wheels/hash.
    restore: восстановить archive_key, сохранив служебное состояние/lock.
    start/stop: все писатели; health: HTTP 200 без proxy/redirect + hooks,
    ограниченные общим timeout. pre_start выполняется до запуска компонентов.
    switch_pointer: атомарная durable запись manifest.update.pointer_file.
    """

    def current_release(self) -> str: ...
    def stop(self, key: str, components: tuple[str, ...]) -> None: ...
    def backup(self, key: str, purpose: str, command: tuple[str, ...]) -> None: ...
    def stage_release(self, key: str, release: str) -> None: ...
    def migrate(self, key: str, migration: str, command: tuple[str, ...]) -> None: ...
    def switch_pointer(self, key: str, release: str) -> None: ...
    def start(self, key: str, components: tuple[str, ...],
              pre_start: tuple[tuple[str, ...], ...]) -> None: ...
    def health(self, key: str, url: str, timeout: int,
               checks: tuple[tuple[str, ...], ...]) -> None: ...
    def restore(self, key: str, archive_key: str, command: tuple[str, ...]) -> None: ...
    def post_update(self, key: str, hooks: tuple[tuple[str, ...], ...]) -> None: ...


@contextmanager
def operation_lock(path: Path) -> Iterator[bool]:
    """OS advisory lock: постоянный inode, автоматически освобождается при crash.

    Файл не удаляется: unlink создал бы гонку между старым и новым inode.
    Все управляющие процессы обязаны использовать тот же путь и протокол.
    Возвращает stale=True, если после захвата OS lock осталась отметка held
    (либо оборванные метаданные). PID и возраст не разрешают отбирать живой lock.
    """
    path = checked_path(path)
    try:
        stream = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
    except OSError:
        raise LockBusy("Блокировка операции недоступна") from None
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            # Байт вне содержимого: Windows запрещает читать заблокированный
            # диапазон через другой handle, в том числе полному архиватору.
            stream.seek(0x7FFFFFFF)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise LockBusy("Операция занята") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise LockBusy("Операция занята") from None
        acquired = True
        stream.seek(1)
        previous = stream.read(4096)
        try:
            stale = bool(previous) and json.loads(previous).get("status") != "released"
        except (ValueError, UnicodeError, AttributeError):
            stale = True

        def mark(status):
            stream.seek(1)
            stream.write(json.dumps({"version": 1, "status": status,
                                     "pid": os.getpid()}, sort_keys=True).encode("ascii"))
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

        mark("held")
        try:
            yield stale
        finally:
            mark("released")
    finally:
        if acquired:
            if os.name == "nt":
                stream.seek(0x7FFFFFFF)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    checked_path(path)
    temporary = path.with_name(".update-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _identifier(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value) is not None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("Повтор полей состояния")
        result[key] = value
    return result


class UpdatePlan:
    """Один state_file = одна транзакция. Завершённый файл сохраняется для аудита.

    migration_ids — полный неизменяемый порядок продуктовых миграций; по
    умолчанию один вызов manifest.migrations.cmd. Адаптер не должен прятать
    несколько миграций за одним вызовом без собственной копии перед каждой.
    """

    def __init__(self, manifest: dict[str, Any], install_root: Path,
                 ports: UpdatePorts, *, release: str, modes: tuple[str, ...],
                 migration_ids: tuple[str, ...] = ("manifest",),
                 failure_policy: str = "automatic_restore"):
        self.manifest = validate_manifest(manifest)
        self.components = component_order(self.manifest, modes)
        if (not _identifier(release) or type(migration_ids) is not tuple
                or not all(_identifier(item) for item in migration_ids)
                or len(set(migration_ids)) != len(migration_ids)):
            raise UpdateError("Недопустимые публичные идентификаторы плана")
        self.release, self.migrations, self.ports = release, migration_ids, ports
        if type(failure_policy) is not str or failure_policy not in {"automatic_restore", "preserve_forward_journal_no_automatic_restore"}:
            raise UpdateError("Неизвестная политика отказа обновления")
        self.failure_policy = failure_policy
        home = resolve_datahome(self.manifest, install_root)
        services = home.roots["services"]
        self.state_path = checked_path(services / self.manifest["update"]["state_file"].replace("\\", "/"))
        self.lock_path = checked_path(services / "operation.lock")
        pointer = checked_path(services / self.manifest["update"]["pointer_file"].replace("\\", "/"))
        paths = (self.state_path, self.lock_path, pointer)
        if any(contains(a, b) or contains(b, a) for i, a in enumerate(paths) for b in paths[i + 1:]):
            raise UpdateError("Пути состояния, указателя и блокировки пересекаются")
        self.forward = ["stop", "archive", "stage"]
        for index in range(len(migration_ids)):
            self.forward.extend((f"backup:{index}", f"migrate:{index}"))
        self.forward.extend(("switch", "start", "health", "post_update"))
        self.rollback = ["rollback_stop", "before_rollback", "restore", "pointer_back",
                         "rollback_start", "rollback_health"]
        public_plan = {"manifest": self.manifest, "release": release,
                       "modes": sorted(modes), "migrations": migration_ids}
        # Default сохраняет digest существующих legacy journals. Opt-in связан
        # с persisted state.plan и не допускает смену rollback policy при resume.
        if failure_policy != "automatic_restore":
            public_plan["failure_policy"] = failure_policy
        self.digest = hashlib.sha256(json.dumps(public_plan, sort_keys=True,
                                               ensure_ascii=True).encode()).hexdigest()

    def _save(self, state):
        try:
            _atomic_write(self.state_path, state)
        except OSError:
            # Запись checkpoint не считается ошибкой продукта: эффект мог уже
            # завершиться. Следующий run повторит тот же key, не начнёт rollback.
            raise UpdateError("Не удалось атомарно сохранить состояние; повторите run") from None

    def _load(self):
        try:
            with self.state_path.open(encoding="utf-8") as stream:
                value = json.load(stream, object_pairs_hook=_object)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise UpdateError("Состояние повреждено или недоступно") from None
        keys = {"version", "transaction", "plan", "old_release", "release", "direction",
                "index", "failed_phase", "archive_ready", "outcome"}
        if (type(value) is not dict or set(value) != keys
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["transaction"]) is not str
                or re.fullmatch(r"[0-9a-f]{32}", value["transaction"]) is None
                or value["plan"] != self.digest or value["release"] != self.release
                or not _identifier(value["old_release"]) or value["old_release"] == self.release
                or value["direction"] not in ("forward", "rollback")
                or self.failure_policy == "preserve_forward_journal_no_automatic_restore"
                    and value["direction"] != "forward"
                or type(value["index"]) is not int
                or type(value["archive_ready"]) is not bool
                or value["outcome"] not in ("running", "updated", "rolled_back")):
            raise UpdateError("Состояние не соответствует плану")
        phases = self.forward if value["direction"] == "forward" else self.rollback
        index = value["index"]
        if (not 0 <= index <= len(phases)
                or (value["direction"] == "forward" and value["failed_phase"] is not None)
                or (value["direction"] == "rollback" and value["failed_phase"] not in self.forward)
                or value["archive_ready"] != (
                    index > 1 if value["direction"] == "forward"
                    else self.forward.index(value["failed_phase"]) > 1)
                or value["outcome"] != ("running" if index < len(phases) else
                                        "updated" if value["direction"] == "forward" else "rolled_back")):
            raise UpdateError("Нарушены инварианты состояния")
        return value

    def _key(self, state, phase):
        return state["transaction"] + "/" + phase

    def status(self) -> dict[str, Any] | None:
        """Прочитать проверенный checkpoint без mkdir, lock, портов и исполнения.

        Атомарная замена checkpoint позволяет увидеть предыдущую или следующую
        полную версию. Это наблюдение, не разрешение на параллельное управление.
        """
        checked_path(self.state_path)
        state = self._load()
        return dict(state) if state is not None else None

    def _execute(self, state, phase):
        key = self._key(state, phase)
        manifest, ports = self.manifest, self.ports
        hooks = manifest["hooks"]
        commands = lambda group: tuple(tuple(cmd) for cmd in hooks[group])
        if phase in ("stop", "rollback_stop"):
            ports.stop(key, tuple(reversed(self.components)))
        elif phase in ("archive", "before_rollback") or phase.startswith("backup:"):
            purpose = "before-rollback" if phase == "before_rollback" else phase
            ports.backup(key, purpose, tuple(manifest["backup"]["cmd"]))
        elif phase == "stage":
            ports.stage_release(key, self.release)
        elif phase.startswith("migrate:"):
            ports.migrate(key, self.migrations[int(phase.split(":")[1])], tuple(manifest["migrations"]["cmd"]))
        elif phase in ("switch", "pointer_back"):
            ports.switch_pointer(key, self.release if phase == "switch" else state["old_release"])
        elif phase in ("start", "rollback_start"):
            ports.start(key, self.components, commands("pre_start"))
        elif phase in ("health", "rollback_health"):
            web = manifest["web"]
            ports.health(key, f"http://127.0.0.1:{web['port']}{web['healthz']}",
                         manifest["update"]["health_wait"], commands("health_checks"))
        elif phase == "restore":
            # Индекс доказывает отдельный успешно сохранённый checkpoint backup.
            if state["direction"] != "rollback" or state["index"] < 2:
                raise UpdateError("Нет подтверждения before-rollback")
            if state["archive_ready"]:
                ports.restore(key, self._key(state, "archive"), tuple(manifest["restore"]["cmd"]))
        elif phase == "post_update":
            ports.post_update(key, commands("post_update"))

    def run(self) -> dict[str, Any]:
        """Выполнить/возобновить; сбой rollback оставляет фазу для безопасного retry.

        BaseException (смерть процесса) и ошибки checkpoint не запускают откат.
        Обычная ошибка внешнего шага переключает durable направление на rollback.
        """
        checked_path(self.lock_path.parent).mkdir(parents=True, exist_ok=True)
        with operation_lock(self.lock_path):
            checked_path(self.state_path.parent).mkdir(parents=True, exist_ok=True)
            state = self._load()
            if state is None:
                try:
                    previous = self.ports.current_release()
                except Exception:
                    raise UpdateError("Не удалось получить текущий выпуск") from None
                if not _identifier(previous) or previous == self.release:
                    raise UpdateError("Недопустимый текущий выпуск")
                state = {"version": 1, "transaction": uuid.uuid4().hex, "plan": self.digest,
                         "old_release": previous, "release": self.release, "direction": "forward",
                         "index": 0, "failed_phase": None, "archive_ready": False, "outcome": "running"}
                self._save(state)
            while state["outcome"] == "running":
                phases = self.forward if state["direction"] == "forward" else self.rollback
                phase = phases[state["index"]]
                try:
                    self._execute(state, phase)
                except Exception:
                    if self.failure_policy == "preserve_forward_journal_no_automatic_restore":
                        raise
                    if state["direction"] == "rollback":
                        raise UpdateError("Откат приостановлен; повторите run после устранения причины") from None
                    state.update(direction="rollback", index=0, failed_phase=phase)
                    self._save(state)
                    continue
                state["index"] += 1
                if phase == "archive":
                    state["archive_ready"] = True
                if state["index"] == len(phases):
                    state["outcome"] = "updated" if state["direction"] == "forward" else "rolled_back"
                self._save(state)
            return dict(state)
