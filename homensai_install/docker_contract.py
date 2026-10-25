# HomenS.AI · © 2026 Serhii Khomenko
"""Подписанный узкий contract собственных Linux containers.

Здесь нет pull/build/create и нет запуска Python хоста. Чистый resource plan
предназначен для review и provisioning фаз DockerEngine. Его наличие не
подтверждает фактическую установку Docker или пригодность image license.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .engine import EngineError
from .trust import canonical_bytes, hash_manifest
from shared.adapters_fs import _tree
from shared.datahome import checked_path, contains, resolve_datahome
from shared.manifest import validate_manifest
from shared.services import component_order


CONTRACT_FILE = "docker-contract.json"
LABEL_PREFIX = "io.homensai.installer."
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,240}@sha256:[0-9a-f]{64}\Z")
PERSISTENT_ROOTS = {"data", "config", "runtime", "services", "backup"}


def _fail(reason, code="INVALID_MANIFEST"):
    raise EngineError(code, "Docker contract отклонён", details={"reason": reason})


def _text(value, maximum=4096):
    if (type(value) is not str or not value or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                   or c in "\u0085\u2028\u2029\ufffe\uffff" for c in value)):
        _fail("invalid_text")
    return value


def _relative(value):
    _text(value)
    if (value.startswith("/") or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        _fail("invalid_relative_file")
    return value


def _guest(value):
    """Только именованное поддерево данных; /proc, sockets и root недоступны."""
    _text(value)
    path = PurePosixPath(value)
    if (not value.startswith("/homensai/") or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value[1:].split("/"))
            or str(path) != value):
        _fail("invalid_container_data_path")
    return value


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _fail("duplicate_contract_key")
        out[key] = value
    return out


def _read_signed(payload, name, files):
    if _relative(name) not in files:
        _fail("contract_evidence_not_signed", "INTEGRITY_FAILED")
    path = checked_path(payload / name)
    if not path.is_file() or path.stat().st_size > 512 * 1024:
        _fail("contract_metadata_unavailable")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != files[name]:
        _fail("contract_changed_after_verification", "INTEGRITY_FAILED")
    try:
        def bad_constant(_):
            _fail("nonfinite_contract_value")
        return json.loads(raw, object_pairs_hook=_object, parse_constant=bad_constant)
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid_contract_json")


def _command(value):
    if type(value) is not list or not 1 <= len(value) <= 64:
        _fail("invalid_container_argv")
    for arg in value:
        _text(arg)
    if value[0].startswith("-"):
        _fail("invalid_container_executable")


@dataclass(frozen=True)
class VerifiedDockerContract:
    manifest_json: bytes
    document_json: bytes
    release: str
    files: tuple[tuple[str, str], ...]
    contract_sha256: str

    @property
    def payload_sha256(self):
        """Закрепляет полный проверенный map path→SHA-256, не только descriptor."""
        return hashlib.sha256(canonical_bytes(dict(self.files))).hexdigest()

    @property
    def manifest(self):
        return json.loads(self.manifest_json)

    @property
    def document(self):
        return json.loads(self.document_json)


def load_verified_docker_contract(manifest, target, options, *, verifier=None):
    """Signature связывает source manifest, release, target и image descriptors.

    Совместимость inventory schema1: runtime_executable равен имени JSON
    descriptor, это selector backend, НЕ host executable. Новый bootstrap
    обязан явно выбирать DockerEngine; NativeEngine этот contract не запускает.
    """
    if verifier is None:
        _fail("trusted_verifier_required", "INTEGRITY_FAILED")
    source = validate_manifest(manifest)
    if (source["schema_version"] != 2 or target.get("mode") != "docker"
            or target.get("os") not in {"linux", "windows"}
            or target.get("arch") not in {"x64", "arm64"}
            or target.get("scope", "user") != "user"
            or options.get("runtime_executable") != CONTRACT_FILE):
        _fail("unsupported_docker_target_or_selector", "CAPABILITY_UNSUPPORTED")
    release = options.get("release")
    if type(release) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", release) is None:
        _fail("invalid_release")
    files = verifier(source, target, options)
    if (type(files) is not dict or CONTRACT_FILE not in files
            or any(type(v) is not str or _HEX.fullmatch(v) is None for v in files.values())):
        _fail("signed_docker_inventory_required", "INTEGRITY_FAILED")
    payload = checked_path(options["payload_dir"])
    if _tree(payload)[1] != files:
        _fail("payload_changed_after_verification", "INTEGRITY_FAILED")
    value = _read_signed(payload, CONTRACT_FILE, files)
    if (type(value) is not dict or set(value) != {"schema_version", "backend", "platform",
            "network", "components", "web", "storage", "license_review"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["backend"] != "docker-managed"):
        _fail("invalid_docker_contract_schema")
    expected_platform = "linux/" + {"x64": "amd64", "arm64": "arm64"}[target["arch"]]
    if value["platform"] != expected_platform:
        _fail("container_platform_mismatch", "CAPABILITY_UNSUPPORTED")
    if value["network"] != {"internal": True}:
        _fail("private_internal_bridge_required")
    if value["storage"] != {"strategy": "filesystem_quiesced", "all_writers_managed": True}:
        _fail("managed_filesystem_writers_required", "CAPABILITY_UNSUPPORTED")
    if source["secrets"] or any(source["hooks"].values()):
        _fail("container_credentials_or_product_hooks_require_adapter", "CAPABILITY_UNSUPPORTED")
    entries = value["components"]
    declared = {item["name"]: item for item in source["components"]}
    if type(entries) is not list or len(entries) != len(declared) or len(entries) > 32:
        _fail("docker_components_mismatch")
    seen = set()
    for item in entries:
        if (type(item) is not dict or set(item) != {"name", "image", "image_id", "user", "mounts",
                "memory_bytes", "pids_limit", "stop_timeout_seconds", "require_docker_health"}):
            _fail("invalid_container_spec")
        name = item["name"]
        if type(name) is not str or name in seen or name not in declared:
            _fail("docker_components_mismatch")
        seen.add(name)
        _command(declared[name]["cmd"])
        if (type(item["image"]) is not str or _IMAGE.fullmatch(item["image"]) is None
                or type(item["image_id"]) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item["image_id"]) is None):
            _fail("immutable_image_digest_required")
        if type(item["user"]) is not str or re.fullmatch(r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}", item["user"]) is None:
            _fail("nonroot_numeric_container_user_required")
        for field, lower, upper in (("memory_bytes", 16 * 1024 * 1024, 64 * 1024**3),
                                     ("pids_limit", 8, 4096), ("stop_timeout_seconds", 1, 600)):
            if type(item[field]) is not int or not lower <= item[field] <= upper:
                _fail("invalid_container_resource_limit")
        if type(item["require_docker_health"]) is not bool:
            _fail("invalid_container_health_requirement")
        mounts = item["mounts"]
        if type(mounts) is not list or not 1 <= len(mounts) <= 5:
            _fail("persistent_mounts_required")
        roots, destinations = set(), []
        for mount in mounts:
            if (type(mount) is not dict or set(mount) != {"root", "target", "read_only"}
                    or type(mount["root"]) is not str or mount["root"] not in PERSISTENT_ROOTS
                    or mount["root"] in roots or type(mount["read_only"]) is not bool):
                _fail("invalid_persistent_mount")
            roots.add(mount["root"])
            destination = PurePosixPath(_guest(mount["target"]))
            if any(destination == old or destination in old.parents or old in destination.parents for old in destinations):
                _fail("overlapping_container_mounts")
            destinations.append(destination)
        # API paths inside each image are deterministic, not guessed per component.
        by_root = {m["root"]: m for m in mounts}
        if any(key not in by_root or by_root[key]["target"] != "/homensai/" + key for key in ("data", "config")):
            _fail("data_and_config_mount_contract_required")
    web = value["web"]
    if (type(web) is not dict or set(web) != {"component", "container_port", "probe_cmd"}
            or type(web["component"]) is not str or web["component"] not in declared
            or type(web["container_port"]) is not int or not 1024 <= web["container_port"] <= 65535):
        _fail("invalid_container_web_contract")
    _command(web["probe_cmd"])
    review = _read_signed(payload, value["license_review"], files)
    if (type(review) is not dict or set(review) != {"schema_version", "status", "images"}
            or type(review["schema_version"]) is not int or review["schema_version"] != 1
            or review["status"] != "approved" or type(review["images"]) is not dict
            or set(review["images"]) != seen):
        _fail("approved_image_license_evidence_required", "CAPABILITY_UNSUPPORTED")
    for entry in entries:
        evidence = review["images"][entry["name"]]
        if (type(evidence) is not dict or set(evidence) != {"image", "image_id", "commercial_use",
                "redistribution", "sbom_file", "notices_file"}
                or evidence["image"] != entry["image"] or evidence["image_id"] != entry["image_id"]
                or evidence["commercial_use"] is not True or evidence["redistribution"] is not True):
            _fail("image_license_not_approved", "CAPABILITY_UNSUPPORTED")
        for key in ("sbom_file", "notices_file"):
            if _relative(evidence[key]) not in files:
                _fail("image_license_evidence_not_signed", "INTEGRITY_FAILED")
    return VerifiedDockerContract(canonical_bytes(source), canonical_bytes(value), release,
                                  tuple(sorted(files.items())), files[CONTRACT_FILE])


def resource_name(installation_id, component="network"):
    if type(installation_id) is not str or _ID.fullmatch(installation_id) is None:
        _fail("invalid_installation_id")
    _text(component, 128)
    return "hsai-" + installation_id + "-" + hashlib.sha256(component.encode()).hexdigest()[:16]


def resource_plan(contract, *, install_root, installation_id, modes):
    """Immutable inputs для DockerEngine; pure review не создаёт ресурсы."""
    if not isinstance(contract, VerifiedDockerContract):
        _fail("verified_docker_contract_required", "INTEGRITY_FAILED")
    manifest, document = contract.manifest, contract.document
    root = checked_path(install_root)
    if root == Path(root.anchor):
        _fail("invalid_install_root")
    home = resolve_datahome(manifest, root)
    control = root / ".hinstall"
    for path in (home.app, home.releases, *home.roots.values()):
        if path == root or not contains(root, path) or contains(path, control) or contains(control, path):
            _fail("docker_paths_must_be_disjoint_children")
    order = component_order(manifest, tuple(modes))
    if document["web"]["component"] not in order:
        _fail("selected_profile_requires_web_component", "CAPABILITY_UNSUPPORTED")
    by_name = {item["name"]: item for item in document["components"]}
    commands = {item["name"]: item["cmd"] for item in manifest["components"]}
    common = {LABEL_PREFIX + "installation": installation_id, LABEL_PREFIX + "product": manifest["product_id"],
              LABEL_PREFIX + "release": contract.release, LABEL_PREFIX + "contract": contract.contract_sha256}
    network = {"name": resource_name(installation_id), "driver": "bridge", "internal": True,
               "labels": {**common, LABEL_PREFIX + "component": "network"}}
    components = {}
    for name in order:
        entry = by_name[name]
        environment = {"HOMENSAI_PRODUCT_ID": manifest["product_id"], "HOMENSAI_INSTALLATION_ID": installation_id,
            "HOMENSAI_RELEASE": contract.release, "HOMENSAI_COMPONENT": name,
            "HOMENSAI_DATA_HOME": "/homensai/data", "HOMENSAI_CONFIG_HOME": "/homensai/config",
            "HOMENSAI_WEB_HOST": "0.0.0.0", "HOMENSAI_WEB_PORT": str(document["web"]["container_port"]),
            "HOMENSAI_MAINTENANCE": "1"}
        ports = ({str(document["web"]["container_port"]) + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(manifest["web"]["port"])}]}
                 if name == document["web"]["component"] else {})
        spec = {"name": resource_name(installation_id, name), "image": entry["image"], "image_id": entry["image_id"],
            "platform": document["platform"], "user": entry["user"], "argv": commands[name], "environment": environment,
            "mounts": [{"Type": "bind", "Source": str(home.roots[m["root"]]), "Destination": m["target"],
                        "RW": not m["read_only"], "Propagation": "rprivate"} for m in entry["mounts"]],
            "network_name": network["name"], "ports": ports, "memory_bytes": entry["memory_bytes"],
            "pids_limit": entry["pids_limit"], "stop_timeout_seconds": entry["stop_timeout_seconds"],
            "require_docker_health": entry["require_docker_health"], "read_only_root": True,
            "restart_policy": "no", "log_driver": "none",
            "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864"}}
        spec["labels"] = {**common, LABEL_PREFIX + "component": name,
                          LABEL_PREFIX + "spec": hashlib.sha256(canonical_bytes(spec)).hexdigest()}
        components[name] = spec
    return {"schema_version": 1, "backend": "docker-managed", "manifest_sha256": hash_manifest(manifest),
            "contract_sha256": contract.contract_sha256, "payload_sha256": contract.payload_sha256,
            "order": list(order), "network": network,
            "components": components, "execution_supported": False, "provisioning_implemented": True,
            "production_ready": False, "requires": ["trusted_local_daemon", "private_owned_receipt",
                "image_license_review", "all_writers_managed", "live_docker_acceptance"]}
