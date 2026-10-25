"""Read-only preflight и честный manual/recheck план зависимостей.

Автоустановка системных пакетов не заявлена: без проверенного платформенного
адаптера она отклоняется. Никаких sudo/apt/WSL install/curl|shell здесь нет.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import InstallerError, redact, utcnow
from .i18n import normalize_locale

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_VERSION = re.compile(r"^v?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?:\+[A-Za-z0-9_.-]+)?$")
_ENV_NAMES = ("PATH", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
              "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
              "PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "HTTP_PROXY",
              "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "DOCKER_HOST", "DOCKER_CONTEXT",
              "DOCKER_CONFIG", "COMPOSE_PROJECT_NAME", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
_PATH_LIST_ENV = {"PATH", "PYTHONPATH", "LD_LIBRARY_PATH"}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_STATE = {"present", "missing", "unsupported", "manual", "incompatible", "reboot_required", "relogin_required"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ProbeResult:
    returncode: int | None
    stdout: str = ""
    timed_out: bool = False
    output_limited: bool = False
    error_code: str | None = None


class SafeProbeRunner:
    """Запускает только точные argv из доверенного allowlist, с ограничениями.

    Конфигурация runner — доверенный Python-код. Её нельзя загружать из
    непроверенного манифеста, статьи справочника или HTTP-запроса.
    """
    def __init__(self, allowed_commands: dict[str, list[tuple[str, ...]]] | None = None, *, max_output_bytes=16384):
        if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 1024 * 1024:
            raise ValueError("invalid probe output limit")
        self.max_output_bytes = max_output_bytes
        self.allowed = {}
        for executable, arguments in (allowed_commands or {}).items():
            path = Path(executable)
            if not path.is_absolute():
                raise ValueError("probe executables must be absolute")
            if not isinstance(arguments, (list, tuple)) or not all(isinstance(args, (list, tuple)) and all(isinstance(arg, str) and "\x00" not in arg for arg in args) for args in arguments):
                raise ValueError("invalid probe command allowlist")
            self.allowed[str(path.resolve())] = {tuple(args) for args in arguments}

    def run(self, argv, *, timeout=5.0, env=None) -> ProbeResult:
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
            raise ValueError("probe argv must be a string list")
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("invalid probe timeout")
        executable = Path(argv[0])
        resolved = str(executable.resolve())
        if not executable.is_absolute() or tuple(argv[1:]) not in self.allowed.get(resolved, set()):
            raise InstallerError("CAPABILITY_UNSUPPORTED", phase="preflight", details={"reason": "probe_not_allowlisted"})
        # No shell profiles, PYTHONPATH, LD_PRELOAD, credentials, arbitrary PATH,
        # or user-supplied flags enter a diagnostic subprocess implicitly.
        child_env = {"PATH": os.pathsep.join((str(Path(resolved).parent), os.defpath)), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        for name in ("SystemRoot", "WINDIR", "SYSTEMDRIVE"):
            if name in os.environ and os.name == "nt":
                child_env[name] = os.environ[name]
        if env:
            if set(env) - {"DOCKER_CONFIG", "HOME", "USERPROFILE", "LOCALAPPDATA"}:
                raise ValueError("unsupported probe environment override")
            for name, value in env.items():
                if not isinstance(value, str) or "\x00" in value:
                    raise ValueError("invalid probe environment value")
                child_env[name] = value
        output = bytearray()
        limited = threading.Event()
        try:
            process = subprocess.Popen([resolved, *argv[1:]], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, env=child_env,
                start_new_session=os.name != "nt")
        except OSError:
            return ProbeResult(None, error_code="PROBE_START_FAILED")

        def drain():
            try:
                while True:
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        return
                    remaining = self.max_output_bytes - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        limited.set()
                        return
            except (OSError, ValueError):
                return
            finally:
                process.stdout.close()

        def stop(force=False):
            try:
                if os.name != "nt":
                    # This group belongs to the new session created above;
                    # no unrelated process or shared service is signalled.
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                elif force:
                    process.kill()
                else:
                    process.terminate()
            except ProcessLookupError:
                pass

        reader = threading.Thread(target=drain, daemon=True, name="hinstall-safe-probe")
        reader.start()
        timed_out = False
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if limited.is_set() or time.monotonic() >= deadline:
                    timed_out = not limited.is_set()
                    stop()
                    try:
                        process.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        stop(force=True)
                    break
                time.sleep(.01)
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            stop(force=True)
            process.wait(timeout=1)
        finally:
            if timed_out or limited.is_set():
                stop(force=True)
            reader.join(timeout=.5)
            if reader.is_alive() and os.name != "nt":
                stop(force=True)
                reader.join(timeout=.5)
        raw = bytes(output)
        # Native WSL may print UTF-16LE even through a pipe.
        encoding = "utf-16-le" if raw and raw.count(b"\x00") > len(raw) // 4 else "utf-8"
        text = raw.decode(encoding, errors="replace")
        return ProbeResult(process.returncode, text, timed_out, limited.is_set())


def _os_release():
    result = {}
    try:
        with Path("/etc/os-release").open("rb") as handle:
            raw = handle.read(8193)
        if len(raw) > 8192:
            return {"status": "unavailable"}
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            if key in {"ID", "VERSION_ID", "PRETTY_NAME"}:
                result[key.lower()] = value.strip().strip('"').strip("'")[:256]
    except OSError:
        return {"status": "unavailable"}
    return result


def _memory_bytes():
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("physical", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                    ("pagefile", ctypes.c_ulonglong), ("available_pagefile", ctypes.c_ulonglong),
                    ("virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                    ("extended", ctypes.c_ulonglong)]
            memory = MemoryStatus()
            memory.length = ctypes.sizeof(memory)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                return None
            return memory.physical
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        return None


def _admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin()) if os.name == "nt" else os.geteuid() == 0
    except (OSError, AttributeError):
        return None


def _environment_scope(values, source):
    records = {}
    for name in _ENV_NAMES:
        value = values.get(name)
        if value is None:
            value = values.get(name.lower())
        item = {"present": value is not None, "source": source, "value_exposed": False}
        if isinstance(value, str):
            if name in _PATH_LIST_ENV:
                entries = value.split(os.pathsep)
                item.update(entry_count=len(entries), contains_empty=any(not entry for entry in entries),
                            contains_relative=any(entry and not Path(entry).is_absolute() for entry in entries))
            # Environment values may contain secrets even under innocuous
            # names such as TEMP. Inventory records structure and presence.
            item["contains_control_characters"] = any(ord(char) < 32 for char in value)
        records[name] = item
    return {"status": "inspected", "variables": records}


def _windows_environment():
    if os.name != "nt":
        return {"machine": {"status": "unavailable", "reason": "not_windows_host"},
                "user": {"status": "unavailable", "reason": "not_windows_host"}}
    import winreg
    result = {}
    for scope, hive, key_name in (
            ("machine", winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ("user", winreg.HKEY_CURRENT_USER, r"Environment")):
        values = {}
        try:
            with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ) as key:
                for name in _ENV_NAMES:
                    try:
                        value, value_type = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                        values[name] = value
            result[scope] = _environment_scope(values, scope)
        except OSError:
            result[scope] = {"status": "unavailable", "reason": "registry_access_unavailable"}
    return result


def probe_port(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid local port")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
            handle.bind(("127.0.0.1", port))
        return {"port": port, "host": "127.0.0.1", "status": "available"}
    except OSError as error:
        import errno
        reason = "in_use" if error.errno in (errno.EADDRINUSE, 10048) else "unavailable"
        return {"port": port, "host": "127.0.0.1", "status": reason}


def _disk(path):
    candidate = Path(path).absolute()
    for item in (candidate, *candidate.parents):
        if item.is_symlink():
            return {"path": redact(str(candidate)), "status": "unsafe", "reason": "symlink_path"}
    existing = candidate
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        usage = shutil.disk_usage(existing)
        return {"path": redact(str(candidate)), "status": "available", "exists": candidate.exists(),
                "free_bytes": usage.free, "total_bytes": usage.total, "parent_writable": os.access(existing, os.W_OK)}
    except OSError:
        return {"path": redact(str(candidate)), "status": "unavailable"}


def _program(name, os_name, supplied):
    if name in supplied:
        path = Path(supplied[name])
        if not path.is_absolute() or not path.is_file():
            return None
        return str(path.resolve())
    candidates = {
        "linux": {"docker": ("/usr/bin/docker", "/usr/local/bin/docker")},
        "windows": {"docker": (r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",),
                    "wsl": (r"C:\Windows\System32\wsl.exe",)},
    }
    for candidate in candidates.get(os_name, {}).get(name, ()):
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
    return None


def _result(runner, argv):
    try:
        result = runner.run(argv, timeout=5)
        if isinstance(result, dict):
            result = ProbeResult(**result)
        if not isinstance(result, ProbeResult):
            return ProbeResult(None, error_code="INVALID_PROBE_RESULT")
        return result
    except (OSError, ValueError, InstallerError):
        return ProbeResult(None, error_code="PROBE_UNAVAILABLE")


def _component(status, *, version=None, reason=None, scope="host", evidence=None):
    return {"status": status, "version": version, "reason": reason, "scope": scope,
            "ownership": "external", "automatic_install_supported": False, "evidence": evidence or []}


def collect_inventory(*, paths=(), ports=(), program_paths=None, runner=None, environ=None,
                      platform_info=None, port_probe=None) -> dict:
    if not isinstance(paths, (list, tuple)) or not isinstance(ports, (list, tuple)) or len(paths) > 32 or len(ports) > 64:
        raise ValueError("inventory request is too large")
    if not all(isinstance(path, (str, Path)) and "\x00" not in str(path) for path in paths) or not all(type(port) is int and 1 <= port <= 65535 for port in ports):
        raise ValueError("invalid inventory path or port")
    environment = dict(os.environ if environ is None else environ)
    supplied = dict(program_paths or {})
    if set(supplied) - {"docker", "wsl"}:
        raise ValueError("unsupported probe program")
    actual_os = platform.system().lower()
    info = {"os": actual_os, "release": platform.release(), "arch": platform.machine().lower()}
    if platform_info is not None:
        if not isinstance(platform_info, dict) or set(platform_info) - {"os", "release", "arch"}:
            raise ValueError("invalid platform probe fixture")
        info.update(platform_info)
    if not all(isinstance(value, str) and len(value) <= 256 for value in info.values()):
        raise ValueError("invalid platform information")
    os_name = info["os"]
    info["arch"] = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(info["arch"], info["arch"])
    if os_name == "linux":
        info["distribution"] = _os_release()
    docker = _program("docker", os_name, supplied)
    wsl = _program("wsl", os_name, supplied) if os_name == "windows" else None
    docker_args = None
    if docker:
        endpoint = "npipe:////./pipe/docker_engine" if os_name == "windows" else "unix:///var/run/docker.sock"
        docker_args = ("--host", endpoint, "version", "--format", "{{json .}}")
    commands = {}
    if docker:
        commands[docker] = [docker_args, ("compose", "version", "--short")]
    if wsl:
        commands[wsl] = [("--status",), ("--version",)]
    active_runner = runner if runner is not None else SafeProbeRunner(commands)
    components = {
        "controller_runtime": _component("present", version=platform.python_version(), scope="controller",
            reason="controller_runtime_does_not_prove_product_runtime"),
        "sqlite": _component("present", version=sqlite3.sqlite_version, scope="controller",
            reason="controller_sqlite_does_not_prove_product_database_readiness"),
        "docker_engine": _component("missing", reason="trusted_docker_executable_not_found"),
        "docker_compose": _component("missing", reason="trusted_docker_executable_not_found"),
        "wsl": _component("unsupported" if os_name != "windows" else "missing", reason="windows_only" if os_name != "windows" else "wsl_executable_not_found"),
    }
    if docker:
        version = _result(active_runner, [docker, *docker_args])
        if version.returncode == 0 and not version.timed_out and not version.output_limited:
            try:
                value = json.loads(version.stdout)
                server = value.get("Server")
                if not isinstance(server, dict) or not isinstance(server.get("Version"), str) or not _VERSION.fullmatch(server["Version"]):
                    raise ValueError("missing Docker server version")
                components["docker_engine"] = _component("present", version=server["Version"],
                    evidence=[{"check": "local_docker_api", "ok": True}])
            except (ValueError, TypeError, AttributeError):
                components["docker_engine"] = _component("manual", reason="docker_version_response_unrecognized")
        else:
            components["docker_engine"] = _component("manual", reason="local_docker_api_not_ready",
                evidence=[{"check": "local_docker_api", "ok": False, "returncode": version.returncode}])
        compose = _result(active_runner, [docker, "compose", "version", "--short"])
        detected = compose.stdout.strip()
        if compose.returncode == 0 and not compose.timed_out and not compose.output_limited and _VERSION.fullmatch(detected):
            components["docker_compose"] = _component("present", version=detected.lstrip("v"),
                evidence=[{"check": "compose_version", "ok": True}])
        else:
            components["docker_compose"] = _component("missing", reason="compatible_compose_version_not_detected")
    if wsl:
        result = _result(active_runner, [wsl, "--status"])
        version_result = _result(active_runner, [wsl, "--version"])
        found = re.search(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?)", version_result.stdout[:16384])
        if result.returncode == 0 and not result.timed_out and not result.output_limited:
            components["wsl"] = _component("present", version=found.group(1) if found else None,
                evidence=[{"check": "wsl_status", "ok": True}])
        elif result.returncode == 3010:
            components["wsl"] = _component("reboot_required", reason="wsl_reports_restart")
        else:
            components["wsl"] = _component("manual", reason="wsl_not_ready")
    managers = []
    if os_name == "linux":
        for name, path in (("apt", "/usr/bin/apt-get"), ("dpkg", "/usr/bin/dpkg"), ("dnf", "/usr/bin/dnf"),
                           ("rpm", "/usr/bin/rpm"), ("pacman", "/usr/bin/pacman"), ("zypper", "/usr/bin/zypper")):
            if Path(path).is_file():
                managers.append({"name": name, "executable": path, "automatic_install_supported": False})
    elif os_name == "windows" and os.name == "nt":
        candidates = [("msiexec", Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "msiexec.exe")]
        if environment.get("LOCALAPPDATA"):
            candidates.append(("winget", Path(environment["LOCALAPPDATA"]) / "Microsoft" / "WindowsApps" / "winget.exe"))
        for name, path in candidates:
            if path.is_file():
                managers.append({"name": name, "executable": redact(str(path)), "automatic_install_supported": False,
                                 "presence_only": True})
    scopes = {"process": _environment_scope(environment, "process"),
              "machine": {"status": "not_inspected", "reason": "shell_profiles_not_executed"},
              "user": {"status": "not_inspected", "reason": "shell_profiles_not_executed"},
              "service": {"status": "not_inspected", "reason": "requires_installation_specific_service_adapter"}}
    if os_name == "windows":
        scopes.update(_windows_environment())
    report = {"schema_version": 1, "timestamp": utcnow(), "platform": info,
              "evidence_level": "simulated" if any(value is not None for value in (platform_info, runner, environ, port_probe)) else "local_read_only",
              "identity": {"scope": "current_process", "elevated": _admin(), "uid": os.getuid() if hasattr(os, "getuid") else None},
              "resources": {"cpu_count": os.cpu_count(), "physical_memory_bytes": _memory_bytes()},
              "disks": [_disk(path) for path in paths],
              "ports": [(port_probe or probe_port)(port) for port in ports],
              "environment": scopes, "components": components, "package_managers": managers,
              "container": {"marker_detected": Path("/.dockerenv").exists(), "host_access_proven": False},
              "wsl_environment": {"marker_detected": os_name == "linux" and
                  ("microsoft" in info["release"].lower() or "WSL_INTEROP" in environment or "WSL_DISTRO_NAME" in environment),
                  "host_windows_access_proven": False},
              "virtualization": {"status": "not_checked", "reason": "firmware_or_platform_specific_probe_required"},
              "capabilities": {"read_only_inventory": os_name in {"windows", "linux"}, "pinned_download": True,
                  "automatic_package_install": False, "manual_recheck": True, "automatic_reboot": False},
              "limitations": ["System package installation adapters are not accepted.",
                  "The default Docker probe is local only; remote contexts and custom/rootless endpoints need an explicit adapter.",
                  "Controller runtime/SQLite versions do not establish product runtime or database readiness.",
                  "Windows registry/WSL probing requires separate installed Windows acceptance."]}
    # Stable discovery fingerprint excludes observation time and private values.
    fingerprint_fields = {key: report[key] for key in ("platform", "identity", "resources", "disks", "ports", "components")}
    report["fingerprint"] = hashlib.sha256(_canonical(fingerprint_fields).encode()).hexdigest()
    return redact(report)


@dataclass(frozen=True)
class DependencyRequirement:
    component_id: str
    kind: str
    required: bool = True
    minimum_version: str | None = None
    maximum_version: str | None = None
    scope: str = "host"

    def __post_init__(self):
        if not isinstance(self.component_id, str) or not isinstance(self.kind, str) or not _ID.fullmatch(self.component_id) or not _ID.fullmatch(self.kind) or type(self.required) is not bool:
            raise ValueError("invalid dependency requirement")
        if not isinstance(self.scope, str) or self.scope not in {"host", "controller", "product"}:
            raise ValueError("invalid dependency scope")
        for version in (self.minimum_version, self.maximum_version):
            if version is not None and (not isinstance(version, str) or not _VERSION.fullmatch(version)):
                raise ValueError("unsupported dependency version constraint")
        if self.minimum_version and self.maximum_version and _version(self.minimum_version) > _version(self.maximum_version):
            raise ValueError("dependency version range is empty")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {"component_id", "kind", "required", "minimum_version", "maximum_version", "scope"}:
            raise ValueError("invalid dependency requirement fields")
        if not {"component_id", "kind"} <= set(value):
            raise ValueError("dependency identity is required")
        return cls(**value)


def _version(value):
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value)
    return tuple(int(item or 0) for item in match.groups()) if match else None


def manual_instructions(kind, locale="ru", os_name="linux"):
    locale = normalize_locale(locale)
    urls = {
        "docker_engine": "https://docs.docker.com/desktop/setup/install/windows-install/" if os_name == "windows" else "https://docs.docker.com/engine/install/",
        "docker_compose": "https://docs.docker.com/compose/install/",
        "wsl": "https://learn.microsoft.com/en-us/windows/wsl/basic-commands#install",
    }
    texts = {
        "ru": ["Откройте официальную инструкцию для вашей ОС и проверьте совместимость и условия компонента.",
               "Установите или подготовьте компонент штатным способом. Не удаляйте чужие данные и общие зависимости.",
               "Если система попросит перезагрузку или повторный вход, сохраните работу и выполните это по своему выбору.",
               "Вернитесь в мастер и нажмите «Проверить снова». Установка приложения продолжится только после проверки обязательных компонентов."],
        "en": ["Open the official instructions for your OS and review component compatibility and terms.",
               "Install or prepare the component using its supported procedure. Preserve unrelated data and shared dependencies.",
               "If a restart or sign-in is required, save your work and choose when to do so.",
               "Return to the wizard and check again. Application installation continues only after required components are verified."],
        "de": ["Öffnen Sie die offizielle Anleitung für Ihr Betriebssystem und prüfen Sie Kompatibilität und Bedingungen.",
               "Installieren oder richten Sie die Komponente mit dem unterstützten Verfahren ein. Erhalten Sie fremde Daten und gemeinsam genutzte Abhängigkeiten.",
               "Falls Neustart oder erneute Anmeldung erforderlich sind, speichern Sie Ihre Arbeit und wählen Sie den Zeitpunkt.",
               "Kehren Sie zum Assistenten zurück und prüfen Sie erneut. Die Installation wird erst nach Prüfung aller erforderlichen Komponenten fortgesetzt."],
    }
    return {"locale": locale, "official_url": urls.get(kind), "steps": texts[locale], "contains_executable_commands": False}


def check_requirements(requirements, inventory, locale="ru"):
    if not isinstance(requirements, (list, tuple)) or len(requirements) > 128 or not isinstance(inventory, dict):
        raise ValueError("invalid prerequisites input")
    components = inventory.get("components", {})
    if not isinstance(components, dict):
        raise ValueError("invalid prerequisite inventory")
    result = []
    seen = set()
    for raw in requirements:
        requirement = raw if isinstance(raw, DependencyRequirement) else DependencyRequirement.from_dict(raw)
        if requirement.component_id in seen:
            raise ValueError("duplicate dependency identifier")
        seen.add(requirement.component_id)
        found = components.get(requirement.kind)
        status, reason, detected = "unsupported", "detector_not_implemented", None
        if isinstance(found, dict):
            status = found.get("status") if found.get("status") in _STATE else "manual"
            detected, reason = found.get("version"), found.get("reason")
            if found.get("scope", "host") != requirement.scope:
                status, reason = "unsupported", "component_scope_does_not_prove_requirement"
            elif status == "present" and (requirement.minimum_version or requirement.maximum_version):
                version = _version(detected)
                if version is None:
                    status, reason = "manual", "version_cannot_be_compared"
                elif requirement.minimum_version and version < _version(requirement.minimum_version):
                    status, reason = "incompatible", "version_below_minimum"
                elif requirement.maximum_version and version > _version(requirement.maximum_version):
                    status, reason = "incompatible", "version_above_maximum"
        result.append({"component_id": requirement.component_id, "kind": requirement.kind, "required": requirement.required,
            "status": status, "ready": status == "present", "reason": reason, "detected_version": detected,
            "minimum_version": requirement.minimum_version, "maximum_version": requirement.maximum_version,
            "scope": requirement.scope, "ownership": "external", "automatic_install_supported": False,
            "manual": manual_instructions(requirement.kind, locale, inventory.get("platform", {}).get("os", "linux")),
            "evidence": found.get("evidence", []) if isinstance(found, dict) else []})
    return redact(result)


def plan_prerequisites(requirements, inventory, locale="ru"):
    checks = check_requirements(requirements, inventory, locale)
    if not isinstance(inventory.get("fingerprint"), str) or not _SHA.fullmatch(inventory["fingerprint"]):
        raise ValueError("a discovery fingerprint is required")
    specs = [asdict(item if isinstance(item, DependencyRequirement) else DependencyRequirement.from_dict(item)) for item in requirements]
    body = {"schema_version": 1, "requirements": specs, "checks": checks,
            "inventory_fingerprint": inventory.get("fingerprint"), "locale": normalize_locale(locale),
            "automatic_install_supported": False,
            "actions": [{"component_id": check["component_id"], "strategy": "manual", "required": check["required"],
                         "instructions": check["manual"]} for check in checks if not check["ready"]]}
    digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
    return {**body, "plan_digest": digest, "required_consents": ["prerequisites:review:" + digest]}


def apply_prerequisite_plan(plan, *, consents, inventory, automatic=False):
    """Проверяет план и возвращает READY/AWAITING_USER; системных изменений нет."""
    expected_keys = {"schema_version", "requirements", "checks", "inventory_fingerprint", "locale",
                     "automatic_install_supported", "actions", "plan_digest", "required_consents"}
    if type(automatic) is not bool or not isinstance(plan, dict) or set(plan) != expected_keys or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise InstallerError("INVALID_REQUEST", phase="preflight")
    body = {key: value for key, value in plan.items() if key not in {"plan_digest", "required_consents"}}
    digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
    if digest != plan["plan_digest"] or plan["required_consents"] != ["prerequisites:review:" + digest]:
        raise InstallerError("INTEGRITY_FAILED", phase="preflight")
    if not isinstance(consents, (list, tuple, set)) or not all(isinstance(value, str) for value in consents) or not set(plan["required_consents"]) <= set(consents):
        raise InstallerError("CONSENT_REQUIRED", phase="preflight")
    current = plan_prerequisites(plan["requirements"], inventory, plan["locale"])
    if current["plan_digest"] != plan["plan_digest"]:
        raise InstallerError("PLAN_STALE", phase="preflight")
    if automatic and plan["actions"]:
        raise InstallerError("CAPABILITY_UNSUPPORTED", phase="preflight", details={"reason": "system_install_adapter_not_accepted"})
    blocking = [item for item in current["checks"] if item["required"] and not item["ready"]]
    if any(item["status"] == "reboot_required" for item in blocking):
        status = "REBOOT_REQUIRED"
    elif any(item["status"] == "relogin_required" for item in blocking):
        status = "RELOGIN_REQUIRED"
    else:
        status = "AWAITING_USER" if blocking else "READY"
    return {"status": status, "ready": not blocking, "blocking_components": [item["component_id"] for item in blocking],
            "checks": current["checks"], "system_changes_performed": False, "automatic_install_supported": False}
