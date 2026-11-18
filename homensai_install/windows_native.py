"""Пользовательский Windows backend общего NativeEngine."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import socket
import time

from shared.datahome import checked_path

from .engine import EngineError, NativeEngine, _owner_id, _write
from .process_liveness import process_liveness
from .windows_user import (check_private_path, create_job_child, job_members, job_pids,
                           open_job, process_identity, session_id, tcp_listeners)


class WindowsNativeEngine(NativeEngine):
    ACTIONS = ("install", "start", "stop")

    def _host_native(self, target):
        return (os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"}
                and target.get("os", "windows") == "windows" and target.get("arch", "x64") == "x64"
                and target.get("mode", "native") == "native" and target.get("scope", "user") == "user"
                and self.product_adapter_descriptor is None)

    def _context(self, manifest, target, options):
        ctx = super()._context(manifest, target, options)
        # Не создавать lifecycle lock рядом с чужим или общим каталогом.
        try:
            for path in (ctx["root"], ctx["work"]):
                if path.exists():
                    check_private_path(path)
                parent = path.parent
                while not parent.exists():
                    parent = parent.parent
                check_private_path(parent)
        except OSError:
            raise EngineError("PERMISSION_REQUIRED", "Требуется приватное расположение установки") from None
        return ctx

    def _owner(self, ctx, *, required=False):
        directory = ctx["control"]
        path = directory / "installation.json"
        try:
            if directory.exists():
                check_private_path(directory)
            if path.exists():
                check_private_path(path)
        except OSError:
            raise EngineError("PERMISSION_REQUIRED", "Нельзя подтвердить права метаданных установки") from None
        return super()._owner(ctx, required=required)

    def _records(self, ctx):
        try:
            path = ctx["control"] / "processes.json"
            if path.exists():
                check_private_path(path)
        except OSError:
            raise EngineError("PERMISSION_REQUIRED", "Нельзя подтвердить права журнала процессов") from None
        records = super()._records(ctx)
        for record in records.values():
            if type(record) is not dict:
                raise EngineError("RECOVERY_REQUIRED", "Повреждён журнал процессов Windows")
            if record.get("windows_job") is True:
                launch_id = record.get("launch_id")
                if (type(launch_id) is not str or record.get("stop_request") != str(
                        ctx["control"] / "windows-stop" / (launch_id + ".json"))):
                    raise EngineError("RECOVERY_REQUIRED", "Windows stop intent отличается от установки")
        return records

    def _matches_process(self, record):
        identity = process_identity(record.get("pid"))
        executable = record.get("executable")
        return bool(identity is not None and type(executable) is str and executable
                    and identity.created == record.get("birth") and identity.owner == _owner_id()
                    and identity.executable == Path(executable))

    @staticmethod
    def _job_id(record):
        if record.get("windows_job") is not True or record.get("windows_session") != session_id():
            raise ValueError("Windows job session mismatch")
        return record["launch_id"]

    def _job_pids(self, record):
        try:
            pids = job_pids(self._job_id(record))
            if pids is None and record.get("status") != "spawn_failed":
                raise OSError("Windows job receipt is unavailable")
            return pids or set()
        except (OSError, ValueError, KeyError):
            raise EngineError("RECOVERY_REQUIRED", "Нельзя подтвердить дерево процессов Windows") from None

    def _running(self, record):
        return self._matches_process(record) and record["pid"] in self._job_pids(record)

    def _launch_details(self, ctx, argv, launch_id):
        return {"executable": argv[0], "windows_job": True, "windows_session": session_id(),
                "stop_request": str(ctx["control"] / "windows-stop" / (launch_id + ".json"))}

    def _spawn_component(self, ctx, release, argv, env, intent):
        env["HOMENSAI_WINDOWS_STOP_PATH"] = intent["stop_request"]
        return create_job_child(intent["launch_id"], argv, self._release(ctx, release), env)

    def _adopt_launch(self, ctx, name, record):
        candidates = []
        for pid in self._job_pids(record):
            identity = process_identity(pid)
            if identity is not None:
                candidate = {**record, "pid": pid, "birth": identity.created, "status": "running"}
                if self._matches_process(candidate):
                    candidates.append(candidate)
        if len(candidates) != 1:
            raise EngineError("RECOVERY_REQUIRED", "Нельзя однозначно сверить Windows launch")
        return candidates[0]

    def _assert_session_quiesced(self, record):
        if self._job_pids(record):
            raise EngineError("RECOVERY_REQUIRED", "В Windows job остались процессы")
        if type(record.get("pid")) is int and process_liveness(record["pid"]) is None:
            raise EngineError("RECOVERY_REQUIRED", "Недоступна identity процесса Windows")

    def _terminate(self, record):
        try:
            with open_job(self._job_id(record)) as job:
                if job is None:
                    if record.get("status") == "spawn_failed":
                        return
                    raise OSError("Windows job receipt is unavailable")
                pids = job_members(job)
                if pids:
                    if record.get("pid") not in pids or not self._matches_process(record):
                        raise EngineError("RECOVERY_REQUIRED", "Живой Windows job не подтвердил основной процесс")
                    _write(Path(record["stop_request"]), {"launch_id": record["launch_id"]})
                    deadline = time.monotonic() + 10
                    while job_members(job) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if job_members(job):
                        raise EngineError("RECOVERY_REQUIRED", "В Windows job остались процессы")
        except (OSError, KeyError, ValueError):
            raise EngineError("RECOVERY_REQUIRED", "Нельзя подтвердить остановку Windows job") from None
        child = self._children.pop(record.get("pid"), None)
        if child is not None:
            child.wait(1)

    def _owns_loopback_listener(self, records, port):
        try:
            listeners = tcp_listeners(port)
        except OSError:
            return False
        if len(listeners) != 1:
            return False
        address, pid = next(iter(listeners))
        return address == "127.0.0.1" and any(record.get("pid") == pid and self._running(record)
                                              for record in records.values())

    def _port_state(self, ctx, records):
        port = ctx["manifest"]["web"]["port"]
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                owned = self._owns_loopback_listener(records, port)
                return {"port_conflict": not owned, "owned_listener": owned}
        return {"port_conflict": False, "owned_listener": False}

    def _argv(self, ctx, release, command):
        argv = super()._argv(ctx, release, command)
        if checked_path(argv[0]).suffix.lower() != ".exe":
            raise EngineError("INTEGRITY_FAILED", "Windows payload требует подписанный EXE")
        return argv
