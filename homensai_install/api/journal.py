"""Авторитетный журнал API. Диагностическая БД — отдельная проекция событий."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from .model import canonical, checked_id, checked_product_id, fail, parse_json


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def owned_directory(path: Path, *, create: bool) -> Path:
    from shared.datahome import checked_path, DataHomeError
    try:
        result = checked_path(path)
        if result == Path(result.anchor):
            fail("INVALID_REQUEST", details={"field_path": "state_dir"})
        if create:
            result.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not result.is_dir():
            raise FileNotFoundError(result)
        info = result.lstat()
        if not stat.S_ISDIR(info.st_mode):
            fail("STATE_STORAGE_UNAVAILABLE")
        if os.name != "nt" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            fail("STATE_STORAGE_UNAVAILABLE", details={"reason": "private_owned_directory_required"})
        return result
    except (DataHomeError, OSError):
        if not create:
            raise FileNotFoundError from None
        fail("STATE_STORAGE_UNAVAILABLE")


class Journal:
    def __init__(self, directory: Path, *, create: bool = True):
        self.directory = owned_directory(directory, create=create)
        self.path = self.directory / "operations.sqlite3"
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                fail("STATE_STORAGE_UNAVAILABLE")
            if os.name != "nt" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                fail("STATE_STORAGE_UNAVAILABLE")
        elif not create:
            raise FileNotFoundError
        if create:
            try:
                with self.connection(write=True) as db:
                    schema = """
                    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
                    CREATE TABLE IF NOT EXISTS plans (
                        plan_id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS operations (
                        operation_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                        request_digest TEXT NOT NULL, resource_key TEXT NOT NULL,
                        body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS claims (
                        resource_key TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL);
                    CREATE TABLE IF NOT EXISTS events (
                        operation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                        event_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
                        PRIMARY KEY(operation_id, sequence));
                    """
                    for statement in schema.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                    if row != ("1",):
                        fail("API_UNSUPPORTED")
                if os.name != "nt":
                    self.path.chmod(0o600)
            except sqlite3.Error:
                fail("STATE_STORAGE_UNAVAILABLE")

    @contextmanager
    def connection(self, *, write: bool = False):
        db = None
        try:
            db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.execute("COMMIT")
        except sqlite3.Error:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            fail("STATE_STORAGE_UNAVAILABLE")
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            if db is not None:
                db.close()

    def save_plan(self, plan: dict):
        with self.connection(write=True) as db:
            db.execute("INSERT INTO plans VALUES (?,?,?)",
                       (plan["plan_id"], plan["plan_digest"], canonical(plan)))

    def plan(self, plan_id: str) -> dict:
        checked_id(plan_id, field="plan_id")
        with self.connection() as db:
            row = db.execute("SELECT body FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            fail("PLAN_NOT_FOUND")
        return parse_json(row[0], error_code="INTEGRITY_FAILED")

    def by_key(self, key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT request_digest,body FROM operations WHERE idempotency_key=?", (key,)).fetchone()
        return {"request_digest": row[0], "operation": parse_json(row[1])} if row else None

    def create_operation(self, operation: dict, key: str, request_digest: str, resource_key: str) -> tuple[dict, bool]:
        with self.connection(write=True) as db:
            old = db.execute("SELECT request_digest,body FROM operations WHERE idempotency_key=?", (key,)).fetchone()
            if old:
                if old[0] != request_digest:
                    fail("OPERATION_CONFLICT")
                return parse_json(old[1]), False
            if db.execute("SELECT operation_id FROM claims WHERE resource_key=?", (resource_key,)).fetchone():
                fail("OPERATION_CONFLICT")
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?)",
                       (operation["operation_id"], key, request_digest, resource_key, canonical(operation)))
            db.execute("INSERT INTO claims VALUES (?,?)", (resource_key, operation["operation_id"]))
            self._event(db, operation, {"type": "operation.queued", "status": operation["status"]})
            db.execute("UPDATE operations SET body=? WHERE operation_id=?",
                       (canonical(operation), operation["operation_id"]))
        return operation, True

    def operation(self, operation_id: str) -> dict:
        checked_id(operation_id, field="operation_id")
        with self.connection() as db:
            row = db.execute("SELECT body FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            fail("OPERATION_NOT_FOUND")
        return parse_json(row[0], error_code="RECOVERY_REQUIRED")

    def operations_list(self, product_id: str | None = None, *, unfinished_only: bool = True,
                        limit: int = 101) -> list[dict]:
        """Ограниченная SQL-проекция; plans/options/consents/results не возвращаются.

        Product берётся из сохранённого плана, поэтому работает и с прежними
        journal v1 без product_id непосредственно в записи операции.
        """
        if (type(limit) is not int or not 1 <= limit <= 201
                or type(unfinished_only) is not bool):
            fail("INVALID_REQUEST")
        if product_id is not None:
            checked_product_id(product_id)
        query = """
            SELECT o.operation_id,
                   json_extract(o.body, '$.plan_id'),
                   json_extract(o.body, '$.action'),
                   json_extract(o.body, '$.status'),
                   json_extract(p.body, '$.source_manifest.product_id'),
                   json_extract(o.body, '$.updated_at')
            FROM operations AS o
            JOIN plans AS p ON p.plan_id = json_extract(o.body, '$.plan_id')
            WHERE (? IS NULL OR json_extract(p.body, '$.source_manifest.product_id') = ?)
              AND (? = 0 OR json_extract(o.body, '$.status') NOT IN
                   ('SUCCEEDED', 'FAILED', 'FAILED_ROLLED_BACK', 'CANCELED')
                   OR json_extract(o.body, '$.result.resume_required') = 1)
            ORDER BY json_extract(o.body, '$.updated_at') DESC, o.operation_id DESC
            LIMIT ?
        """
        with self.connection() as db:
            rows = db.execute(query, (product_id, product_id, int(unfinished_only), limit)).fetchall()
        fields = ("operation_id", "plan_id", "action", "status", "product_id", "updated_at")
        return [dict(zip(fields, row)) for row in rows]

    def update(self, operation_id: str, updates: dict, event: dict | None = None, *, release: bool = False) -> dict:
        with self.connection(write=True) as db:
            row = db.execute("SELECT body FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                fail("OPERATION_NOT_FOUND")
            operation = parse_json(row[0])
            operation.update(updates)
            operation["updated_at"] = now()
            if event:
                self._event(db, operation, event)
            db.execute("UPDATE operations SET body=? WHERE operation_id=?", (canonical(operation), operation_id))
            if release:
                db.execute("DELETE FROM claims WHERE operation_id=?", (operation_id,))
        return operation

    def append_event(self, operation_id: str, event: dict) -> dict:
        with self.connection(write=True) as db:
            row = db.execute("SELECT body FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                fail("OPERATION_NOT_FOUND")
            operation = parse_json(row[0])
            result = self._event(db, operation, event)
            operation["updated_at"] = now()
            db.execute("UPDATE operations SET body=? WHERE operation_id=?", (canonical(operation), operation_id))
        return result

    @staticmethod
    def _event(db, operation: dict, event: dict) -> dict:
        opid = operation["operation_id"]
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE operation_id=?", (opid,)).fetchone()[0]
        body = {**event, "operation_id": opid, "sequence": sequence,
                "event_id": "evt_" + uuid.uuid4().hex, "timestamp": now()}
        db.execute("INSERT INTO events VALUES (?,?,?,?)", (opid, sequence, body["event_id"], canonical(body)))
        operation["last_sequence"] = sequence
        return body

    def events(self, operation_id: str, after_sequence: int, *, limit: int = 1000) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT body FROM events WHERE operation_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                              (operation_id, after_sequence, limit)).fetchall()
        return [parse_json(row[0]) for row in rows]

    def event_page(self, operation_id: str, after_sequence: int, *, limit: int = 1000) -> dict:
        """Snapshot и страница берутся из одной read-транзакции SQLite."""
        with self.connection() as db:
            row = db.execute("SELECT body FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                fail("OPERATION_NOT_FOUND")
            rows = db.execute("SELECT body FROM events WHERE operation_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                              (operation_id, after_sequence, limit)).fetchall()
        events = [parse_json(item[0]) for item in rows]
        return {"operation_id": operation_id, "events": events, "snapshot": parse_json(row[0]),
                "next_sequence": events[-1]["sequence"] if events else after_sequence}

    def claim_for_resume(self, operation_id: str, *, owner: dict) -> bool:
        with self.connection(write=True) as db:
            row = db.execute("SELECT resource_key,body FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                fail("OPERATION_NOT_FOUND")
            operation = parse_json(row[1])
            if operation["status"] in {"RUNNING", "QUEUED"}:
                fail("OPERATION_CONFLICT")
            occupied = db.execute("SELECT operation_id FROM claims WHERE resource_key=?", (row[0],)).fetchone()
            if occupied and occupied[0] != operation_id:
                fail("OPERATION_CONFLICT")
            db.execute("INSERT OR IGNORE INTO claims VALUES (?,?)", (row[0], operation_id))
            operation.update(status="QUEUED", cancel_requested=False, updated_at=now(), owner=owner)
            self._event(db, operation, {"type": "operation.resume_queued", "status": "QUEUED"})
            db.execute("UPDATE operations SET body=? WHERE operation_id=?", (canonical(operation), operation_id))
        return True
