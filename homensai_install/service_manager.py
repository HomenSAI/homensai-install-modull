# HomenS.AI · © 2026 Serhii Khomenko
"""Исполняемый systemd user platform port с ownership и durable effects.

NativeEngine/UpdatePlan остаётся единственным lifecycle orchestrator. Здесь нет
backup, migration, restore, выбора релиза или установки prerequisites.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .errors import InstallerError
from .service_contracts import load_verified_service_contract, render_service_plan
from .service_systemd import fail, unit_name, check_environment
from .trust import canonical_bytes
from shared.datahome import checked_path, DataHomeError
from shared.update_plan import operation_lock, LockBusy


ACTIONS = {"register", "enable", "start", "stop", "disable", "uninstall"}
_UNSET = {"PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"}


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _path(value):
    try:
        if not Path(value).is_absolute() or ".." in Path(value).parts:
            fail("absolute_service_path_required")
        return checked_path(value)
    except (DataHomeError, TypeError, ValueError, OSError):
        fail("unsafe_service_path")


def _private_directory(path, *, create=False):
    path = _path(path)
    if create:
        # Не chmod уже существующих пользовательских каталогов.
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        _path(path)
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            fail("foreign_or_writable_service_directory")
    for parent in path.parents:
        if not parent.exists():
            continue
        info = parent.stat()
        shared_temporary = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if (info.st_uid not in {0, os.getuid()} or not stat.S_ISDIR(info.st_mode)
                or (info.st_mode & 0o022 and not shared_temporary)):
            fail("unsafe_service_directory_ancestor")
    return path


def _file_bytes(path, *, missing=False, maximum=4 * 1024 * 1024):
    path = _path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing:
            return None
        fail("service_metadata_missing", "RECOVERY_REQUIRED")
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > maximum):
            fail("unsafe_service_file")
        output = bytearray()
        while True:
            part = os.read(fd, min(65536, maximum + 1 - len(output)))
            if not part:
                break
            output.extend(part)
            if len(output) > maximum:
                fail("service_metadata_too_large", "RECOVERY_REQUIRED")
        return bytes(output)
    finally:
        os.close(fd)


def _json(path, *, missing=False):
    raw = _file_bytes(path, missing=missing)
    if raw is None:
        return None
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=pairs)
        if type(value) is not dict:
            raise ValueError("object")
        return value
    except (ValueError, UnicodeError, RecursionError):
        fail("invalid_service_metadata", "RECOVERY_REQUIRED")


def _sync_directory(path):
    fd = os.open(_private_directory(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path, raw, *, exclusive=False):
    """Durable replace. До него caller уже записал intent; tmp всегда exclusive."""
    parent = _private_directory(path.parent, create=True)
    temporary = parent / (".service-tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        _path(path)
        if exclusive:
            # Linux RENAME_NOREPLACE публикует только целый fsync'ed unit и
            # атомарно отклоняет чужой destination. O_EXCL + запись прямо в
            # unit оставляли бы частичный файл при interruption.
            import ctypes
            import errno
            library = ctypes.CDLL(None, use_errno=True)
            rename = getattr(library, "renameat2", None)
            if rename is None:
                fail("atomic_exclusive_unit_publish_unavailable", "CAPABILITY_UNSUPPORTED")
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(temporary), -100, os.fsencode(path), 1):
                error = ctypes.get_errno()
                if error == errno.EEXIST:
                    fail("registration_destination_changed")
                fail("atomic_exclusive_unit_publish_failed", "RECOVERY_REQUIRED")
        else:
            os.replace(temporary, path)
        _sync_directory(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class ServicePlan:
    """Reviewable bytes; не verifier, не proof of ownership и не HTTP callback."""
    document: bytes

    @property
    def digest(self):
        return hashlib.sha256(self.document).hexdigest()

    def as_dict(self):
        return json.loads(self.document)


class SystemdUserManager:
    """runner/verifier внедряются trusted SDK. Любой effect требует exact consent.

    runner protocol: snapshot(name), environment(), command(action, name,
    timeout=...), quiescent(name,cgroup), verify_process(snapshot,receipt).
    Fake protocol тестирует source, но не превращается в live-host evidence.
    """
    def __init__(self, *, verifier, runner, unit_dir, state_dir):
        if verifier is None or runner is None or os.name != "posix":
            fail("trusted_linux_service_ports_required", "CAPABILITY_UNSUPPORTED")
        self.verifier, self.runner = verifier, runner
        self.unit_dir = _private_directory(unit_dir)
        self.state_dir = _private_directory(state_dir)
        if self.unit_dir == self.state_dir or self.unit_dir in self.state_dir.parents or self.state_dir in self.unit_dir.parents:
            fail("overlapping_service_directories")
        if getattr(runner, "live", False) and (str(self.unit_dir) != runner.unit_dir or runner.uid != os.getuid()):
            fail("foreign_user_manager_directory")

    def _identity(self, body):
        root = _private_directory(body["install_root"])
        _private_directory(root / ".hinstall")
        if self.state_dir != root / ".hinstall/service-manager":
            fail("service_state_outside_installation")
        value = _json(root / ".hinstall/installation.json")
        binding = {"product_id": body["manifest"]["product_id"],
                   "installation_id": body["installation_id"], "owner": "uid:" + str(os.getuid()), "mode": "native"}
        if any(value.get(key) != item for key, item in binding.items()):
            fail("installation_ownership_mismatch")
        return binding

    def _state(self, binding):
        value = _json(self.state_dir / "state.json", missing=True)
        if value is None:
            return {"schema_version": 1, "binding": binding, "revision": 0, "services": {}, "operations": {}}
        if (set(value) != {"schema_version", "binding", "revision", "services", "operations"}
                or value["schema_version"] != 1 or value["binding"] != binding
                or type(value["revision"]) is not int or value["revision"] < 0
                or type(value["services"]) is not dict or type(value["operations"]) is not dict):
            fail("service_state_binding_mismatch", "RECOVERY_REQUIRED")
        return value

    def _save(self, state):
        raw = canonical_bytes(state)
        if len(raw) > 4 * 1024 * 1024:
            fail("service_journal_capacity_reached", "RECOVERY_REQUIRED")
        _atomic(self.state_dir / "state.json", raw)

    @contextmanager
    def _lock(self):
        import fcntl
        _private_directory(self.state_dir, create=True)
        path = self.state_dir / "lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                fail("unsafe_service_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("service_operation_already_running", "LOCK_BUSY")
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _lifecycle_lock(self, body):
        """Тот же постоянный root inode, что удерживает NativeEngine.execute.

        Direct SDK apply сериализуется с native/user целиком, включая первое
        journal intent. Это lock существующего lifecycle, не новый orchestrator.
        """
        root = _path(body["install_root"])
        lock = _path(root.parent / (".hinstall-" + _digest(str(root))[:24] + ".lock"))
        # operation_lock проверяет path, flock и durable held/released markers.
        # До его open дополнительно отвергаем чужой inode/hardlink/permissions.
        _file_bytes(lock, missing=True, maximum=8192)
        try:
            with operation_lock(lock):
                yield
        except LockBusy:
            fail("installation_lifecycle_already_running", "LOCK_BUSY")

    def _verify(self, body):
        root = _path(body["install_root"])
        release = root / body["manifest"]["paths"]["releases"] / ("app-" + body["options"]["release"])
        options = {**body["options"], "payload_dir": str(_path(release))}
        contract = load_verified_service_contract(body["manifest"], body["target"], options, verifier=self.verifier)
        result = render_service_plan(contract, install_root=str(root),
            installation_id=body["installation_id"], modes=body["modes"])
        if result["backend"] != "systemd-user":
            fail("only_systemd_user_apply_supported", "CAPABILITY_UNSUPPORTED")
        if "rendered" in body and result != body["rendered"]:
            fail("signed_service_plan_changed", "PLAN_STALE")
        return result

    def plan(self, manifest, target, options, *, install_root, installation_id, modes,
             action, operation_id=None):
        """Без effects: payload должен быть уже staged orchestration в releases."""
        if action not in ACTIONS:
            fail("unsupported_service_action", "CAPABILITY_UNSUPPORTED")
        operation_id = operation_id or uuid.uuid4().hex
        if type(operation_id) is not str or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            fail("invalid_service_operation_identity")
        selected = {key: options[key] for key in ("inventory_file", "signature_file", "release", "runtime_executable")}
        selected["inventory_file"] = str(_path(selected["inventory_file"]))
        selected["signature_file"] = str(_path(selected["signature_file"]))
        body = {"schema_version": 1, "kind": "systemd-user-action", "action": action,
                "operation_id": operation_id, "manifest": manifest, "target": target,
                "options": selected, "install_root": str(_path(install_root)),
                "installation_id": installation_id, "modes": list(modes),
                "unit_dir": str(self.unit_dir), "state_dir": str(self.state_dir)}
        binding = self._identity(body)
        state = self._state(binding)
        body["revision"] = state["revision"]
        body["rendered"] = self._verify(body)
        body["effects"] = {
            "register": ["write_owned_unit", "user_daemon_reload"],
            "enable": ["create_owned_default_target_link", "user_daemon_reload"],
            "start": ["start_verified_owned_unit"], "stop": ["graceful_stop_and_cgroup_verification"],
            "disable": ["remove_owned_default_target_link", "user_daemon_reload"],
            "uninstall": ["graceful_stop_and_cgroup_verification", "remove_owned_link_and_unit", "user_daemon_reload"],
        }[action]
        body["production_ready"] = False
        return ServicePlan(canonical_bytes(body))

    def _validate_plan(self, plan):
        if not isinstance(plan, ServicePlan):
            fail("reviewed_service_plan_required")
        try:
            body = plan.as_dict()
            if (canonical_bytes(body) != plan.document or body["schema_version"] != 1
                    or body["kind"] != "systemd-user-action" or body["action"] not in ACTIONS
                    or body["unit_dir"] != str(self.unit_dir) or body["state_dir"] != str(self.state_dir)
                    or re.fullmatch(r"[0-9a-f]{32}", body["operation_id"]) is None):
                fail("invalid_service_plan")
            self._identity(body)
            return body
        except (KeyError, ValueError, TypeError):
            fail("invalid_service_plan")

    def _manager_environment(self):
        check_environment(self.runner.environment(), uid=os.getuid(),
            home=getattr(self.runner, "home", None), user=getattr(self.runner, "user", None),
            shell=getattr(self.runner, "shell", None))

    def _configuration(self, expected):
        raw = _file_bytes(self.unit_dir / expected["configuration_file"])
        if hashlib.sha256(raw).hexdigest() != expected["configuration_sha256"]:
            fail("owned_unit_file_changed")

    def _effective(self, expected, *, check_environment=False, configuration_removed=False):
        if not configuration_removed:
            self._configuration(expected)
        name = unit_name(expected["configuration_file"])
        value = self.runner.snapshot(name)
        if not isinstance(value, dict):
            fail("owned_service_not_loaded", "RECOVERY_REQUIRED")
        fixed = {"Id": name, "Names": [name], "LoadState": "loaded",
            "FragmentPath": str(self.unit_dir / name), "SourcePath": "", "DropInPaths": [],
            "Transient": False, "NeedDaemonReload": False, "Type": "exec",
            "WorkingDirectory": expected["working_directory"], "EnvironmentFiles": [], "PassEnvironment": [],
            "User": "", "Group": "", "DynamicUser": False, "RootDirectory": "", "RootImage": "",
            "KillMode": "control-group", "KillSignal": 15, "SendSIGKILL": False,
            "NoNewPrivileges": True, "UMask": 0o077,
            "TimeoutStopUSec": expected["stop_timeout_seconds"] * 1000000,
            "TimeoutStartUSec": expected["stop_timeout_seconds"] * 1000000,
            "TimeoutStopFailureMode": "terminate", "TimeoutStartFailureMode": "terminate",
            "Restart": "on-failure", "RestartUSec": expected["restart"]["delay"] * 1000000,
            "StandardOutput": "null", "StandardError": "null"}
        if configuration_removed:
            fixed.pop("NeedDaemonReload")
        if any(type(value.get(key)) is not type(item) or value.get(key) != item for key, item in fixed.items()):
            fail("effective_service_configuration_mismatch")
        for prop, args in (("ExecStart", expected["start_argv"]), ("ExecStop", expected["stop_argv"])):
            entries = value.get(prop)
            if (type(entries) is not list or len(entries) != (1 if args else 0)
                    or (args and (type(entries[0]) is not list or len(entries[0]) != 10
                                 or entries[0][:3] != [args[0], args, False]))):
                fail("effective_service_argv_mismatch")
        for prop in ("ExecStartPre", "ExecStartPost", "ExecStopPost", "ExecCondition", "ExecReload"):
            if value.get(prop) != []:
                fail("unowned_service_exec_hook")
        environment = value.get("Environment")
        if (type(environment) is not list or any(type(item) is not str or "=" not in item for item in environment)
                or len(environment) != len(expected["environment"])
                or dict(item.split("=", 1) for item in environment) != expected["environment"]
                or type(value.get("UnsetEnvironment")) is not list or set(value["UnsetEnvironment"]) != _UNSET):
            fail("effective_service_environment_mismatch")
        if check_environment:
            self._manager_environment()
        if (type(value.get("MainPID")) is not int or type(value.get("ControlPID")) is not int
                or value["MainPID"] < 0 or value["ControlPID"] < 0
                or type(value.get("Job")) is not list or len(value["Job"]) != 2
                or type(value["Job"][0]) is not int or type(value.get("ControlGroup")) is not str):
            fail("incomplete_service_liveness_inventory", "RECOVERY_REQUIRED")
        return value

    def _quiescent(self, expected, value, record):
        if (value["ActiveState"] not in {"inactive", "failed"} or value["MainPID"] or value["ControlPID"]
                or value["Job"][0] or value.get("Result") != "success"):
            fail("graceful_service_stop_not_confirmed", "RECOVERY_REQUIRED")
        groups = {record.get("cgroup", ""), value["ControlGroup"]} - {""}
        if record.get("start_uncertain") and not groups:
            fail("interrupted_start_cgroup_unknown", "RECOVERY_REQUIRED")
        if not all(self.runner.quiescent(expected["configuration_file"], group) for group in groups):
            fail("service_writers_remain", "RECOVERY_REQUIRED")

    def _link(self, expected, *, exists=None):
        directory = _private_directory(self.unit_dir / "default.target.wants")
        path = directory / unit_name(expected["configuration_file"])
        try:
            info = path.lstat()
        except FileNotFoundError:
            if exists is True:
                fail("owned_enable_link_missing")
            return False
        if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or os.readlink(path) != str(self.unit_dir / expected["configuration_file"])):
            fail("foreign_enable_link")
        if exists is False:
            fail("enable_link_without_receipt")
        return True

    def _step(self, state, operation, key, effect, verify):
        step = operation["steps"].get(key)
        if step == "done":
            return
        if step == "pending" and verify():
            operation["steps"][key] = "done"
            self._save(state)
            return
        operation["steps"][key] = "pending"
        self._save(state)
        effect()
        if not verify():
            fail("service_effect_not_confirmed", "RECOVERY_REQUIRED")
        operation["steps"][key] = "done"
        self._save(state)

    def apply(self, plan, *, consent, resume=False):
        body = self._validate_plan(plan)
        if type(consent) is not str or consent != plan.digest or type(resume) is not bool:
            fail("exact_service_plan_consent_required", "CONSENT_REQUIRED")
        with self._lifecycle_lock(body):
            state = self._state(self._identity(body))
            operation = state["operations"].get(body["operation_id"])
            if operation is not None:
                if operation.get("digest") != plan.digest:
                    fail("service_operation_body_changed")
                if operation.get("status") == "done":
                    return dict(operation["result"], replayed=True)
                if not resume:
                    fail("explicit_service_resume_required", "RECOVERY_REQUIRED")
            else:
                if resume:
                    fail("service_resume_journal_missing", "RECOVERY_REQUIRED")
                if body["revision"] != state["revision"]:
                    fail("service_plan_revision_changed", "PLAN_STALE")
                if any(item.get("status") != "done" for item in state["operations"].values()):
                    fail("unfinished_service_operation_requires_resume", "RECOVERY_REQUIRED")
            # A rejected fresh plan must not create a service directory without
            # a journal: NativeEngine treats that as an uncertain service effect.
            # The common lifecycle lease already protects these read-only checks.
            rendered = self._verify(body)
            with self._lock():
                if self._state(self._identity(body)) != state:
                    fail("service_state_changed_before_reservation", "PLAN_STALE")
                if operation is None:
                    operation = {"digest": plan.digest, "action": body["action"], "status": "running", "steps": {}}
                    state["operations"][body["operation_id"]] = operation
                    state["revision"] += 1
                    self._save(state)
                _private_directory(self.unit_dir, create=True)
                expected_items = rendered["services"]
                if body["action"] in {"stop", "uninstall"}:
                    expected_items = list(reversed(expected_items))
                for expected in expected_items:
                    self._apply_service(body, state, operation, expected)
                result = {"operation_id": body["operation_id"], "action": body["action"], "status": "applied",
                          "backend": "systemd-user", "services": [v["service_id"] for v in expected_items],
                          "quiescent": body["action"] in {"stop", "uninstall"}, "production_ready": False,
                          "evidence_scope": "live-manager" if getattr(self.runner, "live", False) else "injected-runner"}
                operation.update(status="done", result=result)
                self._save(state)
                return result

    def _apply_service(self, body, state, operation, expected):
        name = unit_name(expected["configuration_file"])
        action = body["action"]
        record = state["services"].get(name)
        if action == "register":
            return self._register(body, state, operation, expected, record)
        if record is None or record.get("expected") != expected:
            fail("owned_service_receipt_missing_or_stale")
        if action == "uninstall" and operation["steps"].get(name + ":remove") in {"pending", "done"}:
            if operation["steps"].get(name + ":stop") != "done" or operation["steps"].get(name + ":disable") != "done":
                fail("incomplete_uninstall_journal", "RECOVERY_REQUIRED")
            return self._remove(state, operation, expected, record)
        value = self._effective(expected, check_environment=action in {"start", "stop", "uninstall"})
        # Перед каждым изменением заново проверяется весь установленный release.
        self._verify(body)
        if action in {"stop", "uninstall"}:
            self._stop(state, operation, expected, record, value)
            if action == "stop":
                return
        if action == "start":
            def verify_start():
                current = self._effective(expected, check_environment=True)
                if current["ActiveState"] != "active" or current["SubState"] != "running" or current["Job"][0]:
                    return False
                self.runner.verify_process(current, expected)
                if not current["ControlGroup"]:
                    fail("running_service_cgroup_missing", "RECOVERY_REQUIRED")
                record.update(cgroup=current["ControlGroup"], start_uncertain=False)
                return True
            if operation["steps"].get(name + ":start") == "done":
                if not verify_start():
                    fail("completed_start_no_longer_running", "RECOVERY_REQUIRED")
                self._save(state)
                return
            if value["Job"][0]:
                fail("service_job_in_progress", "RECOVERY_REQUIRED")
            if value["ActiveState"] != "active":
                self._quiescent(expected, value, record)
            record["start_uncertain"] = True
            self._save(state)
            self._step(state, operation, name + ":start",
                lambda: self.runner.command("start", name, timeout=expected["stop_timeout_seconds"] + 10), verify_start)
            return
        if action in {"enable", "disable", "uninstall"}:
            enabled = action == "enable"
            key = name + (":enable" if enabled else ":disable")
            if operation["steps"].get(key) != "pending":
                self._link(expected, exists=record["enabled"])
            else:
                self._link(expected)
            def change_link():
                directory = _private_directory(self.unit_dir / "default.target.wants", create=enabled)
                path = directory / name
                present = self._link(expected)
                if enabled and not present:
                    os.symlink(str(self.unit_dir / name), path)
                elif not enabled and present:
                    path.unlink()
                if directory.exists():
                    _sync_directory(directory)
                record["enabled"] = enabled
                self._save(state)
            self._step(state, operation, key, change_link, lambda: self._link(expected) == enabled)
            record["enabled"] = enabled
            self._save(state)
            if action != "uninstall":
                self._reload(state, operation, key + ":reload", expected)
                return
        if action == "uninstall":
            self._quiescent(expected, self._effective(expected), record)
            self._remove(state, operation, expected, record)

    def _remove(self, state, operation, expected, record):
        name = expected["configuration_file"]
        self._link(expected, exists=False)
        actual = _file_bytes(self.unit_dir / name, missing=True)
        observed = self.runner.snapshot(name)
        if observed is not None:
            current = self._effective(expected, configuration_removed=actual is None)
            self._quiescent(expected, current, record)
        elif not self.runner.quiescent(name, record.get("cgroup", "")):
            fail("service_writers_remain", "RECOVERY_REQUIRED")
        def remove():
            self._configuration(expected)
            (self.unit_dir / name).unlink()
            _sync_directory(self.unit_dir)
        self._step(state, operation, name + ":remove", remove,
                   lambda: _file_bytes(self.unit_dir / name, missing=True) is None)
        record["removed"] = True
        self._save(state)
        self._step(state, operation, name + ":remove:reload",
            lambda: self.runner.command("daemon-reload", timeout=30), lambda: self.runner.snapshot(name) is None)

    def _reload(self, state, operation, key, expected):
        def verify():
            try:
                value = self._effective(expected)
            except InstallerError:
                return False
            record = state["services"][expected["configuration_file"]]
            return value["UnitFileState"] == ("enabled" if record["enabled"] else "disabled")
        self._step(state, operation, key, lambda: self.runner.command("daemon-reload", timeout=30), verify)
        if not verify():
            fail("service_reload_not_effective", "RECOVERY_REQUIRED")

    def _register(self, body, state, operation, expected, record):
        name = expected["configuration_file"]
        key = name + ":write"
        raw = body["rendered"]["files"][name].encode()
        previous = record["expected"] if record and not record.get("removed") else None
        if operation["steps"].get(key) is None:
            if previous is None:
                if (_file_bytes(self.unit_dir / name, missing=True) is not None
                        or self.runner.snapshot(name) is not None):
                    fail("existing_service_not_owned")
                self._link(expected, exists=False)
            else:
                value = self._effective(previous)
                self._link(previous, exists=record["enabled"])
                self._quiescent(previous, value, record)
            operation.setdefault("previous", {})[name] = previous
            # Receipt reservation является intent, не присвоением готовой службы.
            record = {"expected": expected, "enabled": bool(record and record["enabled"]),
                      "cgroup": "", "start_uncertain": False, "removed": False}
            state["services"][name] = record
            operation["steps"][key] = "pending"
            self._save(state)
        else:
            previous = operation.get("previous", {}).get(name)
        def matching():
            actual = _file_bytes(self.unit_dir / name, missing=True)
            return actual is not None and hashlib.sha256(actual).hexdigest() == expected["configuration_sha256"]
        def write():
            actual = _file_bytes(self.unit_dir / name, missing=True)
            if actual is not None and (previous is None or hashlib.sha256(actual).hexdigest() != previous["configuration_sha256"]):
                fail("registration_destination_changed")
            self._verify(body)
            if actual is None:
                _atomic(self.unit_dir / name, raw, exclusive=True)
            else:
                _atomic(self.unit_dir / name, raw)
        self._step(state, operation, key, write, matching)
        self._reload(state, operation, name + ":register:reload", expected)

    def _stop(self, state, operation, expected, record, value):
        name = expected["configuration_file"]
        if value.get("Result") != "success":
            fail("ungraceful_service_result_requires_recovery", "RECOVERY_REQUIRED")
        if value["ControlGroup"]:
            record["cgroup"] = value["ControlGroup"]
            self._save(state)
        def stopped():
            current = self._effective(expected, check_environment=True)
            try:
                self._quiescent(expected, current, record)
            except InstallerError as error:
                if error.context.get("details", {}).get("reason") in {"graceful_service_stop_not_confirmed", "service_writers_remain"}:
                    return False
                raise
            record["start_uncertain"] = False
            return True
        def stop():
            if value["Job"][0]:
                fail("service_job_in_progress", "RECOVERY_REQUIRED")
            self.runner.command("stop", name, timeout=expected["stop_timeout_seconds"] * 2 + 10)
        self._step(state, operation, name + ":stop", stop, stopped)
        # Result=timeout нельзя стереть повторным stop и принять за graceful.
        self._quiescent(expected, self._effective(expected), record)

    def status(self, plan):
        body = self._validate_plan(plan)
        state = self._state(self._identity(body))
        self._verify(body)
        results = []
        for expected in body["rendered"]["services"]:
            name = expected["configuration_file"]
            record = state["services"].get(name)
            if record is None or record.get("removed"):
                if self.runner.snapshot(name) is not None or _file_bytes(self.unit_dir / name, missing=True) is not None:
                    fail("existing_service_not_owned")
                results.append({"service_id": expected["service_id"], "registered": False})
                continue
            if record["expected"] != expected:
                fail("service_status_release_changed", "PLAN_STALE")
            current = self._effective(expected)
            self._link(expected, exists=record["enabled"])
            results.append({"service_id": expected["service_id"], "registered": True,
                            "enabled": record["enabled"], "active_state": current["ActiveState"],
                            "sub_state": current["SubState"], "main_pid": current["MainPID"],
                            "job_pending": bool(current["Job"][0])})
        return {"backend": "systemd-user", "services": results, "revision": state["revision"],
                "recovery_required": any(item.get("status") != "done" for item in state["operations"].values()),
                "production_ready": False,
                "evidence_scope": "live-manager" if getattr(self.runner, "live", False) else "injected-runner"}
