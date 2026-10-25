# HomenS.AI · © 2026 Serhii Khomenko
"""Контроллер уже подготовленных принадлежащих установке Docker resources.

Реальные CLI операции: inspect/start/SIGTERM/exec health. Pull/create/remove,
provisioning, update и restore здесь не реализованы и не объявляются доступными.
По умолчанию нет runner/verifier: ни implicit context, ни host Python fallback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import stat
import struct
import sys
import time
import urllib.error
import urllib.request

from .docker_contract import (CONTRACT_FILE, LABEL_PREFIX, load_verified_docker_contract,
                              resource_plan)
from .engine import EngineError, _digest, _file_digest, _owner_id, _read, _write
from .trust import canonical_bytes, hash_manifest
from shared import archive_crypto
from shared.archive import _inventory as archive_inventory
from shared.datahome import checked_path, contains, resolve_datahome
from shared.update_plan import operation_lock


def _projection(fields):
    return "{" + ",".join(json.dumps(name) + ":{{json " + path + "}}" for name, path in fields.items()) + "}"


INFO_FORMAT = _projection({"ID": ".ID", "OSType": ".OSType", "Architecture": ".Architecture",
    "SecurityOptions": ".SecurityOptions", "MemoryLimit": ".MemoryLimit", "PidsLimit": ".PidsLimit"})
IMAGE_FORMAT = _projection({"Id": ".Id", "RepoDigests": ".RepoDigests", "Os": ".Os",
                           "Architecture": ".Architecture", "Config": ".Config"})
CONTAINER_FORMAT = _projection({"Id": ".Id", "Name": ".Name", "Image": ".Image", "Config": ".Config",
    "HostConfig": ".HostConfig", "Mounts": ".Mounts", "Networks": ".NetworkSettings.Networks",
    "Ports": ".NetworkSettings.Ports", "Running": ".State.Running", "Status": ".State.Status",
    "ExitCode": ".State.ExitCode", "OOMKilled": ".State.OOMKilled", "Error": ".State.Error",
    "Paused": ".State.Paused", "Restarting": ".State.Restarting", "Dead": ".State.Dead",
    "StartedAt": ".State.StartedAt", "FinishedAt": ".State.FinishedAt", "RestartCount": ".RestartCount"})[:-1] + ',"Health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}'
NETWORK_FORMAT = _projection({"Id": ".Id", "Name": ".Name", "Driver": ".Driver", "Internal": ".Internal",
    "Labels": ".Labels", "Options": ".Options", "Containers": ".Containers", "Scope": ".Scope"})
FOREIGN_FORMAT = _projection({"Id": ".Id", "Mounts": ".Mounts"})
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")


def _fail(code, reason):
    raise EngineError(code, "Docker controller отклонил операцию", details={"reason": reason})


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _fail("INTEGRITY_FAILED", "duplicate_docker_json_key")
        out[key] = value
    return out


def _json(raw, *, maximum=1024 * 1024):
    if type(raw) is not bytes or len(raw) > maximum:
        _fail("RECOVERY_REQUIRED", "docker_response_limit")
    try:
        def bad(_):
            _fail("INTEGRITY_FAILED", "nonfinite_docker_value")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=bad)
    except (ValueError, UnicodeError, RecursionError):
        _fail("RECOVERY_REQUIRED", "invalid_docker_json")
    if type(value) is not dict:
        _fail("RECOVERY_REQUIRED", "docker_object_required")
    return value


def _private_existing(path, *, directory):
    path = checked_path(path)
    try:
        info = path.stat()
    except OSError:
        _fail("RECOVERY_REQUIRED", "owned_metadata_missing")
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not expected or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            or (not directory and info.st_nlink != 1)):
        _fail("PERMISSION_REQUIRED", "owned_metadata_not_private")
    return path


def _env(value):
    if value is None:
        return {}
    if type(value) is not list or len(value) > 256:
        _fail("INTEGRITY_FAILED", "invalid_container_environment")
    result = {}
    for entry in value:
        if type(entry) is not str or "=" not in entry or len(entry) > 16384:
            _fail("INTEGRITY_FAILED", "invalid_container_environment")
        name, text = entry.split("=", 1)
        if name in result or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None or "\0" in text:
            _fail("INTEGRITY_FAILED", "ambiguous_container_environment")
        result[name] = text
    return result


def _local_daemon_namespaces(endpoint):
    """Attest a root-owned dockerd on this host, not a forwarded VM socket.

    Without this boundary, daemon bind-source paths and our archive paths may
    refer to different filesystems. Rootless/Desktop/remote adapters need their
    own reviewed attestation; the current profile fails closed for them.
    """
    if type(endpoint) is not str or not endpoint.startswith("unix:///"):
        _fail("CAPABILITY_UNSUPPORTED", "local_unix_daemon_required")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(endpoint[len("unix://"):])
            pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if pid <= 0 or uid != 0:
            _fail("CAPABILITY_UNSUPPORTED", "rootful_local_daemon_profile_required")
        executable = Path(os.readlink("/proc/" + str(pid) + "/exe"))
        metadata = executable.stat()
        if executable.name != "dockerd" or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            _fail("CAPABILITY_UNSUPPORTED", "docker_socket_peer_is_not_trusted_dockerd")
        identities = {}
        for namespace in ("mnt", "net"):
            own = Path("/proc/self/ns/" + namespace).stat()
            peer = Path("/proc/" + str(pid) + "/ns/" + namespace).stat()
            if (own.st_dev, own.st_ino) != (peer.st_dev, peer.st_ino):
                _fail("CAPABILITY_UNSUPPORTED", "daemon_host_namespace_differs")
            identities[namespace] = f"{own.st_dev}:{own.st_ino}"
        return identities
    except EngineError:
        raise
    except (OSError, ValueError, struct.error, AttributeError):
        _fail("CAPABILITY_UNSUPPORTED", "daemon_host_namespace_cannot_be_verified")


def _mount_table():
    """Kernel mapping detects bind aliases that lexical path overlap misses."""
    try:
        with open("/proc/self/mountinfo", "rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            _fail("RECOVERY_REQUIRED", "host_mount_inventory_too_large")
        rows = raw.decode("utf-8").splitlines()
        if len(rows) > 8192:
            _fail("RECOVERY_REQUIRED", "host_mount_inventory_too_large")
        result = []
        for row in rows:
            columns = row.split(" ")
            if len(columns) < 10 or "-" not in columns or re.fullmatch(r"[0-9]+:[0-9]+", columns[2]) is None:
                _fail("RECOVERY_REQUIRED", "invalid_host_mount_inventory")
            def decode(value):
                decoded = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
                if not decoded.startswith("/") or any(ord(c) < 32 for c in decoded):
                    _fail("RECOVERY_REQUIRED", "unsupported_host_mount_path")
                return Path(decoded)
            result.append((columns[2], decode(columns[3]), decode(columns[4])))
        return result
    except (OSError, UnicodeError, ValueError):
        _fail("RECOVERY_REQUIRED", "host_mount_inventory_unavailable")


def _physical_region(path, mounts):
    covering = [entry for entry in mounts if contains(entry[2], path)]
    if not covering:
        _fail("RECOVERY_REQUIRED", "host_path_mount_unresolved")
    device, root, mountpoint = max(covering, key=lambda entry: len(entry[2].parts))
    return device, root / path.relative_to(mountpoint)


def _regions(path, mounts):
    return [_physical_region(path, mounts), *((device, root) for device, root, mountpoint in mounts if contains(path, mountpoint))]


def _regions_overlap(left, right):
    return any(a_device == b_device and (contains(a, b) or contains(b, a))
               for a_device, a in left for b_device, b in right)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_health(url, challenge, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "X-HomenSAI-Challenge": challenge})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                _fail("HEALTH_CHECK_FAILED", "health_http_status")
            return _json(response.read(16385), maximum=16384)
    except (OSError, urllib.error.URLError, TimeoutError):
        _fail("HEALTH_CHECK_FAILED", "health_http_unavailable")


class _Canceled(BaseException):
    pass


class DockerEngine:
    """Inject trusted DockerCLI, verifier и secret provider из SDK/bootstrap.

    Linux local Unix socket — единственный исполняемый профиль этого выпуска.
    Windows Docker Desktop требует проверенный VM path/ACL adapter; renderer и
    похожие строки путей не считаются таким adapter. Production всегда false.
    """
    ACTIONS = ("install", "start", "stop", "backup", "restore", "update", "rollback",
               "repair", "reinstall", "uninstall")
    EXECUTABLE_ACTIONS = {"start", "stop", "backup"}

    def __init__(self, verifier=None, runner=None, secret_provider=None, *, fetcher=None, host_probe=None):
        self.verifier = verifier
        self.runner = runner
        self.secret_provider = secret_provider
        self.fetcher = fetcher or _http_health
        self.host_probe = host_probe or _local_daemon_namespaces

    def _host_supported(self, target):
        host_arch = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "ARM64": "arm64"}.get(platform.machine())
        endpoint = getattr(self.runner, "endpoint", "")
        return (sys.platform.startswith("linux") and target.get("os") == "linux"
                and target.get("mode") == "docker" and target.get("scope", "user") == "user"
                and target.get("arch") == host_arch and type(endpoint) is str
                and endpoint.startswith("unix:///") and self.runner is not None)

    def _run(self, args, *, timeout=30, code="RECOVERY_REQUIRED"):
        if self.runner is None:
            _fail("PREREQUISITE_MISSING", "trusted_docker_runner_required")
        try:
            result = self.runner.run(tuple(args), timeout=timeout)
        except EngineError:
            raise
        except Exception:
            _fail(code, "docker_command_unavailable")
        if type(result.returncode) is not int or result.returncode != 0:
            _fail(code, "docker_command_failed")
        if type(result.stdout) is not bytes or len(result.stdout) > 1024 * 1024:
            _fail(code, "docker_response_limit")
        return result.stdout

    def _ctx_run(self, ctx, args, *, timeout=30, code="RECOVERY_REQUIRED"):
        if "_health_deadline" in ctx:
            remaining = ctx["_health_deadline"] - time.monotonic()
            if remaining <= 0:
                _fail("HEALTH_CHECK_FAILED", "health_wait_timeout")
            timeout = min(timeout, remaining)
        return self._run(args, timeout=timeout, code=code)

    def _daemon(self, target, ctx=None):
        if not self._host_supported(target):
            _fail("CAPABILITY_UNSUPPORTED", "unsupported_local_docker_host")
        try:
            binding = self.host_probe(self.runner.endpoint)
        except EngineError:
            raise
        except Exception:
            _fail("CAPABILITY_UNSUPPORTED", "daemon_host_namespace_cannot_be_verified")
        if (type(binding) is not dict or set(binding) != {"mnt", "net"}
                or any(type(value) is not str or re.fullmatch(r"[0-9]+:[0-9]+", value) is None for value in binding.values())):
            _fail("CAPABILITY_UNSUPPORTED", "invalid_trusted_host_namespace_attestation")
        info = _json(self._ctx_run(ctx or {}, ("info", "--format", INFO_FORMAT), code="PREREQUISITE_MISSING"))
        arch = {"x64": {"x86_64", "amd64"}, "arm64": {"aarch64", "arm64"}}[target["arch"]]
        if (type(info.get("ID")) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}", info["ID"]) is None
                or info.get("OSType") != "linux" or info.get("Architecture") not in arch
                or info.get("MemoryLimit") is not True or info.get("PidsLimit") is not True
                or type(info.get("SecurityOptions")) is not list
                or not any(type(x) is str and x.startswith("name=seccomp") for x in info["SecurityOptions"])):
            _fail("CAPABILITY_UNSUPPORTED", "docker_daemon_constraints_not_met")
        return info | {"host_namespaces": binding}

    def capabilities(self, target):
        available, reason = False, "PREREQUISITE_MISSING"
        if not self._host_supported(target):
            reason = "CAPABILITY_UNSUPPORTED"
        else:
            try:
                self._daemon(target)
                available = self.verifier is not None
            except Exception as error:
                reason = getattr(error, "code", "PREREQUISITE_MISSING")
        actions = {}
        for action in self.ACTIONS:
            supported = available and action in self.EXECUTABLE_ACTIONS and (action != "backup" or self.secret_provider is not None)
            actions[action] = {"supported": supported, "readiness": "development",
                               "reason": None if supported else reason if action in self.EXECUTABLE_ACTIONS else "CAPABILITY_UNSUPPORTED"}
        return {"target": dict(target), "actions": actions, "production_ready": False,
            "execution_scope": "managed-container-controller", "manifest_schemas": [2],
            "provisioning_implemented": False, "automatic_install": False,
            "backup_strategy": "filesystem_quiesced", "backup_contains_images": False,
            "product_backup_hooks_supported": False, "restart_supervision": False,
            "health_model": "owned_container_binding_and_private_challenge_probe",
            "runtime_constraints": ["rootful_linux_daemon_in_same_mount_and_network_namespaces", "exact_private_ownership_receipt", "digest_pinned_images",
                "nonroot_containers", "internal_bridge", "loopback_publish", "all_writers_managed"],
            "live_acceptance": "REQUIRED", "docker_was_probed": self._host_supported(target)}

    def _context(self, manifest, target, options):
        if not self._host_supported(target):
            _fail("CAPABILITY_UNSUPPORTED", "unsupported_local_docker_host")
        contract = load_verified_docker_contract(manifest, target, options, verifier=self.verifier)
        root = checked_path(options.get("install_root", ""))
        if not root.is_dir() or root == Path(root.anchor):
            _fail("RECOVERY_REQUIRED", "provisioned_install_root_required")
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            _fail("PERMISSION_REQUIRED", "install_root_owner_mismatch")
        control = _private_existing(root / ".hinstall", directory=True)
        if (control / "installation.json").exists():
            _fail("OPERATION_CONFLICT", "native_and_docker_ownership_conflict")
        receipt_path = _private_existing(control / "docker-installation.json", directory=False)
        owner = _read(receipt_path)
        required = {"schema_version", "mode", "product_id", "installation_id", "owner", "daemon_id", "endpoint_sha256",
                    "release", "manifest_sha256", "contract_sha256", "modes", "containers", "network"}
        if (type(owner) is not dict or set(owner) != required or type(owner["schema_version"]) is not int
                or owner["schema_version"] != 1 or owner["mode"] != "docker" or owner["owner"] != _owner_id()
                or owner["product_id"] != contract.manifest["product_id"] or owner["release"] != contract.release
                or owner["manifest_sha256"] != hash_manifest(contract.manifest)
                or owner["contract_sha256"] != contract.contract_sha256
                or type(owner["installation_id"]) is not str or re.fullmatch(r"[0-9a-f]{32}", owner["installation_id"]) is None
                or owner["endpoint_sha256"] != hashlib.sha256(self.runner.endpoint.encode()).hexdigest()):
            _fail("OPERATION_CONFLICT", "docker_ownership_mismatch")
        if type(owner["modes"]) is not list or any(type(mode) is not str for mode in owner["modes"]):
            _fail("RECOVERY_REQUIRED", "invalid_owned_modes")
        if options.get("modes", owner["modes"]) != owner["modes"]:
            _fail("CAPABILITY_UNSUPPORTED", "changing_owned_profile_requires_provisioner")
        spec = resource_plan(contract, install_root=str(root), installation_id=owner["installation_id"], modes=owner["modes"])
        if (type(owner["containers"]) is not dict or set(owner["containers"]) != set(spec["components"])
                or any(type(cid) is not str or _HEX.fullmatch(cid) is None for cid in owner["containers"].values())
                or len(set(owner["containers"].values())) != len(owner["containers"])):
            _fail("RECOVERY_REQUIRED", "invalid_owned_container_ids")
        network = owner["network"]
        if (type(network) is not dict or set(network) != {"id", "name"} or type(network["id"]) is not str
                or _HEX.fullmatch(network["id"]) is None or network["name"] != spec["network"]["name"]):
            _fail("RECOVERY_REQUIRED", "invalid_owned_network_id")
        home = resolve_datahome(contract.manifest, root)
        for path in home.roots.values():
            if not checked_path(path).is_dir():
                _fail("RECOVERY_REQUIRED", "managed_data_root_missing")
        work = checked_path(options.get("work_dir", str(root.parent / ("." + root.name + "-docker-recovery"))))
        if work == Path(work.anchor) or contains(root, work) or contains(work, root):
            _fail("INVALID_MANIFEST", "backup_work_must_be_outside_installation")
        mounts = _mount_table()
        managed = [home.app, home.releases, *home.roots.values(), control]
        for index, path in enumerate(managed):
            if any(_regions_overlap(_regions(path, mounts), _regions(other, mounts)) for other in managed[index + 1:]):
                _fail("INVALID_MANIFEST", "managed_paths_alias_same_host_data")
        if _regions_overlap(_regions(work, mounts), _regions(root, mounts)):
            _fail("INVALID_MANIFEST", "backup_work_aliases_installation")
        return {"manifest": contract.manifest, "target": dict(target), "options": dict(options),
            "root": root, "control": control, "home": home, "contract": contract, "owner": owner,
            "receipt_digest": _file_digest(receipt_path), "spec": spec, "work": work}

    def _image(self, ctx, spec):
        image = _json(self._ctx_run(ctx, ("image", "inspect", "--format", IMAGE_FORMAT, "--", spec["image_id"])))
        digests = image.get("RepoDigests")
        if (image.get("Id") != spec["image_id"] or image.get("Os") != "linux"
                or image.get("Architecture") != spec["platform"].split("/")[1]
                or type(digests) is not list or not any(type(x) is str and x.endswith("@" + spec["image"].rsplit("@", 1)[1]) for x in digests)
                or type(image.get("Config")) is not dict):
            _fail("INTEGRITY_FAILED", "immutable_image_identity_mismatch")
        return image

    def _container(self, ctx, name):
        spec = ctx["spec"]["components"][name]
        cid = ctx["owner"]["containers"][name]
        image = self._image(ctx, spec)
        value = _json(self._ctx_run(ctx, ("container", "inspect", "--format", CONTAINER_FORMAT, "--", cid)))
        config, host = value.get("Config"), value.get("HostConfig")
        if (value.get("Id") != cid or value.get("Name") != "/" + spec["name"] or value.get("Image") != spec["image_id"]
                or type(config) is not dict or type(host) is not dict):
            _fail("OPERATION_CONFLICT", "container_identity_mismatch")
        labels = config.get("Labels") or {}
        if type(labels) is not dict or any(labels.get(k) != v for k, v in spec["labels"].items()):
            _fail("OPERATION_CONFLICT", "container_owner_labels_mismatch")
        if any(key.startswith(LABEL_PREFIX) and key not in spec["labels"] for key in labels):
            _fail("OPERATION_CONFLICT", "unexpected_owner_label")
        expected_env = {**_env(image["Config"].get("Env")), **spec["environment"]}
        if (config.get("Image") != spec["image"] or config.get("User") != spec["user"]
                or config.get("Entrypoint") != spec["argv"][:1] or (config.get("Cmd") or []) != spec["argv"][1:]
                or _env(config.get("Env")) != expected_env or config.get("WorkingDir", "") != image["Config"].get("WorkingDir", "")
                or config.get("Healthcheck") != image["Config"].get("Healthcheck") or config.get("OpenStdin") is not False
                or config.get("Tty") is not False):
            _fail("INTEGRITY_FAILED", "container_configuration_mismatch")
        # Нельзя обходить readonly root и ограничения namespaces через host options.
        if (host.get("ReadonlyRootfs") is not True or host.get("Privileged") is not False
                or set(host.get("CapDrop") or []) != {"ALL"} or host.get("CapAdd")
                or host.get("SecurityOpt") not in (["no-new-privileges"], ["no-new-privileges:true"], ["no-new-privileges=true"])
                or host.get("PidMode", "") != "" or host.get("IpcMode") != "private"
                or host.get("UTSMode", "") != "" or host.get("UsernsMode", "") != ""
                or host.get("CgroupnsMode") != "private" or host.get("CgroupParent", "") != ""
                or any(host.get(key) for key in ("Devices", "DeviceRequests", "DeviceCgroupRules", "VolumesFrom", "Binds", "GroupAdd", "Links", "ExtraHosts", "Sysctls"))
                or host.get("NetworkMode") != ctx["spec"]["network"]["name"]
                or host.get("Memory") != spec["memory_bytes"] or host.get("PidsLimit") != spec["pids_limit"]
                or host.get("PublishAllPorts") is not False or host.get("AutoRemove") is not False
                or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
                or host.get("LogConfig") != {"Type": "none", "Config": {}} or host.get("Tmpfs") != spec["tmpfs"]
                or (host.get("PortBindings") or {}) != spec["ports"]):
            _fail("INTEGRITY_FAILED", "unsafe_or_changed_container_host_settings")
        mounts = value.get("Mounts")
        if type(mounts) is not list or len(mounts) != len(spec["mounts"]):
            _fail("INTEGRITY_FAILED", "unexpected_container_mount")
        actual = []
        for mount in mounts:
            if type(mount) is not dict:
                _fail("INTEGRITY_FAILED", "invalid_container_mount")
            actual.append({key: mount.get(key) for key in ("Type", "Source", "Destination", "RW", "Propagation")})
        if sorted(actual, key=lambda x: str(x["Destination"])) != sorted(spec["mounts"], key=lambda x: x["Destination"]):
            _fail("INTEGRITY_FAILED", "container_mount_identity_mismatch")
        for mount in actual:
            checked_path(mount["Source"])
        networks = value.get("Networks")
        if (type(networks) is not dict or set(networks) != {ctx["spec"]["network"]["name"]}
                or type(next(iter(networks.values()))) is not dict
                or next(iter(networks.values())).get("NetworkID") != ctx["owner"]["network"]["id"]):
            _fail("INTEGRITY_FAILED", "container_network_mismatch")
        if any(type(value.get(key)) is not bool for key in ("Running", "OOMKilled", "Paused", "Restarting", "Dead")):
            _fail("RECOVERY_REQUIRED", "invalid_container_state")
        if value["Paused"] or value["Restarting"] or value["Dead"] or value.get("Status") not in {"created", "running", "exited"}:
            _fail("RECOVERY_REQUIRED", "container_requires_manual_reconciliation")
        if value["Running"] != (value["Status"] == "running") or type(value.get("ExitCode")) is not int:
            _fail("RECOVERY_REQUIRED", "contradictory_container_state")
        # Docker may retain empty exposed-port entries; an actual binding is exact.
        ports = value.get("Ports") or {}
        if type(ports) is not dict:
            _fail("INTEGRITY_FAILED", "invalid_live_port_bindings")
        bound = {key: val for key, val in ports.items() if val}
        if bound != (spec["ports"] if value["Running"] else {}):
            _fail("INTEGRITY_FAILED", "live_port_binding_mismatch")
        return value

    def _foreign_writers(self, ctx):
        raw = self._ctx_run(ctx, ("container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"))
        try:
            ids = raw.decode("ascii").splitlines()
        except UnicodeError:
            _fail("RECOVERY_REQUIRED", "invalid_container_inventory")
        if len(ids) > 256 or len(set(ids)) != len(ids) or any(_HEX.fullmatch(cid) is None for cid in ids):
            _fail("RECOVERY_REQUIRED", "container_inventory_limit_or_ambiguity")
        owned = set(ctx["owner"]["containers"].values())
        if not owned <= set(ids):
            _fail("RECOVERY_REQUIRED", "owned_container_missing_from_inventory")
        mounts = _mount_table()
        protected = []
        for name, root in ctx["home"].roots.items():
            protected.append((name, *_physical_region(root, mounts)))
            for device, fs_root, mountpoint in mounts:
                if contains(root, mountpoint):
                    protected.append((name, device, fs_root))
        conflicts = []
        for cid in sorted(set(ids) - owned):
            value = _json(self._ctx_run(ctx, ("container", "inspect", "--format", FOREIGN_FORMAT, "--", cid)))
            if value.get("Id") != cid or type(value.get("Mounts")) is not list:
                _fail("RECOVERY_REQUIRED", "foreign_mount_inventory_unavailable")
            for mount in value["Mounts"]:
                if type(mount) is not dict:
                    _fail("RECOVERY_REQUIRED", "invalid_foreign_mount")
                if type(mount.get("RW")) is not bool:
                    _fail("RECOVERY_REQUIRED", "foreign_mount_write_access_unknown")
                if mount["RW"] is not True:
                    continue
                if mount.get("Type") != "bind":
                    # A local volume driver can alias a managed directory using
                    # mount options; external drivers need an audited adapter.
                    conflicts.append({"container_id": cid, "root": "unresolved_volume_alias"})
                    continue
                source = checked_path(mount.get("Source", ""))
                device, physical = _physical_region(source, mounts)
                for name, root in ctx["home"].roots.items():
                    overlap = any(region_name == name and region_device == device
                        and (contains(region, physical) or contains(physical, region))
                        for region_name, region_device, region in protected)
                    if contains(source, root) or contains(root, source) or overlap:
                        conflicts.append({"container_id": cid, "root": name})
        return conflicts

    def _snapshot(self, ctx):
        # Callable re-verifies signed bytes on every mutation boundary; ownership
        # is never inferred merely from a container name or one label.
        verified = load_verified_docker_contract(ctx["manifest"], ctx["target"], ctx["options"], verifier=self.verifier)
        if verified != ctx["contract"] or _file_digest(ctx["control"] / "docker-installation.json") != ctx["receipt_digest"]:
            _fail("PLAN_STALE", "signed_contract_or_local_receipt_changed")
        daemon = self._daemon(ctx["target"], ctx)
        if daemon["ID"] != ctx["owner"]["daemon_id"]:
            _fail("OPERATION_CONFLICT", "docker_daemon_identity_changed")
        network = _json(self._ctx_run(ctx, ("network", "inspect", "--format", NETWORK_FORMAT, "--", ctx["owner"]["network"]["id"])))
        expected = ctx["spec"]["network"]
        labels, members = network.get("Labels") or {}, network.get("Containers") or {}
        if (network.get("Id") != ctx["owner"]["network"]["id"] or network.get("Name") != expected["name"]
                or network.get("Driver") != "bridge" or network.get("Internal") is not True or network.get("Scope") != "local"
                or network.get("Options") or type(labels) is not dict
                or any(labels.get(k) != v for k, v in expected["labels"].items())
                or any(key.startswith(LABEL_PREFIX) and key not in expected["labels"] for key in labels)
                or type(members) is not dict or set(members) - set(ctx["owner"]["containers"].values())):
            _fail("OPERATION_CONFLICT", "network_ownership_or_isolation_mismatch")
        values = {name: self._container(ctx, name) for name in ctx["spec"]["order"]}
        for name, value in values.items():
            if value["Running"] and ctx["owner"]["containers"][name] not in members:
                _fail("INTEGRITY_FAILED", "running_container_network_membership_missing")
        conflicts = self._foreign_writers(ctx)
        public = {name: {"container_id": value["Id"], "running": value["Running"], "state": value["Status"],
            "exit_code": value["ExitCode"], "health": value.get("Health"), "started_at": value.get("StartedAt"),
            "finished_at": value.get("FinishedAt"), "restart_count": value.get("RestartCount")}
            for name, value in values.items()}
        fingerprint = {"root": str(ctx["root"]), "owner": _owner_id(), "receipt_sha256": ctx["receipt_digest"],
            "manifest_sha256": hash_manifest(ctx["manifest"]), "contract_sha256": ctx["contract"].contract_sha256,
            "daemon_id": daemon["ID"], "endpoint_sha256": ctx["owner"]["endpoint_sha256"],
            "host_namespaces": daemon["host_namespaces"],
            "components": public, "foreign_writers": conflicts}
        return {"fingerprint_data": fingerprint, "components": public, "raw": values,
                "foreign_writers": conflicts, "daemon_id": daemon["ID"]}

    def discover(self, manifest, target, options):
        ctx = self._context(manifest, target, options)
        snapshot = self._snapshot(ctx)
        pending = []
        folder = ctx["control"] / "docker-operations"
        if folder.exists():
            _private_existing(folder, directory=True)
            for file in sorted(folder.glob("*.json")):
                _private_existing(file, directory=False)
                record = _read(file)
                if record.get("status") != "SUCCEEDED":
                    pending.append({"operation": file.stem, "action": record.get("action"), "status": record.get("status"), "phase": record.get("phase")})
        return {key: value for key, value in snapshot.items() if key != "raw"} | {
            "installation": {"installation_id": ctx["owner"]["installation_id"], "release": ctx["contract"].release,
                             "mode": "docker", "provisioning_external": True},
            "integrity": {"state": "VERIFIED", "scope": "signed_descriptor_and_daemon_attested_images"},
            "unfinished_operations": pending, "read_only": True, "production_ready": False}

    status = discover

    def plan(self, action, manifest, target, inventory, options):
        ctx = self._context(manifest, target, options)
        if action not in self.EXECUTABLE_ACTIONS:
            _fail("CAPABILITY_UNSUPPORTED", "docker_provisioning_lifecycle_not_implemented")
        if action != "stop" and inventory.get("foreign_writers"):
            _fail("OPERATION_CONFLICT", "foreign_data_writers_present")
        if action == "backup":
            self._password_reference(ctx)
        phases = {"start": ["verify", "start", "health"], "stop": ["verify", "stop"],
                  "backup": ["verify", "stop", "backup", "start", "health"]}[action]
        return {"steps": [{"id": phase, "component_id": "product", "weight": 1} for phase in phases],
            "resources": [str(ctx["root"]), "docker-daemon:" + ctx["owner"]["daemon_id"], str(ctx["work"])],
            "required_consents": ["action:" + action], "scope": "managed-container-controller",
            "resource_fingerprint": _digest(inventory["fingerprint_data"]),
            "preserves_user_data": True, "backup_strategy": "filesystem_quiesced", "backup_contains_images": False,
            "provisioning_implemented": False, "production_ready": False,
            "legacy_commands_not_executed": ["backup.cmd", "restore.cmd", "migrations.cmd"]}

    def _password_reference(self, ctx):
        ref = ctx["options"].get("backup_password_ref")
        if type(ref) is not str or _REF.fullmatch(ref) is None or self.secret_provider is None:
            _fail("PREREQUISITE_MISSING", "backup_secret_provider_required")
        return ref

    def _password(self, ctx):
        ref = self._password_reference(ctx)
        try:
            password = self.secret_provider(ref)
        except Exception:
            _fail("PREREQUISITE_MISSING", "backup_secret_unavailable")
        if type(password) is not str or len(password) < 12:
            _fail("PREREQUISITE_MISSING", "backup_secret_too_short")
        return lambda: password

    def _save(self, ctx, state):
        _write(ctx["journal"], state)

    def _cleanly_stopped(self, value):
        if value["Running"] or value["OOMKilled"] or value.get("Error") or value["ExitCode"] != 0:
            _fail("RECOVERY_REQUIRED", "graceful_shutdown_not_confirmed")

    def _stop(self, ctx, state):
        for name in reversed(ctx["spec"]["order"]):
            ctx["emit"]({"phase": "stop", "component_id": name, "status": "RUNNING", "completed": 0, "total": None})
            value = self._snapshot(ctx)["raw"][name]
            key = "stop:" + name
            if not value["Running"]:
                self._cleanly_stopped(value)
                state["effects"][key] = "CONFIRMED"
                self._save(ctx, state)
                ctx["emit"]({"phase": "stop", "component_id": name, "status": "SUCCEEDED", "completed": 1, "total": 1})
                continue
            if state["effects"].get(key) == "CONFIRMED":
                _fail("RECOVERY_REQUIRED", "container_restarted_outside_operation")
            if key not in state["effects"]:
                state["effects"][key] = "INTENT"
                self._save(ctx, state)
                # docker stop would escalate to SIGKILL at its timeout. We send
                # one explicit SIGTERM and independently await a clean exit.
                try:
                    self._ctx_run(ctx, ("container", "kill", "--signal=SIGTERM", "--", value["Id"]))
                except Exception:
                    # Reconcile observed state; do not repeat uncertain RPC.
                    pass
            deadline = time.monotonic() + ctx["spec"]["components"][name]["stop_timeout_seconds"]
            while True:
                value = self._container(ctx, name)
                if not value["Running"]:
                    self._cleanly_stopped(value)
                    state["effects"][key] = "CONFIRMED"
                    self._save(ctx, state)
                    ctx["emit"]({"phase": "stop", "component_id": name, "status": "SUCCEEDED", "completed": 1, "total": 1})
                    break
                if time.monotonic() >= deadline:
                    _fail("RECOVERY_REQUIRED", "graceful_shutdown_timeout_no_force_kill")
                time.sleep(0.1)

    def _start(self, ctx, state, names):
        for name in ctx["spec"]["order"]:
            if name not in names:
                continue
            ctx["emit"]({"phase": "start", "component_id": name, "status": "RUNNING", "completed": 0, "total": None})
            snapshot = self._snapshot(ctx)
            if snapshot["foreign_writers"]:
                _fail("OPERATION_CONFLICT", "foreign_data_writers_present")
            value, key = snapshot["raw"][name], "start:" + name
            if value["Running"]:
                state["effects"][key] = "CONFIRMED"
                self._save(ctx, state)
                ctx["emit"]({"phase": "start", "component_id": name, "status": "SUCCEEDED", "completed": 1, "total": 1})
                continue
            if key in state["effects"]:
                _fail("RECOVERY_REQUIRED", "uncertain_start_not_replayed")
            self._cleanly_stopped(value)
            state["effects"][key] = "INTENT"
            self._save(ctx, state)
            try:
                self._ctx_run(ctx, ("container", "start", "--", value["Id"]))
            except Exception:
                pass
            value = self._container(ctx, name)
            if not value["Running"]:
                _fail("RECOVERY_REQUIRED", "container_start_not_confirmed")
            state["effects"][key] = "CONFIRMED"
            self._save(ctx, state)
            ctx["emit"]({"phase": "start", "component_id": name, "status": "SUCCEEDED", "completed": 1, "total": 1})

    def _health_once(self, ctx):
        before = self._snapshot(ctx)
        if before["foreign_writers"]:
            _fail("OPERATION_CONFLICT", "foreign_data_writers_present")
        for name, value in before["raw"].items():
            if not value["Running"] or value["OOMKilled"] or value.get("Error"):
                _fail("HEALTH_CHECK_FAILED", "owned_component_not_running")
            if ctx["spec"]["components"][name]["require_docker_health"] and value.get("Health") != "healthy":
                _fail("HEALTH_CHECK_FAILED", "docker_health_not_ready")
        web = ctx["contract"].document["web"]
        cid = ctx["owner"]["containers"][web["component"]]
        challenge = secrets.token_hex(32)
        direct = _json(self._ctx_run(ctx, ("container", "exec", "--", cid, *web["probe_cmd"], "--challenge", challenge),
                                 timeout=10, code="HEALTH_CHECK_FAILED"), maximum=16384)
        url = "http://127.0.0.1:" + str(ctx["manifest"]["web"]["port"]) + ctx["manifest"]["web"]["healthz"]
        try:
            remaining = ctx.get("_health_deadline", time.monotonic() + 10) - time.monotonic()
            if remaining <= 0:
                _fail("HEALTH_CHECK_FAILED", "health_wait_timeout")
            via_port = self.fetcher(url, challenge, min(10, remaining))
        except Exception:
            _fail("HEALTH_CHECK_FAILED", "health_http_unavailable")
        expected = {"product_id": ctx["manifest"]["product_id"], "installation_id": ctx["owner"]["installation_id"],
                    "release": ctx["contract"].release, "ready": True, "challenge": challenge}
        for value in (direct, via_port):
            if (type(value) is not dict or set(value) != {*expected, "proof"}
                    or any(value.get(k) != v for k, v in expected.items()) or value.get("ready") is not True
                    or type(value.get("proof")) is not str or _HEX.fullmatch(value["proof"]) is None):
                _fail("HEALTH_CHECK_FAILED", "health_identity_challenge_mismatch")
        if not hmac.compare_digest(direct["proof"], via_port["proof"]):
            _fail("HEALTH_CHECK_FAILED", "published_port_is_not_owned_web_listener")
        after = self._snapshot(ctx)
        if after["fingerprint_data"] != before["fingerprint_data"]:
            _fail("HEALTH_CHECK_FAILED", "container_identity_changed_during_health")
        return {"status": "HEALTHY", "url": url, "components": after["components"],
                "proof_model": "private_server_instance_challenge", "production_ready": False}

    def _health(self, ctx):
        bounded = ctx | {"_health_deadline": time.monotonic() + ctx["manifest"]["update"]["health_wait"]}
        while True:
            try:
                return self._health_once(bounded)
            except EngineError as error:
                if error.code != "HEALTH_CHECK_FAILED":
                    raise
                remaining = bounded["_health_deadline"] - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.2, remaining))

    def health(self, manifest, target, options):
        return self._health(self._context(manifest, target, options))

    def _work(self, ctx):
        work = ctx["work"]
        marker = work / "docker-owner.json"
        identity = {"schema_version": 1, "installation_id": ctx["owner"]["installation_id"], "owner": _owner_id(),
                    "daemon_id": ctx["owner"]["daemon_id"]}
        if work.exists():
            _private_existing(work, directory=True)
            if not marker.exists() or _read(_private_existing(marker, directory=False)) != identity:
                _fail("OPERATION_CONFLICT", "backup_work_owned_by_another_installation")
        else:
            work.mkdir(mode=0o700)
            _write(marker, identity)
        return work

    def _backup_preflight(self, ctx, state):
        work = ctx["work"]
        if work.exists():
            # Validate existing ownership without creating/changing directories.
            self._work(ctx)
        elif not checked_path(work.parent).is_dir():
            _fail("INVALID_MANIFEST", "backup_work_parent_missing")
        path = checked_path(ctx["options"].get("archive_path", str(work / ("data-" + ctx["token"] + ".haes"))))
        if path.parent != work or path == work / "docker-owner.json":
            _fail("INVALID_MANIFEST", "archive_requires_owned_work_directory")
        if path.exists() and state.get("archive_intent", {}).get("path") != str(path):
            _fail("OPERATION_CONFLICT", "archive_destination_exists")

    def _data_fingerprint(self, ctx):
        directories, files, _ = archive_inventory(ctx["home"])
        # Hard-link aliases outside DataHome could remain writable by a foreign
        # process/container while all lexical managed paths appear quiesced.
        for entry in files:
            _, root, *relative = entry["path"].split("/")
            if (ctx["home"].roots[root] / Path(*relative)).stat().st_nlink != 1:
                _fail("RECOVERY_REQUIRED", "hardlinked_user_data_requires_storage_adapter")
        return _digest({"roots": sorted(ctx["home"].roots), "directories": directories, "files": files})

    def _backup(self, ctx, state, password):
        snapshot = self._snapshot(ctx)
        if snapshot["foreign_writers"]:
            _fail("OPERATION_CONFLICT", "foreign_data_writers_present")
        for value in snapshot["raw"].values():
            self._cleanly_stopped(value)
        work = self._work(ctx)
        path = checked_path(ctx["options"].get("archive_path", str(work / ("data-" + ctx["token"] + ".haes"))))
        if path.parent != work or path == work / "docker-owner.json":
            _fail("INVALID_MANIFEST", "archive_requires_owned_work_directory")
        if "archive_intent" not in state:
            if path.exists():
                _fail("OPERATION_CONFLICT", "archive_destination_exists")
            state["archive_intent"] = {"path": str(path), "data_fingerprint": self._data_fingerprint(ctx)}
            self._save(ctx, state)
        intent = state["archive_intent"]
        if intent["path"] != str(path) or intent["data_fingerprint"] != self._data_fingerprint(ctx):
            _fail("RECOVERY_REQUIRED", "data_changed_during_interrupted_backup")
        if not path.exists():
            archive_crypto.build_encrypted_archive(ctx["home"], path, password_provider=password)
        if not archive_crypto.is_encrypted(path):
            _fail("INTEGRITY_FAILED", "encrypted_archive_required")
        manifest = archive_crypto.verify_archive(path, password_provider=password, expected_product_id=ctx["manifest"]["product_id"])
        if _digest({k: manifest[k] for k in ("roots", "directories", "files")}) != intent["data_fingerprint"]:
            _fail("INTEGRITY_FAILED", "archive_snapshot_does_not_match_operation")
        after = self._snapshot(ctx)
        if after["foreign_writers"] or any(value["Running"] for value in after["raw"].values()):
            _fail("RECOVERY_REQUIRED", "writers_appeared_during_backup")
        if self._data_fingerprint(ctx) != intent["data_fingerprint"]:
            _fail("RECOVERY_REQUIRED", "data_changed_during_backup")
        return {"archive_path": str(path), "archive_sha256": _file_digest(path), "encrypted": True,
                "contains_images": False, "release": ctx["contract"].release,
                "contract_sha256": ctx["contract"].contract_sha256, "restoration_supported": False}

    def _verify_completed_backup(self, ctx, state, password):
        result = state.get("results", {}).get("backup")
        intent = state.get("archive_intent")
        if type(result) is not dict or type(intent) is not dict:
            _fail("RECOVERY_REQUIRED", "completed_backup_receipt_missing")
        path = checked_path(result.get("archive_path", ""))
        work = self._work(ctx)
        if (path.parent != work or str(path) != intent.get("path") or not path.is_file()
                or result.get("archive_sha256") != _file_digest(path) or not archive_crypto.is_encrypted(path)
                or result.get("release") != ctx["contract"].release
                or result.get("contract_sha256") != ctx["contract"].contract_sha256):
            _fail("INTEGRITY_FAILED", "completed_backup_changed_or_missing")
        _private_existing(path, directory=False)
        manifest = archive_crypto.verify_archive(path, password_provider=password, expected_product_id=ctx["manifest"]["product_id"])
        if _digest({k: manifest[k] for k in ("roots", "directories", "files")}) != intent.get("data_fingerprint"):
            _fail("INTEGRITY_FAILED", "completed_backup_snapshot_mismatch")
        return result

    def execute(self, plan, emit=lambda event: None, cancel_requested=lambda: False):
        action = plan.get("action")
        if action not in self.EXECUTABLE_ACTIONS:
            _fail("CAPABILITY_UNSUPPORTED", "docker_provisioning_lifecycle_not_implemented")
        ctx = self._context(plan["source_manifest"], plan["target"], plan["options"])
        password = self._password(ctx) if action == "backup" else None
        plan_id = plan.get("plan_id")
        if type(plan_id) is not str or not 1 <= len(plan_id) <= 256:
            _fail("INVALID_MANIFEST", "durable_plan_id_required")
        token = hashlib.sha256(plan_id.encode()).hexdigest()
        ctx["token"] = token
        lock = checked_path(ctx["control"] / "docker.lock")
        with operation_lock(lock):
            # Root lock is independent of the public API's state directory.
            ctx = self._context(plan["source_manifest"], plan["target"], plan["options"]) | {"token": token, "emit": emit}
            folder = ctx["control"] / "docker-operations"
            if folder.exists():
                _private_existing(folder, directory=True)
            else:
                folder.mkdir(mode=0o700)
            ctx["journal"] = folder / (token + ".json")
            binding = _digest({"action": action, "manifest": hash_manifest(ctx["manifest"]),
                "target": ctx["target"], "options": ctx["options"], "receipt": ctx["receipt_digest"],
                "engine_plan": plan.get("engine_plan")})
            for file in folder.glob("*.json"):
                _private_existing(file, directory=False)
                if file != ctx["journal"] and _read(file).get("status") != "SUCCEEDED":
                    _fail("OPERATION_CONFLICT", "unfinished_docker_operation_requires_resume")
            snapshot = self._snapshot(ctx)
            if ctx["journal"].exists():
                state = _read(_private_existing(ctx["journal"], directory=False))
                if state.get("binding") != binding or state.get("action") != action:
                    _fail("OPERATION_CONFLICT", "durable_docker_plan_mismatch")
                if state.get("host_namespaces") != snapshot["fingerprint_data"]["host_namespaces"]:
                    _fail("RECOVERY_REQUIRED", "host_namespace_changed_during_operation")
                if state.get("status") == "SUCCEEDED":
                    if action == "backup":
                        self._verify_completed_backup(ctx, state, password)
                    return state["result"]
            else:
                if plan.get("engine_plan", {}).get("resource_fingerprint") != _digest(snapshot["fingerprint_data"]):
                    _fail("PLAN_STALE", "docker_resource_state_changed")
                if action != "stop" and snapshot["foreign_writers"]:
                    _fail("OPERATION_CONFLICT", "foreign_data_writers_present")
                state = {"schema_version": 1, "action": action, "binding": binding, "status": "RUNNING", "effects": {},
                    "completed": [], "results": {}, "initially_running": [n for n, v in snapshot["raw"].items() if v["Running"]],
                    "host_namespaces": snapshot["fingerprint_data"]["host_namespaces"]}
                self._save(ctx, state)

            def phase(name, function):
                completed = name in state["completed"]
                if completed and name != "health":
                    return state["results"].get(name)
                if cancel_requested():
                    raise _Canceled()
                state.update(status="RUNNING", phase=name)
                self._save(ctx, state)
                emit({"phase": name, "status": "RUNNING", "completed": 0, "total": None})
                result = function()
                if not completed:
                    state["completed"].append(name)
                if isinstance(result, dict):
                    state["results"][name] = result
                self._save(ctx, state)
                emit({"phase": name, "status": "SUCCEEDED", "completed": 1, "total": 1})
                return result

            try:
                if action == "backup":
                    self._backup_preflight(ctx, state)
                phase("verify", lambda: None)
                if action in {"stop", "backup"}:
                    phase("stop", lambda: self._stop(ctx, state))
                archive = phase("backup", lambda: self._backup(ctx, state, password)) if action == "backup" else None
                if archive:
                    self._verify_completed_backup(ctx, state, password)
                if action in {"start", "backup"}:
                    names = ctx["spec"]["order"] if action == "start" else state["initially_running"]
                    phase("start", lambda: self._start(ctx, state, names))
                    if ctx["contract"].document["web"]["component"] in names:
                        health = phase("health", lambda: self._health(ctx))
                    else:
                        health = phase("health", lambda: {"status": "NOT_RUNNING", "initial_state_restored": True})
                else:
                    health = {"status": "STOPPED"}
                # Durable completion records an observation, not a guarantee
                # that old cached process/health state survived a host crash.
                observed = self._snapshot(ctx)
                expected_running = (set(ctx["spec"]["order"]) if action == "start" else
                                    set(state["initially_running"]) if action == "backup" else set())
                if {name for name, value in observed["raw"].items() if value["Running"]} != expected_running:
                    _fail("RECOVERY_REQUIRED", "final_container_state_differs_from_operation")
                for name, value in observed["raw"].items():
                    if name not in expected_running:
                        self._cleanly_stopped(value)
                    elif value["OOMKilled"] or value.get("Error"):
                        _fail("HEALTH_CHECK_FAILED", "owned_component_failed_before_completion")
                    elif ctx["spec"]["components"][name]["require_docker_health"] and value.get("Health") != "healthy":
                        _fail("HEALTH_CHECK_FAILED", "docker_health_not_ready")
                result = {"status": "SUCCEEDED", "action": action, "health": health,
                    "production_ready": False, "backup_strategy": "filesystem_quiesced", "preserves_user_data": True}
                if archive:
                    result["backup"] = archive
                if health and health.get("url"):
                    result["url"] = health["url"]
                state.update(status="SUCCEEDED", result=result)
                self._save(ctx, state)
                return result
            except _Canceled:
                result = {"status": "CANCELED", "resume_required": True, "phase": state.get("phase"), "production_ready": False}
                state.update(status="CANCELED", result=result)
                self._save(ctx, state)
                return result
            except Exception as error:
                state.update(status="RECOVERY_REQUIRED", error_code=getattr(error, "code", "RECOVERY_REQUIRED"))
                self._save(ctx, state)
                raise

    def resume(self, plan, previous_operation, emit=lambda event: None, cancel_requested=lambda: False):
        return self.execute(plan, emit, cancel_requested)
