"""Trusted execution domain shared by product and prerequisite controllers.

The OS lock bounds the lifetime of a live controller. The durable claim bounds
unfinished intent after its death. Losing a PID, an OS lock, or a timeout never
clears that intent. Only an explicit exact retry/resume can acquire it again.
This is an internal bootstrap port: HTTP/options cannot select a domain.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from .api.journal import owned_directory
from .api.model import canonical, parse_json
from .errors import InstallerError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_DESCRIPTORS = set()
_MAX_PARTICIPANTS = 256


def _fail(code, reason):
    raise InstallerError(code, details={"reason": reason})


def _after_fork():
    # close(), not LOCK_UN: a fork shares its open-file description with the
    # parent. An external child must neither retain nor release its lease.
    for descriptor in tuple(_DESCRIPTORS):
        try:
            os.close(descriptor)
        except OSError:
            pass
    _DESCRIPTORS.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class _UnlinkedPrivateFile(InstallerError):
    """Strict rejection; only the SQLite sidecar checker may re-observe it."""


def _private_file(path, flags):
    descriptor = None
    try:
        descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode)
                or (os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_file_not_private")
        if info.st_nlink != 1:
            if info.st_nlink == 0:
                raise _UnlinkedPrivateFile("STATE_STORAGE_UNAVAILABLE",
                    details={"reason": "execution_domain_file_not_private"})
            _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_file_not_private")
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _check_sqlite_files(path, *, allow_missing_main=False):
    """Check opened private files; commit may remove a transient sidecar.

    The owner's OS lease serializes writers, but read-only pending/busy probes
    deliberately do not acquire it. ENOENT on a SQLite sidecar is therefore
    normal even between its observation and open; other errors stay fatal.
    """
    for candidate in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        for observation in range(4):
            try:
                descriptor = _private_file(candidate, os.O_RDONLY)
            except FileNotFoundError:
                if candidate != path or allow_missing_main:
                    break
                raise
            except _UnlinkedPrivateFile:
                # _private_file stays strict for all callers and closes its FD.
                # Only this known sidecar may be re-opened and fully rechecked.
                if candidate == path or observation == 3:
                    raise
                time.sleep(0.01)  # Bounded opportunity (at most 30 ms) for commit unlink.
                continue
            os.close(descriptor)
            break


def _lock(descriptor, *, initialize=False):
    if os.name == "nt":
        import msvcrt
        if os.fstat(descriptor).st_size == 0:
            if not initialize:
                _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_lock_empty")
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            _fail("OPERATION_CONFLICT", "execution_domain_busy")
    else:
        import fcntl
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("OPERATION_CONFLICT", "execution_domain_busy")


def _unlock_close(descriptor):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        _DESCRIPTORS.discard(descriptor)
        os.close(descriptor)


def _sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ExecutionDomain:
    """Lazy private domain rooted in trusted original-owner bootstrap state.

    All participating APIs must receive the same directory/owner. Different
    arbitrary bootstrap roots and legacy direct engine calls are outside this
    guarantee. A coordinator's default root is its already stable shared owner
    state directory, independent of catalogue, target and product API state.
    """
    def __init__(self, directory, *, owner_id):
        if type(owner_id) is not str or _ID.fullmatch(owner_id) is None:
            _fail("INVALID_REQUEST", "execution_domain_owner")
        self.directory = Path(directory).absolute()
        self.owner_id = owner_id
        self.path = self.directory / "execution.sqlite3"
        self.lock_path = self.directory / "execution.lock"
        self._participants = set()

    def register_participant(self, kind, directory):
        """Trusted bootstrap registration, lazy until a mutating reserve.

        Registration never discovers arbitrary folders. Once observed during
        reserve, a journal cannot disappear or be replaced to bypass recovery.
        """
        if kind not in {"product", "prerequisite"}:
            _fail("INVALID_REQUEST", "execution_domain_participant_kind")
        participant = kind, os.path.normcase(os.path.abspath(directory))
        if participant not in self._participants and len(self._participants) >= _MAX_PARTICIPANTS:
            _fail("INVALID_REQUEST", "execution_domain_participant_limit")
        self._participants.add(participant)

    def same_domain(self, other):
        return (isinstance(other, ExecutionDomain) and self.owner_id == other.owner_id
                and os.path.normcase(str(self.directory)) == os.path.normcase(str(other.directory)))

    def _check_files(self, *, initialize=False):
        _check_sqlite_files(self.path, allow_missing_main=initialize)

    @contextmanager
    def _db(self, *, initialize=False, write=False):
        db = None
        try:
            self._check_files(initialize=initialize)
            if initialize and not self.path.exists():
                descriptor = _private_file(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.close(descriptor)
            # mode=rw forbids recreating a removed authoritative file.
            db = sqlite3.connect(self.path.as_uri() + "?mode=" + ("rw" if write else "ro"),
                                 uri=True, timeout=5, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if initialize:
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS claim (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
                db.execute("INSERT OR IGNORE INTO meta VALUES ('owner_id',?)", (self.owner_id,))
            if (db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() != ("1",)
                    or db.execute("SELECT value FROM meta WHERE key='owner_id'").fetchone() != (self.owner_id,)):
                _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_identity_mismatch")
            yield db
            db.execute("COMMIT")
        except (OSError, sqlite3.Error):
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_storage_unavailable")
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _claim(db):
        row = db.execute("SELECT body FROM claim WHERE id=1").fetchone()
        if row is None:
            return None
        value = parse_json(row[0], error_code="STATE_STORAGE_UNAVAILABLE")
        if (type(value) is not dict or set(value) != {"kind", "operation_id", "binding", "request_digest",
                                                    "token", "phase", "uncertain"}
                or value["kind"] not in {"product", "prerequisite"}
                or type(value["operation_id"]) is not str or _ID.fullmatch(value["operation_id"]) is None
                or any(type(value[field]) is not str or _HASH.fullmatch(value[field]) is None
                       for field in ("binding", "request_digest"))
                or type(value["token"]) is not str or re.fullmatch(r"[0-9a-f]{32}", value["token"]) is None
                or value["phase"] not in {"RESERVED", "QUEUED", "ACTIVE"}
                or type(value["uncertain"]) is not bool):
            _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_claim_invalid")
        return value

    @staticmethod
    def _registry(db):
        row = db.execute("SELECT value FROM meta WHERE key='participants'").fetchone()
        result = parse_json(row[0], error_code="STATE_STORAGE_UNAVAILABLE") if row else []
        if type(result) is not list or len(result) > _MAX_PARTICIPANTS:
            _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_registry_invalid")
        seen = set()
        for item in result:
            if (type(item) is not dict or set(item) != {"kind", "directory", "identity"}
                    or item["kind"] not in {"product", "prerequisite"}
                    or type(item["directory"]) is not str or len(item["directory"]) > 8192
                    or not Path(item["directory"]).is_absolute()
                    or (item["identity"] is not None and (type(item["identity"]) is not list
                        or len(item["identity"]) != 2 or any(type(value) is not int or value < 0 for value in item["identity"])))):
                _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_registry_invalid")
            key = item["kind"], item["directory"]
            if key in seen:
                _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_registry_duplicate")
            seen.add(key)
        return result

    def _merge_registry(self, db):
        records = {(item["kind"], item["directory"]): item for item in self._registry(db)}
        for kind, directory in self._participants:
            records.setdefault((kind, directory), {"kind": kind, "directory": directory, "identity": None})
        if len(records) > _MAX_PARTICIPANTS:
            _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_registry_limit")
        return [records[key] for key in sorted(records)]

    @staticmethod
    def _observe_registry(records):
        observed = []
        for record in records:
            item = dict(record)
            directory = Path(item["directory"])
            path = directory / ("operations.sqlite3" if item["kind"] == "product" else "prerequisites.sqlite3")
            if not directory.exists() and not directory.is_symlink():
                if item["identity"] is not None:
                    _fail("STATE_STORAGE_UNAVAILABLE", "registered_journal_missing")
                observed.append(item)
                continue
            try:
                owned_directory(directory, create=False)
            except FileNotFoundError:
                _fail("STATE_STORAGE_UNAVAILABLE", "registered_journal_directory_unavailable")
            if not path.exists() and not path.is_symlink():
                if item["identity"] is not None:
                    _fail("STATE_STORAGE_UNAVAILABLE", "registered_journal_missing")
                observed.append(item)
                continue
            descriptor = _private_file(path, os.O_RDONLY)
            try:
                info = os.fstat(descriptor)
                identity = [info.st_dev, info.st_ino]
            finally:
                os.close(descriptor)
            if item["identity"] is not None and item["identity"] != identity:
                _fail("STATE_STORAGE_UNAVAILABLE", "registered_journal_replaced")
            item["identity"] = identity
            observed.append(item)
        return observed

    @staticmethod
    def _guard_registry(records, *, kind=None, participant=None, operation_id=None, resume=False):
        """Probe known old journals outside the domain SQLite transaction.

        Old binaries that start new work without this protocol are outside the
        guarantee. Existing durable intent is nevertheless never ignored just
        because it predates the common lease.
        """
        for item in records:
            if item["identity"] is None:
                continue
            directory = Path(item["directory"])
            path = directory / ("operations.sqlite3" if item["kind"] == "product" else "prerequisites.sqlite3")
            excluded = (operation_id if resume and item["kind"] == kind
                        and item["directory"] == participant else "")
            db, descriptor, locked = None, None, False
            try:
                # Revalidate immediately before and after the SQL observation.
                # A normal restore/move between registry scan and this open
                # cannot replace an unresolved journal with an empty database.
                ExecutionDomain._observe_registry([item])
                # A concurrent cooperative cancel may commit this participant
                # journal without owning the common lease; its sidecars are
                # transient just as for read-only common-domain probes.
                _check_sqlite_files(path)
                db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
                db.execute("BEGIN")
                if db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() != ("1",):
                    _fail("STATE_STORAGE_UNAVAILABLE", "registered_journal_schema")
                if item["kind"] == "product":
                    invalid = db.execute("""SELECT 1 FROM operations WHERE
                        json_type(body) IS NOT 'object'
                        OR json_type(body,'$.status') IS NOT 'text'
                        OR json_extract(body,'$.status') NOT IN ('QUEUED','RUNNING','SUCCEEDED','FAILED',
                            'FAILED_ROLLED_BACK','CANCELED','RECOVERY_REQUIRED','AWAITING_USER','REBOOT_REQUIRED','RELOGIN_REQUIRED')
                        OR COALESCE(json_extract(body,'$.operation_id'),'')<>operation_id
                        OR json_type(body,'$.attempt') IS NOT 'integer'
                        OR json_extract(body,'$.attempt')<0
                        OR COALESCE(json_type(body,'$.result'),'null') NOT IN ('null','object')
                        OR COALESCE(json_type(body,'$.result.resume_required'),'false') NOT IN ('true','false')
                        OR COALESCE(json_type(body,'$.result.execution_quiescent'),'false') NOT IN ('true','false')
                        LIMIT 1""").fetchone()
                    if invalid:
                        _fail("STATE_STORAGE_UNAVAILABLE", "registered_product_record_invalid")
                    claimed = db.execute("SELECT 1 FROM claims WHERE operation_id<>? LIMIT 1", (excluded,)).fetchone()
                    pending = db.execute("""SELECT 1 FROM operations WHERE operation_id<>? AND (
                        json_extract(body,'$.status') NOT IN ('SUCCEEDED','FAILED_ROLLED_BACK','CANCELED','FAILED')
                        OR json_extract(body,'$.result.resume_required')=1
                        OR (json_extract(body,'$.status') IN ('CANCELED','FAILED')
                            AND COALESCE(json_extract(body,'$.attempt'),1)>0
                            AND COALESCE(json_extract(body,'$.result.execution_quiescent'),0)<>1)) LIMIT 1""",
                        (excluded,)).fetchone()
                else:
                    invalid = db.execute("""SELECT 1 FROM operations WHERE
                        json_type(body) IS NOT 'object'
                        OR json_type(body,'$.status') IS NOT 'text'
                        OR json_extract(body,'$.status') NOT IN ('READY','CANCELED','RUNNING','AWAITING_USER',
                            'RECOVERY_REQUIRED','REBOOT_REQUIRED','RELOGIN_REQUIRED')
                        OR COALESCE(json_extract(body,'$.operation_id'),'')<>id
                        OR json_type(body,'$.steps') IS NOT 'object'
                        OR EXISTS (SELECT 1 FROM json_each(body,'$.steps') AS step WHERE
                            step.type<>'object' OR json_type(step.value,'$.state') IS NOT 'text'
                            OR json_extract(step.value,'$.state') NOT IN ('SKIPPED','READY','PENDING','DOWNLOADING',
                                'NOT_STARTED','INSTALLING','VERIFYING','WAITING_REBOOT','WAITING_RELOGIN','UNKNOWN','FAILED'))
                        LIMIT 1""").fetchone()
                    if invalid:
                        _fail("STATE_STORAGE_UNAVAILABLE", "registered_prerequisite_record_invalid")
                    claimed = db.execute("SELECT 1 FROM claim WHERE operation_id<>? LIMIT 1", (excluded,)).fetchone()
                    pending = db.execute("""SELECT 1 FROM operations WHERE id<>? AND (
                        json_extract(body,'$.status') NOT IN ('READY','CANCELED')
                        OR EXISTS (SELECT 1 FROM json_each(body,'$.steps') AS step
                                   WHERE json_extract(step.value,'$.state') IN
                                   ('INSTALLING','UNKNOWN','WAITING_REBOOT','WAITING_RELOGIN'))) LIMIT 1""",
                        (excluded,)).fetchone()
                db.execute("COMMIT")
                ExecutionDomain._observe_registry([item])
                if claimed or pending:
                    _fail("OPERATION_CONFLICT", "registered_journal_unfinished")
                if item["kind"] == "prerequisite":
                    # Includes a pre-lease controller still running its final
                    # observer after a terminal journal commit.
                    descriptor = _private_file(directory / "controller.lock", os.O_RDWR)
                    _DESCRIPTORS.add(descriptor)
                    _lock(descriptor)
                    locked = True
            except (OSError, sqlite3.Error):
                _fail("STATE_STORAGE_UNAVAILABLE", "registered_journal_unavailable")
            finally:
                if db is not None:
                    db.close()
                if descriptor is not None:
                    if locked:
                        _unlock_close(descriptor)
                    else:
                        _DESCRIPTORS.discard(descriptor)
                        os.close(descriptor)

    def reserve(self, kind, operation_id, binding, *, request_digest=None, resume=False, participant=None):
        """Acquire before journal publication; transfer this handle to worker.

        Exact orphan RESERVED retries reuse its original operation ID. Existing
        QUEUED/ACTIVE intent requires explicit resume and the exact operation.
        New fencing token is committed before the recovered controller acts.
        """
        request_digest = binding if request_digest is None else request_digest
        if (kind not in {"product", "prerequisite"} or type(resume) is not bool
                or type(operation_id) is not str or _ID.fullmatch(operation_id) is None
                or any(type(value) is not str or _HASH.fullmatch(value) is None for value in (binding, request_digest))):
            _fail("INVALID_REQUEST", "execution_domain_binding")
        if participant is not None:
            participant = os.path.normcase(os.path.abspath(participant))
            self.register_participant(kind, participant)
        descriptor, locked = None, False
        try:
            missing_directories = []
            parent = self.directory
            while not parent.exists():
                missing_directories.append(parent)
                parent = parent.parent
            owned_directory(self.directory, create=True)
            for parent in reversed(missing_directories):
                _sync_directory(parent.parent)
            if self.path.exists() and not self.lock_path.exists():
                _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_lock_missing")
            created = False
            try:
                descriptor = _private_file(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                created = True
            except FileExistsError:
                descriptor = _private_file(self.lock_path, os.O_RDWR)
            _DESCRIPTORS.add(descriptor)
            _lock(descriptor, initialize=created)
            locked = True
            if not self.path.exists() and not created:
                _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_journal_missing")
            with self._db(initialize=created, write=True) as db:
                old = self._claim(db)
                if old is not None:
                    same = old["kind"] == kind and old["binding"] == binding
                    if not same or (resume and old["operation_id"] != operation_id):
                        _fail("OPERATION_CONFLICT", "execution_domain_unfinished")
                    if not resume and (old["phase"] != "RESERVED" or old["request_digest"] != request_digest):
                        _fail("OPERATION_CONFLICT", "execution_domain_resume_required")
                    operation_id = old["operation_id"]
                claim = {"kind": kind, "operation_id": operation_id, "binding": binding,
                         "request_digest": request_digest, "token": uuid.uuid4().hex, "phase": "RESERVED",
                         "uncertain": bool(old and (old["uncertain"] or old["phase"] == "ACTIVE"))}
                db.execute("INSERT OR REPLACE INTO claim VALUES (1,?)", (canonical(claim),))
                records = self._merge_registry(db)
                db.execute("INSERT OR REPLACE INTO meta VALUES ('participants',?)", (canonical(records),))
            if created:
                os.fsync(descriptor)
                _sync_directory(self.directory)
            lease = ExecutionLease(self, descriptor, claim)
            try:
                observed = self._observe_registry(records)
                if observed != records:
                    with self._db(write=True) as db:
                        lease._current(db)
                        db.execute("UPDATE meta SET value=? WHERE key='participants'", (canonical(observed),))
                self._guard_registry(observed, kind=kind, participant=participant,
                                     operation_id=operation_id, resume=resume)
            except BaseException:
                try:
                    lease.close(release=old is None or (old["phase"] == "RESERVED" and not old["uncertain"]))
                finally:
                    descriptor = None
                raise
            return lease
        except BaseException as error:
            if descriptor is not None:
                if locked:
                    _unlock_close(descriptor)
                else:
                    _DESCRIPTORS.discard(descriptor)
                    os.close(descriptor)
            if isinstance(error, OSError):
                _fail("STATE_STORAGE_UNAVAILABLE", "execution_domain_lock_unavailable")
            raise

    def busy(self):
        """Read-only True/False/None. No path, token or foreign ID is exposed."""
        descriptor, locked = None, False
        try:
            if not self.directory.exists() and not self.directory.is_symlink():
                return False
            owned_directory(self.directory, create=False)
            if not self.lock_path.exists() and not self.path.exists():
                return False
            descriptor = _private_file(self.lock_path, os.O_RDWR)
            _DESCRIPTORS.add(descriptor)
            _lock(descriptor)
            locked = True
            with self._db() as db:
                if self._claim(db) is not None:
                    return True
                records = self._registry(db)
            self._guard_registry(self._observe_registry(records))
            return False
        except InstallerError as error:
            return True if error.code == "OPERATION_CONFLICT" else None
        except OSError:
            return None
        finally:
            if descriptor is not None:
                if locked:
                    _unlock_close(descriptor)
                else:
                    _DESCRIPTORS.discard(descriptor)
                    os.close(descriptor)

    def pending(self, kind, operation_id, binding):
        """Internal exact-operation query; cannot clear or disclose a claim."""
        if not self.directory.exists() and not self.directory.is_symlink():
            return False
        owned_directory(self.directory, create=False)
        if not self.path.exists():
            return False if not self.lock_path.exists() else None
        with self._db() as db:
            claim = self._claim(db)
            return bool(claim and claim["kind"] == kind and claim["operation_id"] == operation_id
                        and claim["binding"] == binding)

    def prerequisites_busy(self):
        """Read-only prerequisite gate, distinct from all-domain ownership.

        A product's own active/recovery claim must not prevent its duplicate
        ACK or explicit resume from reaching the authoritative SDK lease check.
        Known prerequisite journals remain conservative and unfiltered.
        """
        try:
            records = []
            if self.directory.exists() or self.directory.is_symlink():
                owned_directory(self.directory, create=False)
                if self.path.exists() or self.path.is_symlink():
                    with self._db() as db:
                        claim = self._claim(db)
                        if claim and claim["kind"] == "prerequisite":
                            return True
                        records = self._registry(db)
                elif self.lock_path.exists() or self.lock_path.is_symlink():
                    return None
            known = {(item["kind"], item["directory"]): item for item in records if item["kind"] == "prerequisite"}
            for kind, directory in self._participants:
                if kind == "prerequisite":
                    known.setdefault((kind, directory), {"kind": kind, "directory": directory, "identity": None})
            self._guard_registry(self._observe_registry(list(known.values())))
            return False
        except InstallerError as error:
            return True if error.code == "OPERATION_CONFLICT" else None
        except (OSError, sqlite3.Error):
            return None


class ExecutionLease:
    """One non-inheritable live handle plus its durable fencing token."""
    def __init__(self, domain, descriptor, claim):
        self.domain, self._descriptor, self._pid = domain, descriptor, os.getpid()
        self.operation_id, self.token = claim["operation_id"], claim["token"]
        self.phase, self.uncertain = claim["phase"], claim["uncertain"]

    def _current(self, db):
        if self._descriptor is None or self._pid != os.getpid():
            _fail("OPERATION_CONFLICT", "execution_lease_not_owned")
        claim = self.domain._claim(db)
        if claim is None or claim["token"] != self.token or claim["operation_id"] != self.operation_id:
            _fail("OPERATION_CONFLICT", "execution_lease_fenced")
        return claim

    def _transition(self, phase, previous):
        with self.domain._db(write=True) as db:
            claim = self._current(db)
            if claim["phase"] not in previous:
                _fail("OPERATION_CONFLICT", "execution_lease_phase")
            claim["phase"] = phase
            db.execute("UPDATE claim SET body=? WHERE id=1", (canonical(claim),))
        self.phase = phase

    def queued(self):
        self._transition("QUEUED", {"RESERVED"})

    def active(self):
        self._transition("ACTIVE", {"QUEUED"})

    def close(self, *, release=False):
        """Release intent only on a trusted proven safe completion boundary."""
        if self._descriptor is None or self._pid != os.getpid():
            return
        try:
            if release:
                with self.domain._db(write=True) as db:
                    self._current(db)
                    db.execute("DELETE FROM claim WHERE id=1")
        finally:
            descriptor, self._descriptor = self._descriptor, None
            _unlock_close(descriptor)
