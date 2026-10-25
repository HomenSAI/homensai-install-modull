# HomenS.AI · © 2026 Serhii Khomenko
"""Узкий Linux user-manager runner: фиксированные argv и typed D-Bus inventory.

Не принимает shell, произвольные manager methods, remote host или environment.
Наличие этого кода не является live systemd/reboot acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import time

from .errors import InstallerError
from shared.datahome import checked_path


UNIT = re.compile(r"hsai[0-9a-f]{48}\.service\Z")
BUS = "org.freedesktop.systemd1"
MANAGER_PATH = "/org/freedesktop/systemd1"
UNIT_PROPERTIES = {
    "Id": "s", "Names": "as", "LoadState": "s", "ActiveState": "s", "SubState": "s",
    "FragmentPath": "s", "SourcePath": "s", "DropInPaths": "as", "Transient": "b",
    "NeedDaemonReload": "b", "Job": "(uo)", "UnitFileState": "s",
}
SERVICE_PROPERTIES = {
    "Type": "s", "ExecStart": "a(sasbttttuii)", "ExecStop": "a(sasbttttuii)",
    "ExecStartPre": "a(sasbttttuii)", "ExecStartPost": "a(sasbttttuii)",
    "ExecStopPost": "a(sasbttttuii)", "ExecCondition": "a(sasbttttuii)",
    "ExecReload": "a(sasbttttuii)", "WorkingDirectory": "s", "Environment": "as",
    "EnvironmentFiles": "a(sb)", "PassEnvironment": "as", "UnsetEnvironment": "as",
    "User": "s", "Group": "s", "DynamicUser": "b", "RootDirectory": "s", "RootImage": "s",
    "KillMode": "s", "KillSignal": "i", "SendSIGKILL": "b", "NoNewPrivileges": "b",
    "UMask": "u", "TimeoutStopUSec": "t", "TimeoutStartUSec": "t",
    "TimeoutStopFailureMode": "s", "TimeoutStartFailureMode": "s",
    "Restart": "s", "RestartUSec": "t", "StandardOutput": "s", "StandardError": "s",
    "MainPID": "u", "ControlPID": "u", "ControlGroup": "s", "Result": "s",
}


def fail(reason, code="OPERATION_CONFLICT"):
    raise InstallerError(code, phase="service_manager", details={"reason": reason})


def unit_name(value):
    if type(value) is not str or not UNIT.fullmatch(value):
        fail("invalid_service_identity")
    return value


def check_environment(values, *, uid, home=None, user=None, shell=None, process_pid=None):
    """Не логирует values; неизвестное наследуемое имя закрывает service start."""
    if type(values) is not list or len(values) > 256:
        fail("invalid_manager_environment")
    seen = set()
    for assignment in values:
        if type(assignment) is not str or "=" not in assignment or len(assignment) > 8192:
            fail("invalid_manager_environment")
        key, value = assignment.split("=", 1)
        if key in seen:
            fail("duplicate_manager_environment")
        seen.add(key)
        if process_pid is None and key in {"PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH"}:
            continue
        fixed = {"HOME": home, "USER": user, "LOGNAME": user, "SHELL": shell,
                 "XDG_RUNTIME_DIR": "/run/user/" + str(uid)}
        if key in fixed and fixed[key] is not None and value == fixed[key]:
            continue
        if (key in {"LANG", "LANGUAGE", "LC_ALL"} or re.fullmatch(r"LC_[A-Z_]+", key)) and re.fullmatch(r"[A-Za-z0-9_.:@-]{0,128}", value):
            continue
        if process_pid is not None:
            if key == "INVOCATION_ID" and re.fullmatch(r"[0-9a-f]{32}", value):
                continue
            if key == "SYSTEMD_EXEC_PID" and value == str(process_pid):
                continue
        fail("manager_environment_not_allowlisted", "CAPABILITY_UNSUPPORTED")


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def bus_value(raw, signature, *, property_value=False):
    """call содержит body array; get-property — само typed значение.

    Это различие проверено настоящим busctl на отдельном private D-Bus.
    """
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if (type(value) is not dict or set(value) != {"type", "data"}
                or value["type"] != signature):
            raise ValueError("shape")
        if property_value:
            item = value["data"]
        else:
            if type(value["data"]) is not list or len(value["data"]) != 1:
                raise ValueError("body")
            item = value["data"][0]
        if signature in {"s", "o"} and type(item) is not str:
            raise ValueError("string")
        if signature == "b" and type(item) is not bool:
            raise ValueError("boolean")
        if signature in {"u", "t", "i"} and type(item) is not int:
            raise ValueError("integer")
        if signature.startswith(("a", "(")) and type(item) is not list:
            raise ValueError("array")
        return item
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail("invalid_typed_manager_response", "RECOVERY_REQUIRED")


class SystemdUserRunner:
    """Trusted SDK создаёт runner; HTTP не может подменять paths/runner.

    Требуется текущий непривилегированный Linux UID и уже работающий local user
    bus. Не включает linger, не применяет sudo/UAC и не запускает daemon.
    """
    live = True

    def __init__(self, *, systemctl="/usr/bin/systemctl", busctl="/usr/bin/busctl"):
        if os.name != "posix" or not Path("/proc/self").exists() or os.getuid() == 0:
            fail("unprivileged_linux_user_required", "CAPABILITY_UNSUPPORTED")
        if os.readlink("/proc/self") != str(os.getpid()):
            fail("pid_and_proc_namespace_mismatch", "CAPABILITY_UNSUPPORTED")
        self.uid = os.getuid()
        import pwd
        account = pwd.getpwuid(self.uid)
        self.home = str(checked_path(account.pw_dir))
        self.user = account.pw_name
        self.shell = account.pw_shell
        self.unit_dir = str(Path(self.home) / ".config/systemd/user")
        self._runtime = Path("/run/user") / str(self.uid)
        self._socket = self._runtime / "bus"
        self._tools = {}
        for name, value in (("systemctl", systemctl), ("busctl", busctl)):
            path = checked_path(value)
            if str(path) != value or not path.is_absolute():
                fail("manager_binary_must_be_absolute")
            self._tools[name] = (path, self._binary(path))
        self._environment = {
            "PATH": "/usr/bin:/bin", "HOME": self.home, "USER": self.user,
            "LOGNAME": self.user, "LANG": "C", "LC_ALL": "C",
            "XDG_RUNTIME_DIR": str(self._runtime),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(self._socket),
            "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0",
        }
        self._endpoint()

    @staticmethod
    def _binary(path):
        checked_path(path)
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not info.st_mode & 0o111):
            fail("untrusted_manager_binary")
        for parent in path.parents:
            info_parent = parent.stat()
            if info_parent.st_uid != 0 or info_parent.st_mode & 0o022:
                fail("untrusted_manager_binary_directory")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _endpoint(self):
        try:
            checked_path(self._runtime)
            directory, socket = self._runtime.stat(), self._socket.lstat()
            if (directory.st_uid != self.uid or directory.st_mode & 0o077
                    or not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != self.uid):
                fail("unsafe_local_user_bus")
        except OSError:
            fail("user_manager_unavailable", "PREREQUISITE_MISSING")

    def _run(self, tool, arguments, timeout):
        """Ограничены время и вывод. При timeout завершается только CLI client."""
        self._endpoint()
        binary, digest = self._tools[tool]
        if self._binary(binary) != digest:
            fail("manager_binary_changed", "PLAN_STALE")
        if not 0 < timeout <= 1200:
            fail("invalid_manager_timeout")
        try:
            process = subprocess.Popen([str(binary), *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self._environment,
                cwd="/", close_fds=True, start_new_session=True)
        except OSError:
            fail("manager_client_unavailable", "PREREQUISITE_MISSING")
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        fail("manager_response_timeout", "RECOVERY_REQUIRED")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        part = os.read(key.fd, 65536)
                        if not part:
                            selector.unregister(key.fd)
                        else:
                            output.extend(part)
                            if len(output) > 2 * 1024 * 1024:
                                fail("manager_response_too_large", "RECOVERY_REQUIRED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail("manager_response_timeout", "RECOVERY_REQUIRED")
            if process.wait(timeout=remaining):
                fail("manager_command_failed", "RECOVERY_REQUIRED")
            return bytes(output)
        except subprocess.TimeoutExpired:
            fail("manager_response_timeout", "RECOVERY_REQUIRED")
        finally:
            # SIGKILL относится только к запущенному нами CLI-процессу; unit,
            # cgroup и продукт через kill/killall никогда не затрагиваются.
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def _bus(self, arguments, signature, *, timeout=15):
        raw = self._run("busctl", ["--address=unix:path=" + str(self._socket),
            "--auto-start=no", "--allow-interactive-authorization=no", "--no-pager",
            "--json=short", "--timeout=" + str(timeout), *arguments], timeout + 1)
        return bus_value(raw, signature)

    def _method(self, name, output_type, *arguments):
        return self._bus(["call", BUS, MANAGER_PATH, BUS + ".Manager", name, *arguments], output_type)

    def _properties(self, path, interface, fields):
        # get-property допускает несколько свойств и возвращает JSON body
        # каждого отдельной строкой. Нет неоднозначного systemctl argv text.
        raw = self._run("busctl", ["--address=unix:path=" + str(self._socket),
            "--auto-start=no", "--allow-interactive-authorization=no", "--no-pager",
            "--json=short", "--timeout=15", "get-property", BUS, path,
            BUS + "." + interface, *fields], 16)
        lines = raw.splitlines()
        if len(lines) != len(fields):
            fail("incomplete_manager_inventory", "RECOVERY_REQUIRED")
        return {key: bus_value(line, kind, property_value=True) for (key, kind), line in zip(fields.items(), lines)}

    def snapshot(self, name):
        name = unit_name(name)
        files = self._method("ListUnitFiles", "a(ss)")
        units = self._method("ListUnits", "a(ssssssouso)")
        if (any(type(row) is not list or len(row) != 2 or not all(type(v) is str for v in row) for row in files)
                or any(type(row) is not list or len(row) != 10 or type(row[0]) is not str for row in units)):
            fail("invalid_manager_inventory", "RECOVERY_REQUIRED")
        if not any(PurePosixPath(row[0]).name == name for row in files) and not any(row[0] == name for row in units):
            return None
        loaded = any(row[0] == name for row in units)
        path = self._method("GetUnit" if loaded else "LoadUnit", "o", "s", name)
        if not path.startswith(MANAGER_PATH + "/unit/"):
            fail("invalid_manager_object_path", "RECOVERY_REQUIRED")
        unit = self._properties(path, "Unit", UNIT_PROPERTIES)
        if unit.get("LoadState") == "not-found":
            process = self._properties(path, "Service", {"MainPID": "u", "ControlPID": "u", "ControlGroup": "s"})
            if (unit.get("ActiveState") == "inactive" and unit.get("Job") == [0, "/"]
                    and not unit.get("FragmentPath") and not unit.get("SourcePath")
                    and unit.get("DropInPaths") == [] and unit.get("Transient") is False
                    and process == {"MainPID": 0, "ControlPID": 0, "ControlGroup": ""}):
                return None
            fail("removed_unit_still_has_manager_state", "RECOVERY_REQUIRED")
        return {**unit, **self._properties(path, "Service", SERVICE_PROPERTIES)}

    def environment(self):
        return self._properties(MANAGER_PATH, "Manager", {"Environment": "as"})["Environment"]

    def command(self, action, name=None, *, timeout=30):
        if action == "daemon-reload" and name is None:
            arguments = ["daemon-reload"]
        elif action in {"start", "stop"}:
            arguments = ["--job-mode=fail", action, unit_name(name)]
        else:
            fail("manager_action_not_allowed")
        self._run("systemctl", ["--user", "--no-pager", "--no-ask-password", *arguments], timeout)

    def quiescent(self, name, cgroup):
        """cgroup v2 populated учитывает потомков; один MainPID=0 недостаточен."""
        name = unit_name(name)
        if not cgroup:
            return True
        value = PurePosixPath(cgroup)
        prefix = "/user.slice/user-" + str(self.uid) + ".slice/user@" + str(self.uid) + ".service/"
        if (not cgroup.startswith(prefix) or value.name != name or ".." in value.parts
                or "//" in cgroup or "\\" in cgroup):
            fail("foreign_control_group")
        root = Path("/sys/fs/cgroup")
        if not (root / "cgroup.controllers").is_file():
            fail("cgroup_v2_required", "CAPABILITY_UNSUPPORTED")
        target = checked_path(root / cgroup.lstrip("/"))
        try:
            raw = (target / "cgroup.events").read_text()
        except FileNotFoundError:
            if target.exists():
                fail("cgroup_inventory_unavailable", "RECOVERY_REQUIRED")
            return True
        except OSError:
            fail("cgroup_inventory_unavailable", "RECOVERY_REQUIRED")
        lines = [line.split() for line in raw.splitlines()]
        populated = [parts[1] for parts in lines if len(parts) == 2 and parts[0] == "populated"]
        if populated not in (["0"], ["1"]):
            fail("invalid_cgroup_inventory", "RECOVERY_REQUIRED")
        return populated == ["0"]

    def verify_process(self, snapshot, expected):
        """Kernel identity дополняет manager inventory после start."""
        pid = snapshot.get("MainPID")
        if type(pid) is not int or pid <= 0:
            fail("service_main_process_missing", "RECOVERY_REQUIRED")
        proc = Path("/proc") / str(pid)
        try:
            before = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            if (proc.stat().st_uid != self.uid or os.readlink(proc / "exe") != expected["executable"]
                    or os.readlink(proc / "cwd") != expected["working_directory"]
                    or (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0") != [s.encode() for s in expected["start_argv"]]):
                fail("service_process_identity_mismatch")
            entries = (proc / "environ").read_bytes().rstrip(b"\0").split(b"\0")
            actual = dict(item.split(b"=", 1) for item in entries if b"=" in item)
            if any(actual.get(k.encode()) != v.encode() for k, v in expected["environment"].items()):
                fail("service_process_environment_mismatch")
            inherited = [entry.decode() for entry in entries if b"=" in entry
                         and entry.split(b"=", 1)[0].decode() not in expected["environment"]]
            check_environment(inherited, uid=self.uid, home=self.home, user=self.user,
                              shell=self.shell, process_pid=pid)
            groups = (proc / "cgroup").read_text().splitlines()
            if "0::" + snapshot["ControlGroup"] not in groups:
                fail("service_process_cgroup_mismatch")
            after = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            if before != after:
                fail("service_process_replaced", "RECOVERY_REQUIRED")
        except (OSError, ValueError, IndexError, UnicodeError):
            fail("service_process_inventory_unavailable", "RECOVERY_REQUIRED")
