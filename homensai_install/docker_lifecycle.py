# HomenS.AI · © 2026 Serhii Khomenko
"""Фазы Docker provisioning для единственного executor в DockerEngine.

Здесь нет отдельного orchestrator, shell или фонового restart loop. Методы
используют тот же lock/journal, verifier, daemon attestation и health adapter.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from .docker_commands import container_create, network_create, provisioning_arguments
from .docker_cli import _arguments
from .docker_contract import load_verified_docker_contract, resource_plan
from .engine import EngineError, _digest, _file_digest, _owner_id, _read
from .trust import canonical_bytes, hash_manifest
from shared.adapters_fs import _tree
from shared.datahome import checked_path, contains


_HEX = re.compile(r"[0-9a-f]{64}\Z")
CONTAINER_LIST_FORMAT = '{"Id":{{json .ID}},"Name":{{json .Names}}}'
NETWORK_LIST_FORMAT = '{"Id":{{json .ID}},"Name":{{json .Name}}}'
NETWORK_CONSUMER_FORMAT = '{"Id":{{json .Id}},"NetworkMode":{{json .HostConfig.NetworkMode}},"Networks":{{json .NetworkSettings.Networks}}}'


def _fail(code, reason):
    raise EngineError(code, "Docker lifecycle отклонил операцию", details={"reason": reason})


def _sync(path):
    descriptor = os.open(checked_path(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(path):
    info = checked_path(path).stat()
    return {"device": info.st_dev, "inode": info.st_ino}


class DockerLifecycle:
    """Файловые и daemon-фазы, вызываемые общим DockerEngine.execute."""

    @staticmethod
    def _loopback_engine_supported(daemon):
        version = daemon.get("ServerVersion")
        parsed = re.fullmatch(r"([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(?:[-+][A-Za-z0-9_.-]+)?", version) if type(version) is str else None
        return bool(parsed and tuple(map(int, parsed.groups())) >= (28, 0, 0))

    def _require_loopback_engine(self, daemon):
        # Docker Docs: до 28.0.0 localhost publish доступен соседям L2.
        # Stop/uninstall остаются возможными на старом daemon.
        if not self._loopback_engine_supported(daemon):
            _fail("CAPABILITY_UNSUPPORTED", "docker_engine_28_required_for_loopback_publish")

    def _durable_json(self, path, value):
        path = checked_path(path)
        self._private_path(path.parent, directory=True)
        if path.exists():
            self._private_path(path, directory=False)
        temporary = path.with_name(".docker-write-" + uuid.uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_bytes(value) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_contract_source(self, manifest, target, options):
        """Исчезнувший cache заменяют только сохранённые подписанные bytes.

        Options reviewed plan не меняются: retained paths являются внутренним
        источником проверки, а не способом обойти plan/receipt binding. Если
        исходные файлы существуют, их неверная подпись/подмена не маскируется.
        """
        try:
            return load_verified_docker_contract(manifest, target, options, verifier=self.verifier), dict(options)
        except Exception as failure:
            paths = [checked_path(options.get(key, "")) for key in ("payload_dir", "inventory_file", "signature_file")]
            if not any(not path.exists() for path in paths):
                raise
            root = checked_path(options.get("install_root", ""))
            control, release = root / ".hinstall", options.get("release")
            if (not control.exists() or type(release) is not str
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", release) is None):
                raise
            self._private_path(control, directory=True)
            recovery = control / "docker-recovery"
            if not recovery.exists():
                raise
            self._private_path(recovery, directory=True)
            entries = list(recovery.iterdir())
            if len(entries) > 128:
                _fail("RECOVERY_REQUIRED", "retained_release_inventory_limit")
            matches = []
            for directory in entries:
                if re.fullmatch(re.escape(release) + r"-[0-9a-f]{16}", directory.name) is None:
                    continue
                self._private_path(directory, directory=True)
                metadata_path = directory / "recovery.json"
                if not metadata_path.exists():
                    continue
                metadata = _read(self._private_path(metadata_path, directory=False))
                if (type(metadata) is not dict or set(metadata) != {"schema_version", "backend", "manifest", "target", "options", "contains_images", "images_retained_in_daemon"}
                        or metadata["schema_version"] != 1 or metadata["backend"] != "docker-managed"
                        or metadata["target"] != target or hash_manifest(metadata["manifest"]) != hash_manifest(manifest)
                        or metadata["contains_images"] is not False or metadata["images_retained_in_daemon"] is not True):
                    _fail("INTEGRITY_FAILED", "retained_release_metadata_mismatch")
                declared = metadata["options"]
                expected_paths = {"payload_dir": str(directory / "payload"), "inventory_file": str(directory / "inventory.json"),
                                  "signature_file": str(directory / "signature.json")}
                if (type(declared) is not dict or set(declared) != {"install_root", "payload_dir", "release", "runtime_executable", "modes", "work_dir", "inventory_file", "signature_file"}
                        or any(declared.get(key) != value for key, value in expected_paths.items())
                        or declared["install_root"] != str(root) or declared["release"] != release
                        or declared["runtime_executable"] != options.get("runtime_executable")
                        or ("modes" in options and options["modes"] != declared["modes"])):
                    _fail("INTEGRITY_FAILED", "retained_release_paths_not_owned")
                selected = dict(options) | expected_paths
                contract = load_verified_docker_contract(manifest, target, selected, verifier=self.verifier)
                if directory.name != release + "-" + contract.contract_sha256[:16]:
                    _fail("INTEGRITY_FAILED", "retained_release_contract_identity_mismatch")
                matches.append((contract, selected))
            if not matches:
                raise failure
            if len(matches) != 1:
                _fail("RECOVERY_REQUIRED", "ambiguous_retained_release_requires_explicit_selection")
            return matches[0]

    def _install_context(self, manifest, target, options, installation_id=None):
        ctx = self._layout_context(manifest, target, options)
        installation_id = installation_id or "0" * 32
        modes = options.get("modes")
        if modes is None and len(ctx["manifest"]["modes"]) == 1:
            modes = [ctx["manifest"]["modes"][0]["name"]]
        if type(modes) is not list or not modes or any(type(mode) is not str for mode in modes):
            _fail("INVALID_MANIFEST", "explicit_docker_profile_required")
        spec = resource_plan(ctx["contract"], install_root=str(ctx["root"]), installation_id=installation_id, modes=modes)
        owner = {"schema_version": 2, "mode": "docker", "product_id": ctx["manifest"]["product_id"],
            "installation_id": installation_id, "owner": _owner_id(), "daemon_id": None,
            "endpoint_sha256": hashlib.sha256(self.runner.endpoint.encode()).hexdigest(),
            "release": ctx["contract"].release, "manifest_sha256": hash_manifest(ctx["manifest"]),
            "contract_sha256": ctx["contract"].contract_sha256, "payload_sha256": ctx["contract"].payload_sha256,
            "modes": list(modes), "containers": {},
            "network": {"id": None, "name": spec["network"]["name"]}}
        return ctx | {"owner": owner, "spec": spec, "receipt_digest": None}

    def _fresh_root(self, ctx):
        root, control = ctx["root"], ctx["control"]
        parent = checked_path(root.parent)
        if not parent.is_dir():
            _fail("PREREQUISITE_MISSING", "install_parent_directory_required")
        for ancestor in (parent, *parent.parents):
            info = ancestor.stat()
            if info.st_uid not in {0, os.getuid()} or (stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX):
                _fail("PERMISSION_REQUIRED", "install_parent_not_protected")
        if not root.exists():
            return
        self._private_path(root, directory=True)
        if any(child != control for child in root.iterdir()):
            _fail("OPERATION_CONFLICT", "fresh_install_requires_empty_owned_root")
        if control.exists():
            self._private_path(control, directory=True)
            # Только пустая bootstrap-граница: до durable journal ни data dirs,
            # ни daemon resources создавать нельзя. Crash здесь безопасно
            # возобновляется тем же reviewed plan.
            for child in control.iterdir():
                if child.name == "docker.lock":
                    self._private_path(child, directory=False)
                elif child.name == "docker-operations":
                    self._private_path(child, directory=True)
                    entries = list(child.iterdir())
                    if len(entries) > 256:
                        _fail("RECOVERY_REQUIRED", "bootstrap_temporary_limit")
                    for entry in entries:
                        if re.fullmatch(r"\.docker-write-[0-9a-f]{32}\.tmp", entry.name) is None:
                            _fail("OPERATION_CONFLICT", "unfinished_docker_operation_requires_resume")
                        self._private_path(entry, directory=False)
                    # Непубликованный первый journal не разрешает RPC. Его
                    # private temporary можно оставить для диагностики; он не
                    # считается receipt и не даёт права усыновить resource.
                else:
                    _fail("OPERATION_CONFLICT", "existing_control_metadata_requires_owned_resume")

    def _install_user(self, spec):
        if spec["user"] != str(os.getuid()) + ":" + str(os.getgid()):
            _fail("CAPABILITY_UNSUPPORTED", "private_bind_mount_uid_gid_must_match_installer")

    def _install_inputs(self, ctx, *, require_terms=True):
        locale = ctx["options"].get("eula_locale", "en")
        digest = ctx["options"].get("eula_digest")
        if require_terms and (locale not in {"ru", "en", "de"} or type(digest) is not str or not _HEX.fullmatch(digest)
                or dict(ctx["contract"].files).get("legal/" + locale + ".txt") != digest):
            _fail("CONSENT_REQUIRED", "shown_terms_must_match_signed_locale_bytes")
        images = {}
        for name, spec in ctx["spec"]["components"].items():
            self._install_user(spec)
            for mount in spec["mounts"]:
                if any(char in mount["Source"] for char in (",", '"', "=")):
                    _fail("CAPABILITY_UNSUPPORTED", "bind_path_not_representable_in_strict_docker_argv")
            if not provisioning_arguments(container_create(spec)):
                _fail("CAPABILITY_UNSUPPORTED", "container_spec_not_representable_in_strict_docker_argv")
            _arguments(container_create(spec))
            image = self._image(ctx, spec)
            if image["Config"].get("Volumes") or image["Config"].get("OnBuild"):
                _fail("CAPABILITY_UNSUPPORTED", "implicit_image_volumes_or_onbuild_not_supported")
            images[name] = _digest(image)
        return images

    def _fresh_snapshot(self, ctx):
        self._fresh_root(ctx)
        daemon = self._daemon(ctx["target"], ctx)
        self._require_loopback_engine(daemon)
        images = self._install_inputs(ctx, require_terms=False)
        conflicts = self._foreign_writers(ctx)
        fingerprint = {"root": str(ctx["root"]), "owner": _owner_id(), "installation": None,
            "manifest_sha256": hash_manifest(ctx["manifest"]), "contract_sha256": ctx["contract"].contract_sha256,
            "payload_sha256": ctx["contract"].payload_sha256,
            "daemon_id": daemon["ID"], "endpoint_sha256": ctx["owner"]["endpoint_sha256"],
            "host_namespaces": daemon["host_namespaces"], "images": images, "foreign_writers": conflicts}
        return {"fingerprint_data": fingerprint, "daemon_id": daemon["ID"], "foreign_writers": conflicts,
                "components": {}, "raw": {}}

    def _bootstrap_install(self, ctx):
        self._fresh_root(ctx)
        for directory in (ctx["root"], ctx["control"]):
            if not directory.exists():
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                _sync(directory.parent)
            self._private_path(directory, directory=True)

    def _resource_inventory(self, ctx, kind):
        if kind == "container":
            command = ("container", "ls", "--all", "--no-trunc", "--format", CONTAINER_LIST_FORMAT)
        else:
            command = ("network", "ls", "--no-trunc", "--format", NETWORK_LIST_FORMAT)
        rows = self._ctx_run(ctx, command).splitlines()
        if len(rows) > 256:
            _fail("RECOVERY_REQUIRED", "resource_inventory_limit")
        result, ids = [], set()
        for row in rows:
            value = self._decode(row)
            if (set(value) != {"Id", "Name"} or type(value["Id"]) is not str or not _HEX.fullmatch(value["Id"])
                    or value["Id"] in ids or type(value["Name"]) is not str
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", value["Name"]) is None):
                _fail("RECOVERY_REQUIRED", "resource_inventory_ambiguous")
            result.append(value)
            ids.add(value["Id"])
        return result

    def _lifecycle_binding(self, ctx, state):
        verified, source_options = self._load_contract_source(ctx["manifest"], ctx["target"], ctx["options"])
        if verified != ctx["contract"]:
            _fail("PLAN_STALE", "signed_contract_changed_during_operation")
        if state.get("payload_sha256") != verified.payload_sha256:
            _fail("PLAN_STALE", "durable_full_payload_pin_changed")
        ctx["verification_options"] = source_options
        self._private_path(ctx["root"], directory=True)
        self._private_path(ctx["control"], directory=True)
        for relative, record in state.get("directories", {}).items():
            path = checked_path(ctx["root"] / relative)
            if not contains(ctx["root"], path):
                _fail("RECOVERY_REQUIRED", "invalid_directory_receipt")
            if record.get("status") == "CONFIRMED":
                self._private_path(path, directory=True)
                if _identity(path) != {key: record.get(key) for key in ("device", "inode")}:
                    _fail("PLAN_STALE", "managed_directory_replaced")
        receipt = ctx["control"] / "docker-installation.json"
        if state["action"] == "install":
            if receipt.exists():
                self._private_path(receipt, directory=False)
                if _read(receipt) != state["owner"] or set(state["owner"]["containers"]) != set(ctx["spec"]["components"]):
                    _fail("OPERATION_CONFLICT", "published_receipt_differs_from_durable_creation")
            elif state["effects"].get("publish:receipt") == "CONFIRMED":
                _fail("RECOVERY_REQUIRED", "published_ownership_receipt_missing")
        elif _file_digest(self._private_path(receipt, directory=False)) != ctx["receipt_digest"]:
            _fail("PLAN_STALE", "ownership_receipt_changed_during_operation")
        daemon = self._daemon(ctx["target"], ctx)
        if state["action"] == "install":
            self._require_loopback_engine(daemon)
        if daemon["ID"] != ctx["owner"]["daemon_id"]:
            _fail("OPERATION_CONFLICT", "docker_daemon_identity_changed")
        if daemon["host_namespaces"] != state["host_namespaces"]:
            _fail("RECOVERY_REQUIRED", "host_namespace_changed_during_operation")
        return daemon

    def _lifecycle_snapshot(self, ctx, state):
        daemon = self._lifecycle_binding(ctx, state)
        containers = self._resource_inventory(ctx, "container")
        networks = self._resource_inventory(ctx, "network")
        cids, nids = {row["Id"] for row in containers}, {row["Id"] for row in networks}
        raw, present = {}, {}
        for name, cid in ctx["owner"]["containers"].items():
            effect = state["effects"].get("remove:container:" + name)
            if cid not in cids:
                if state["action"] == "uninstall" and effect in {"INTENT", "CONFIRMED"}:
                    continue
                _fail("RECOVERY_REQUIRED", "owned_container_disappeared_without_remove_intent")
            if effect == "CONFIRMED":
                _fail("OPERATION_CONFLICT", "removed_container_reappeared")
            raw[name] = self._container(ctx, name)
            present[name] = cid
        nid = ctx["owner"]["network"]["id"]
        members = {}
        if nid is not None:
            if nid in nids:
                if state["effects"].get("remove:network:network") == "CONFIRMED":
                    _fail("OPERATION_CONFLICT", "removed_network_reappeared")
                members = self._network(ctx).get("Containers") or {}
            elif not (state["action"] == "uninstall" and state["effects"].get("remove:network:network") in {"INTENT", "CONFIRMED"}):
                _fail("RECOVERY_REQUIRED", "owned_network_disappeared_without_remove_intent")
        for name, value in raw.items():
            if value["Running"] and present[name] not in members:
                _fail("INTEGRITY_FAILED", "running_container_network_membership_missing")
        conflicts = [] if state["action"] == "uninstall" else self._foreign_writers(ctx)
        public = {name: {"container_id": value["Id"], "running": value["Running"], "state": value["Status"],
            "exit_code": value["ExitCode"], "health": value.get("Health"), "started_at": value.get("StartedAt"),
            "finished_at": value.get("FinishedAt"), "restart_count": value.get("RestartCount")} for name, value in raw.items()}
        fingerprint = {"root": str(ctx["root"]), "owner": _owner_id(), "receipt_sha256": ctx["receipt_digest"],
            "manifest_sha256": hash_manifest(ctx["manifest"]), "contract_sha256": ctx["contract"].contract_sha256,
            "payload_sha256": ctx["contract"].payload_sha256,
            "daemon_id": daemon["ID"], "endpoint_sha256": ctx["owner"]["endpoint_sha256"],
            "host_namespaces": daemon["host_namespaces"], "components": public, "foreign_writers": conflicts}
        return {"fingerprint_data": fingerprint, "components": public, "raw": raw, "foreign_writers": conflicts,
                "daemon_id": daemon["ID"], "present_network": nid in nids if nid else False}

    def _prepare_data(self, ctx, state):
        self._lifecycle_binding(ctx, state)
        desired = [ctx["home"].app, ctx["home"].releases, *ctx["home"].roots.values()]
        paths = set()
        for path in desired:
            while path != ctx["root"]:
                paths.add(path)
                path = path.parent
        for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
            relative = path.relative_to(ctx["root"]).as_posix()
            record = state["directories"].get(relative)
            if record and record.get("status") == "CONFIRMED":
                continue
            if record is None:
                if path.exists():
                    _fail("OPERATION_CONFLICT", "unowned_data_directory_present")
                state["directories"][relative] = {"status": "INTENT"}
                self._save(ctx, state)
            if path.exists():
                self._private_path(path, directory=True)
                if any(path.iterdir()):
                    _fail("OPERATION_CONFLICT", "unconfirmed_data_directory_not_empty")
            else:
                checked_path(path).mkdir(mode=0o700)
                _sync(path.parent)
            self._private_path(path, directory=True)
            state["directories"][relative] = {"status": "CONFIRMED", **_identity(path)}
            self._save(ctx, state)

    def _private_recovery_directory(self, path):
        if not path.exists():
            self._private_recovery_directory(path.parent)
            path.mkdir(mode=0o700)
            _sync(path.parent)
        return self._private_path(path, directory=True)

    def _retain_file(self, ctx, state, source, destination, expected):
        source, destination = checked_path(source), checked_path(destination)
        self._private_recovery_directory(destination.parent)
        if destination.exists():
            self._private_path(destination, directory=False)
            if _file_digest(destination) != expected:
                _fail("INTEGRITY_FAILED", "retained_recovery_file_changed")
            return
        if _file_digest(source) != expected:
            _fail("INTEGRITY_FAILED", "recovery_source_changed")
        relative = destination.relative_to(ctx["control"]).as_posix()
        copies = state.setdefault("copies", {})
        if relative not in copies:
            copies[relative] = {"temporary": ".copy-" + uuid.uuid4().hex + ".tmp", "sha256": expected}
            self._save(ctx, state)
        record = copies[relative]
        if (type(record) is not dict or set(record) != {"temporary", "sha256"} or record["sha256"] != expected
                or type(record["temporary"]) is not str or re.fullmatch(r"\.copy-[0-9a-f]{32}\.tmp", record["temporary"]) is None):
            _fail("RECOVERY_REQUIRED", "recovery_copy_intent_mismatch")
        temporary = checked_path(destination.with_name(record["temporary"]))
        if temporary.exists():
            # Только точный private temporary из durable intent. Повреждённый
            # опубликованный файл выше никогда не переписывается этим путём.
            self._private_path(temporary, directory=False)
            if _file_digest(temporary) == expected:
                os.replace(temporary, destination)
                _sync(destination.parent)
                return
            temporary.unlink()
            _sync(temporary.parent)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as outgoing, source.open("rb") as incoming:
                while block := incoming.read(1024 * 1024):
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if _file_digest(temporary) != expected or _file_digest(source) != expected:
                _fail("INTEGRITY_FAILED", "recovery_copy_changed")
            os.replace(temporary, destination)
            _sync(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _stage_recovery(self, ctx, state):
        self._lifecycle_binding(ctx, state)
        destination = ctx["control"] / "docker-recovery" / (ctx["contract"].release + "-" + ctx["contract"].contract_sha256[:16])
        if state["effects"].get("retain:release") is None:
            state["effects"]["retain:release"] = "INTENT"
            self._save(ctx, state)
        payload = destination / "payload"
        self._private_recovery_directory(payload)
        for name, digest in ctx["contract"].files:
            self._retain_file(ctx, state, Path(ctx["verification_options"]["payload_dir"]) / name, payload / name, digest)
        if _tree(payload)[1] != dict(ctx["contract"].files):
            _fail("INTEGRITY_FAILED", "retained_payload_inventory_mismatch")
        options = {"install_root": str(ctx["root"]), "payload_dir": str(payload), "release": ctx["contract"].release,
            "runtime_executable": ctx["options"]["runtime_executable"], "modes": ctx["owner"]["modes"], "work_dir": str(ctx["work"])}
        for key, filename in (("inventory_file", "inventory.json"), ("signature_file", "signature.json")):
            source = checked_path(ctx["verification_options"][key])
            self._retain_file(ctx, state, source, destination / filename, _file_digest(source))
            options[key] = str(destination / filename)
        retained = load_verified_docker_contract(ctx["manifest"], ctx["target"], options, verifier=self.verifier)
        if retained != ctx["contract"]:
            _fail("INTEGRITY_FAILED", "retained_contract_not_verified")
        metadata = {"schema_version": 1, "backend": "docker-managed", "manifest": ctx["manifest"],
            "target": ctx["target"], "options": options, "contains_images": False, "images_retained_in_daemon": True}
        path = destination / "recovery.json"
        if path.exists() and _read(self._private_path(path, directory=False)) != metadata:
            _fail("INTEGRITY_FAILED", "retained_recovery_metadata_changed")
        self._durable_json(path, metadata)
        state["effects"]["retain:release"] = "CONFIRMED"
        self._save(ctx, state)
        return {"metadata_path": str(path), "payload_dir": str(payload), "contains_images": False, "images_retained": True}

    def _reconcile_create(self, ctx, state, kind, name, *, returned_id=None):
        key = "create:" + kind + ":" + name
        if state["effects"].get(key) not in {"INTENT", "CONFIRMED"}:
            return
        spec = ctx["spec"]["network"] if kind == "network" else ctx["spec"]["components"][name]
        rows = self._resource_inventory(ctx, kind)
        candidates = [row for row in rows if row["Name"] == spec["name"]]
        if len(candidates) != 1:
            _fail("RECOVERY_REQUIRED", "uncertain_create_not_replayed_without_exact_resource")
        identity = candidates[0]["Id"]
        previous = ctx["owner"]["network"]["id"] if kind == "network" else ctx["owner"]["containers"].get(name)
        if ((previous is not None and previous != identity) or (returned_id is not None and returned_id != identity)
                or identity in state.get("before_create", {}).get(key, [])):
            _fail("OPERATION_CONFLICT", "creation_identity_does_not_match_private_intent")
        # Временное добавление ID допустимо только для строгой проверки inspect.
        # Private receipt/journal публикуются после успешной проверки всех полей.
        candidate = dict(ctx) | {"owner": copy.deepcopy(ctx["owner"])}
        if kind == "network":
            candidate["owner"]["network"]["id"] = identity
            self._network(candidate)
        else:
            candidate["owner"]["containers"][name] = identity
            value = self._container(candidate, name)
            if state["effects"][key] == "INTENT" and (value["Running"] or value["Status"] != "created"):
                _fail("OPERATION_CONFLICT", "new_container_started_outside_install_transaction")
        ctx["owner"].update(candidate["owner"])
        state["owner"] = ctx["owner"]
        state["effects"][key] = "CONFIRMED"
        self._save(ctx, state)

    def _reconcile_install(self, ctx, state):
        self._lifecycle_binding(ctx, state)
        for kind, name in [("network", "network"), *(("container", name) for name in ctx["spec"]["order"])]:
            if state["effects"].get("create:" + kind + ":" + name) == "INTENT":
                self._reconcile_create(ctx, state, kind, name)

    def _provision(self, ctx, state):
        self._install_inputs(ctx)
        for kind, name in [("network", "network"), *(("container", name) for name in ctx["spec"]["order"])]:
            observed = self._lifecycle_snapshot(ctx, state)
            if observed["foreign_writers"]:
                _fail("OPERATION_CONFLICT", "foreign_data_writers_present")
            key = "create:" + kind + ":" + name
            if state["effects"].get(key):
                self._reconcile_create(ctx, state, kind, name)
                continue
            spec = ctx["spec"]["network"] if kind == "network" else ctx["spec"]["components"][name]
            before = self._resource_inventory(ctx, kind)
            if any(row["Name"] == spec["name"] for row in before):
                _fail("OPERATION_CONFLICT", "resource_name_already_exists_before_owned_intent")
            state.setdefault("before_create", {})[key] = [row["Id"] for row in before]
            state["effects"][key] = "INTENT"
            self._save(ctx, state)
            returned = None
            try:
                raw = self._ctx_run(ctx, network_create(spec) if kind == "network" else container_create(spec))
                returned = raw.decode("ascii").strip()
                if not _HEX.fullmatch(returned):
                    _fail("RECOVERY_REQUIRED", "create_returned_invalid_resource_id")
            except (EngineError, UnicodeError):
                # CLI failure/timeout не доказывает отсутствие эффекта daemon.
                # Inspect допускает ровно один ресурс из ранее записанного intent.
                returned = None
            self._reconcile_create(ctx, state, kind, name, returned_id=returned)
            ctx["emit"]({"phase": "provision", "component_id": name, "status": "SUCCEEDED", "completed": 1, "total": 1})

    def _publish_installation(self, ctx, state):
        observed = self._lifecycle_snapshot(ctx, state)
        if set(observed["raw"]) != set(ctx["spec"]["components"]) or not observed["present_network"] or observed["foreign_writers"]:
            _fail("RECOVERY_REQUIRED", "provisioning_not_complete")
        receipt = ctx["control"] / "docker-installation.json"
        if receipt.exists() and _read(self._private_path(receipt, directory=False)) != ctx["owner"]:
            _fail("OPERATION_CONFLICT", "ownership_receipt_already_exists")
        state["effects"]["publish:receipt"] = "INTENT"
        self._save(ctx, state)
        self._durable_json(receipt, ctx["owner"])
        ctx["receipt_digest"] = _file_digest(receipt)
        state["effects"]["publish:receipt"] = "CONFIRMED"
        self._save(ctx, state)

    def _remove_owned(self, ctx, state):
        for kind, name in [*(("container", name) for name in reversed(ctx["spec"]["order"])), ("network", "network")]:
            observed = self._lifecycle_snapshot(ctx, state)
            key = "remove:" + kind + ":" + name
            exists = observed["present_network"] if kind == "network" else name in observed["raw"]
            if not exists:
                if state["effects"].get(key) not in {"INTENT", "CONFIRMED"}:
                    _fail("RECOVERY_REQUIRED", "missing_resource_without_owned_remove_intent")
                state["effects"][key] = "CONFIRMED"
                self._save(ctx, state)
                continue
            if state["effects"].get(key):
                _fail("RECOVERY_REQUIRED", "uncertain_remove_not_replayed")
            self._no_foreign_network_consumers(ctx)
            if kind == "container":
                self._cleanly_stopped(observed["raw"][name])
                identity = ctx["owner"]["containers"][name]
            else:
                if observed["raw"] or self._network(ctx).get("Containers"):
                    _fail("OPERATION_CONFLICT", "network_has_remaining_members")
                identity = ctx["owner"]["network"]["id"]
            state["effects"][key] = "INTENT"
            self._save(ctx, state)
            try:
                self._ctx_run(ctx, (kind, "rm", "--", identity))
            except EngineError:
                pass
            after = self._lifecycle_snapshot(ctx, state)
            if (after["present_network"] if kind == "network" else name in after["raw"]):
                _fail("RECOVERY_REQUIRED", "remove_effect_not_confirmed")
            state["effects"][key] = "CONFIRMED"
            self._save(ctx, state)
            ctx["emit"]({"phase": "remove_owned", "component_id": name, "status": "SUCCEEDED", "completed": 1, "total": 1})

    def _no_foreign_network_consumers(self, ctx):
        owned = set(ctx["owner"]["containers"].values())
        owned_names = {ctx["spec"]["components"][name]["name"] for name in ctx["owner"]["containers"]}
        network = ctx["owner"]["network"]
        inventory = self._resource_inventory(ctx, "container")
        for row in inventory:
            if row["Id"] in owned:
                continue
            value = self._decode(self._ctx_run(ctx, ("container", "inspect", "--format", NETWORK_CONSUMER_FORMAT, "--", row["Id"])))
            connections = value.get("Networks")
            if connections is None:
                connections = {}
            if (value.get("Id") != row["Id"] or type(value.get("NetworkMode")) is not str or type(connections) is not dict
                    or any(type(item) is not dict for item in connections.values())):
                _fail("RECOVERY_REQUIRED", "foreign_network_inventory_unavailable")
            mode = value["NetworkMode"]
            if mode.startswith("container:"):
                reference = mode[len("container:"):]
                # Docker принимает name, полный ID и уникальный ID prefix.
                # Receipt IDs/names проверяются даже после частичного removal;
                # неполный/неоднозначный foreign reference не угадывается.
                is_prefix = bool(re.fullmatch(r"[0-9a-f]{1,64}", reference))
                if (reference.lstrip("/") in owned_names or reference in owned
                        or (is_prefix and any(cid.startswith(reference) for cid in owned))):
                    _fail("OPERATION_CONFLICT", "foreign_container_depends_on_owned_namespace")
                matches = {item["Id"] for item in inventory if item["Id"] == reference or item["Name"] == reference.lstrip("/")}
                if not matches and is_prefix:
                    matches = {item["Id"] for item in inventory if item["Id"].startswith(reference)}
                if len(matches) != 1:
                    _fail("RECOVERY_REQUIRED", "foreign_namespace_reference_unresolved")
            if (mode in {network["name"], network["id"]} or network["name"] in connections
                    or any(item.get("NetworkID") == network["id"] for item in connections.values())):
                _fail("OPERATION_CONFLICT", "foreign_container_depends_on_owned_network")

    def _uninstall_receipt(self, ctx, state):
        observed = self._lifecycle_snapshot(ctx, state)
        if observed["raw"] or observed["present_network"]:
            _fail("RECOVERY_REQUIRED", "owned_resources_remain_after_uninstall")
        value = {"schema_version": 1, "mode": "docker", "installation_id": ctx["owner"]["installation_id"],
            "receipt_sha256": ctx["receipt_digest"], "operation": ctx["token"], "binding": state["binding"],
            "preserves_user_data": True, "images_retained": True,
            "data_available": all(path.is_dir() for path in ctx["home"].roots.values()),
            "recovery": state["results"].get("preserve_recovery"), "production_ready": False}
        path = ctx["control"] / "docker-uninstalled.json"
        if path.exists() and _read(self._private_path(path, directory=False)) != value:
            _fail("OPERATION_CONFLICT", "uninstall_tombstone_mismatch")
        self._durable_json(path, value)
        return value
