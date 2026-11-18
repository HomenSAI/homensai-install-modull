"""Пользовательский Windows backend общего NativeEngine."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import socket

from shared.datahome import checked_path

from .engine import EngineError, NativeEngine, _owner_id
from .windows_user import check_private_path, process_identity, tcp_listeners


class WindowsNativeEngine(NativeEngine):
    ACTIONS = ("install",)

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

    def _running(self, record):
        identity = process_identity(record.get("pid"))
        executable = record.get("executable")
        return bool(identity is not None and type(executable) is str and executable
                    and identity.created == record.get("birth") and identity.owner == _owner_id()
                    and identity.executable == Path(executable))

    def _adopt_launch(self, ctx, name, record):
        raise EngineError("RECOVERY_REQUIRED", "Неподтверждённый Windows launch требует сверки")

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
