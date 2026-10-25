"""Ограниченный staging явно выбранных файлов мастера, без исполнения.

Идентификаторы относятся только к этой доверенной HTTP-сессии. Сохранённые
планы SDK удерживают проверенные файлы независимо от времени жизни браузера.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import threading
import uuid

from .api.journal import owned_directory
from .errors import InstallerError
from shared.datahome import checked_path

MAX_UPLOAD_BYTES = 4 * 1024**3
MAX_UPLOADS = 16
CHUNK_BYTES = 128 * 1024
_UPLOAD_ID = re.compile(r"^upload_[0-9a-f]{32}$")


def _fail(code="INVALID_REQUEST", reason="local_upload_invalid"):
    raise InstallerError(code, details={"reason": reason})


class LocalArtifactUploads:
    """Session quota, cancel fencing and immutable, owner-private staged bytes."""

    def __init__(self, state_dir):
        self.parent = Path(state_dir)
        self.directory = self.parent / "wizard-uploads" / uuid.uuid4().hex
        self._records = {}
        self._lock = threading.RLock()
        self._directory_identity = None

    def _directory(self):
        owned_directory(self.parent, create=True)
        owned_directory(self.parent / "wizard-uploads", create=True)
        result = owned_directory(self.directory, create=self._directory_identity is None)
        info = result.lstat()
        identity = (info.st_dev, info.st_ino)
        if self._directory_identity is not None and identity != self._directory_identity:
            _fail("INTEGRITY_FAILED", "local_upload_directory_changed")
        self._directory_identity = identity
        return result

    @staticmethod
    def _public(record):
        return {key: record[key] for key in (
            "upload_id", "component_id", "artifact_id", "size", "sha256", "state", "verified")}

    def _record(self, upload_id):
        if type(upload_id) is not str or not _UPLOAD_ID.fullmatch(upload_id):
            _fail()
        record = self._records.get(upload_id)
        if record is None:
            _fail("OPERATION_NOT_FOUND", "local_upload_not_found")
        return record

    def create(self, component_id, artifact, *, binding):
        size, sha256 = artifact.get("size"), artifact.get("sha256")
        if (type(size) is not int or not 0 < size <= MAX_UPLOAD_BYTES
                or type(sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
            _fail("INVALID_REQUEST", "local_upload_limit")
        with self._lock:
            # Finished/canceled records are retained to make late requests safe.
            # A new HTTP session obtains a fresh bounded set of identifiers.
            if len(self._records) >= MAX_UPLOADS or sum(
                    item["size"] for item in self._records.values()
                    if item["charged"]) + size > MAX_UPLOAD_BYTES:
                _fail("INVALID_REQUEST", "local_upload_limit")
            self._directory()
            upload_id = "upload_" + uuid.uuid4().hex
            record = {"upload_id": upload_id, "component_id": component_id,
                      "artifact_id": artifact["artifact_id"], "size": size, "sha256": sha256,
                      "state": "pending", "verified": False, "binding": binding,
                      "cancel": threading.Event(), "bound": False, "reservations": 0,
                      "charged": True, "receiving": False}
            self._records[upload_id] = record
            return self._public(record)

    def status(self, upload_id):
        with self._lock:
            return self._public(self._record(upload_id))

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _validate_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            _fail("INTEGRITY_FAILED", "local_upload_unsafe_file")
        if os.name != "nt" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            _fail("PERMISSION_REQUIRED", "local_upload_private_file_required")

    def _remove(self, record):
        path, identity = record.get("path"), record.get("file_identity")
        if path is None:
            record["charged"] = False  # Allocation never created a staged file.
            return
        self._directory()
        try:
            info = path.lstat()
        except FileNotFoundError:
            # A previously created inode may have been moved: no proof of
            # deletion, so conservatively retain its byte reservation.
            return
        self._validate_file(info)
        if self._identity(info) != identity:
            _fail("INTEGRITY_FAILED", "local_upload_file_changed")
        path.unlink()
        record["charged"] = False

    def cancel(self, upload_id):
        with self._lock:
            record = self._record(upload_id)
            if record["bound"] or record["reservations"]:
                _fail("OPERATION_CONFLICT", "local_upload_bound_to_plan")
            # Repeated cancel keeps the writer fence even after public state
            # changed to canceled; an open/unlinked file still consumes bytes.
            active = record["receiving"]
            record["cancel"].set()
            record.update(state="canceled", verified=False)
            if not active:
                self._remove(record)
            return self._public(record)

    def _verify(self, record):
        descriptor = None
        try:
            self._directory()
            path = checked_path(record["path"])
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            before = os.fstat(descriptor)
            self._validate_file(before)
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                if self._identity(before) != record["file_identity"] or before.st_size != record["size"]:
                    _fail("INTEGRITY_FAILED", "local_upload_file_changed")
                observed = hashlib.sha256()
                count = 0
                while chunk := source.read(CHUNK_BYTES):
                    count += len(chunk)
                    if count > record["size"]:
                        _fail("INTEGRITY_FAILED", "local_upload_file_changed")
                    observed.update(chunk)
                after = os.fstat(source.fileno())
                named = path.lstat()
                self._validate_file(named)
                if (self._identity(named) != record["file_identity"] or after.st_size != before.st_size
                        or after.st_mtime_ns != before.st_mtime_ns or count != record["size"]
                        or observed.hexdigest() != record["sha256"]):
                    _fail("INTEGRITY_FAILED", "local_upload_hash")
            return str(path)
        except (OSError, ValueError):
            _fail("INTEGRITY_FAILED", "local_upload_file_changed")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def receive(self, upload_id, stream, length, *, binding):
        with self._lock:
            record = self._record(upload_id)
            if record["binding"] != binding:
                _fail("PLAN_STALE", "local_upload_binding")
            if type(length) is not int or length != record["size"]:
                _fail("INVALID_REQUEST", "local_upload_size")
            if record["state"] != "pending" or record["cancel"].is_set():
                _fail("OPERATION_CONFLICT", "local_upload_already_submitted")
            directory = self._directory()
            path = directory / (upload_id + ".artifact")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
            try:
                info = os.fstat(descriptor)
            except OSError:
                # The file was created even if its identity could not be read.
                # Keep quota and fail closed rather than treating it as absent.
                record.update(state="failed", path=path, file_identity=None)
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                _fail("STATE_STORAGE_UNAVAILABLE", "local_upload_io")
            record.update(state="transferring", path=path, file_identity=self._identity(info), receiving=True)
        succeeded = False
        try:
            with os.fdopen(descriptor, "wb") as output:
                self._validate_file(info)
                remaining, observed = length, hashlib.sha256()
                while remaining:
                    if record["cancel"].is_set():
                        _fail("OPERATION_CONFLICT", "local_upload_canceled")
                    chunk = stream.read(min(CHUNK_BYTES, remaining))
                    if not chunk or type(chunk) is not bytes or len(chunk) > remaining:
                        _fail("INTEGRITY_FAILED", "local_upload_incomplete")
                    output.write(chunk)
                    observed.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
                if observed.hexdigest() != record["sha256"]:
                    _fail("INTEGRITY_FAILED", "local_upload_hash")
            self._verify(record)
            with self._lock:
                if record["cancel"].is_set():
                    _fail("OPERATION_CONFLICT", "local_upload_canceled")
                record.update(state="verified", verified=True, receiving=False)
                succeeded = True
                return self._public(record)
        except (OSError, ValueError):
            _fail("STATE_STORAGE_UNAVAILABLE", "local_upload_io")
        finally:
            if not succeeded:
                primary = sys.exc_info()[0] is not None
                with self._lock:
                    record.update(state="canceled" if record["cancel"].is_set() else "failed", verified=False,
                                  receiving=False)
                    try:
                        self._remove(record)
                    except (InstallerError, OSError, ValueError):
                        if not primary:
                            raise

    def _paths(self, uploads, *, binding):
        if type(uploads) is not dict or len(uploads) > MAX_UPLOADS:
            _fail()
        result = {}
        for artifact_id, upload_id in uploads.items():
            record = self._record(upload_id)
            if (record["artifact_id"] != artifact_id or record["binding"] != binding
                    or record["state"] != "verified" or record["verified"] is not True
                    or record["cancel"].is_set()):
                _fail("INTEGRITY_FAILED", "local_upload_binding")
            result[artifact_id] = self._verify(record)
        return result

    @contextmanager
    def selection(self, uploads, *, binding):
        """Temporary pin while SDK plans; failed/stale plans release only it."""
        with self._lock:
            paths = self._paths(uploads, binding=binding)
            records = [self._record(upload_id) for upload_id in uploads.values()]
            for record in records:
                record["reservations"] += 1
        try:
            yield paths
        finally:
            with self._lock:
                for record in records:
                    record["reservations"] -= 1

    def paths(self, uploads, *, binding):
        """Trusted direct retention; transport uses transactional selection."""
        with self.selection(uploads, binding=binding) as paths:
            self.retain(uploads)
            return paths

    def retain(self, uploads):
        # Call after a successful SDK plan: cancel must not delete bytes retained
        # by a review or durable operation, including after browser recreation.
        with self._lock:
            for upload_id in uploads.values():
                self._record(upload_id)["bound"] = True
