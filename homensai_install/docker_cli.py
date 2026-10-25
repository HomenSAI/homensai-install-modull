# HomenS.AI · © 2026 Serhii Khomenko
"""Ограниченный запуск доверенного Docker CLI без implicit context и shell.

Этот модуль не ищет, не скачивает и не запускает Docker daemon. Bootstrap
передаёт абсолютный executable, его закреплённый SHA-256, локальный endpoint
и существующий приватный пустой config directory. Controller отдельно
доказывает владение daemon/container и допустимость каждой операции.

Исполнение в этом выпуске реализовано для Linux ELF + local Unix endpoint.
Точная Windows named-pipe URI распознаётся, но без ACL/handle backend Windows
исполнение запрещено. Поддержка URI не означает Windows acceptance.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

from shared.datahome import DataHomeError, checked_path
from .engine import EngineError


MAX_OUTPUT_BYTES = 1024 * 1024  # Совокупно stdout + stderr; stderr не сохраняется.
MAX_ARGUMENT_BYTES = 32768
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 300.0
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_NPIPE_ENDPOINT = "npipe:////./pipe/docker_engine"


@dataclass(frozen=True, slots=True)
class DockerCommandResult:
    returncode: int
    stdout: bytes


def _fail(code, reason, *, outcome=None):
    details = {"reason": reason}
    if outcome is not None:
        details["outcome"] = outcome
    raise EngineError(code, "Ограниченный Docker CLI отклонил операцию", details=details)


def _path(value, reason):
    if not isinstance(value, (str, Path)):
        _fail("INVALID_REQUEST", reason)
    try:
        return checked_path(value)
    except (DataHomeError, OSError, ValueError, TypeError):
        _fail("INVALID_REQUEST", reason)


def _endpoint(value):
    if type(value) is not str or len(value) > 4096:
        _fail("INVALID_REQUEST", "docker_endpoint_invalid")
    if value == _NPIPE_ENDPOINT:
        return value
    if (not value.startswith("unix:///") or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or any(c in value for c in "%?#\\")):
        _fail("INVALID_REQUEST", "docker_endpoint_must_be_local")
    path = _path(value[len("unix://"):], "docker_socket_path_invalid")
    if path == Path(path.anchor) or "unix://" + str(path) != value:
        _fail("INVALID_REQUEST", "docker_socket_path_not_canonical")
    return value


def _protected_parents(path):
    """Владельцы ancestor — текущий пользователь/root, writable только sticky."""
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or (stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX)):
            _fail("PERMISSION_REQUIRED", "docker_cli_parent_not_protected")


def _private_config(value):
    path = _path(value, "docker_config_path_invalid")
    if path == Path(path.anchor):
        _fail("INVALID_REQUEST", "docker_config_root_rejected")
    try:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            _fail("PERMISSION_REQUIRED", "docker_config_not_private")
        _protected_parents(path)
        # Не наследуем credential helpers, cli-plugins, contexts или auth headers.
        # Directory используется исключительно этим runner и никогда им не создаётся.
        with os.scandir(path) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                if len(names) > 1:
                    _fail("INVALID_REQUEST", "docker_config_must_be_empty")
        if names:
            if names != ["config.json"]:
                _fail("INVALID_REQUEST", "docker_config_must_be_empty")
            config = _path(path / "config.json", "docker_config_file_invalid")
            item = config.lstat()
            if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid() or item.st_nlink != 1
                    or stat.S_IMODE(item.st_mode) & 0o077 or item.st_size > 4096):
                _fail("PERMISSION_REQUIRED", "docker_config_file_not_private")
            with config.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096 or json.loads(raw) != {}:
                _fail("INVALID_REQUEST", "docker_config_features_not_permitted")
        return path
    except EngineError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        _fail("PREREQUISITE_MISSING", "docker_private_config_unavailable")


def _arguments(args):
    if isinstance(args, (str, bytes, bytearray)) or not isinstance(args, Sequence):
        _fail("INVALID_REQUEST", "docker_argv_sequence_required")
    if not 1 <= len(args) <= 128:
        _fail("INVALID_REQUEST", "docker_argv_limit")
    values = tuple(args)
    total = 0
    for value in values:
        if type(value) is not str or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            _fail("INVALID_REQUEST", "docker_argv_invalid")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            _fail("INVALID_REQUEST", "docker_argv_invalid")
        if size > 8192:
            _fail("INVALID_REQUEST", "docker_argv_limit")
        total += size + 1
    if total > MAX_ARGUMENT_BYTES:
        _fail("INVALID_REQUEST", "docker_argv_limit")
    allowed = values == ("info",) or (len(values) == 3 and values[:2] == ("info", "--format"))
    if (len(values) == 6 and values[0] in {"image", "container", "network"}
            and values[1:3] == ("inspect", "--format") and values[4] == "--"):
        identity = values[5]
        allowed = bool(_HEX.fullmatch(identity[len("sha256:"):])) if values[0] == "image" and identity.startswith("sha256:") else bool(values[0] != "image" and _HEX.fullmatch(identity))
    if (len(values) == 6 and values[:5] == ("container", "ls", "--all", "--no-trunc", "--format")):
        allowed = True
    if len(values) == 4 and values[:3] == ("container", "start", "--"):
        allowed = bool(_HEX.fullmatch(values[3]))
    if len(values) == 5 and values[:4] == ("container", "kill", "--signal=SIGTERM", "--"):
        allowed = bool(_HEX.fullmatch(values[4]))
    if (len(values) >= 5 and values[:3] == ("container", "exec", "--")
            and _HEX.fullmatch(values[3]) and not values[4].startswith("-")):
        allowed = True
    if not allowed:
        _fail("INVALID_REQUEST", "docker_command_not_permitted")
    return values


class DockerCLI:
    """Без daemon calls при construction; rehash/permissions проверяются на run.

    ``config_dir`` должен быть существующим private directory, пустым либо
    содержащим единственный private ``config.json`` с пустым JSON object.
    Любая ошибка timeout/overflow оставляет результат мутации неизвестным:
    controller обязан inspect/reconcile, а не слепо повторять команду.
    """

    def __init__(self, executable, sha256, endpoint, config_dir):
        if type(sha256) is not str or _HEX.fullmatch(sha256) is None:
            _fail("INVALID_REQUEST", "docker_cli_sha256_required")
        self._endpoint = _endpoint(endpoint)
        self._executable = _path(executable, "docker_cli_absolute_path_required")
        self._sha256 = sha256
        if not sys.platform.startswith("linux") or os.name != "posix":
            _fail("CAPABILITY_UNSUPPORTED", "docker_cli_acl_execution_backend_unavailable")
        self._config_dir = _private_config(config_dir)
        descriptor = self._verified_executable()
        os.close(descriptor)

    @property
    def endpoint(self):
        return self._endpoint

    def _verified_executable(self):
        path = _path(self._executable, "docker_cli_path_changed")
        descriptor = None
        try:
            _protected_parents(path)
            # Сначала тип: открытие FIFO/device само по себе не должно повиснуть
            # или иметь побочный эффект. NONBLOCK закрывает FIFO race до fstat.
            if not stat.S_ISREG(path.lstat().st_mode):
                _fail("INTEGRITY_FAILED", "docker_cli_regular_file_required")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, os.getuid()}
                    or before.st_mode & 0o022 or before.st_mode & (stat.S_ISUID | stat.S_ISGID)
                    or before.st_nlink != 1 or not 4 <= before.st_size <= MAX_EXECUTABLE_BYTES
                    or not os.access(path, os.X_OK)):
                _fail("INTEGRITY_FAILED", "docker_cli_executable_not_protected")
            digest = hashlib.sha256()
            first = os.read(descriptor, 65536)
            if not first.startswith(b"\x7fELF"):
                _fail("CAPABILITY_UNSUPPORTED", "docker_cli_native_elf_required")
            digest.update(first)
            total = len(first)
            while block := os.read(descriptor, 65536):
                total += len(block)
                if total > MAX_EXECUTABLE_BYTES:
                    _fail("INTEGRITY_FAILED", "docker_cli_executable_limit")
                digest.update(block)
            after = os.fstat(descriptor)
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if (identity(before) != identity(after) or total != before.st_size
                    or not hmac.compare_digest(digest.hexdigest(), self._sha256)):
                _fail("INTEGRITY_FAILED", "docker_cli_digest_mismatch")
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.set_blocking(descriptor, True)
            result, descriptor = descriptor, None
            return result
        except EngineError:
            raise
        except (OSError, ValueError):
            _fail("PREREQUISITE_MISSING", "docker_cli_executable_unavailable")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def run(self, args: Sequence[str], *, timeout: float = 30) -> DockerCommandResult:
        values = _arguments(args)
        if (type(timeout) not in {int, float} or not 0 < timeout <= MAX_TIMEOUT_SECONDS
                or not math.isfinite(timeout)):
            _fail("INVALID_REQUEST", "docker_cli_timeout_invalid")
        if not self._endpoint.startswith("unix:///"):
            _fail("CAPABILITY_UNSUPPORTED", "docker_cli_named_pipe_execution_unavailable")
        # Не доверяем последующей подмене config, endpoint path или executable.
        _endpoint(self._endpoint)
        config = _private_config(self._config_dir)
        descriptor = self._verified_executable()
        process = None
        try:
            # Descriptor pin предотвращает pathname-replacement между hash и exec.
            # Родительские каталоги защищены; активный вредоносный процесс того же
            # пользователя/root не объявляется устранённым одной проверкой SHA-256.
            fd_path = "/proc/self/fd/" + str(descriptor)
            if not Path("/proc/self/fd").is_dir():
                _fail("CAPABILITY_UNSUPPORTED", "docker_cli_descriptor_exec_unavailable")
            argv = [str(self._executable), "--host", self._endpoint, "--config", str(config), *values]
            env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": str(config), "PATH": "",
                   "DOCKER_CLI_HINTS": "false"}
            try:
                process = subprocess.Popen(argv, executable=fd_path, cwd=config, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, close_fds=True, pass_fds=(descriptor,), start_new_session=True)
            except (OSError, ValueError, subprocess.SubprocessError):
                _fail("PREREQUISITE_MISSING", "docker_cli_process_start_failed")
        finally:
            os.close(descriptor)
        return self._collect(process, timeout=float(timeout))

    @staticmethod
    def _collect(process, *, timeout):
        stdout = bytearray()
        total = 0
        reason = None
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                for stream, retain in ((process.stdout, True), (process.stderr, False)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, retain)
                while selector.get_map() or process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "docker_cli_deadline_exceeded"
                        break
                    for key, _ in selector.select(min(remaining, 0.05)):
                        try:
                            block = os.read(key.fd, 8192)
                        except BlockingIOError:
                            continue
                        if not block:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        total += len(block)
                        if total > MAX_OUTPUT_BYTES:
                            reason = "docker_cli_output_limit"
                            break
                        if key.data:
                            stdout.extend(block)
                    if reason:
                        break
                if reason is None:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except (OSError, ValueError, subprocess.SubprocessError):
            reason = "docker_cli_stream_unavailable"
        finally:
            # Только Popen child. Никаких killpg, pkill, container kill или daemon stop.
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except OSError:
                    reason = reason or "docker_cli_child_cleanup_failed"
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                reason = reason or "docker_cli_child_cleanup_failed"
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        if reason:
            _fail("RECOVERY_REQUIRED", reason, outcome="unknown")
        return DockerCommandResult(returncode=process.returncode, stdout=bytes(stdout))
