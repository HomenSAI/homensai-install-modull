"""Локальная диагностика: SQLite, ограниченный аварийный журнал и статистика.

Это best-effort projection. Он НЕ является авторитетным recovery journal
установщика. Неудача диагностики не превращает успешную установку в отказ,
а отказ обязательного recovery journal должен обрабатываться самим движком.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ErrorCatalog, InstallerError, error_envelope, redact, utcnow

SCHEMA_VERSION = 1
APPLICATION_ID = 0x48494D44
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+ -]{0,127}$")
_TERMINAL = {"SUCCEEDED", "SUCCESS", "FAILED", "FAILED_ROLLED_BACK", "CANCELLED", "CANCELED", "ROLLED_BACK"}
_SUCCESS = {"SUCCEEDED", "SUCCESS"}
_FAILURE = {"FAILED", "FAILED_ROLLED_BACK"}
_EVENT_FIELDS = {"event_id", "sequence", "type", "timestamp", "phase", "component_id", "step_id", "status",
                 "progress", "percent", "completed", "current", "total", "unit", "message_code", "error",
                 "duration_ms", "bytes_delta", "attempt_id", "retryable"}
_PUBLIC_EVENT_FIELDS = _EVENT_FIELDS - {"error"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
 operation_id TEXT PRIMARY KEY, action TEXT NOT NULL, product_id TEXT NOT NULL,
 product_version TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, duration_ms REAL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
 attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, duration_ms REAL);
CREATE INDEX IF NOT EXISTS attempts_operation ON attempts(operation_id);
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, sequence INTEGER,
 type TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(operation_id, sequence));
CREATE INDEX IF NOT EXISTS events_operation ON events(operation_id, sequence, timestamp);
CREATE TABLE IF NOT EXISTS errors (
 error_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, attempt_id TEXT,
 code TEXT NOT NULL, cause_status TEXT NOT NULL, parent_error_id TEXT,
 timestamp TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS errors_operation ON errors(operation_id, timestamp);
CREATE TABLE IF NOT EXISTS ingested_records (record_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS diagnostic_meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS overflow_tracking (episode_id TEXT PRIMARY KEY, discarded_records INTEGER NOT NULL);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(value, name="identifier") -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError("invalid " + name)
    return value


def _label(value, name="label") -> str:
    if value == "":
        return ""
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        raise ValueError("invalid " + name)
    return value


def _target(value) -> str:
    if isinstance(value, str) and value.startswith("{"):
        if len(value) > 1024:
            raise ValueError("target is too large")
        value = json.loads(value)
    if isinstance(value, dict):
        if set(value) - {"os", "arch", "mode", "scope"}:
            raise ValueError("unknown diagnostic target fields")
        return _json({key: _label(item, "target field") for key, item in value.items()})
    return _label(value, "target")


def _number(value, name="number"):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > 2**53:
        raise ValueError("invalid " + name)
    return value


def _stamp(value: str | None) -> str:
    if value is None:
        return utcnow()
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _private_directory(path: Path):
    # Never follow an existing symlink in the diagnostic location.
    absolute = path.absolute()
    for parent in (absolute, *absolute.parents):
        if parent.is_symlink():
            raise OSError("diagnostic directory is a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _safe_open(path: Path, flags: int, mode: int = 0o600):
    if path.is_symlink():
        raise OSError("diagnostic file is a symlink")
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


class DiagnosticStore:
    """Записи возвращают stored=sqlite/fallback/unavailable и не рекурсируют.

    Входные details/metadata уже должны состоять из разрешённых технических
    фактов. Дополнительная redaction не заменяет это требование.
    """
    def __init__(self, path, *, fallback_path=None, known_secrets=(), max_fallback_bytes=1_048_576,
                 max_record_bytes=None, max_events=100000):
        self.path = Path(path)
        self.fallback_path = Path(fallback_path) if fallback_path is not None else self.path.with_suffix(self.path.suffix + ".fallback.ndjson")
        if self.path.absolute() == self.fallback_path.absolute():
            raise ValueError("database and fallback paths must differ")
        if type(max_fallback_bytes) is not int or not 4096 <= max_fallback_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid fallback size limit")
        if max_record_bytes is None:
            max_record_bytes = min(65536, max_fallback_bytes // 2)
        if type(max_record_bytes) is not int or not 1024 <= max_record_bytes <= max_fallback_bytes // 2:
            raise ValueError("invalid record size limit")
        if type(max_events) is not int or max_events < 1:
            raise ValueError("invalid retention event limit")
        self.max_fallback_bytes = max_fallback_bytes
        self.max_record_bytes = max_record_bytes
        self.max_events = max_events
        self.known_secrets = tuple(known_secrets)
        self._mutex = threading.RLock()
        self._started = {}
        self._attempt_started = {}
        self._sqlite_available = False
        self._unpersisted = 0
        self.last_warning = None
        try:
            with self._connection():
                pass
            self._sqlite_available = True
            if self.fallback_path.exists():
                self.import_fallback()
        except (sqlite3.Error, OSError):
            self.last_warning = "DIAGNOSTIC_STORE_UNAVAILABLE"

    @contextmanager
    def _connection(self):
        _private_directory(self.path.parent)
        if not self.path.exists():
            descriptor = _safe_open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(descriptor)
        if self.path.is_symlink():
            raise OSError("diagnostic database is a symlink")
        connection = sqlite3.connect(str(self.path), timeout=1.0)
        connection.row_factory = sqlite3.Row
        try:
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if application not in (0, APPLICATION_ID) or version > SCHEMA_VERSION:
                raise sqlite3.DatabaseError("foreign or newer diagnostic database")
            if version == 0:
                existing = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if existing:
                    raise sqlite3.DatabaseError("unrecognized existing database; preserved")
                connection.executescript(_SCHEMA)
                connection.execute("PRAGMA application_id=" + str(APPLICATION_ID))
                connection.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
                connection.commit()
            elif application != APPLICATION_ID:
                raise sqlite3.DatabaseError("unrecognized diagnostic database identity")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _fallback_lock(self):
        """Короткая межпроцессная блокировка отдельного файла, без network I/O."""
        with self._mutex:
            _private_directory(self.fallback_path.parent)
            lock_path = self.fallback_path.with_name(self.fallback_path.name + ".lock")
            descriptor = _safe_open(lock_path, os.O_CREAT | os.O_RDWR)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            locked = False
            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                deadline = time.monotonic() + 1.0
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except (OSError, BlockingIOError):
                        if time.monotonic() >= deadline:
                            raise OSError("diagnostic fallback lock unavailable")
                        time.sleep(0.02)
                yield
            finally:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def _fallback_lines(self):
        if not self.fallback_path.exists():
            return []
        descriptor = _safe_open(self.fallback_path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as handle:
            content = handle.read(self.max_fallback_bytes + 1)
        if len(content) > self.max_fallback_bytes:
            raise OSError("fallback exceeds configured limit; preserved")
        return content.splitlines(keepends=True)

    def _replace_fallback(self, content: bytes):
        if len(content) > self.max_fallback_bytes:
            raise ValueError("fallback size overflow")
        descriptor, temp_name = tempfile.mkstemp(prefix=".diagnostics-", suffix=".tmp", dir=self.fallback_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if self.fallback_path.is_symlink():
                raise OSError("fallback is a symlink")
            os.replace(temp_name, self.fallback_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _fallback(self, record):
        try:
            encoded = (_json(record) + "\n").encode("utf-8")
            with self._fallback_lock():
                lines = self._fallback_lines()
                total_lost = 0
                overflow_id = "overflow-" + str(uuid.uuid4())
                if lines:
                    try:
                        first = json.loads(lines[0])
                        if first.get("kind") == "overflow":
                            total_lost = int(first["payload"]["discarded_records"])
                            overflow_id = first["record_id"]
                            lines.pop(0)
                    except (ValueError, KeyError, TypeError):
                        pass
                # Keep bounded tail; loss is explicit and survives another process.
                lines.append(encoded)
                size = sum(map(len, lines))
                while lines and size > self.max_fallback_bytes - 512:
                    size -= len(lines.pop(0))
                    total_lost += 1
                if total_lost:
                    gap = {"record_id": overflow_id, "kind": "overflow", "timestamp": utcnow(),
                           "payload": {"discarded_records": total_lost}}
                    lines.insert(0, (_json(gap) + "\n").encode("utf-8"))
                self._replace_fallback(b"".join(lines))
            self.last_warning = "DIAGNOSTIC_STORE_UNAVAILABLE"
            return {"stored": "fallback", "record_id": record["record_id"], "warning": self.last_warning,
                    "discarded_records": total_lost}
        except (OSError, ValueError, TypeError):
            self._unpersisted += 1
            self.last_warning = "DIAGNOSTIC_STORE_UNAVAILABLE"
            return {"stored": "unavailable", "record_id": record["record_id"], "warning": self.last_warning}

    def _apply(self, connection, record):
        """Один record импортируется ровно один раз в одной SQLite-транзакции."""
        if record["kind"] == "overflow":
            count = record["payload"]["discarded_records"]
            if type(count) is not int or count < 0:
                raise ValueError("invalid overflow count")
            previous = connection.execute("SELECT discarded_records FROM overflow_tracking WHERE episode_id=?", (record["record_id"],)).fetchone()
            previous_count = previous[0] if previous else 0
            difference = max(0, count - previous_count)
            connection.execute("INSERT INTO overflow_tracking VALUES (?,?) ON CONFLICT(episode_id) DO UPDATE SET discarded_records=MAX(discarded_records,excluded.discarded_records)", (record["record_id"], count))
            connection.execute("INSERT INTO diagnostic_meta VALUES ('discarded_records',?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (difference,))
            return bool(difference)
        if connection.execute("SELECT 1 FROM ingested_records WHERE record_id=?", (record["record_id"],)).fetchone():
            return False
        payload = record["payload"]
        kind = record["kind"]
        if kind == "operation_start":
            previous = connection.execute("SELECT action,product_id,product_version,target FROM operations WHERE operation_id=?", (payload["operation_id"],)).fetchone()
            if previous and tuple(previous) != (payload["action"], payload["product_id"], payload["product_version"], payload["target"]):
                raise ValueError("operation identity reused for another request")
            connection.execute("INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)", (
                payload["operation_id"], payload["action"], payload["product_id"], payload["product_version"],
                payload["target"], "RUNNING", payload["started_at"], None, None, _json(payload["metadata"])))
            if previous:
                connection.execute("UPDATE operations SET status='RUNNING',finished_at=NULL WHERE operation_id=?", (payload["operation_id"],))
        elif kind == "operation_finish":
            connection.execute("UPDATE operations SET status=?,finished_at=?,duration_ms=? WHERE operation_id=?", (
                payload["status"], payload["finished_at"], payload["duration_ms"], payload["operation_id"]))
        elif kind == "attempt_start":
            connection.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,?,?,?,?,?)", (
                payload["attempt_id"], payload["operation_id"], payload["kind"], "RUNNING", payload["started_at"], None, None))
        elif kind == "attempt_finish":
            connection.execute("UPDATE attempts SET status=?,finished_at=?,duration_ms=? WHERE attempt_id=?", (
                payload["status"], payload["finished_at"], payload["duration_ms"], payload["attempt_id"]))
        elif kind == "event":
            previous = connection.execute("SELECT operation_id FROM events WHERE event_id=?", (payload["event_id"],)).fetchone()
            if previous and previous[0] != payload["operation_id"]:
                raise ValueError("event identity belongs to another operation")
            if payload.get("sequence") is not None:
                sequence = connection.execute("SELECT event_id FROM events WHERE operation_id=? AND sequence=?", (payload["operation_id"], payload["sequence"])).fetchone()
                if sequence and sequence[0] != payload["event_id"]:
                    raise ValueError("conflicting diagnostic event sequence")
            connection.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (
                payload["event_id"], payload["operation_id"], payload.get("sequence"), payload["type"], payload["timestamp"], _json(payload)))
        elif kind == "error":
            previous = connection.execute("SELECT operation_id FROM errors WHERE error_id=?", (payload["error"]["error_id"],)).fetchone()
            if previous and previous[0] != payload["operation_id"]:
                raise ValueError("error identity belongs to another operation")
            error = payload["error"]
            connection.execute("INSERT OR IGNORE INTO errors VALUES (?,?,?,?,?,?,?,?)", (
                error["error_id"], payload["operation_id"], payload["attempt_id"], error["code"], error["cause_status"],
                error.get("parent_error_id"), error["timestamp"], _json(error)))
        else:
            raise ValueError("unknown diagnostic record kind")
        connection.execute("INSERT INTO ingested_records VALUES (?,?)", (record["record_id"], record["timestamp"]))
        return True

    def _record(self, kind, payload):
        record = {"record_id": str(uuid.uuid4()), "kind": kind, "timestamp": utcnow(),
                  "payload": redact(payload, known_secrets=self.known_secrets)}
        if len(_json(record).encode("utf-8")) > self.max_record_bytes:
            raise ValueError("diagnostic record exceeds size limit")
        # Replay older records before newer state transitions. Never let a
        # recovered SQLite receive finish before pending start/update records.
        if self.fallback_path.exists():
            imported = self.import_fallback()
            if not imported.get("available"):
                return self._fallback(record)
        try:
            with self._connection() as connection, connection:
                self._apply(connection, record)
            self._sqlite_available = True
            return {"stored": "sqlite", "record_id": record["record_id"]}
        except (sqlite3.Error, OSError):
            self._sqlite_available = False
            return self._fallback(record)

    def start_operation(self, action, *, operation_id=None, product_id="", product_version="", target="", metadata=None):
        operation_id = _id(operation_id or str(uuid.uuid4()), "operation_id")
        self._record("operation_start", {"operation_id": operation_id, "action": _label(action, "action"),
                     "product_id": _label(product_id, "product_id"), "product_version": _label(product_version, "version"),
                     "target": _target(target), "started_at": utcnow(), "metadata": metadata or {}})
        self._started.setdefault(operation_id, time.monotonic())
        return operation_id

    def finish_operation(self, operation_id, status, *, duration_ms=None):
        operation_id = _id(operation_id, "operation_id")
        status = _id(status, "status").upper()
        # Only the controller knows which intervals were active vs waiting or
        # powered off. Missing active duration remains unknown, never wall time.
        if status in _TERMINAL:
            self._started.pop(operation_id, None)
        return self._record("operation_finish", {"operation_id": operation_id, "status": status,
                     "finished_at": utcnow() if status in _TERMINAL else None, "duration_ms": _number(duration_ms, "duration_ms")})

    def start_attempt(self, operation_id, *, attempt_id=None, kind="execution"):
        operation_id = _id(operation_id, "operation_id")
        attempt_id = _id(attempt_id or str(uuid.uuid4()), "attempt_id")
        self._record("attempt_start", {"operation_id": operation_id, "attempt_id": attempt_id,
                     "kind": _label(kind, "attempt kind"), "started_at": utcnow()})
        self._attempt_started.setdefault(attempt_id, time.monotonic())
        return attempt_id

    def finish_attempt(self, attempt_id, status, *, duration_ms=None):
        attempt_id = _id(attempt_id, "attempt_id")
        status = _id(status, "status").upper()
        start = self._attempt_started.pop(attempt_id, None)
        if duration_ms is None and start is not None:
            duration_ms = max(0.0, (time.monotonic() - start) * 1000)
        return self._record("attempt_finish", {"attempt_id": attempt_id, "status": status,
                     "finished_at": utcnow(), "duration_ms": _number(duration_ms, "duration_ms")})

    def record_event(self, operation_id, event: dict):
        operation_id = _id(operation_id, "operation_id")
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        payload = {key: value for key, value in event.items() if key in _EVENT_FIELDS}
        sequence = payload.get("sequence")
        if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= 2**53):
            raise ValueError("invalid event sequence")
        event_id = payload.get("event_id")
        if event_id is None:
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id + ":" + str(sequence))) if sequence is not None else str(uuid.uuid4())
        payload.update(event_id=_id(event_id, "event_id"), operation_id=operation_id,
                       type=_label(payload.get("type", "event"), "event type"), timestamp=_stamp(payload.get("timestamp")))
        for field in ("phase", "component_id", "step_id", "status", "message_code", "attempt_id"):
            if payload.get(field) is not None:
                payload[field] = _id(payload[field], field)
        if payload.get("unit") is not None:
            payload["unit"] = _label(payload["unit"], "unit")
        if isinstance(payload.get("progress"), dict):
            progress = payload["progress"]
            payload["progress"] = {key: _number(progress[key], key) for key in ("completed", "total", "percent") if key in progress}
            if progress.get("unit") is not None:
                payload["progress"]["unit"] = _label(progress["unit"], "unit")
        elif "progress" in payload:
            payload["progress"] = _number(payload["progress"], "progress")
        for field in ("duration_ms", "bytes_delta", "percent", "completed", "current", "total"):
            if field in payload:
                payload[field] = _number(payload[field], field)
        result = self._record("event", payload)
        result["event_id"] = payload["event_id"]
        return result

    def record_error(self, operation_id, error: dict, *, attempt_id=None):
        operation_id = _id(operation_id, "operation_id")
        if isinstance(error, InstallerError):
            error = error.to_dict()
        if not isinstance(error, dict):
            raise ValueError("error must be an object")
        envelope = error_envelope(error.get("code", "UNKNOWN_ERROR"), locale=error.get("locale", "ru"),
            phase=error.get("phase"), component_id=error.get("component_id"), cause_status=error.get("cause_status", "unknown"),
            evidence=error.get("evidence"), details=error.get("details"), retryable=error.get("retryable"),
            error_id=error.get("error_id"), parent_error_id=error.get("parent_error_id"),
            known_secrets=self.known_secrets, external_exit_code=error.get("external_exit_code"))
        envelope["error_id"] = _id(envelope["error_id"], "error_id")
        envelope["timestamp"] = _stamp(error.get("timestamp"))
        result = self._record("error", {"operation_id": operation_id, "attempt_id": _id(attempt_id, "attempt_id") if attempt_id else None, "error": envelope})
        result["error_id"] = envelope["error_id"]
        return result

    def import_fallback(self):
        """Повторный импорт безопасен даже после сбоя между commit и очисткой."""
        if not self.fallback_path.exists():
            return {"imported": 0, "duplicates": 0, "pending": 0, "malformed": 0, "available": self._sqlite_available}
        imported = duplicates = malformed = 0
        try:
            with self._fallback_lock():
                lines = self._fallback_lines()
                remaining = []
                with self._connection() as connection, connection:
                    for line in lines:
                        try:
                            record = json.loads(line)
                            if not isinstance(record, dict) or set(record) != {"record_id", "kind", "timestamp", "payload"}:
                                raise ValueError("invalid fallback record")
                            _id(record["record_id"])
                            _stamp(record["timestamp"])
                            if not isinstance(record["payload"], dict):
                                raise ValueError("invalid fallback payload")
                            applied = self._apply(connection, record)
                            imported += int(applied)
                            duplicates += int(not applied)
                        except (ValueError, KeyError, TypeError):
                            remaining.append(line)
                            malformed += 1
                self._replace_fallback(b"".join(remaining))
            self._sqlite_available = True
            return {"imported": imported, "duplicates": duplicates, "pending": len(remaining), "malformed": malformed, "available": True}
        except (sqlite3.Error, OSError):
            self._sqlite_available = False
            return {"imported": 0, "duplicates": 0, "pending": None, "malformed": malformed, "available": False,
                    "warning": "DIAGNOSTIC_STORE_UNAVAILABLE"}

    def _snapshot(self):
        """SQLite snapshot; при отказе доступны только сохранившиеся fallback записи."""
        import_result = self.import_fallback()
        try:
            with self._connection() as connection, connection:
                connection.execute("BEGIN")
                return self._rows(connection), {"source": "sqlite", "fallback_pending": import_result.get("pending", 0),
                    "unpersisted_records": self._unpersisted, "available": True}
        except (sqlite3.Error, OSError):
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            try:
                connection.executescript(_SCHEMA)
                try:
                    with self._fallback_lock():
                        lines = self._fallback_lines()
                    malformed = 0
                    for line in lines:
                        try:
                            self._apply(connection, json.loads(line))
                        except (ValueError, KeyError, TypeError, sqlite3.Error):
                            malformed += 1
                except OSError:
                    malformed = 1
                return self._rows(connection), {"source": "fallback", "fallback_pending": None, "malformed": malformed,
                    "unpersisted_records": self._unpersisted, "available": False}
            finally:
                connection.close()

    @staticmethod
    def _rows(connection):
        rows = {}
        for name, identity in (("operations", "operation_id"), ("attempts", "attempt_id"), ("events", "event_id"), ("errors", "error_id")):
            rows[name] = [dict(row) for row in connection.execute("SELECT * FROM " + name + " ORDER BY " + identity)]
        for row in rows["operations"]:
            row["metadata"] = json.loads(row["metadata"])
        for name in ("events", "errors"):
            for row in rows[name]:
                row["payload"] = json.loads(row["payload"])
        rows["meta"] = {row[0]: row[1] for row in connection.execute("SELECT name,value FROM diagnostic_meta")}
        return rows

    def get(self, operation_id, limit=500):
        operation_id = _id(operation_id, "operation_id")
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("invalid diagnostic limit")
        rows, state = self._snapshot()
        operation = next((row for row in rows["operations"] if row["operation_id"] == operation_id), None)
        events = [row["payload"] for row in rows["events"] if row["operation_id"] == operation_id]
        events.sort(key=lambda row: (row.get("sequence") is None, row.get("sequence") or 0, row["timestamp"], row["event_id"]))
        errors = [dict(row["payload"], operation_id=row["operation_id"], attempt_id=row["attempt_id"]) for row in rows["errors"] if row["operation_id"] == operation_id]
        errors.sort(key=lambda row: (row["timestamp"], row["error_id"]))
        return {"schema_version": SCHEMA_VERSION, "operation": operation,
                "attempts": [row for row in rows["attempts"] if row["operation_id"] == operation_id],
                "events": events[:limit], "errors": errors[:limit], "truncated": len(events) > limit or len(errors) > limit,
                "diagnostic_state": state}

    def query_statistics(self, product_id=None, target=None, since=None, until=None, product_version=None, action=None, status=None):
        since = _stamp(since) if since else None
        until = _stamp(until) if until else None
        if since and until and since > until:
            raise ValueError("invalid statistics interval")
        if product_id is not None:
            _label(product_id, "product_id")
        if target is not None:
            target = _target(target)
        for name, value in (("product_version", product_version), ("action", action), ("status", status)):
            if value is not None:
                _label(value, name)
        rows, state = self._snapshot()
        operations = [op for op in rows["operations"] if (product_id is None or op["product_id"] == product_id)
                      and (target is None or op["target"] == target) and (since is None or op["started_at"] >= since)
                      and (product_version is None or op["product_version"] == product_version)
                      and (action is None or op["action"] == action) and (status is None or op["status"] == status)
                      and (until is None or op["started_at"] <= until)]
        ids = {row["operation_id"] for row in operations}
        attempts = [row for row in rows["attempts"] if row["operation_id"] in ids]
        events = [row["payload"] for row in rows["events"] if row["operation_id"] in ids]
        errors = [row for row in rows["errors"] if row["operation_id"] in ids]
        statuses = Counter(row["status"] for row in operations)
        successes = sum(statuses.get(status, 0) for status in _SUCCESS)
        failures = sum(statuses.get(status, 0) for status in _FAILURE)
        completed = successes + failures
        execution = [row for row in attempts if row["kind"] == "execution"]
        execution_by_op = Counter(row["operation_id"] for row in execution)
        by_error = {}
        for row in errors:
            entry = by_error.setdefault(row["code"], {"occurrences": 0, "operation_ids": set()})
            entry["occurrences"] += 1
            entry["operation_ids"].add(row["operation_id"])
        by_error = {code: {"occurrences": item["occurrences"], "affected_operations": len(item["operation_ids"])} for code, item in sorted(by_error.items())}
        durations = [row["duration_ms"] for row in operations if row["duration_ms"] is not None]
        phases = {}
        for event in events:
            if event.get("type") in ("phase.completed", "phase_finished") and isinstance(event.get("phase"), str) and event.get("duration_ms") is not None:
                phases[event["phase"]] = phases.get(event["phase"], 0) + event["duration_ms"]
        lost = rows["meta"].get("discarded_records", 0)
        return {"schema_version": SCHEMA_VERSION, "scope": "local", "selection": {"product_id": product_id, "target": target,
                    "product_version": product_version, "action": action, "status": status, "since": since, "until": until},
                "total_operations": len(operations), "operations_by_status": dict(sorted(statuses.items())),
                "operations_by_action": dict(sorted(Counter(op["action"] for op in operations).items())),
                "total_attempts": len(attempts), "attempts_by_kind": dict(Counter(a["kind"] for a in attempts)),
                "execution_retries": sum(max(0, count - 1) for count in execution_by_op.values()),
                "successful_operations": successes, "failed_operations": failures,
                "cancelled_operations": statuses.get("CANCELLED", 0) + statuses.get("CANCELED", 0),
                "success_rate_all_operations": {"numerator": successes, "denominator": len(operations), "ratio": successes / len(operations) if operations else None},
                "success_rate_technical_outcomes": {"numerator": successes, "denominator": completed, "ratio": successes / completed if completed else None,
                    "excludes": ["cancelled", "waiting", "running", "unknown"]},
                "errors_by_code": by_error,
                "errors_by_phase": dict(Counter(row["payload"].get("phase") or "unknown" for row in errors)),
                "errors_by_component": dict(Counter(row["payload"].get("component_id") or "unknown" for row in errors)),
                "download_bytes": sum(event.get("bytes_delta", 0) or 0 for event in events if event.get("type") == "download.bytes"),
                "download_retries": sum(event.get("type") == "download.retry" for event in events),
                "cache_hits": sum(event.get("type") == "download.cache_hit" for event in events),
                "active_duration_ms": {"measured_operations": len(durations), "sum": sum(durations), "mean": sum(durations) / len(durations) if durations else None},
                "phase_duration_ms": phases, "diagnostic_record_loss_count": lost,
                "retention_deleted_events": rows["meta"].get("retention_deleted_events", 0),
                "retention_deleted_operations": rows["meta"].get("retention_deleted_operations", 0),
                "data_complete": state["available"] and state.get("fallback_pending") == 0 and not self._unpersisted and not lost and not rows["meta"].get("retention_deleted_events", 0) and not rows["meta"].get("retention_deleted_operations", 0),
                "diagnostic_state": state}

    def export_preview(self, operation_id=None):
        if operation_id is not None:
            _id(operation_id, "operation_id")
        rows, state = self._snapshot()
        selected = [row for row in rows["operations"] if operation_id is None or row["operation_id"] == operation_id]
        ids = {row["operation_id"] for row in selected}
        operations = [{key: value for key, value in row.items() if key != "metadata"} for row in selected]
        attempts = [row for row in rows["attempts"] if row["operation_id"] in ids]
        events = []
        for row in rows["events"]:
            if row["operation_id"] not in ids:
                continue
            event = {key: value for key, value in row["payload"].items() if key in _PUBLIC_EVENT_FIELDS}
            # Arbitrary nested progress/message bodies are not support-export data.
            if isinstance(event.get("progress"), dict):
                event["progress"] = {k: v for k, v in event["progress"].items() if k in ("completed", "total", "percent", "unit") and type(v) in (int, float, str)}
            elif "progress" in event and type(event["progress"]) not in (int, float):
                event.pop("progress")
            event["operation_id"] = row["operation_id"]
            events.append(event)
        errors = []
        for row in rows["errors"]:
            if row["operation_id"] not in ids:
                continue
            value = row["payload"]
            allowed = {"schema_version", "error_id", "code", "known_code", "catalog_version", "locale", "severity", "retryable", "cause_status", "phase", "component_id", "parent_error_id", "external_exit_code", "timestamp"}
            exported = {key: value[key] for key in allowed if key in value}
            exported["operation_id"] = row["operation_id"]
            # Regenerate explanation from local trusted catalog, never raw stderr.
            description = ErrorCatalog().describe(value["code"], value.get("locale", "ru"))
            exported.update(title=description["title"], message=description["description"])
            errors.append(exported)
        report = redact({"schema_version": SCHEMA_VERSION, "selection": {"operation_id": operation_id},
            "operations": operations, "attempts": attempts, "events": events, "errors": errors,
            "diagnostic_state": state}, known_secrets=self.known_secrets)
        digest = hashlib.sha256(_json(report).encode("utf-8")).hexdigest()
        return {"schema_version": SCHEMA_VERSION, "sha256": digest, "requires_confirmation": True,
                "contents": {"operations": len(operations), "attempts": len(attempts), "events": len(events), "errors": len(errors)},
                "omitted": ["operation metadata", "raw stdout/stderr", "error details and evidence values", "environment dumps", "secrets", "user documents and database contents"],
                "report": report}

    def export(self, path, *, operation_id=None, confirmed=False, expected_digest=None):
        if confirmed is not True:
            raise InstallerError("CONSENT_REQUIRED")
        preview = self.export_preview(operation_id)
        if expected_digest is not None and preview["sha256"] != expected_digest:
            raise InstallerError("PLAN_STALE")
        destination = Path(path)
        _private_directory(destination.parent)
        descriptor = _safe_open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(preview["report"], ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return {"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "preview_sha256": preview["sha256"], "contents": preview["contents"], "uploaded": False}

    def prune(self, *, older_than_days=90, max_events=None):
        """Удаляется только диагностика завершённых операций, не recovery state."""
        if type(older_than_days) is not int or older_than_days < 1:
            raise ValueError("invalid retention period")
        limit = self.max_events if max_events is None else max_events
        if type(limit) is not int or limit < 1:
            raise ValueError("invalid event retention limit")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            with self._connection() as connection, connection:
                placeholders = ",".join("?" for _ in _TERMINAL)
                old = [row[0] for row in connection.execute("SELECT operation_id FROM operations WHERE finished_at < ? AND status IN (" + placeholders + ")", (cutoff, *sorted(_TERMINAL)))]
                removed_events = removed_operations = 0
                for operation_id in old:
                    removed_events += connection.execute("DELETE FROM events WHERE operation_id=?", (operation_id,)).rowcount
                    connection.execute("DELETE FROM attempts WHERE operation_id=?", (operation_id,))
                    connection.execute("DELETE FROM errors WHERE operation_id=?", (operation_id,))
                    removed_operations += connection.execute("DELETE FROM operations WHERE operation_id=?", (operation_id,)).rowcount
                total_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                if total_events > limit:
                    candidates = [row[0] for row in connection.execute("SELECT events.event_id FROM events JOIN operations USING(operation_id) WHERE operations.status IN (" + placeholders + ") ORDER BY events.timestamp LIMIT ?", (*sorted(_TERMINAL), total_events - limit))]
                    for event_id in candidates:
                        removed_events += connection.execute("DELETE FROM events WHERE event_id=?", (event_id,)).rowcount
                if removed_events:
                    connection.execute("INSERT INTO diagnostic_meta VALUES ('retention_deleted_events',?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (removed_events,))
                if removed_operations:
                    connection.execute("INSERT INTO diagnostic_meta VALUES ('retention_deleted_operations',?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (removed_operations,))
                # Idempotency receipts are retained while fallback is pending.
                if not self.fallback_path.exists() or self.fallback_path.stat().st_size == 0:
                    connection.execute("DELETE FROM ingested_records WHERE timestamp < ?", (cutoff,))
            return {"pruned": True, "operations": removed_operations, "events": removed_events, "cutoff": cutoff}
        except (sqlite3.Error, OSError):
            return {"pruned": False, "warning": "DIAGNOSTIC_STORE_UNAVAILABLE"}
