"""Наблюдение PID без сигналов Windows; None означает неизвестный результат."""
from __future__ import annotations

import os

_WINDOWS = os.name == "nt"
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_INVALID_PARAMETER = 87


class _WindowsProcessPort:
    """Read-only Win32 handle; отдельный порт для воспроизводимых проверок."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def open(self, pid):
        return self.kernel32.OpenProcess(SYNCHRONIZE, False, pid)

    def wait(self, handle):
        return self.kernel32.WaitForSingleObject(handle, 0)

    def close(self, handle):
        return bool(self.kernel32.CloseHandle(handle))

    def last_error(self):
        return self.ctypes.get_last_error()


def _windows_process_liveness(pid, *, port=None):
    # Не усекать произвольный Python int до DWORD и не запрашивать другой PID.
    if pid > 0xFFFFFFFF:
        return None
    try:
        port = _WindowsProcessPort() if port is None else port
        handle = port.open(pid)
        if not handle:
            return False if port.last_error() == ERROR_INVALID_PARAMETER else None
    except Exception:
        return None
    result = None
    try:
        observed = port.wait(handle)
        if observed == WAIT_OBJECT_0:
            result = False
        elif observed == WAIT_TIMEOUT:
            result = True
    except Exception:
        result = None
    finally:
        try:
            if not port.close(handle):
                result = None
        except Exception:
            result = None
    return result


def process_liveness(pid):
    """True: жив; False: завершён/невалидный PID; None: нет доказательства.

    Windows никогда не вызывает os.kill: нулевой сигнал там является CTRL_C_EVENT.
    Неизвестный результат не разрешает повтор неизвестного эффекта установщика.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if _WINDOWS:
        return _windows_process_liveness(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, OverflowError):
        return None
    return True
