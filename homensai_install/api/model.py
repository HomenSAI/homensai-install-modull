"""Ограниченные JSON-входы и нормализация существующих manifest v1/v2."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import platform
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from shared.manifest import ManifestError, validate_manifest

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 48
MAX_JSON_NODES = 50000
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
TARGET_KEYS = frozenset({"os", "arch", "mode", "scope"})


def fail(code: str, **context):
    from homensai_install.errors import InstallerError
    raise InstallerError(code, **context)


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        fail("INVALID_REQUEST")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def bounded_json(value: Any, *, error_code: str = "INVALID_REQUEST") -> Any:
    """Ограничить дерево до сериализации; исключить custom objects и NaN."""
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            fail(error_code)
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                fail(error_code)
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) in (str, int, bool) or item is None:
            if type(item) is str and len(item) > MAX_JSON_BYTES:
                fail(error_code)
            if type(item) is int and item.bit_length() > 128:
                fail(error_code)
        elif type(item) is float:
            if not math.isfinite(item):
                fail(error_code)
        else:
            fail(error_code)
    if len(canonical(value).encode("utf-8")) > MAX_JSON_BYTES:
        fail(error_code)
    return deepcopy(value)


def parse_json(text: str | bytes, *, error_code: str = "INVALID_REQUEST") -> Any:
    if len(text) > MAX_JSON_BYTES:
        fail(error_code)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite constant")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        fail(error_code)
    return bounded_json(value, error_code=error_code)


def read_manifest(value: dict | str | Path) -> dict:
    if isinstance(value, (str, Path)):
        try:
            with Path(value).open("rb") as source:
                raw = source.read(MAX_JSON_BYTES + 1)
            value = parse_json(raw.decode("utf-8-sig"), error_code="INVALID_MANIFEST")
        except (OSError, UnicodeError):
            fail("INVALID_MANIFEST")
    else:
        value = bounded_json(value, error_code="INVALID_MANIFEST")
    try:
        return validate_manifest(value)
    except ManifestError as error:
        # shared.ManifestError содержит только путь/описание, не значения.
        match = re.match(r"^(manifest(?:\.[A-Za-z_]+|\[\d+\])*)\:", str(error))
        fail("INVALID_MANIFEST", details={"field_path": match.group(1) if match else "manifest"})


def normalize_manifest(source: dict) -> dict:
    """Единая модель без выдумывания путей, runtime и native-hooks у legacy v1."""
    source = read_manifest(source)
    common = {
        "model_version": 1,
        "source_schema_version": source["schema_version"],
        "product": {"id": source["product_id"], "name": source["product_name"]},
        "archive_before_replace": source["archive_before_replace"],
        "source_digest": digest(source),
    }
    if source["schema_version"] == 1:
        return {**common, "native_complete": False,
                "paths": None, "runtime": None,
                "data_home_env": source["data_home_env"],
                "components": [{"name": "web", "command": deepcopy(source["docker"]["start_command"]),
                                "dependencies": []}],
                "web": {"host": "127.0.0.1", "port": source["port"],
                        "healthcheck_url": source["healthcheck_url"]},
                "migrations": {"command": deepcopy(source["migration_command"])},
                "docker": deepcopy(source["docker"]),
                "modes": [{"name": "default", "components": ["web"]}],
                "mutual_exclusion": [], "secrets": [],
                "warnings": ["LEGACY_V1_NATIVE_METADATA_REQUIRED"]}
    return {**common, "native_complete": True,
            "paths": deepcopy(source["paths"]), "runtime": deepcopy(source["runtime"]),
            "components": [{"name": item["name"], "command": deepcopy(item["cmd"]),
                            "dependencies": deepcopy(item["after"]),
                            "heartbeat": deepcopy(item["heartbeat"]),
                            "restart": deepcopy(item["restart"])} for item in source["components"]],
            "web": deepcopy(source["web"]), "modes": deepcopy(source["modes"]),
            "mutual_exclusion": deepcopy(source["mutual_exclusion"]),
            "secrets": deepcopy(source["secrets"]),
            "migrations": deepcopy(source["migrations"]),
            "backup": deepcopy(source["backup"]), "restore": deepcopy(source["restore"]),
            "update": deepcopy(source["update"]), "hooks": deepcopy(source["hooks"]),
            "warnings": []}


def target_model(value: dict | None = None) -> dict:
    machine = platform.machine().lower()
    host_os = "windows" if platform.system() == "Windows" else platform.system().lower()
    default = {"os": host_os, "arch": {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine),
               "mode": "native", "scope": "user"}
    if value is None:
        return default
    if type(value) is not dict or set(value) - TARGET_KEYS:
        fail("INVALID_REQUEST", details={"field_path": "target"})
    result = {**default, **value}
    allowed = {"os": {"windows", "linux"}, "arch": {"x64", "arm64"},
               "mode": {"native", "docker"}, "scope": {"user", "service"}}
    if any(type(result[key]) is not str or result[key] not in options for key, options in allowed.items()):
        fail("CAPABILITY_UNSUPPORTED", details={"field_path": "target"})
    return result


def checked_id(value: str, *, field: str = "id") -> str:
    if type(value) is not str or ID_PATTERN.fullmatch(value) is None:
        fail("INVALID_REQUEST", details={"field_path": field})
    return value


def checked_product_id(value: str) -> str:
    """Product ID следует shared manifest, включая допустимую первую цифру."""
    if type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value) is None:
        fail("INVALID_REQUEST", details={"field_path": "product_id"})
    return value


@runtime_checkable
class ProductAdapter(Protocol):
    """Контракт продуктовых hooks. Регистрация не выполняет hooks автоматически.

    invoke принимает только явно объявленные capability: quiesce, readiness,
    migration_plan/apply, backup, restore, post_restore/activate.
    Конкретная доступность регистрации отражается engine.capabilities().
    """

    def capabilities(self) -> Mapping[str, Any]: ...

    def invoke(self, hook: str, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
