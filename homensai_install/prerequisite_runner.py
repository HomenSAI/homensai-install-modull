"""Фиксированные команды prerequisites и read-only маркеры reboot/relogin.

Политики создаёт доверенный bootstrap. Каталог/HTTP не задают аргументы,
окружение, пути исполняемых файлов либо коды завершения команды.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import platform
import re
import stat
import subprocess

from .api.model import digest
from .downloads import ArtifactSpec, DownloadManager
from .prerequisite_catalog import fail, identifier
from .prerequisites import _admin
from shared.datahome import checked_path

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PLACEHOLDER = re.compile(r"\{artifact_([0-9]+)\}\Z")
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "fish", "cmd", "cmd.exe",
           "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_OWNER_ENV = {"HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "SystemRoot", "WINDIR"}


def verify_regular_file(path, expected, *, max_bytes=512 * 1024 * 1024):
    """Проверка inode/байтов непосредственно перед исполнением, без PATH lookup."""
    if type(expected) is not str or not _HASH.fullmatch(expected):
        fail("INTEGRITY_FAILED", "prerequisite_executable_pin_missing")
    path = checked_path(Path(path))
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes
                or (os.name != "nt" and before.st_mode & 0o022)):
            fail("INTEGRITY_FAILED", "prerequisite_executable_not_private_regular")
        value, count = hashlib.sha256(), 0
        while True:
            chunk = os.read(descriptor, 128 * 1024)
            if not chunk:
                break
            value.update(chunk)
            count += len(chunk)
            if count > max_bytes:
                fail("INTEGRITY_FAILED", "prerequisite_executable_too_large")
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or value.hexdigest() != expected):
            fail("INTEGRITY_FAILED", "prerequisite_executable_changed")
    finally:
        os.close(descriptor)
    return path


def system_markers():
    """Не подменяет login новым PID/новым окном браузера.

    Linux boot_id — идентификатор текущего boot. Audit sessionid пригоден лишь
    когда ядро действительно назначило login session. Windows provider должен
    быть передан платформенным helper: отсутствие доказательства остаётся None.
    """
    boot, session = None, None
    if platform.system() == "Linux":
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            if re.fullmatch(r"[0-9a-f-]{36}", value):
                boot = hashlib.sha256(value.encode()).hexdigest()
        except (OSError, UnicodeError):
            pass
        try:
            value = Path("/proc/self/sessionid").read_text(encoding="ascii").strip()
            if value.isdecimal() and 0 <= int(value) < 4294967295 and boot:
                session = digest({"boot": boot, "uid": os.getuid(), "audit_session": value})
        except (OSError, UnicodeError):
            pass
    return {"boot_id": boot, "session_id": session}


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    started: bool
    timed_out: bool = False
    pid: int | None = None

    def __post_init__(self):
        if (self.returncode is not None and (type(self.returncode) is not int or not -(2**31) <= self.returncode < 2**32)
                or type(self.started) is not bool or type(self.timed_out) is not bool
                or (self.pid is not None and (type(self.pid) is not int or self.pid <= 0))):
            raise ValueError("invalid prerequisite command result")
        if not self.started and (self.returncode is not None or self.timed_out or self.pid is not None):
            raise ValueError("unstarted command cannot have process results")


class FixedCommandRunner:
    """Настоящий subprocess, только exact allowlist; вывод installer не сохраняется.

    Timeout не убивает произвольный системный installer. Он возвращает unknown
    outcome, сохраняемый coordinator как RECOVERY_REQUIRED, без blind replay.
    """
    def __init__(self, allowed_commands, *, executable_hashes, environment=None):
        self.allowed = set()
        for command in allowed_commands:
            if (not isinstance(command, (list, tuple)) or not 1 <= len(command) <= 64
                    or any(type(arg) is not str or not arg or len(arg) > 4096 or "\0" in arg for arg in command)):
                raise ValueError("invalid prerequisite command allowlist")
            if not Path(command[0]).is_absolute() or Path(command[0]).name.lower() in _SHELLS or Path(command[0]).suffix.lower() in {".cmd", ".bat"}:
                raise ValueError("shell and relative prerequisite executables are forbidden")
            self.allowed.add(tuple(command))
        if not self.allowed or len(self.allowed) > 128:
            raise ValueError("bounded exact commands are required")
        self.hashes = dict(executable_hashes)
        if any(command[0] not in self.hashes or not _HASH.fullmatch(str(self.hashes[command[0]])) for command in self.allowed):
            raise ValueError("each executable needs a reviewed hash")
        owner_env = dict(environment or {})
        if (set(owner_env) - _OWNER_ENV or any(type(value) is not str or not value or len(value) > 32768
                or any(ord(c) < 32 for c in value) for value in owner_env.values())):
            raise ValueError("invalid original-owner environment")
        self.environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", **owner_env}
        self._children = []

    def run(self, argv, *, timeout=900, elevated=False):
        if (not isinstance(argv, (list, tuple)) or any(type(arg) is not str for arg in argv)
                or tuple(argv) not in self.allowed):
            fail("CAPABILITY_UNSUPPORTED", "prerequisite_command_not_allowlisted")
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600 or type(elevated) is not bool:
            fail("INVALID_REQUEST", "prerequisite_command_limits")
        if elevated and _admin() is not True:
            fail("PERMISSION_REQUIRED", "prerequisite_elevated_helper_required")
        executable = verify_regular_file(argv[0], self.hashes[argv[0]])
        # No caller cwd, shell, inherited credentials, loader variables or raw logs.
        try:
            process = subprocess.Popen([str(executable), *argv[1:]], shell=False,
                cwd=str(executable.parent), env=dict(self.environment), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                start_new_session=os.name != "nt")
        except OSError:
            return CommandResult(None, False)
        try:
            return CommandResult(process.wait(timeout=timeout), True, pid=process.pid)
        except subprocess.TimeoutExpired:
            self._children.append(process)  # Can be observed/reaped; never guessed complete.
            return CommandResult(None, True, timed_out=True, pid=process.pid)


class FixedCommandAdapter:
    """Одна проверенная команда установки; postcondition предоставляет discover.

    args/executable_hash/environment — trusted Python configuration. Только
    целые аргументы {artifact_0}… заменяются на уже проверенные private copies.
    Нет remote templates, произвольных дополнительных flags или shell strings.
    """
    def __init__(self, adapter_id, *, os_name, argv, artifact_suffixes,
                 executable_sha256=None, elevated=False, timeout=900,
                 success_codes=(0,), reboot_codes=(), relogin_codes=(), canceled_codes=(),
                 environment=None, publisher_check=None, preflight=None, runner_factory=FixedCommandRunner):
        if not identifier(adapter_id) or os_name not in {"windows", "linux"}:
            raise ValueError("invalid prerequisite adapter identity")
        if not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= 64 or any(type(arg) is not str or not arg or len(arg) > 4096 or "\0" in arg for arg in argv):
            raise ValueError("invalid fixed prerequisite argv")
        if not isinstance(artifact_suffixes, (list, tuple)) or not 1 <= len(artifact_suffixes) <= 16 or any(suffix not in {".exe", ".msi", ".deb", ".rpm", ".bin", ".py"} for suffix in artifact_suffixes):
            raise ValueError("invalid prerequisite artifact suffixes")
        used = []
        for arg in argv:
            match = _PLACEHOLDER.fullmatch(arg)
            if match:
                index = int(match[1])
                if index >= len(artifact_suffixes):
                    raise ValueError("unknown prerequisite artifact placeholder")
                used.append(index)
            elif "{artifact" in arg:
                raise ValueError("artifact placeholders must occupy the whole argument")
        if set(used) != set(range(len(artifact_suffixes))) or len(used) != len(set(used)):
            raise ValueError("each artifact must be consumed exactly once")
        if not _PLACEHOLDER.fullmatch(argv[0]):
            if not Path(argv[0]).is_absolute() or not _HASH.fullmatch(str(executable_sha256)):
                raise ValueError("fixed system executable needs absolute path and hash")
            if Path(argv[0]).name.lower() in _SHELLS or Path(argv[0]).suffix.lower() in {".bat", ".cmd"}:
                raise ValueError("shell execution is forbidden")
        elif executable_sha256 is not None:
            raise ValueError("artifact executable uses its signed artifact hash")
        if type(elevated) is not bool or type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError("invalid prerequisite execution policy")
        codes = [tuple(group) for group in (success_codes, reboot_codes, relogin_codes, canceled_codes)]
        flat = [code for group in codes for code in group]
        if not codes[0] or len(flat) > 64 or len(set(flat)) != len(flat) or any(type(code) is not int or not 0 <= code < 2**32 for code in flat):
            raise ValueError("exit code groups must be bounded and disjoint")
        if any(value is not None and not callable(value) for value in (publisher_check, preflight)) or not callable(runner_factory):
            raise ValueError("trusted adapter callbacks must be callable")
        self.adapter_id, self.os_name, self.argv = adapter_id, os_name, tuple(argv)
        self.artifact_suffixes = tuple(artifact_suffixes)
        self.executable_sha256, self.elevated, self.timeout = executable_sha256, elevated, timeout
        self.success_codes, self.reboot_codes, self.relogin_codes, self.canceled_codes = codes
        self.environment = dict(environment or {})
        self.publisher_check, self.preflight, self.runner_factory = publisher_check, preflight, runner_factory
        self.policy_digest = digest({"adapter_id": adapter_id, "os": os_name, "argv": list(argv),
            "suffixes": list(artifact_suffixes), "executable_sha256": executable_sha256, "elevated": elevated,
            "timeout": timeout, "exit_codes": [list(group) for group in codes], "environment_digest": digest(self.environment),
            "publisher_check_required": os_name == "windows", "has_preflight": preflight is not None})

    def check(self, entry, inventory):
        if inventory.get("platform", {}).get("os") != self.os_name or len(entry["artifacts"]) != len(self.artifact_suffixes):
            fail("CAPABILITY_UNSUPPORTED", "prerequisite_adapter_target", entry["requirement"]["component_id"])
        if self.os_name == "windows" and self.publisher_check is None:
            fail("CAPABILITY_UNSUPPORTED", "windows_publisher_verifier_required")
        if self.elevated and self.runner_factory is FixedCommandRunner and _admin() is not True:
            fail("PERMISSION_REQUIRED", "prerequisite_elevated_helper_required")
        if self.preflight is not None and self.preflight(entry, inventory) is not True:
            fail("PREREQUISITE_MISSING", "prerequisite_platform_preflight")

    def execute(self, entry, paths, *, downloads, inventory):
        self.check(entry, inventory)
        if platform.system().lower() != self.os_name:
            fail("CAPABILITY_UNSUPPORTED", "prerequisite_execution_host_mismatch")
        if not isinstance(downloads, DownloadManager) or len(paths) != len(entry["artifacts"]):
            fail("INTEGRITY_FAILED", "prerequisite_execution_artifacts")
        specs = [ArtifactSpec.from_dict(raw) for raw in entry["artifacts"]]
        for path, spec in zip(paths, specs):
            if not downloads.verify(spec, path):
                fail("INTEGRITY_FAILED", "prerequisite_bytes_before_execution")
            if self.publisher_check is not None and self.publisher_check(Path(path), spec) is not True:
                fail("INTEGRITY_FAILED", "prerequisite_publisher_rejected")
        argv = [str(paths[int(match[1])]) if (match := _PLACEHOLDER.fullmatch(arg)) else arg for arg in self.argv]
        match = _PLACEHOLDER.fullmatch(self.argv[0])
        executable_hash = specs[int(match[1])].sha256 if match else self.executable_sha256
        runner = self.runner_factory([tuple(argv)], executable_hashes={argv[0]: executable_hash}, environment=self.environment)
        result = runner.run(argv, timeout=self.timeout, elevated=self.elevated)
        if not isinstance(result, CommandResult):
            fail("RECOVERY_REQUIRED", "prerequisite_invalid_execution_result")
        return result

    def outcome(self, result):
        if not result.started:
            return "NOT_STARTED"
        if result.timed_out or result.returncode is None:
            return "UNKNOWN"
        for codes, state in ((self.success_codes, "VERIFY"), (self.reboot_codes, "REBOOT_REQUIRED"),
                             (self.relogin_codes, "RELOGIN_REQUIRED"), (self.canceled_codes, "CANCELED")):
            if result.returncode in codes:
                return state
        return "FAILED"


def docker_desktop_user_adapter(*, environment, publisher_check, preflight):
    """Профиль Docker Desktop per-user, WSL2. Pins и terms — в signed catalogue.

    preflight обязан доказать Windows/build/virtualization/WSL compatibility;
    publisher_check использует OS trust APIs с ожидаемым Docker publisher.
    Сам installer не гарантирует запущенный daemon: coordinator ждёт discover.
    """
    if not callable(preflight) or not callable(publisher_check):
        raise ValueError("Docker Desktop needs platform and publisher verification")
    return FixedCommandAdapter("docker-desktop-user-v1", os_name="windows",
        argv=("{artifact_0}", "install", "--user", "--quiet", "--accept-license", "--backend=wsl-2", "--no-windows-containers"),
        artifact_suffixes=(".exe",), environment=environment, publisher_check=publisher_check, preflight=preflight)


def docker_ubuntu_deb_adapter(*, dpkg_executable, dpkg_sha256, preflight):
    """Полный заранее закреплённый набор пяти deb, без apt resolver/removal.

    Порядок: containerd.io, docker-ce, docker-ce-cli, buildx, compose. Проверка
    конфликтов, distro/архитектуры и внешних OS deps обязательна в preflight.
    Доступ пользователя/rootless/служба подтверждаются discover отдельно.
    """
    if not callable(preflight):
        raise ValueError("Docker Engine needs explicit conflict/dependency preflight")
    return FixedCommandAdapter("docker-ubuntu-deb-v1", os_name="linux", elevated=True,
        argv=(str(dpkg_executable), "--install", *(f"{{artifact_{index}}}" for index in range(5))),
        artifact_suffixes=(".deb",) * 5, executable_sha256=dpkg_sha256, preflight=preflight)
