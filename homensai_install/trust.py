# HomenS.AI · © 2026 Serhii Khomenko
"""Проверка подписанного полного inventory; ключи задаёт доверенный bootstrap.

Подпись связывает payload, исходный манифест (включая argv/hooks), target,
release и runtime. Приватные ключи и операции подписи сюда не входят.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from shared.adapters_fs import _tree
from shared.datahome import checked_path

from .errors import InstallerError

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_FILES = 50000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _fail(reason: str) -> None:
    raise InstallerError("INTEGRITY_FAILED", phase="verification",
                         details={"reason": reason}, cause_status="confirmed",
                         evidence=[{"check": "signed_payload_inventory",
                                    "result": "failed", "reason": reason}])


def canonical_bytes(value: Any) -> bytes:
    """Единое представление для подписи: UTF-8, отсортированные ключи, без NaN."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _fail("invalid_canonical_value")


def hash_manifest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_metadata_key")
        value[key] = item
    return value


def _bad_constant(_):
    _fail("nonfinite_metadata_number")


def _read_json(path: str | Path, *, maximum: int = MAX_METADATA_BYTES) -> dict:
    try:
        file = checked_path(Path(path).absolute())
        if not file.is_file() or file.stat().st_size > maximum:
            _fail("metadata_unavailable_or_too_large")
        with file.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            _fail("metadata_too_large")
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                            parse_constant=_bad_constant)
        if type(result) is not dict:
            _fail("metadata_must_be_object")
        return result
    except InstallerError:
        raise
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        _fail("metadata_unavailable_or_invalid")


def _relative_file(value: Any) -> str:
    if (type(value) is not str or not value or len(value) > 4096
            or "\\" in value or ":" in value or value.startswith("/")
            or "\x00" in value):
        _fail("invalid_inventory_path")
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in parts):
        _fail("invalid_inventory_path")
    return value


class SignedPayloadVerifier:
    """Проверяющий callable для NativeEngine; никаких доверенных bool из GUI."""

    def __init__(self, public_keys: Mapping[str, bytes], *, allow_test_keys: bool = False):
        if not public_keys or len(public_keys) > 128:
            _fail("trust_root_missing")
        self._keys = {}
        for key_id, public_key in public_keys.items():
            if (type(key_id) is not str or not _KEY_ID.fullmatch(key_id)
                    or type(public_key) is not bytes or len(public_key) != 32):
                _fail("invalid_trust_root")
            self._keys[key_id] = public_key
        if type(allow_test_keys) is not bool:
            _fail("invalid_development_policy")
        self.allow_test_keys = allow_test_keys

    def verify_inventory(self, inventory: dict, signature: dict, *,
                         manifest: Mapping[str, Any], target: Mapping[str, Any],
                         release: str, runtime_executable: str) -> dict[str, str]:
        if set(signature) != {"algorithm", "key_id", "signature"}:
            _fail("invalid_signature_envelope")
        if signature.get("algorithm") != "ed25519":
            _fail("unsupported_signature_algorithm")
        key_id = signature.get("key_id")
        if type(key_id) is not str or key_id not in self._keys:
            _fail("unknown_publisher_key")
        try:
            encoded = signature["signature"]
            if type(encoded) is not str or len(encoded) > 128:
                _fail("invalid_signature")
            sig = base64.b64decode(encoded, validate=True)
            if len(sig) != 64:
                _fail("invalid_signature")
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature
            try:
                Ed25519PublicKey.from_public_bytes(self._keys[key_id]).verify(
                    sig, canonical_bytes(inventory))
            except InvalidSignature:
                _fail("invalid_signature")
        except InstallerError:
            raise
        except (ValueError, TypeError, binascii.Error):
            _fail("invalid_signature")
        except ImportError:
            raise InstallerError("PREREQUISITE_MISSING", phase="verification",
                                 details={"component": "cryptography"}) from None

        fields = {"schema_version", "product_id", "release", "target", "files",
                  "development", "manifest_sha256", "runtime_executable"}
        if set(inventory) != fields or type(inventory["schema_version"]) is not int:
            _fail("unsupported_inventory_schema")
        if inventory["schema_version"] != 1:
            _fail("unsupported_inventory_schema")
        if type(inventory["development"]) is not bool:
            _fail("invalid_development_marker")
        if inventory["development"] and not self.allow_test_keys:
            _fail("development_payload_forbidden")
        expected_target = {key: target.get(key) for key in ("os", "arch", "mode")}
        if (any(type(value) is not str or not value for value in expected_target.values())
                or inventory["target"] != expected_target):
            _fail("payload_target_mismatch")
        if (inventory["product_id"] != manifest.get("product_id")
                or inventory["release"] != release
                or inventory["manifest_sha256"] != hash_manifest(manifest)):
            _fail("payload_manifest_or_release_mismatch")
        runtime = _relative_file(runtime_executable)
        if inventory["runtime_executable"] != runtime:
            _fail("payload_runtime_mismatch")
        files = inventory["files"]
        if type(files) is not dict or not 1 <= len(files) <= MAX_FILES:
            _fail("invalid_inventory_files")
        folded = set()
        for name, digest in files.items():
            _relative_file(name)
            if name.casefold() in folded:
                _fail("case_colliding_inventory_paths")
            folded.add(name.casefold())
            if type(digest) is not str or not _SHA256.fullmatch(digest):
                _fail("invalid_inventory_digest")
        if runtime not in files:
            _fail("runtime_not_in_inventory")
        return dict(files)

    def __call__(self, manifest: dict, target: dict, options: dict) -> dict[str, str]:
        try:
            inventory = _read_json(options["inventory_file"])
            signature = _read_json(options["signature_file"], maximum=16384)
            files = self.verify_inventory(inventory, signature, manifest=manifest,
                                          target=target, release=options["release"],
                                          runtime_executable=options["runtime_executable"])
            payload = checked_path(Path(options["payload_dir"]).absolute())
            # Inventory/signature живут вне payload. Полный обход запрещает
            # ссылки и спецфайлы; добавленный неподписанный файл тоже ошибка.
            _, actual = _tree(payload)
            if actual != files:
                _fail("payload_tree_mismatch")
            return files
        except InstallerError:
            raise
        except (KeyError, OSError, TypeError, ValueError, RuntimeError):
            _fail("payload_unavailable_or_invalid")


def load_trust_config(path: str | Path, *, allow_development: bool = False) -> SignedPayloadVerifier:
    """Только публичные закреплённые ключи. Вызывается bootstrap/CLI, не HTTP body."""
    value = _read_json(path, maximum=65536)
    if (set(value) != {"schema_version", "keys"} or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or type(value["keys"]) is not dict):
        _fail("invalid_trust_configuration")
    keys = {}
    for key_id, encoded in value["keys"].items():
        try:
            if type(encoded) is not str or len(encoded) > 128:
                _fail("invalid_trust_configuration")
            keys[key_id] = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            _fail("invalid_trust_configuration")
    return SignedPayloadVerifier(keys, allow_test_keys=allow_development)
