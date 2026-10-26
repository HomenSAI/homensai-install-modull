"""Локальные receipts реальных encrypted recovery archives.

Путь архива хранится только в private store. Digest ID подтверждает неизменность
body, а право на новую миграцию требует точного expected binding и повторной
расшифровки/распаковки. Store не наблюдает schema/writers самостоятельно:
эти факты обязан установить вызывающий lifecycle engine под своей блокировкой.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from shared import archive_crypto
from shared.datahome import checked_path, DataHomeError
from shared.update_plan import _atomic_write

from .api.journal import owned_directory
from .api.model import canonical, checked_product_id, digest, parse_json
from .errors import InstallerError

_HASH = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_BINDING = {"product_id", "installation_id", "operation_id", "from_release",
            "from_manifest_sha256", "from_inventory_sha256", "from_schema_version",
            "plan_binding_sha256"}
_CONSISTENCY = "owned_components_stopped_and_adapter_quiesced"
_VERIFIED = "encrypted_archive_and_recovery_payload_restored"


def _fail(reason):
    raise InstallerError("RECOVERY_REQUIRED", phase="product_adapter", details={"reason": reason})


def _binding(value):
    if type(value) is not dict or set(value) != _BINDING:
        _fail("backup_receipt_binding_shape")
    checked_product_id(value["product_id"])
    for name in ("installation_id", "operation_id", "from_release"):
        if type(value[name]) is not str or not _NAME.fullmatch(value[name]):
            _fail("backup_receipt_binding_identity")
    for name in ("from_manifest_sha256", "from_inventory_sha256"):
        if type(value[name]) is not str or not _HASH.fullmatch(value[name]):
            _fail("backup_receipt_binding_digest")
    if (value["from_schema_version"] is not None and
            (type(value["from_schema_version"]) is not str or not _NAME.fullmatch(value["from_schema_version"]))):
        _fail("backup_receipt_schema_version")
    if (value["plan_binding_sha256"] is not None and
            (type(value["plan_binding_sha256"]) is not str or not _HASH.fullmatch(value["plan_binding_sha256"]))):
        _fail("backup_receipt_plan_digest")
    return deepcopy(value)


def _regular(path, *, private=False):
    path = checked_path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400
            or (os.name != "nt" and (info.st_uid != os.getuid()
                or info.st_mode & (0o077 if private else 0o022)))):
        _fail("backup_receipt_unsafe_file")
    return path, info


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


@contextmanager
def _reader(path, *, private=False):
    path, before = _regular(path, private=private)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or _signature(info) != _signature(before)
                or (os.name != "nt" and (info.st_uid != os.getuid()
                    or info.st_mode & (0o077 if private else 0o022)))):
            _fail("backup_archive_replaced")
        yield stream, info
        after = os.fstat(stream.fileno())
        _, current = _regular(path, private=private)
        if _signature(after) != _signature(before) or _signature(current) != _signature(before):
            _fail("backup_archive_changed")


def _file_digest(path):
    with _reader(path) as (stream, _):
        return hashlib.file_digest(stream, "sha256").hexdigest()


class BackupReceiptStore:
    """Trusted local store; recovery_verifier checks restored signed payload.

    Callback candidate is a freshly restored archive root, with roots/<name>.
    A recovery-only receipt (unknown schema/review) never authorizes migration.
    """

    def __init__(self, directory, *, password_provider, recovery_verifier):
        if not callable(password_provider) or not callable(recovery_verifier):
            _fail("backup_receipt_trusted_verifiers_required")
        self.directory = Path(directory).absolute()
        self.password_provider = password_provider
        self.recovery_verifier = recovery_verifier

    def _directory(self, *, create):
        return owned_directory(self.directory, create=create)

    def _verified(self, archive, binding, expected_sha256=None):
        archive, _ = _regular(Path(archive).absolute())
        initial = _file_digest(archive)
        if expected_sha256 is not None and initial != expected_sha256:
            _fail("backup_archive_digest_mismatch")
        directory = self._directory(create=False)
        # Verify a private snapshot of the expected ciphertext, so path changes
        # during extraction cannot redirect crypto to a different archive.
        with tempfile.TemporaryDirectory(prefix="verify-", dir=directory) as name:
            temporary = Path(name)
            snapshot = temporary / "archive.hsac"
            fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as destination, _reader(archive) as (source, captured):
                remaining = captured.st_size
                while remaining:
                    block = source.read(min(remaining, 1024 * 1024))
                    if not block:
                        _fail("backup_archive_changed_during_snapshot")
                    destination.write(block)
                    remaining -= len(block)
                if source.read(1):
                    _fail("backup_archive_changed_during_snapshot")
            if _file_digest(snapshot) != initial or not archive_crypto.is_encrypted(snapshot):
                _fail("backup_archive_not_expected_ciphertext")
            candidate = temporary / "restored"
            candidate.mkdir(mode=0o700)
            manifest = archive_crypto.restore_archive(snapshot, candidate,
                password_provider=self.password_provider, expected_product_id=binding["product_id"])
            if self.recovery_verifier(candidate, deepcopy(binding)) is not True:
                _fail("backup_recovery_payload_not_verified")
            if _file_digest(archive) != initial:
                _fail("backup_archive_changed_during_verification")
            return {"archive_sha256": initial, "archive_manifest_sha256": digest(manifest)}

    def issue(self, archive_path, binding, *, consistency_scope):
        try:
            binding = _binding(binding)
            if consistency_scope != _CONSISTENCY:
                _fail("backup_consistency_not_established")
            directory = self._directory(create=True)
            verified = self._verified(archive_path, binding)
            eligible = binding["from_schema_version"] is not None and binding["plan_binding_sha256"] is not None
            body = {"schema_version": 1, "kind": "encrypted_backup", **binding, **verified,
                "encrypted": True, "includes_recovery_payload": True, "migration_eligible": eligible,
                "verification_scope": _VERIFIED if eligible else "recovery_only",
                "consistency_scope": consistency_scope}
            receipt_id = digest(body)
            record = {"receipt_id": receipt_id, "body": body,
                      "archive_path": str(checked_path(Path(archive_path).absolute()))}
            path = directory / (receipt_id + ".json")
            if path.exists():
                if self._read(receipt_id) != record:
                    _fail("backup_receipt_id_conflict")
            else:
                _atomic_write(path, record)
                if os.name != "nt": path.chmod(0o600)
            return {"receipt_id": receipt_id, **deepcopy(body)}
        except InstallerError:
            raise
        except Exception:
            _fail("backup_receipt_issuance_failed")

    def _read(self, receipt_id):
        if type(receipt_id) is not str or not _HASH.fullmatch(receipt_id):
            _fail("backup_receipt_id_invalid")
        directory = self._directory(create=False)
        path = directory / (receipt_id + ".json")
        with _reader(path, private=True) as (stream, info):
            if info.st_size > 64 * 1024:
                _fail("backup_receipt_too_large")
            raw = stream.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                _fail("backup_receipt_too_large")
        record = parse_json(raw)
        if (type(record) is not dict or set(record) != {"receipt_id", "body", "archive_path"}
                or record["receipt_id"] != receipt_id or type(record["body"]) is not dict
                or digest(record["body"]) != receipt_id):
            _fail("backup_receipt_digest_mismatch")
        return record

    def verify(self, receipt_id, *, expected_binding):
        try:
            expected = _binding(expected_binding)
            if expected["from_schema_version"] is None or expected["plan_binding_sha256"] is None:
                _fail("backup_receipt_review_or_schema_unknown")
            record = self._read(receipt_id)
            body = record["body"]
            required = {"schema_version", "kind", *_BINDING, "archive_sha256", "archive_manifest_sha256",
                        "encrypted", "includes_recovery_payload", "migration_eligible",
                        "verification_scope", "consistency_scope"}
            if (set(body) != required or type(body["schema_version"]) is not int or body["schema_version"] != 1
                    or body["kind"] != "encrypted_backup" or body["encrypted"] is not True
                    or body["includes_recovery_payload"] is not True or body["migration_eligible"] is not True
                    or body["verification_scope"] != _VERIFIED or body["consistency_scope"] != _CONSISTENCY
                    or any(body[name] != expected[name] for name in _BINDING)):
                _fail("backup_receipt_binding_mismatch")
            verified = self._verified(record["archive_path"], expected, body["archive_sha256"])
            if verified["archive_manifest_sha256"] != body["archive_manifest_sha256"]:
                _fail("backup_receipt_manifest_mismatch")
            return {"receipt_id": receipt_id, **deepcopy(body)}
        except InstallerError:
            raise
        except Exception:
            _fail("backup_receipt_verification_failed")


class MigrationReviewStore:
    """Неизменяемые локальные планы, полученные явным preparation hook."""

    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    def issue(self, body):
        directory = owned_directory(self.directory, create=True)
        review_id = digest(body)
        path = directory / (review_id + ".json")
        if path.exists():
            if self.read(review_id) != body:
                _fail("migration_review_id_conflict")
        else:
            _atomic_write(path, deepcopy(body))
            if os.name != "nt": path.chmod(0o600)
        return review_id

    def read(self, review_id):
        if type(review_id) is not str or not _HASH.fullmatch(review_id):
            _fail("migration_review_id_invalid")
        directory = owned_directory(self.directory, create=False)
        with _reader(directory / (review_id + ".json"), private=True) as (stream, info):
            if info.st_size > 256 * 1024:
                _fail("migration_review_too_large")
            raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                _fail("migration_review_too_large")
        body = parse_json(raw)
        if type(body) is not dict or digest(body) != review_id:
            _fail("migration_review_digest_mismatch")
        return body
