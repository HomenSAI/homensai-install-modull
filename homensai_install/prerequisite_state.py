"""Private durable prerequisites journal and process lock.

The claim and operation are committed together. An intent survives controller
death; a lock only prevents concurrent controllers, never proves an installer
or a child process has completed.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat

from .api.journal import owned_directory
from .api.model import bounded_json, canonical, parse_json
from .downloads import _open
from .errors import utcnow
from .prerequisite_catalog import fail, identifier


class PrerequisiteStore:
    def __init__(self, directory):
        self.directory = owned_directory(Path(directory).absolute(), create=True)
        self.path = self.directory / "prerequisites.sqlite3"
        if self.path.exists():
            self._check_files()
            with self.connection() as db:
                if db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() != ("1",):
                    fail("API_UNSUPPORTED", "prerequisite_state_schema")
            return
        with self.lock():
            if not self.path.exists():
                descriptor = _open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                os.close(descriptor)
            self._check_files()
            with self.connection(write=True) as db:
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
                if db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() != ("1",):
                    fail("API_UNSUPPORTED", "prerequisite_state_schema")
                db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS claim (id INTEGER PRIMARY KEY CHECK(id=1), operation_id TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS events (operation_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(operation_id,sequence))")
                db.execute("CREATE TABLE IF NOT EXISTS attempts (operation_id TEXT NOT NULL, component_id TEXT NOT NULL, attempt INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(operation_id,component_id,attempt))")

    def _check_files(self):
        for path in (self.path, Path(str(self.path) + "-journal"), Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or (os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                    fail("STATE_STORAGE_UNAVAILABLE", "prerequisite_journal_not_private")

    @contextmanager
    def lock(self):
        descriptor = None
        locked = False
        try:
            descriptor = _open(self.directory / "controller.lock", os.O_RDWR | os.O_CREAT)
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError:
                    fail("OPERATION_CONFLICT", "prerequisite_controller_busy")
            else:
                import fcntl
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fail("OPERATION_CONFLICT", "prerequisite_controller_busy")
            locked = True
            yield
        except OSError:
            fail("STATE_STORAGE_UNAVAILABLE", "prerequisite_lock_unavailable")
        finally:
            if descriptor is not None:
                if locked:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @contextmanager
    def connection(self, *, write=False):
        db = None
        try:
            self._check_files()
            db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.execute("COMMIT")
        except sqlite3.Error:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            fail("STATE_STORAGE_UNAVAILABLE", "prerequisite_journal_unavailable")
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            if db is not None:
                db.close()

    def get(self, operation_id):
        if not identifier(operation_id):
            fail("INVALID_REQUEST", "prerequisite_operation_id")
        with self.connection() as db:
            row = db.execute("SELECT body FROM operations WHERE id=?", (operation_id,)).fetchone()
        return parse_json(row[0], error_code="INTEGRITY_FAILED") if row else None

    def save(self, operation, message_code, *, create=False):
        operation = dict(operation)
        with self.connection(write=True) as db:
            row = db.execute("SELECT body FROM operations WHERE id=?", (operation["operation_id"],)).fetchone()
            previous = parse_json(row[0], error_code="INTEGRITY_FAILED") if row else None
            if create and previous is not None:
                fail("OPERATION_CONFLICT", "prerequisite_operation_exists")
            if not create and previous is None:
                fail("OPERATION_NOT_FOUND", "prerequisite_operation_missing")
            # A concurrent cancel is the only write allowed without controller
            # lock. Merge it monotonically; stale controller snapshots cannot
            # erase the request or reuse its event sequence.
            if previous:
                operation["cancel_requested"] = operation["cancel_requested"] or previous["cancel_requested"]
            operation["revision"] = (previous["revision"] if previous else 0) + 1
            operation["updated_at"] = utcnow()
            event = {"type": "prerequisite_state", "phase": "prerequisites", "message_code": message_code,
                     "status": operation["status"], "operation_id": operation["operation_id"],
                     "sequence": operation["revision"], "timestamp": operation["updated_at"]}
            bounded_json(operation)  # Never write a record its bounded reader cannot recover.
            row = db.execute("SELECT operation_id FROM claim WHERE id=1").fetchone()
            terminal = operation["status"] in {"READY", "CANCELED"}
            if not terminal:
                if row and row[0] != operation["operation_id"]:
                    fail("OPERATION_CONFLICT", "another_prerequisite_operation_active")
                db.execute("INSERT OR REPLACE INTO claim VALUES (1,?)", (operation["operation_id"],))
            elif row and row[0] == operation["operation_id"]:
                db.execute("DELETE FROM claim WHERE id=1")
            if create:
                db.execute("INSERT INTO operations VALUES (?,?)", (operation["operation_id"], canonical(operation)))
            else:
                row = db.execute("UPDATE operations SET body=? WHERE id=?", (canonical(operation), operation["operation_id"]))
                if row.rowcount != 1:
                    fail("OPERATION_NOT_FOUND", "prerequisite_operation_missing")
            db.execute("INSERT INTO events VALUES (?,?,?)", (operation["operation_id"], operation["revision"], canonical(event)))
            for name, step in operation["steps"].items():
                if step["receipt"] is not None and step["attempt"] > 0:
                    record = {"component_id": name, "attempt": step["attempt"], "state": step["state"], "receipt": step["receipt"]}
                    db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",
                               (operation["operation_id"], name, step["attempt"], canonical(record)))
        return operation, event

    def request_cancel(self, operation_id):
        """Persist only a cancel request while another controller owns the lock."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT body FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                fail("OPERATION_NOT_FOUND", "prerequisite_operation_missing")
            operation = parse_json(row[0], error_code="INTEGRITY_FAILED")
            if operation["status"] in {"READY", "CANCELED"} or operation["cancel_requested"]:
                return operation, None
            operation["cancel_requested"] = True
            operation["required_action"] = "wait_for_safe_boundary"
            operation["revision"] += 1
            operation["updated_at"] = utcnow()
            event = {"type": "prerequisite_state", "phase": "prerequisites", "message_code": "prerequisite_cancel_requested",
                     "status": operation["status"], "operation_id": operation_id,
                     "sequence": operation["revision"], "timestamp": operation["updated_at"]}
            db.execute("UPDATE operations SET body=? WHERE id=?", (canonical(operation), operation_id))
            db.execute("INSERT INTO events VALUES (?,?,?)", (operation_id, operation["revision"], canonical(event)))
        return operation, event

    def events(self, operation_id, *, after=0, limit=100):
        if not identifier(operation_id) or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            fail("INVALID_REQUEST", "prerequisite_events_cursor")
        with self.connection() as db:
            rows = db.execute("SELECT body FROM events WHERE operation_id=? AND sequence>? ORDER BY sequence LIMIT ?", (operation_id, after, limit)).fetchall()
        return [parse_json(row[0], error_code="INTEGRITY_FAILED") for row in rows]

    def attempts(self, operation_id, component_id, *, after=0, limit=100):
        if (not identifier(operation_id) or not identifier(component_id) or type(after) is not int or after < 0
                or type(limit) is not int or not 1 <= limit <= 1000):
            fail("INVALID_REQUEST", "prerequisite_attempt_cursor")
        with self.connection() as db:
            rows = db.execute("SELECT body FROM attempts WHERE operation_id=? AND component_id=? AND attempt>? ORDER BY attempt LIMIT ?",
                              (operation_id, component_id, after, limit)).fetchall()
        return [parse_json(row[0], error_code="INTEGRITY_FAILED") for row in rows]
