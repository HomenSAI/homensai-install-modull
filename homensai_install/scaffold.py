"""Безопасный scaffold интеграции: реальные входы/хеши или явный DRAFT/BLOCKED.

Не скачивает, не устанавливает wheels, не исполняет runtime/product hooks,
не подписывает релиз и не присваивает лицензии чужим компонентам.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Mapping
import zipfile

from . import API_VERSION, __version__
from .api.model import bounded_json, canonical, checked_product_id, read_manifest, target_model
from .errors import InstallerError
from .trust import canonical_bytes, hash_manifest
from shared.adapters_fs import _tree
from shared.datahome import checked_path, contains, DataHomeError


SPEC_KEYS = {"product_id", "product_name", "release", "target", "payload_dir", "runtime_executable",
             "installer_wheel", "manifest", "runtime", "commands", "web", "secrets"}
COMMAND_KEYS = {"web", "migrate", "backup", "restore", "health"}


def _fail(code, reason):
    raise InstallerError(code, phase="integration_scaffold", details={"reason": reason})


def _relative(value):
    if (type(value) is not str or not value or value.startswith("/") or "\\" in value
            or ":" in value or any(ord(char) < 32 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        _fail("INVALID_REQUEST", "scaffold_relative_path")
    return value


def _hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _wheel_info(path):
    """Метаданные wheel читаются без извлечения и без выполнения кода."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.suffix != ".whl" or info.st_size > 512 * 1024 * 1024:
        _fail("INVALID_REQUEST", "wheel_file_required")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > 50000 or len(names) != len(set(names)):
                _fail("INVALID_REQUEST", "wheel_member_limit")
            for name in names:
                _relative(name.rstrip("/"))
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel = [name for name in names if name.endswith(".dist-info/WHEEL")]
            records = [name for name in names if name.endswith(".dist-info/RECORD")]
            if len(metadata) != 1 or len(wheel) != 1 or len(records) != 1:
                _fail("INVALID_REQUEST", "wheel_metadata_required")
            if any(archive.getinfo(name).file_size > 1024 * 1024 for name in metadata + wheel):
                _fail("INVALID_REQUEST", "wheel_metadata_limit")
            raw = archive.read(metadata[0]).decode("utf-8")
            format_info = archive.read(wheel[0]).decode("utf-8")
            headers = {}
            for line in raw.split("\n\n", 1)[0].splitlines():
                name, separator, value = line.partition(":")
                if separator and name in {"Name", "Version"}:
                    if name in headers:
                        _fail("INVALID_REQUEST", "duplicate_wheel_identity")
                    headers[name] = value.strip()
            if (not headers.get("Name") or not headers.get("Version")
                    or "Wheel-Version: 1.0" not in format_info.splitlines()):
                _fail("INVALID_REQUEST", "wheel_identity_or_format")
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", headers["Name"])
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", headers["Version"])):
                _fail("INVALID_REQUEST", "wheel_identity_format")
            return {"name": headers["Name"], "version": headers["Version"], "sha256": _hash_file(path)}
    except InstallerError:
        raise
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError, KeyError):
        _fail("INVALID_REQUEST", "invalid_wheel_archive")


def _template(spec):
    runtime = spec.get("runtime", {})
    commands = spec.get("commands", {})
    if (type(runtime) is not dict or set(runtime) - {"python", "lock_files", "vendor_wheels"}
            or type(commands) is not dict or set(commands) - COMMAND_KEYS):
        _fail("INVALID_REQUEST", "scaffold_runtime_or_commands")
    locks = runtime.get("lock_files", {})
    wheels = runtime.get("vendor_wheels", [])
    if type(locks) is not dict or type(wheels) is not list:
        _fail("INVALID_REQUEST", "scaffold_artifact_lists")
    web = spec.get("web", {"host": "127.0.0.1", "port": None, "healthz": "/healthz"})
    return {
        "schema_version": 2, "product_id": spec["product_id"], "product_name": spec["product_name"],
        "archive_before_replace": True,
        "runtime": {"python": runtime.get("python", "3.14"), "offline": True,
                    "lock_files": {name: {"file": locks.get(name), "sha256": None} for name in ("windows", "linux")},
                    "vendor_wheels": [{"file": path, "sha256": None} for path in wheels]},
        "paths": {"app": "app", "releases": "releases", "data": "data", "backup": "backup",
                  "services": "services", "config": "config", "runtime": "runtime"},
        "components": [{"name": "web", "cmd": commands.get("web"), "after": [],
                        "heartbeat": {"file": "heartbeats/web.json", "interval": 30, "timeout": 90},
                        "restart": {"delay": 30, "max": 3, "window": 600}}],
        "web": deepcopy(web), "secrets": deepcopy(spec.get("secrets", [])),
        "modes": [{"name": "default", "components": ["web"]}], "mutual_exclusion": [],
        "migrations": {"cmd": commands.get("migrate"), "backup_before_each": True, "new_db_no_backup": False},
        "backup": {"cmd": commands.get("backup"), "encrypted": True},
        "restore": {"cmd": commands.get("restore")},
        "update": {"staged_releases": True, "pointer_file": "active.json", "state_file": "update.json",
                   "health_wait": 60, "backup_before_rollback": True,
                   "retain_previous_release": True, "retain_runtime": True},
        "hooks": {"pre_start": [], "health_checks": [commands["health"]] if commands.get("health") else [],
                  "post_update": []},
    }


_CLIENT = '''"""Тонкий пример подключения общего SDK. Импорт не выполняет установку."""
from pathlib import Path
import json

from homensai_install import __version__
from homensai_install.api.v1 import InstallerAPI
from homensai_install.trust import load_trust_config

ROOT = Path(__file__).resolve().parent
PINNED_VERSION = {module_version!r}

def prepare_install(*, state_dir, install_root, locale="ru", allow_development=False):
    if __version__ != PINNED_VERSION:
        raise RuntimeError("Installer module version does not match the reviewed pin")
    integration = json.loads((ROOT / "integration.json").read_text())
    manifest = json.loads((ROOT / "product.manifest.json").read_text())
    # Publisher supplies these files after reviewing/signing the unsigned inventory.
    verifier = load_trust_config(ROOT / "trust-config.json", allow_development=allow_development)
    options = {{
        "install_root": str(Path(install_root).absolute()),
        "payload_dir": str(ROOT / "payload"),
        "inventory_file": str(ROOT / "payload.inventory.json"),
        "signature_file": str(ROOT / "payload.signature.json"),
        "runtime_executable": integration["runtime_executable"],
        "release": integration["release"], "eula_locale": locale,
    }}
    verified = verifier(manifest, integration["target"], options)
    options["eula_digest"] = verified["legal/" + locale + ".txt"]
    api = InstallerAPI.from_bootstrap(state_dir, trust_config=ROOT / "trust-config.json",
                                     allow_development=allow_development, locale=locale)
    described = api.describe()
    if described["api_version"] != {api_version!r}:
        raise RuntimeError("Installer API contract changed")
    api.validate(manifest)
    plan = api.plan("install", manifest, integration["target"], options=options)
    return api, plan

def submit_reviewed_plan(api, plan, *, accepted_consent_refs, idempotency_key):
    # The calling UI must show the real signed terms and complete plan first.
    # No automatic acceptance: the caller supplies the actually accepted references.
    return api.submit(plan["plan_id"], plan["plan_digest"], accepted_consent_refs,
                      idempotency_key=idempotency_key, background=True)

if __name__ == "__main__":
    print(json.dumps(InstallerAPI(ROOT / "local-state").describe(), ensure_ascii=False))
'''


_ADAPTER_DRAFT = '''"""DRAFT product callback skeleton. No successful health/migration is invented.
Implement business hooks and operation_status transactionally in the product.
This file is not referenced by a production manifest or signed inventory.
"""
import json
import sys

def main():
    request = json.load(sys.stdin)
    response = {{key: request[key] for key in
                ("protocol_version", "product_id", "request_id", "request_digest", "hook")}}
    response.update(status="error", error={{"code": "PRODUCT_ADAPTER_NOT_IMPLEMENTED"}})
    print(json.dumps(response))
    return 2

if __name__ == "__main__":
    sys.exit(main())
'''


def scaffold_product(output_dir: str | Path, spec: Mapping | None = None) -> dict:
    """Создать новый каталог; не дописывать/перезаписывать существующий проект.

    complete inputs -> SCAFFOLDED/REVIEW_REQUIRED, всё ещё unsigned/nonproduction;
    missing/invalid real artifacts -> BLOCKED и schema-invalid manifest DRAFT.
    """
    if spec is not None and not isinstance(spec, Mapping):
        _fail("INVALID_REQUEST", "scaffold_spec_object")
    spec = bounded_json(dict(spec) if spec is not None else {})
    if type(spec) is not dict or set(spec) - SPEC_KEYS:
        _fail("INVALID_REQUEST", "scaffold_unknown_fields")
    source_manifest = spec.get("manifest") if type(spec.get("manifest")) is dict else {}
    spec.setdefault("product_id", source_manifest.get("product_id", "new-product"))
    spec.setdefault("product_name", source_manifest.get("product_name", "Новый продукт — требуется описание"))
    checked_product_id(spec["product_id"])
    if type(spec["product_name"]) is not str or not 1 <= len(spec["product_name"]) <= 128:
        _fail("INVALID_REQUEST", "scaffold_product_name")
    try:
        destination = checked_path(Path(output_dir).absolute())
        if destination.exists() or destination == Path(destination.anchor):
            _fail("OPERATION_CONFLICT", "scaffold_output_must_not_exist")
        if not destination.parent.is_dir():
            _fail("INVALID_REQUEST", "scaffold_output_parent_required")
    except DataHomeError:
        _fail("INVALID_REQUEST", "scaffold_unsafe_output_path")
    blockers = []

    def block(code):
        if code not in blockers:
            blockers.append(code)

    target = spec.get("target")
    if target is None:
        block("TARGET_REQUIRED")
        target = None
    else:
        if type(target) is not dict or not {"os", "arch", "mode"} <= set(target):
            block("EXPLICIT_TARGET_OS_ARCH_MODE_REQUIRED")
        target = target_model(target)
        if target["mode"] != "native":
            block("NATIVE_SCAFFOLD_ONLY")
    release = spec.get("release")
    if type(release) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", release):
        block("RELEASE_REQUIRED")
    manifest = deepcopy(spec.get("manifest")) if spec.get("manifest") is not None else _template(spec)
    if type(manifest) is not dict:
        _fail("INVALID_REQUEST", "scaffold_manifest_object")
    manifest["product_id"], manifest["product_name"] = spec["product_id"], spec["product_name"]
    payload = None
    inventory = {}
    if not spec.get("payload_dir"):
        block("PAYLOAD_REQUIRED")
    else:
        try:
            payload = checked_path(Path(spec["payload_dir"]).absolute())
            if not payload.is_dir() or contains(payload, destination) or contains(destination, payload):
                _fail("INVALID_REQUEST", "scaffold_payload_output_overlap")
            _, inventory = _tree(payload)
            if not inventory or len(inventory) > 50000:
                block("PAYLOAD_FILE_COUNT_INVALID")
            for name in inventory:
                info = (payload / name).lstat()
                if info.st_nlink != 1:
                    block("PAYLOAD_HARDLINK_REJECTED")
                basename = Path(name).name.lower()
                if ((basename == ".env" or basename.startswith(".env."))
                        and not basename.endswith((".example", ".sample", ".template"))):
                    block("PAYLOAD_SECRET_CONFIGURATION_REJECTED")
        except InstallerError:
            raise
        except (OSError, ValueError, RuntimeError, DataHomeError):
            block("PAYLOAD_UNAVAILABLE_OR_UNSAFE")
    runtime = spec.get("runtime_executable")
    if runtime is None:
        block("RUNTIME_EXECUTABLE_REQUIRED")
    else:
        runtime = _relative(runtime)
        if runtime not in inventory:
            block("RUNTIME_EXECUTABLE_NOT_IN_PAYLOAD")
        elif target:
            with (payload / runtime).open("rb") as stream:
                magic = stream.read(4)
            if not (magic == b"\x7fELF" if target["os"] == "linux" else magic[:2] == b"MZ"):
                block("NATIVE_RUNTIME_FORMAT_REQUIRED")
    artifacts = []
    try:
        runtime_meta = manifest["runtime"]
        locks = runtime_meta["lock_files"]
        wheels = runtime_meta["vendor_wheels"]
        if set(locks) != {"windows", "linux"} or not isinstance(wheels, list) or not wheels:
            block("REAL_LOCKS_AND_WHEELS_REQUIRED")
        for label, artifact in [("lock", item) for item in locks.values()] + [("wheel", item) for item in wheels]:
            if type(artifact) is not dict or not artifact.get("file"):
                block("REAL_LOCKS_AND_WHEELS_REQUIRED")
                continue
            name = _relative(artifact["file"])
            if name not in inventory:
                artifact["sha256"] = None
                block("DECLARED_ARTIFACT_MISSING")
                continue
            artifact["sha256"] = inventory[name]
            evidence = {"kind": label, "file": name, "sha256": inventory[name], "checks": "bytes_hashed"}
            if label == "wheel":
                try:
                    evidence.update(_wheel_info(payload / name))
                except InstallerError:
                    block("INVALID_PRODUCT_WHEEL")
            else:
                try:
                    with (payload / name).open("rb") as stream:
                        raw = stream.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024 or not raw.decode("utf-8").strip():
                        block("LOCK_FILE_INVALID")
                except UnicodeError:
                    block("LOCK_FILE_INVALID")
            artifacts.append(evidence)
    except (KeyError, TypeError, AttributeError):
        block("REAL_LOCKS_AND_WHEELS_REQUIRED")
    for locale in ("ru", "en", "de"):
        if "legal/" + locale + ".txt" not in inventory:
            block("EULA_" + locale.upper() + "_REQUIRED")
    installer = None
    wheel_source = None
    if not spec.get("installer_wheel"):
        block("INSTALLER_WHEEL_REQUIRED")
    else:
        try:
            wheel_source = checked_path(Path(spec["installer_wheel"]).absolute())
            installer = _wheel_info(wheel_source)
            if (re.sub(r"[-_.]+", "-", installer["name"]).lower() != "homensai-install-module"
                    or installer["version"] != __version__):
                block("INSTALLER_WHEEL_VERSION_MISMATCH")
        except (InstallerError, OSError, DataHomeError):
            block("INSTALLER_WHEEL_INVALID")
    try:
        read_manifest(manifest)
    except InstallerError:
        block("MANIFEST_REQUIRES_PRODUCT_CONFIGURATION")

    # Никакой installer input сам по себе не подтверждает runtime closure/licensing/E2E.
    report = {"schema_version": 1, "status": "BLOCKED" if blockers else "SCAFFOLDED",
              "readiness": "DRAFT" if blockers else "REVIEW_REQUIRED", "production_ready": False,
              "product_id": spec["product_id"], "module_version": __version__, "api_version": API_VERSION,
              "input_blockers": blockers, "artifacts": artifacts, "installer_wheel": installer,
              "release_blockers": ["PUBLISHER_SIGNATURE_REQUIRED", "DEPENDENCY_CLOSURE_NOT_VERIFIED",
                                   "COMMERCIAL_LICENSE_REVIEW_REQUIRED", "TARGET_INSTALL_UPDATE_RESTORE_ROLLBACK_NOT_TESTED"],
              "product_hooks_executed": False, "dependencies_installed": False}
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError:
        _fail("OPERATION_CONFLICT", "scaffold_output_must_not_exist")

    def write_json(name, value):
        (destination / name).write_bytes(canonical_bytes(value) + b"\n")

    try:
        if blockers:
            write_json("product.manifest.draft.json", {"schema_version": "DRAFT", "readiness": "BLOCKED",
                       "input_blockers": blockers, "manifest_template": manifest})
        else:
            copied_payload = destination / "payload"
            copied_payload.mkdir(mode=0o700)
            for relative in inventory:
                source = checked_path(payload / relative)
                copied = checked_path(copied_payload / relative)
                copied.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, copied)
            if _tree(destination / "payload")[1] != inventory:
                _fail("INTEGRITY_FAILED", "payload_changed_during_scaffold_copy")
            vendor = destination / "installer-wheel"
            vendor.mkdir()
            shutil.copyfile(wheel_source, vendor / wheel_source.name)
            if _hash_file(vendor / wheel_source.name) != installer["sha256"]:
                _fail("INTEGRITY_FAILED", "installer_wheel_changed_during_copy")
            write_json("product.manifest.json", manifest)
            write_json("payload.inventory.unsigned.json", {
                "schema_version": 1, "product_id": spec["product_id"], "release": release,
                "target": {key: target[key] for key in ("os", "arch", "mode")},
                "files": inventory, "development": True, "manifest_sha256": hash_manifest(manifest),
                "runtime_executable": runtime})
            write_json("integration.json", {"module_version": __version__, "api_version": API_VERSION,
                       "target": target, "release": release, "runtime_executable": runtime})
            (destination / "installer-wheel.pin.txt").write_text(
                "./installer-wheel/" + wheel_source.name + " --hash=sha256:" + installer["sha256"] + "\n", encoding="utf-8")
        (destination / "integration-client.py").write_text(
            _CLIENT.format(module_version=__version__, api_version=API_VERSION), encoding="utf-8")
        (destination / "product-adapter.draft.py").write_text(_ADAPTER_DRAFT.format(), encoding="utf-8")
        resource = resources.files("homensai_install").joinpath("api", "resources")
        (destination / "INSTALLER_API.md").write_text(resource.joinpath("INSTALLER_API.md").read_text(encoding="utf-8"), encoding="utf-8")
        docs = destination / "docs"
        docs.mkdir()
        (docs / "INTEGRATION_GUIDE.md").write_text(resource.joinpath("INTEGRATION_GUIDE.md").read_text(encoding="utf-8"), encoding="utf-8")
        (destination / "installer-discovery.json").write_text(resource.joinpath("discovery.json").read_text(encoding="utf-8"), encoding="utf-8")
        api_dir = destination / "api"
        schema_dir = api_dir / "schemas"
        schema_dir.mkdir(parents=True)
        (api_dir / "discovery.json").write_text(resource.joinpath("discovery.json").read_text(encoding="utf-8"), encoding="utf-8")
        (api_dir / "openapi.yaml").write_text(resource.joinpath("openapi.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        schema_resource = resources.files("homensai_install").joinpath("api", "schemas")
        for name in ("request", "plan", "operation", "event", "normalized"):
            (schema_dir / (name + ".schema.json")).write_text(
                schema_resource.joinpath(name + ".schema.json").read_text(encoding="utf-8"), encoding="utf-8")
        (schema_dir / "manifest.schema.json").write_text(
            resources.files("shared").joinpath("manifest.schema.json").read_text(encoding="utf-8"), encoding="utf-8")
        write_json("trust-config.example.json", {"schema_version": 1, "keys": {}})
        (destination / "INTEGRATION_TODO.txt").write_text(
            "INTEGRATION-001: Подготовить реальные payload/runtime/locks/wheels; исправить все input_blockers.\n"
            "INTEGRATION-002: Изучить INSTALLER_API.md и installer-discovery.json; проверить version pin.\n"
            "INTEGRATION-003: Реализовать product hooks и транзакционную идемпотентность; draft adapter всегда отказывает.\n"
            "INTEGRATION-004: Проверить dependency closure и право коммерческого/некоммерческого использования.\n"
            "INTEGRATION-005: Проверить EULA ru/en/de; подписать inventory доверенным publisher, закрепить public key.\n"
            "INTEGRATION-006: Собрать и проверить install/update/restore/rollback/uninstall для каждого target.\n"
            "INTEGRATION-007: DONE только после доказательств; сохранять Git checkpoint после законченного задания.\n",
            encoding="utf-8")
        report["files"] = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file())
        report["files"].append("integration-report.json")
        write_json("integration-report.json", report)
        return report
    except BaseException:
        # Не удаляем чужие возможные правки каталога и не выдаём частичный scaffold за готовый.
        report["status"], report["readiness"] = "BLOCKED", "PARTIAL_OUTPUT"
        report["input_blockers"] = [*blockers, "SCAFFOLD_WRITE_INTERRUPTED"]
        try:
            write_json("integration-report.json", report)
        except OSError:
            pass
        raise


__all__ = ["scaffold_product"]
