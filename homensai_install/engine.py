# HomenS.AI · © 2026 Serhii Khomenko
"""Реальные локальные user-session операции поверх существующих FS/crypto ports.

Контроллер не скачивает и не доверяет переданному флагу ``verified``. Доверенный
verifier внедряется bootstrap/SDK и связывает подпись с payload, manifest,
release, target и runtime. По умолчанию исполнение нового payload запрещено.
Windows/Docker/system service acceptance этим модулем не заявляется.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request
import uuid

from shared import archive_crypto
from shared.adapters_fs import FilesystemPorts, ReleaseSource, _copy_file, _tree
from shared.datahome import checked_path, contains, resolve_datahome
from shared.manifest import validate_manifest
from shared.services import component_order
from shared.update_plan import UpdatePlan, _atomic_write, operation_lock


class EngineError(RuntimeError):
    """Стабильный код без stdout/hooks, секретов или исходного исключения."""

    def __init__(self, code: str, message: str = "", *, details=None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(message or code)


class _UncertainEffect(BaseException):
    """Не отдавать обычному UpdatePlan exception: нельзя слепо повторить hook."""


class _Canceled(BaseException):
    """Отмена на durable границе не является ошибкой product hook."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path):
    with checked_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read(path: Path):
    path = checked_path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise EngineError("RECOVERY_REQUIRED", "Служебные метаданные слишком велики")
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise EngineError("RECOVERY_REQUIRED", "Повтор поля метаданных")
            out[key] = value
        return out
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise EngineError("RECOVERY_REQUIRED", "Метаданные недоступны или повреждены") from None


def _write(path, data, *, private_parent=True):
    path = checked_path(path)
    if private_parent:
        _private_dir(path.parent)
    else:
        # Явно выбранный пользователем каталог экспорта не меняет ACL/mode.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, data)
    if os.name != "nt":
        path.chmod(0o600)


def _owner_id():
    if os.name != "nt":
        return "uid:" + str(os.getuid())
    import ctypes
    import ctypes.wintypes as wt
    token = wt.HANDLE()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise EngineError("PERMISSION_REQUIRED", "Не удалось проверить identity Windows")
    try:
        size = wt.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise EngineError("PERMISSION_REQUIRED")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wt.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise EngineError("PERMISSION_REQUIRED")
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def _private_dir(path):
    path = checked_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        if path.stat().st_uid != os.getuid():
            raise EngineError("PERMISSION_REQUIRED", "Каталог принадлежит другой identity")
        path.chmod(0o700)
    else:
        # Только собственный user scope; SID исходного пользователя задаёт
        # bootstrap до elevation. Этот backend сам не повышает привилегии.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
            raise EngineError("PERMISSION_REQUIRED")
        exe = str(Path(buffer.value) / "icacls.exe")
        result = subprocess.run([exe, str(path), "/inheritance:r", "/grant:r",
                                 _owner_id() + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=20, check=False)
        if result.returncode:
            raise EngineError("PERMISSION_REQUIRED", "Не удалось установить приватный ACL")


def _birth(pid):
    """Kernel birth identity; PID сам по себе владение не доказывает."""
    try:
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes as wt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wt.HANDLE
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                times = [wt.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
                    return None
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(handle)
        proc = Path("/proc") / str(pid)
        raw = (proc / "stat").read_text()
        tail = raw.rsplit(")", 1)[1].split()
        if tail[0] == "Z":
            return None
        uid = proc.stat().st_uid
        if uid != os.getuid():
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{tail[19]}:{uid}"
    except (OSError, IndexError, ValueError):
        return None


def _pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Некоторые standalone Python собраны со старыми headers, хотя host libc
    # предоставляет pidfd. Используем её именованную функцию без syscall IDs.
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        fn = libc.pidfd_open
    except AttributeError:
        raise EngineError("CAPABILITY_UNSUPPORTED", "Kernel process handle недоступен") from None
    fn.argtypes, fn.restype = (ctypes.c_int, ctypes.c_uint), ctypes.c_int
    fd = fn(pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return fd


def _pidfd_signal(fd, sig):
    if hasattr(signal, "pidfd_send_signal"):
        return signal.pidfd_send_signal(fd, sig)
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        fn = libc.pidfd_send_signal
    except AttributeError:
        raise EngineError("CAPABILITY_UNSUPPORTED", "Kernel process signal недоступен") from None
    fn.argtypes, fn.restype = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint), ctypes.c_int
    if fn(fd, sig, None, 0) < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeEngine:
    """User-scope backend. Внедрённые verifier/provider — доверенный код SDK.

    ``development_runtime`` допускается только в явно создаваемом dev engine,
    никогда не читается из HTTP options. Такой запуск не является offline
    standalone install и возвращает ``production_ready=False``.
    """

    ACTIONS = ("install", "start", "stop", "backup", "restore", "update",
               "rollback", "repair", "reinstall", "uninstall")

    def __init__(self, verifier=None, secret_provider=None, *, development_runtime=None):
        self.verifier = verifier
        self.secret_provider = secret_provider
        self.development_runtime = (Path(development_runtime).resolve(strict=True)
                                    if development_runtime else None)
        self._children = {}

    def capabilities(self, target):
        host = "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else sys.platform
        native = target.get("mode", "native") == "native" and target.get("os", host) == host
        native = native and target.get("scope", "user") == "user" and sys.platform.startswith("linux")
        host_arch = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "ARM64": "arm64"}.get(platform.machine(), platform.machine())
        native = native and target.get("arch", host_arch) == host_arch
        return {"target": target, "actions": {
            name: {"supported": bool(native), "readiness": "development",
                   "reason": None if native else "CAPABILITY_UNSUPPORTED"}
            for name in self.ACTIONS}, "production_ready": False,
            "execution_scope": "user-session", "manifest_schemas": [2],
            "deployment_form": "prebuilt-release", "installs_vendor_wheels": False,
            "service_autostart": False, "restart_supervision": False,
            "health_model": "owned_processes_owned_listener_and_product_http", "heartbeat_monitoring": False,
            "backup_strategy": "filesystem_quiesced", "product_backup_hooks_supported": False,
            "requires_trusted_payload_verifier": True,
            "development_runtime": self.development_runtime is not None}

    def _context(self, manifest, target, options):
        manifest = validate_manifest(manifest)
        if manifest["schema_version"] != 2:
            raise EngineError("CAPABILITY_UNSUPPORTED", "Native требует полный manifest v2")
        if not self.capabilities(target)["actions"]["install"]["supported"]:
            raise EngineError("CAPABILITY_UNSUPPORTED", "Target/user scope не поддерживается на этом хосте")
        root = checked_path(options.get("install_root", ""))
        if root == Path(root.anchor):
            raise EngineError("INVALID_MANIFEST", "Корень диска не является install root")
        home = resolve_datahome(manifest, root)
        control = checked_path(root / ".hinstall")
        for path in (home.app, home.releases, *home.roots.values()):
            if path == root or not contains(root, path) or contains(path, control) or contains(control, path):
                raise EngineError("INVALID_MANIFEST", "Native paths должны быть раздельными дочерними install root")
        modes = options.get("modes")
        if modes is None and len(manifest["modes"]) == 1:
            modes = [manifest["modes"][0]["name"]]
        component_order(manifest, tuple(modes or ()))
        release = options.get("release")
        if release is not None and (not isinstance(release, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", release) is None):
            raise EngineError("INVALID_MANIFEST", "Некорректный release ID")
        work = checked_path(options.get("work_dir", str(root.parent / ("." + root.name + "-recovery"))))
        if contains(root, work) or contains(work, root) or work == Path(work.anchor):
            raise EngineError("INVALID_MANIFEST", "Recovery work directory должен быть вне установки")
        refs = options.get("secret_refs", {})
        if not isinstance(refs, dict) or set(refs) - {item["name"] for item in manifest["secrets"]}:
            raise EngineError("INVALID_MANIFEST", "Неизвестные ссылки на product secrets")
        if any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", value) is None
               for value in refs.values()):
            raise EngineError("INVALID_MANIFEST", "Некорректная ссылка на product secret")
        return {"manifest": manifest, "target": dict(target), "options": dict(options),
                "root": root, "home": home, "control": control,
                "work": work, "modes": tuple(modes), "release": release}

    def _owner(self, ctx, *, required=False):
        path = ctx["control"] / "installation.json"
        if not path.exists():
            if required:
                raise EngineError("RECOVERY_REQUIRED", "Установка не имеет ownership metadata")
            return None
        owner = _read(path)
        if (owner.get("product_id") != ctx["manifest"]["product_id"]
                or owner.get("owner") != _owner_id() or owner.get("mode") != "native"
                or not re.fullmatch(r"[0-9a-f]{32}", owner.get("installation_id", ""))):
            raise EngineError("OPERATION_CONFLICT", "Установка принадлежит другой identity или продукту")
        return owner

    def _pointer(self, ctx):
        path = ctx["home"].roots["services"] / ctx["manifest"]["update"]["pointer_file"]
        if not path.exists():
            return None
        value = _read(path)
        if set(value) != {"release"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(value.get("release", ""))):
            raise EngineError("RECOVERY_REQUIRED", "Повреждён active pointer")
        return value["release"]

    def discover(self, manifest, target, options):
        ctx = self._context(manifest, target, options)
        owner = self._owner(ctx)
        active = self._pointer(ctx) if owner else None
        root = ctx["root"]
        occupied = root.exists() and any(root.iterdir()) if root.is_dir() else root.exists()
        essential = {"host": platform.system(), "arch": platform.machine(),
                     "owner": _owner_id(), "root": str(root), "active": active,
                     "installation_id": owner.get("installation_id") if owner else None,
                     "registered": owner.get("installed", False) if owner else False,
                     "root_occupied": occupied}
        records = self._records(ctx) if owner else {}
        processes = {name: {"running": self._running(record), "release": record.get("release"),
                            "state": record.get("status", "recorded")}
                     for name, record in records.items()}
        integrity = {"state": "NOT_INSTALLED", "release": active}
        if owner and active and owner.get("installed"):
            if self.verifier is None:
                integrity.update(state="UNVERIFIED", error_code="PREREQUISITE_MISSING")
            else:
                try:
                    self._installed(ctx, active)
                    integrity["state"] = "VERIFIED"
                except Exception:
                    integrity.update(state="VIOLATED", error_code="INTEGRITY_FAILED")
        elif owner and not active:
            integrity["state"] = "INCOMPLETE"
        essential["integrity"] = integrity
        pending = []
        folder = ctx["control"] / "operations"
        if owner and folder.exists():
            for file in sorted(folder.glob("*.json")):
                operation = _read(file)
                if operation.get("status") in {"RUNNING", "CANCELED", "RECOVERY_REQUIRED", "FAILED"}:
                    pending.append({"receipt": file.stem, "action": operation.get("action"),
                                    "status": operation.get("status"), "phase": operation.get("phase")})
        essential.update(self._port_state(ctx, records))
        for option in ("inventory_file", "signature_file", "archive_path"):
            path = options.get(option)
            if path:
                file = checked_path(path)
                essential[option] = _file_digest(file) if file.is_file() else None
        from .prerequisites import collect_inventory
        environment_inventory = collect_inventory(paths=[str(ctx["root"])])
        essential["environment_fingerprint"] = environment_inventory["fingerprint"]
        return {"fingerprint_data": essential, "installation": owner,
                "environment_inventory": environment_inventory,
                "active_release": active, "runtime": {"system_python_required": False},
                "integrity": integrity, "components": processes,
                "unfinished_operations": pending,
                "production_ready": False, "read_only": True}

    def status(self, manifest, target, options):
        """Состояние установки, процессов и signed bytes без запуска/исправлений."""
        return self.discover(manifest, target, options)

    def plan(self, action, manifest, target, inventory, options):
        ctx = self._context(manifest, target, options)
        if action not in self.ACTIONS:
            raise EngineError("CAPABILITY_UNSUPPORTED")
        mutates_payload = action in {"install", "update", "repair", "reinstall"}
        if action in {"install", "start", "update", "repair", "reinstall"} and inventory.get("fingerprint_data", {}).get("port_conflict"):
            raise EngineError("OPERATION_CONFLICT", "Порт занят чужим процессом")
        if mutates_payload and (not ctx["release"] or not options.get("payload_dir")):
            raise EngineError("INVALID_MANIFEST", "Нужны release и payload_dir")
        if action in {"restore", "rollback"} and not options.get("archive_path"):
            raise EngineError("INVALID_MANIFEST", "Восстановление требует явного архива")
        if action == "rollback" and options.get("acknowledge_data_loss") is not True:
            raise EngineError("CONSENT_REQUIRED", "Поздний rollback требует осознанного выбора snapshot")
        steps = {
            "install": ["verify", "prepare", "stage", "migrate", "activate", "start", "health"],
            "update": ["verify", "stop", "backup", "stage", "migration_backups", "migrate", "activate", "start", "health"],
            "backup": ["stop", "backup", "start", "health"],
            "restore": ["verify_archive", "stop", "before_restore", "restore", "activate", "start", "health"],
            "rollback": ["verify_archive", "stop", "before_rollback", "restore", "activate", "start", "health"],
            "repair": ["verify", "stop", "backup", "repair", "start", "health"],
            "reinstall": ["verify", "stop", "backup", "repair", "start", "health"],
            "uninstall": ["stop", "preserve_recovery", "remove_owned_code"],
            "start": ["verify", "start", "health"], "stop": ["stop"],
        }[action]
        # Согласия пользователя хранит общий facade. Текст EULA в payload
        # должен быть включён в inventory; здесь нет автоматического принятия.
        consents = ["action:" + action]
        if mutates_payload:
            eula = options.get("eula_digest")
            if not isinstance(eula, str) or re.fullmatch(r"[a-f0-9]{64}", eula) is None:
                raise EngineError("CONSENT_REQUIRED", "Нужен hash показанного соглашения")
            consents.append("terms:" + eula)
        if action == "rollback":
            consents.append("restore-snapshot-data")
        return {"steps": [{"id": x, "component_id": "product", "weight": 1} for x in steps],
                "resources": [str(ctx["root"]), str(ctx["work"])],
                "required_consents": consents, "scope": "user-session",
                "preserves_user_data": True, "production_ready": False,
                "backup_strategy": "filesystem_quiesced", "product_backup_hooks_supported": False,
                "consistency_requires": ["all_writers_are_owned_components", "graceful_stop_completed"],
                "legacy_commands_not_executed": ["backup.cmd", "restore.cmd"]}

    def _password(self, ctx):
        reference = ctx["options"].get("backup_password_ref")
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", reference):
            raise EngineError("PREREQUISITE_MISSING", "Нужен защищённый источник пароля архива")
        if self.secret_provider is None:
            raise EngineError("PREREQUISITE_MISSING", "Secret provider не подключён")
        try:
            password = self.secret_provider(reference)
        except Exception:
            raise EngineError("PREREQUISITE_MISSING", "Источник пароля недоступен") from None
        if not isinstance(password, str) or len(password) < 12:
            raise EngineError("PREREQUISITE_MISSING", "Пароль архива отсутствует или слишком короткий")
        ctx.setdefault("secret_cache", {})[reference] = password
        return lambda: password

    def _verify(self, manifest, target, options):
        if self.verifier is None:
            raise EngineError("INTEGRITY_FAILED", "Не настроен доверенный verifier")
        result = self.verifier(manifest, target, options)
        if not isinstance(result, Mapping) or not result:
            raise EngineError("INTEGRITY_FAILED", "Verifier не вернул inventory")
        actual = _tree(checked_path(options["payload_dir"]))[1]
        if actual != dict(result):
            raise EngineError("INTEGRITY_FAILED", "Payload изменился после проверки")
        return dict(result)

    def _metadata_path(self, ctx, release):
        return ctx["control"] / "releases" / (release + ".json")

    def _release(self, ctx, release):
        return checked_path(ctx["home"].releases / ("app-" + release))

    def _metadata(self, ctx, release):
        data = _read(self._metadata_path(ctx, release))
        if data.get("release") != release or data.get("manifest", {}).get("product_id") != ctx["manifest"]["product_id"]:
            raise EngineError("RECOVERY_REQUIRED", "Несовместимые release metadata")
        return data

    def _installed(self, ctx, release):
        meta = self._metadata(ctx, release)
        opts = {"payload_dir": str(self._release(ctx, release)), "release": release,
                "runtime_executable": meta["runtime_executable"],
                "inventory_file": str(ctx["control"] / "releases" / (release + ".inventory.json")),
                "signature_file": str(ctx["control"] / "releases" / (release + ".signature.json"))}
        inventory = self._verify(meta["manifest"], meta["target"], opts)
        if inventory != meta["inventory"]:
            raise EngineError("INTEGRITY_FAILED", "Release inventory изменён")
        return meta

    def _prepare(self, ctx):
        owner = self._owner(ctx)
        if owner is None:
            if ctx["root"].exists() and any(ctx["root"].iterdir()):
                raise EngineError("OPERATION_CONFLICT", "Непустой каталог не принадлежит установщику")
            _private_dir(ctx["root"])
            _private_dir(ctx["control"])
            owner = {"schema_version": 1, "product_id": ctx["manifest"]["product_id"],
                     "installation_id": uuid.uuid4().hex, "owner": _owner_id(),
                     "mode": "native", "installed": False}
            _write(ctx["control"] / "installation.json", owner)
        for path in (ctx["home"].app, ctx["home"].releases, *ctx["home"].roots.values()):
            _private_dir(path)
        marker = ctx["work"] / "owner.json"
        expected = {"installation_id": owner["installation_id"], "owner": owner["owner"]}
        if marker.exists() and _read(marker) != expected:
            raise EngineError("OPERATION_CONFLICT", "Recovery directory принадлежит другой установке")
        if not marker.exists():
            if ctx["work"].exists() and any(ctx["work"].iterdir()):
                raise EngineError("OPERATION_CONFLICT", "Recovery directory не пуст")
            _private_dir(ctx["work"])
            _write(marker, expected)
        else:
            _private_dir(ctx["work"])
        return owner

    def _publish_metadata(self, ctx, inventory):
        release, opts = ctx["release"], ctx["options"]
        runtime = opts.get("runtime_executable")
        if not isinstance(runtime, str) or runtime not in inventory:
            raise EngineError("INVALID_MANIFEST", "Runtime должен находиться в подписанном inventory")
        folder = ctx["control"] / "releases"
        _private_dir(folder)
        # Повтор не меняет уже опубликованные metadata иной версией.
        for key, suffix in (("inventory_file", ".inventory.json"), ("signature_file", ".signature.json")):
            source = checked_path(opts[key])
            destination = folder / (release + suffix)
            if destination.exists() and destination.read_bytes() != source.read_bytes():
                raise EngineError("OPERATION_CONFLICT", "Release metadata уже отличается")
            if not destination.exists():
                _copy_file(source, destination)
        destination = self._metadata_path(ctx, release)
        refs = opts.get("secret_refs")
        if refs is None and destination.exists():
            refs = _read(destination).get("secret_refs", {})
        value = {"release": release, "manifest": ctx["manifest"], "target": ctx["target"],
                 "modes": list(ctx["modes"]), "inventory": inventory,
                 "runtime_executable": runtime,
                 "secret_refs": refs or {}}
        if destination.exists() and _read(destination) != value:
            raise EngineError("OPERATION_CONFLICT", "Release ID уже занят другим содержимым")
        _write(destination, value)
        if os.name != "nt":
            checked_path(self._release(ctx, release) / runtime).chmod(0o700)
            for item in ctx["manifest"]["components"]:
                if item["cmd"][0] != "python":
                    path = checked_path(self._release(ctx, release) / item["cmd"][0])
                    if not contains(self._release(ctx, release), path) or item["cmd"][0] not in inventory:
                        raise EngineError("INVALID_MANIFEST", "Executable вне signed release")
                    path.chmod(0o700)
        self._installed(ctx, release)

    def _records(self, ctx):
        path = ctx["control"] / "processes.json"
        records = _read(path) if path.exists() else {}
        if not isinstance(records, dict):
            raise EngineError("RECOVERY_REQUIRED")
        return records

    def _running(self, record):
        live = (type(record.get("pid")) is int and record["pid"] > 0
                and record.get("birth") is not None and _birth(record["pid"]) == record["birth"])
        if not live or os.name == "nt":
            return live
        try:
            with (Path("/proc") / str(record["pid"]) / "environ").open("rb") as stream:
                values = stream.read(1024 * 1024).split(b"\0")
            matches = (b"HOMENSAI_INSTALLATION_ID=" + record["installation_id"].encode() in values
                       and b"HOMENSAI_RELEASE=" + record["release"].encode() in values)
            if record.get("launch_id"):
                matches = matches and b"HOMENSAI_LAUNCH_ID=" + record["launch_id"].encode() in values
            return matches
        except (OSError, KeyError, UnicodeError):
            return False

    def _adopt_launch(self, ctx, name, record):
        """Recovery между Popen и receipt: только exact random launch identity.

        Не найденный незавершённый launch нельзя слепо повторять. Проверяем
        kernel owner, executable, cwd, argv, installation/release/launch token.
        Никакие значения окружения не сохраняются и не выводятся.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", str(record.get("launch_id", ""))):
            raise EngineError("RECOVERY_REQUIRED", "Незавершённый launch не имеет identity")
        release = record.get("release")
        meta = self._installed(ctx, release)
        components = {item["name"]: item for item in meta["manifest"]["components"]}
        if name not in components:
            raise EngineError("OPERATION_CONFLICT", "Неизвестный launch component")
        argv = self._argv(ctx, release, components[name]["cmd"])
        if record.get("argv_digest") != _digest(argv):
            raise EngineError("OPERATION_CONFLICT", "Launch intent отличается от команды")
        executable = Path(argv[0]).resolve(strict=True)
        directory = self._release(ctx, release)
        candidates = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                if Path(os.readlink(entry / "cwd")) != directory or Path(os.readlink(entry / "exe")).resolve() != executable:
                    continue
                actual = (entry / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                if actual != [os.fsencode(item) for item in argv]:
                    continue
                candidate = {**record, "pid": int(entry.name), "birth": _birth(int(entry.name)), "status": "running"}
                if self._running(candidate):
                    candidates.append(candidate)
            except (OSError, ValueError):
                continue
        if len(candidates) != 1:
            raise EngineError("RECOVERY_REQUIRED", "Нельзя однозначно подтвердить незавершённый launch")
        return candidates[0]

    def _reconcile_launches(self, ctx, records):
        for name, record in tuple(records.items()):
            if record.get("status") == "launching":
                records[name] = self._adopt_launch(ctx, name, record)
                _write(ctx["control"] / "processes.json", records)
        return records

    def _owns_loopback_listener(self, records, port):
        """HTTP identity дополняется kernel socket ownership, а не заменяет его.

        /proc/net/tcp — inventory сокетов namespace; принадлежность доказывают
        inode в fd конкретного живого owned PID. Wildcard bind не соответствует
        фиксированному 127.0.0.1 контракту. IPv6/shared proxy требуют adapter.
        """
        try:
            listeners = set()
            for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
                fields = line.split()
                address, encoded_port = fields[1].split(":")
                if fields[3] != "0A" or int(encoded_port, 16) != port:
                    continue
                if address == "00000000":
                    return False
                if address == "0100007F":
                    listeners.add(fields[9])
            if not listeners:
                return False
            owned = set()
            for record in records.values():
                if not self._running(record):
                    continue
                found = set()
                try:
                    for entry in (Path("/proc") / str(record["pid"]) / "fd").iterdir():
                        try:
                            link = os.readlink(entry)
                        except OSError:
                            continue
                        match = re.fullmatch(r"socket:\[([0-9]+)\]", link)
                        if match:
                            found.add(match[1])
                except OSError:
                    continue
                if self._running(record):
                    owned.update(found)
            return listeners <= owned
        except (OSError, ValueError, IndexError):
            return False

    def _port_state(self, ctx, records):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", ctx["manifest"]["web"]["port"]))
                return {"port_conflict": False, "owned_listener": False}
            except OSError:
                owned = self._owns_loopback_listener(records, ctx["manifest"]["web"]["port"])
                return {"port_conflict": not owned, "owned_listener": owned}

    def _assert_session_quiesced(self, record):
        """Main PID exit не означает, что его foreground session опустела.

        Оставшиеся потомки не убиваются догадкой: backup/replace блокируются.
        Процессы, специально ушедшие через setsid/внешнюю службу, требуют
        другого adapter; этот backend не разрешает daemonizing components.
        """
        pid, birth = record.get("pid"), record.get("birth")
        if os.name == "nt" or type(pid) is not int or not isinstance(birth, str):
            return
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if not birth.startswith(boot + ":"):
            return
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal() or int(path.name) == pid:
                continue
            try:
                if path.stat().st_uid != os.getuid():
                    continue
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] != "Z" and int(fields[3]) == pid:
                    raise EngineError("RECOVERY_REQUIRED", "После main PID остались процессы компонента; snapshot запрещён")
            except (OSError, IndexError, ValueError):
                continue

    def _reap_child(self, pid):
        child = self._children.pop(pid, None)
        if child is not None:
            child.wait(timeout=1)
        else:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def _argv(self, ctx, release, command):
        meta = self._metadata(ctx, release)
        directory = self._release(ctx, release)
        if command[0] == "python":
            executable = self.development_runtime or checked_path(directory / meta["runtime_executable"])
        else:
            executable = checked_path(directory / command[0])
            if not contains(directory, executable) or command[0] not in meta["inventory"]:
                raise EngineError("INTEGRITY_FAILED", "Executable не принадлежит release")
        if not executable.is_file():
            raise EngineError("PREREQUISITE_MISSING", "Runtime недоступен")
        return [str(executable), *command[1:]]

    def _environment(self, ctx, release, component="hook"):
        # Не наследуем PYTHONPATH/LD_PRELOAD и произвольные секреты build shell.
        keep = ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL", "PATH")
        env = {key: os.environ[key] for key in keep if key in os.environ}
        owner = self._owner(ctx, required=True)
        manifest = self._metadata(ctx, release)["manifest"]
        env.update(HOMENSAI_PRODUCT_ID=manifest["product_id"],
                   HOMENSAI_INSTALLATION_ID=owner["installation_id"],
                   HOMENSAI_RELEASE=release, HOMENSAI_COMPONENT=component,
                   HOMENSAI_DATA_HOME=str(ctx["home"].roots["data"]),
                   HOMENSAI_CONFIG_HOME=str(ctx["home"].roots["config"]),
                   HOMENSAI_WEB_HOST="127.0.0.1", HOMENSAI_WEB_PORT=str(manifest["web"]["port"]),
                   HOMENSAI_MAINTENANCE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        env["PYTHONNOUSERSITE"] = "1"
        refs = self._secret_refs(ctx, release)
        env.update(self._secrets(manifest, refs, "admin" if component == "hook" else "service",
                                 cache=ctx.get("secret_cache")))
        return env

    def _secret_refs(self, ctx, release, meta=None):
        meta = meta or self._metadata(ctx, release)
        refs = meta.get("secret_refs", {})
        if (not isinstance(refs, dict) or set(refs) - {item["name"] for item in meta["manifest"]["secrets"]}
                or any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", value) is None
                       for value in refs.values())):
            raise EngineError("RECOVERY_REQUIRED", "Повреждены ссылки на product secrets")
        refs = dict(refs)
        # Rollback запускает старый release со старыми refs. Параметры новой
        # версии не подменяют его credentials при automatic rollback.
        if release == ctx["release"] or ctx.get("action") == "start":
            refs.update(ctx["options"].get("secret_refs", {}))
        return refs

    def _secrets(self, manifest, refs, context, *, cache=None):
        values = {}
        for item in manifest["secrets"]:
            if context not in item["contexts"]:
                continue
            name = item["name"]
            if name in {"PATH", "HOME", "USERPROFILE", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"} or name.startswith(("PYTHON", "HOMENSAI_", "LD_", "DYLD_")):
                raise EngineError("INVALID_MANIFEST", "Secret использует управляющее имя окружения")
            reference = refs.get(name, name)
            if cache is not None and reference in cache:
                secret = cache[reference]
            else:
                try:
                    secret = self.secret_provider(reference) if self.secret_provider else None
                except Exception:
                    secret = None
                if cache is not None:
                    cache[reference] = secret
            if secret is None and not item["required"]:
                continue
            if not isinstance(secret, str) or not secret or "\0" in secret:
                raise EngineError("PREREQUISITE_MISSING", "Обязательный product secret недоступен")
            values[name] = secret
        return values

    def _hook(self, ctx, release, command, key):
        path = ctx["work"] / "effects" / (_digest(key) + ".json")
        intent = {"release": release, "command_digest": _digest(command), "status": "started"}
        if path.exists():
            old = _read(path)
            if {k: old.get(k) for k in ("release", "command_digest")} != {k: intent[k] for k in ("release", "command_digest")}:
                raise EngineError("OPERATION_CONFLICT")
            if old.get("status") == "complete":
                return
            raise _UncertainEffect("Hook имеет неподтверждённый результат; нужен product recovery")
        self._installed(ctx, release)
        _write(path, intent)
        result = subprocess.run(self._argv(ctx, release, command), cwd=self._release(ctx, release),
                                env=self._environment(ctx, release), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=120, check=False)
        if result.returncode:
            raise EngineError("RECOVERY_REQUIRED", "Продуктовый hook завершился ошибкой",
                              details={"exit_code": result.returncode})
        _write(path, {**intent, "status": "complete"})

    def _start(self, ctx):
        release = self._pointer(ctx)
        if not release:
            raise EngineError("RECOVERY_REQUIRED", "Нет активного release")
        meta = self._installed(ctx, release)
        records = self._reconcile_launches(ctx, self._records(ctx))
        order = component_order(meta["manifest"], tuple(meta["modes"]))
        for name, record in records.items():
            if record.get("birth") and _birth(record.get("pid")) == record["birth"] and not self._running(record):
                raise EngineError("OPERATION_CONFLICT", "Живой process receipt не подтвердил installation identity")
            if not self._running(record):
                self._assert_session_quiesced(record)
            if self._running(record) and (name not in order or record.get("release") != release):
                raise EngineError("OPERATION_CONFLICT", "Работает другой набор процессов установки")
        if all(name in records and self._running(records[name]) for name in order):
            return
        for i, command in enumerate(meta["manifest"]["hooks"]["pre_start"]):
            result = subprocess.run(self._argv(ctx, release, command), cwd=self._release(ctx, release),
                                    env=self._environment(ctx, release), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=60, check=False)
            if result.returncode:
                raise EngineError("RECOVERY_REQUIRED", "Pre-start проверка не пройдена")
        components = {x["name"]: x for x in meta["manifest"]["components"]}
        for name in order:
            if name in records and self._running(records[name]):
                continue
            kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            argv = self._argv(ctx, release, components[name]["cmd"])
            env = self._environment(ctx, release, name)
            intent = {"pid": None, "birth": None, "release": release, "status": "launching",
                      "installation_id": self._owner(ctx, required=True)["installation_id"],
                      "launch_id": uuid.uuid4().hex, "argv_digest": _digest(argv)}
            records[name] = intent
            _write(ctx["control"] / "processes.json", records)
            env["HOMENSAI_LAUNCH_ID"] = intent["launch_id"]
            try:
                process = subprocess.Popen(argv, cwd=self._release(ctx, release), env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
            except OSError:
                records[name] = {**intent, "status": "spawn_failed"}
                _write(ctx["control"] / "processes.json", records)
                raise EngineError("RECOVERY_REQUIRED", "Компонент не удалось запустить") from None
            self._children[process.pid] = process
            birth = _birth(process.pid)
            if birth is None:
                if process.poll() is not None:
                    records[name] = {**intent, "pid": process.pid, "status": "exited"}
                    _write(ctx["control"] / "processes.json", records)
                raise EngineError("RECOVERY_REQUIRED", "Запущенный компонент уже завершился")
            records[name] = {**intent, "pid": process.pid, "birth": birth, "status": "running"}
            _write(ctx["control"] / "processes.json", records)

    def _terminate(self, record):
        if not self._running(record):
            if record.get("birth") and _birth(record.get("pid")) == record["birth"]:
                raise EngineError("OPERATION_CONFLICT", "Чужая identity живого процесса: stop запрещён")
            self._assert_session_quiesced(record)
            if type(record.get("pid")) is int and _birth(record["pid"]) is None:
                self._reap_child(record["pid"])
            return
        pid = record["pid"]
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes as wt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wt.HANDLE
            handle = kernel.OpenProcess(0x1000 | 0x00100000 | 0x0001, False, pid)
            if not handle:
                raise EngineError("RECOVERY_REQUIRED", "Процесс недоступен")
            try:
                times = [wt.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
                    raise EngineError("RECOVERY_REQUIRED")
                birth = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
                if birth != record["birth"]:
                    raise EngineError("OPERATION_CONFLICT", "PID принадлежит другому процессу")
                # Windows graceful stop требует product adapter/службу. Не
                # вызываем TerminateProcess и не обещаем согласованную БД.
                raise EngineError("CAPABILITY_UNSUPPORTED", "Windows graceful stop требует product adapter")
            finally:
                kernel.CloseHandle(handle)
        try:
            fd = _pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            if not self._running(record):
                return
            _pidfd_signal(fd, signal.SIGTERM)
            until = time.monotonic() + 10
            while self._running(record) and time.monotonic() < until:
                time.sleep(0.05)
            if self._running(record):
                raise EngineError("RECOVERY_REQUIRED", "Компонент не остановился согласованно; данные не заменены")
            self._reap_child(pid)
            self._assert_session_quiesced(record)
        finally:
            os.close(fd)

    def _stop(self, ctx):
        owner = self._owner(ctx, required=True)
        records = self._reconcile_launches(ctx, self._records(ctx))
        order = []
        for release in dict.fromkeys(record.get("release") for record in records.values()):
            meta = self._metadata(ctx, release)
            order.extend(name for name in component_order(meta["manifest"], tuple(meta["modes"])) if name in records and name not in order)
        if set(order) != set(records):
            raise EngineError("OPERATION_CONFLICT", "Неизвестный компонент в process receipts")
        for name in reversed(order):
            record = records[name]
            if record.get("installation_id") != owner["installation_id"]:
                raise EngineError("OPERATION_CONFLICT", "Process receipt другой установки")
            self._terminate(record)
            records.pop(name)
            _write(ctx["control"] / "processes.json", records)

    def _health(self, ctx):
        release = self._pointer(ctx)
        meta = self._metadata(ctx, release)
        manifest = meta["manifest"]
        expected = {"product_id": manifest["product_id"], "release": release,
                    "installation_id": self._owner(ctx, required=True)["installation_id"]}
        url = f"http://127.0.0.1:{manifest['web']['port']}{manifest['web']['healthz']}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        deadline = time.monotonic() + min(manifest["update"]["health_wait"], 120)
        while time.monotonic() < deadline:
            records = self._records(ctx)
            required = component_order(manifest, tuple(meta["modes"]))
            if any(name not in records or not self._running(records[name]) for name in required):
                raise EngineError("HEALTH_CHECK_FAILED", "Обязательный компонент не работает",
                                  details={"check": "owned_component_running"})
            if not self._owns_loopback_listener(records, manifest["web"]["port"]):
                time.sleep(0.1)
                continue
            try:
                with opener.open(url, timeout=1) as response:
                    body = response.read(65537)
                    value = json.loads(body) if len(body) <= 65536 else None
                    if (response.status == 200 and isinstance(value, dict)
                            and value.get("ready") is True and all(value.get(k) == v for k, v in expected.items())
                            and self._owns_loopback_listener(records, manifest["web"]["port"])):
                        for command in manifest["hooks"]["health_checks"]:
                            result = subprocess.run(self._argv(ctx, release, command), cwd=self._release(ctx, release),
                                                    env=self._environment(ctx, release), stdin=subprocess.DEVNULL,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                    timeout=30, check=False)
                            if result.returncode:
                                raise EngineError("RECOVERY_REQUIRED", "Product health hook не пройден")
                        return {**expected, "ready": True, "url": url}
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        raise EngineError("HEALTH_CHECK_FAILED", "Readiness не подтвердил owned listener, установку и версию",
                          details={"check": "owned_listener_and_product_readiness"})

    def _recovery_copy(self, ctx, release):
        destination = checked_path(ctx["home"].roots["runtime"] / "recovery" / release)
        if not self._release(ctx, release).exists() and destination.exists():
            meta = _read(destination / "release.json")
            self._verify(meta["manifest"], meta["target"], {
                "payload_dir": str(destination / "payload"), "release": release,
                "runtime_executable": meta["runtime_executable"],
                "inventory_file": str(destination / "release.inventory.json"),
                "signature_file": str(destination / "release.signature.json")})
            return destination
        meta = self._installed(ctx, release)
        _private_dir(destination)
        payload = destination / "payload"
        if not payload.exists():
            _private_dir(payload)
            FilesystemPorts._reconcile(self._release(ctx, release), payload, set())
        if _tree(payload)[1] != meta["inventory"]:
            raise EngineError("INTEGRITY_FAILED", "Recovery payload не совпадает с release")
        for suffix in (".json", ".inventory.json", ".signature.json"):
            _copy_file(ctx["control"] / "releases" / (release + suffix), destination / ("release" + suffix))
        return destination

    def _backup(self, ctx, path, password):
        release = self._pointer(ctx)
        if release:
            self._recovery_copy(ctx, release)
        path = checked_path(path)
        binding = {"operation": ctx["operation_token"], "installation_id": self._owner(ctx, required=True)["installation_id"]}
        receipt = path.with_name(path.name + ".receipt.json")
        if path.exists():
            if not receipt.exists() or _read(receipt) != binding:
                raise EngineError("OPERATION_CONFLICT", "Архив уже существует и не относится к этой операции")
            if not archive_crypto.is_encrypted(path):
                raise EngineError("INTEGRITY_FAILED", "Ожидался шифрованный архив")
            archive_crypto.verify_archive(path, password_provider=password,
                                           expected_product_id=ctx["manifest"]["product_id"])
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive_crypto.build_encrypted_archive(ctx["home"], path, password_provider=password)
            _write(receipt, binding, private_parent=False)
        return {"path": str(path), "sha256": _file_digest(path),
                "release": release, "encrypted": True, "includes_recovery_payload": bool(release)}

    def _ports(self, ctx, inventory, password, emit):
        engine = self
        effects = _Effects(engine, ctx, emit)
        class Ports(FilesystemPorts):
            def _begin(self, phase):
                if ctx.get("action") == "update":
                    ctx["boundary"](phase)
            def _end(self, phase):
                if ctx.get("action") == "update":
                    emit({"phase": phase, "status": "SUCCEEDED", "completed": 1, "total": 1})
            def stage_release(self, key, release):
                self._begin("stage")
                super().stage_release(key, release)
                engine._publish_metadata(ctx, inventory)
                self._end("stage")
            def backup(self, key, purpose, command):
                self._begin(purpose)
                current = engine._pointer(ctx)
                if current:
                    engine._recovery_copy(ctx, current)
                super().backup(key, purpose, command)
                self._end(purpose)
            def switch_pointer(self, key, release):
                self._begin("activate")
                super().switch_pointer(key, release)
                self._end("activate")
            def restore(self, key, archive_key, command):
                self._begin("restore")
                super().restore(key, archive_key, command)
                self._end("restore")
        payloads = ({ctx["release"]: ReleaseSource(checked_path(ctx["options"]["payload_dir"]), inventory)}
                    if inventory else {})
        return Ports(ctx["manifest"], ctx["root"], work_dir=ctx["work"], payloads=payloads,
                     effects=effects, password_provider=password)

    def execute(self, plan, emit=lambda event: None, cancel_requested=lambda: False):
        manifest = plan.get("source_manifest", plan.get("manifest"))
        ctx = self._context(manifest, plan["target"], plan.get("options", {}))
        action = plan["action"]
        ctx["action"] = action
        if action not in self.ACTIONS:
            raise EngineError("CAPABILITY_UNSUPPORTED")
        inventory = None
        if action in {"install", "update", "repair", "reinstall"}:
            inventory = self._verify(ctx["manifest"], ctx["target"], ctx["options"])
            locale = ctx["options"].get("eula_locale", "en")
            if locale not in {"ru", "en", "de"} or inventory.get("legal/" + locale + ".txt") != ctx["options"].get("eula_digest"):
                raise EngineError("CONSENT_REQUIRED", "Hash соглашения не совпадает с подписанным текстом")
        if cancel_requested():
            return {"status": "CANCELED", "production_ready": False}
        ctx["secret_cache"] = {}
        password = (self._password(ctx) if action in {"update", "backup", "restore", "rollback", "repair", "reinstall"}
                    else None)
        if action != "install":
            self._owner(ctx, required=True)
        if action in {"install", "start", "update", "repair", "reinstall"}:
            records = self._records(ctx) if self._owner(ctx) else {}
            # Незавершённый launch разрешается reconciliation под lock, но
            # отсутствие receipt не даёт права доверять чужому listener.
            unresolved_launch = any(record.get("status") == "launching" for record in records.values())
            if not unresolved_launch and self._port_state(ctx, records)["port_conflict"]:
                raise EngineError("PORT_IN_USE", "Порт занят не подтверждённым listener")
        if action in {"install", "update", "repair", "reinstall", "start"}:
            refs = ctx["options"].get("secret_refs", {})
            current = self._pointer(ctx) if self._owner(ctx) else None
            if current and (current == ctx["release"] or action == "start"):
                refs = self._secret_refs(ctx, current)
            for context in ("service", "admin"):
                self._secrets(ctx["manifest"], refs, context, cache=ctx["secret_cache"])
            if action == "update" and current:
                previous = self._metadata(ctx, current)
                for context in ("service", "admin"):
                    self._secrets(previous["manifest"], self._secret_refs(ctx, current, previous), context,
                                  cache=ctx["secret_cache"])
        # Claim, все записи журнала и файловые эффекты сериализуются одним
        # inode lock, даже при использовании разных API state_dir.
        lock = checked_path(ctx["root"].parent / (".hinstall-" + _digest(str(ctx["root"]))[:24] + ".lock"))
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if lock.exists() and os.name != "nt" and lock.stat().st_uid != os.getuid():
            raise EngineError("PERMISSION_REQUIRED", "Lifecycle lock принадлежит другому пользователю")
        token = _digest([plan.get("plan_id", "direct"), plan.get("plan_digest", _digest(plan))])[:32]
        ctx["operation_token"] = token
        journal = ctx["control"] / "operations" / (token + ".json")
        with operation_lock(lock):
            self._prepare(ctx)
            self._owner(ctx, required=True)
            previous = _read(journal) if journal.exists() else None
            if previous and previous.get("status") in {"SUCCEEDED", "FAILED_ROLLED_BACK"}:
                return previous["result"]
            if previous and previous.get("status") in {"FAILED", "RECOVERY_REQUIRED"} and action not in {"start", "stop", "update"}:
                raise EngineError("RECOVERY_REQUIRED", "Неудавшийся шаг требует проверенного восстановления")
            # Незавершённый другой mutating plan нельзя обойти новым token.
            if action not in {"stop"}:
                pending = journal.parent.glob("*.json") if journal.parent.exists() else ()
                for file in pending:
                    if file == journal:
                        continue
                    other = _read(file)
                    if (other.get("status") in {"RUNNING", "CANCELED", "RECOVERY_REQUIRED"}
                            and other.get("action") in {"install", "update", "restore", "rollback", "repair", "reinstall"}):
                        raise EngineError("RECOVERY_REQUIRED", "Нужно возобновить исходный незавершённый план")
            state = previous or {"action": action, "status": "RUNNING", "completed": [],
                                 "initial_active": self._pointer(ctx),
                                 "initial_data_empty": not any(ctx["home"].roots["data"].iterdir()),
                                 "was_running": any(self._running(record) for record in self._records(ctx).values())}
            state["status"] = "RUNNING"
            ctx["operation_state"] = state
            _write(journal, state)

            def boundary(name):
                if cancel_requested():
                    raise _Canceled()
                state["phase"] = name
                _write(journal, state)
                emit({"phase": name, "status": "RUNNING", "completed": 0, "total": None})

            def step(name, fn):
                # Процессы/health — наблюдаемое состояние, после crash их нужно
                # подтвердить снова. Файловые durable эффекты не повторяются.
                completed = name in state["completed"]
                if completed and name not in {"start", "stop", "health"}:
                    return state.get("step_results", {}).get(name)
                boundary(name)
                result = fn()
                if not completed:
                    state["completed"].append(name)
                if isinstance(result, dict):
                    state.setdefault("step_results", {})[name] = result
                _write(journal, state)
                emit({"phase": name, "status": "SUCCEEDED", "completed": 1, "total": 1})
                return result

            ctx["boundary"] = boundary
            try:
                result = self._execute_locked(ctx, action, token, inventory, password, step, emit)
            except _Canceled:
                result = {"status": "CANCELED", "resume_required": True,
                          "phase": state.get("phase"), "production_ready": False}
                state.update(status="CANCELED", result=result)
                _write(journal, state)
                return result
            except _UncertainEffect:
                state.update(status="RECOVERY_REQUIRED", error_code="RECOVERY_REQUIRED")
                _write(journal, state)
                raise EngineError("RECOVERY_REQUIRED", "Результат продуктового hook не подтверждён; повтор запрещён") from None
            except Exception as error:
                state.update(status="FAILED", error_code=getattr(error, "code", "RECOVERY_REQUIRED"))
                _write(journal, state)
                raise
            result.setdefault("backup_strategy", "filesystem_quiesced")
            result.setdefault("production_ready", False)
            state.update(status=result.get("status", "SUCCEEDED"), result=result)
            _write(journal, state)
            return result

    def _execute_locked(self, ctx, action, token, inventory, password, step, emit):
        active = self._pointer(ctx)
        owner = self._owner(ctx, required=True)
        if active and action not in {"update", "install", "repair", "reinstall", "restore", "rollback"}:
            meta = self._metadata(ctx, active)
            if _digest(meta["manifest"]) != _digest(ctx["manifest"]):
                raise EngineError("PLAN_STALE", "Manifest отличается от активной установки")
        if active and action in {"update", "install", "repair", "reinstall"}:
            meta = self._metadata(ctx, active)
            if meta["manifest"]["paths"] != ctx["manifest"]["paths"]:
                raise EngineError("CAPABILITY_UNSUPPORTED", "Изменение DataHome требует отдельной миграции")
        if action == "install":
            if active and active != ctx["release"]:
                raise EngineError("OPERATION_CONFLICT", "Существующая версия требует update")
            if (not active and any(ctx["home"].roots["data"].iterdir())
                    and not ctx["operation_state"].get("initial_data_empty")):
                raise EngineError("RECOVERY_REQUIRED", "Существующие данные нельзя инициализировать как новые")
            # Fresh не использует архив, но FilesystemPorts требует явной
            # archive policy. Этот provider не будет вызываться при staging.
            ports = self._ports(ctx, inventory, lambda: "unused-fresh-provider", emit)
            step("stage", lambda: ports.stage_release(token + "/stage", ctx["release"]))
            if not active:
                step("migrate", lambda: self._hook(ctx, ctx["release"], ctx["manifest"]["migrations"]["cmd"], token + "/fresh-migration"))
            step("activate", lambda: ports.switch_pointer(token + "/switch", ctx["release"]))
            step("start", lambda: self._start(ctx))
            health = step("health", lambda: self._health(ctx))
            _write(ctx["control"] / "installation.json", {**owner, "installed": True})
            return {"status": "SUCCEEDED", "release": ctx["release"], "health": health, "production_ready": False}
        if not active:
            raise EngineError("RECOVERY_REQUIRED", "Нет активной установки")
        if action == "start":
            step("start", lambda: self._start(ctx))
            return {"status": "SUCCEEDED", "health": step("health", lambda: self._health(ctx)), "production_ready": False}
        if action == "stop":
            step("stop", lambda: self._stop(ctx))
            return {"status": "SUCCEEDED", "stopped": True}
        if action == "backup":
            running = ctx["operation_state"]["was_running"]
            step("stop", lambda: self._stop(ctx))
            path = ctx["options"].get("archive_path", str(ctx["work"] / (token + ".hsac")))
            try:
                result = step("backup", lambda: self._backup(ctx, path, password))
            except Exception:
                # Данные при обычном failure backup не менялись; возвращаем
                # ранее работавшие процессы. Crash/cancel остаются resumable.
                if running:
                    self._start(ctx)
                    self._health(ctx)
                raise
            if running:
                step("start", lambda: self._start(ctx))
                step("health", lambda: self._health(ctx))
            return {"status": "SUCCEEDED", "archive": result}
        if action == "update":
            ports = self._ports(ctx, inventory, password, emit)
            txn = UpdatePlan(ctx["manifest"], ctx["root"], ports, release=ctx["release"], modes=ctx["modes"])
            # Each API operation owns one original UpdatePlan journal; previous
            # complete transactions remain available under their own names.
            txn.state_path = ctx["home"].roots["services"] / ("update-" + token + ".json")
            ports.state = txn.state_path
            checkpoint = txn.status()
            if checkpoint is None and ctx["release"] == active:
                self._installed(ctx, active)
                return {"status": "SUCCEEDED", "already_current": True, "release": active}
            self._installed(ctx, checkpoint["old_release"] if checkpoint else active)
            state = txn.run()
            outcome = "SUCCEEDED" if state["outcome"] == "updated" else "FAILED_ROLLED_BACK"
            return {"status": outcome, "release": self._pointer(ctx), "transaction": state["transaction"],
                    "failed_phase": state["failed_phase"], "production_ready": False}
        if action in {"repair", "reinstall"}:
            if active != ctx["release"]:
                raise EngineError("OPERATION_CONFLICT", "Repair использует ту же версию")
            step("stop", lambda: self._stop(ctx))
            # Corrupt payload cannot be declared good backup. Data backup still
            # preserves all state; recovery source is the newly verified bundle.
            step("backup", lambda: self._data_backup(ctx, ctx["work"] / (token + "-before-repair.hsac"), password))
            def repair():
                destination = self._release(ctx, active)
                quarantine = ctx["work"] / (token + "-corrupt-release")
                if destination.exists():
                    if quarantine.exists():
                        raise EngineError("RECOVERY_REQUIRED")
                    os.rename(destination, quarantine)
                self._ports(ctx, inventory, password, emit).stage_release(token + "/stage", active)
            step("repair", repair)
            step("start", lambda: self._start(ctx))
            return {"status": "SUCCEEDED", "health": step("health", lambda: self._health(ctx))}
        if action == "uninstall":
            step("stop", lambda: self._stop(ctx))
            step("preserve_recovery", lambda: {"path": str(self._recovery_copy(ctx, active))})
            def remove():
                for meta_file in (ctx["control"] / "releases").glob("*.json"):
                    if meta_file.name.endswith((".inventory.json", ".signature.json")):
                        continue
                    meta = _read(meta_file)
                    release_path = self._release(ctx, meta["release"])
                    for relative in meta["inventory"]:
                        file = checked_path(release_path / relative)
                        if not contains(release_path, file):
                            raise EngineError("INTEGRITY_FAILED")
                        if file.is_file():
                            file.unlink()
                    if release_path.exists():
                        for directory in sorted((p for p in release_path.rglob("*") if p.is_dir()), key=lambda x: len(x.parts), reverse=True):
                            checked_path(directory)
                            try:
                                directory.rmdir()
                            except OSError:
                                pass
                        try:
                            release_path.rmdir()
                        except OSError:
                            pass
                _write(ctx["control"] / "installation.json", {**owner, "installed": False})
                return {"data_preserved": True, "recovery_available": True}
            return {"status": "SUCCEEDED", **step("remove_owned_code", remove)}
        if action in {"restore", "rollback"}:
            if action == "rollback" and ctx["options"].get("acknowledge_data_loss") is not True:
                raise EngineError("CONSENT_REQUIRED")
            source = checked_path(ctx["options"]["archive_path"])
            if not archive_crypto.is_encrypted(source):
                raise EngineError("INTEGRITY_FAILED", "Native restore принимает только шифрованные архивы")
            candidate = ctx["work"] / (token + "-restore-candidate")
            def extract():
                if not candidate.exists():
                    _private_dir(candidate)
                    archive_crypto.restore_archive(source, candidate, password_provider=password,
                                                    expected_product_id=ctx["manifest"]["product_id"])
                else:
                    # Failed/partial extraction is not resumed by trusting an
                    # existing directory. Require an explicit new plan.
                    raise EngineError("RECOVERY_REQUIRED", "Restore candidate уже существует")
                return {"path": str(candidate), "archive_sha256": _file_digest(source)}
            extracted = step("verify_archive", extract)
            if _file_digest(source) != extracted["archive_sha256"]:
                raise EngineError("INTEGRITY_FAILED", "Источник restore изменился после проверки")
            # После crash кандидат мог измениться. Проверяем каждый файл и
            # пустой каталог по аутентифицированному manifest исходного AES.
            archive_manifest = archive_crypto.verify_archive(source, password_provider=password,
                                                             expected_product_id=ctx["manifest"]["product_id"])
            expected_files = {item["path"]: item["sha256"] for item in archive_manifest["files"]}
            expected_directories = {".", "roots", *archive_manifest["directories"]}
            if _tree(candidate) != (expected_directories, expected_files):
                raise EngineError("INTEGRITY_FAILED", "Restore candidate не соответствует архиву")
            if set(archive_manifest["roots"]) != set(ctx["home"].roots):
                raise EngineError("INTEGRITY_FAILED", "Archive roots не соответствуют установке")
            pointer = _read(candidate / "roots" / "services" / ctx["manifest"]["update"]["pointer_file"])
            release = pointer["release"]
            recovery = checked_path(candidate / "roots" / "runtime" / "recovery" / release)
            meta = _read(recovery / "release.json")
            opts = {"payload_dir": str(recovery / "payload"), "release": release,
                    "runtime_executable": meta["runtime_executable"],
                    "inventory_file": str(recovery / "release.inventory.json"),
                    "signature_file": str(recovery / "release.signature.json")}
            trusted = self._verify(meta["manifest"], ctx["target"], opts)
            if meta["manifest"]["paths"] != ctx["manifest"]["paths"]:
                raise EngineError("CAPABILITY_UNSUPPORTED", "Restore paths требуют продуктового adapter")
            step("stop", lambda: self._stop(ctx))
            step("before_restore", lambda: self._backup(ctx, ctx["work"] / (token + "-before-restore.hsac"), password))
            def restore():
                for name, destination in ctx["home"].roots.items():
                    preserved = set()
                    if name == "services":
                        preserved = {ctx["manifest"]["update"]["pointer_file"], "operation.lock"}
                        preserved |= {x.name for x in destination.glob("update-*.json")}
                    FilesystemPorts._reconcile(candidate / "roots" / name, destination, preserved)
                recover_ctx = {**ctx, "manifest": meta["manifest"], "release": release,
                               "modes": tuple(meta["modes"]), "options": {**ctx["options"], **opts}}
                ports = self._ports(recover_ctx, trusted, password, emit)
                ports.stage_release(token + "/stage", release)
                ports.switch_pointer(token + "/switch", release)
                _write(ctx["control"] / "installation.json", {**owner, "installed": True})
            step("restore", restore)
            step("start", lambda: self._start(ctx))
            return {"status": "SUCCEEDED", "health": step("health", lambda: self._health(ctx)),
                    "external_actions_on_hold": True}
        raise EngineError("CAPABILITY_UNSUPPORTED")

    def _data_backup(self, ctx, path, password):
        if not path.exists():
            archive_crypto.build_encrypted_archive(ctx["home"], path, password_provider=password)
        if not archive_crypto.is_encrypted(path):
            raise EngineError("INTEGRITY_FAILED", "Ожидался шифрованный recovery snapshot")
        archive_crypto.verify_archive(path, password_provider=password,
                                       expected_product_id=ctx["manifest"]["product_id"])
        return {"path": str(path), "encrypted": True, "recovery_source": "verified replacement bundle"}

    def resume(self, plan, previous_operation, emit=lambda event: None, cancel_requested=lambda: False):
        return self.execute(plan, emit, cancel_requested)


class _Effects:
    """Реальные subprocess эффекты существующего UpdatePlan."""
    def __init__(self, engine, ctx, emit):
        self.engine, self.ctx, self.emit = engine, ctx, emit

    def _run(self, phase, fn):
        self.ctx["boundary"](phase)
        result = fn()
        self.emit({"phase": phase, "status": "SUCCEEDED", "completed": 1, "total": 1})
        return result

    def stop(self, key, components):
        self._run("stop", lambda: self.engine._stop(self.ctx))

    def migrate(self, key, migration, command):
        self._run("migrate", lambda: self.engine._hook(self.ctx, self.ctx["release"], command, key))

    def start(self, key, components, pre_start):
        self._run("start", lambda: self.engine._start(self.ctx))

    def health(self, key, url, timeout, checks):
        def health():
            # After crash между start и health ребёнок мог завершиться;
            # reconciling start проверяет signed bytes и существующие PID.
            self.engine._start(self.ctx)
            return self.engine._health(self.ctx)
        self._run("health", health)

    def post_update(self, key, hooks):
        def run():
            for index, command in enumerate(hooks):
                self.engine._hook(self.ctx, self.ctx["release"], command, key + ":" + str(index))
        self._run("post_update", run)


_default_engine = NativeEngine()

def capabilities(target):
    return _default_engine.capabilities(target)

def discover(manifest, target, options):
    return _default_engine.discover(manifest, target, options)

def plan(action, manifest, target, inventory, options):
    return _default_engine.plan(action, manifest, target, inventory, options)

def execute(plan, emit=lambda event: None, cancel_requested=lambda: False):
    return _default_engine.execute(plan, emit, cancel_requested)

def resume(plan, previous_operation, emit=lambda event: None, cancel_requested=lambda: False):
    return _default_engine.resume(plan, previous_operation, emit, cancel_requested)
