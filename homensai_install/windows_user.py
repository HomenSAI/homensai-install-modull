"""Системные операции для пользовательской установки Windows."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes as wt
from functools import lru_cache
from pathlib import Path
import re
import socket
import struct
from typing import NamedTuple

from shared.datahome import checked_path

_QUERY_LIMITED = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 258
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_OWNER_SECURITY_INFORMATION = 1
_DACL_SECURITY_INFORMATION = 4
_FILE_OBJECT = 1
_INSUFFICIENT_BUFFER = 122
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wt.BOOL)]


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, wt.DWORD) for name in
                ("state", "address", "port", "remote_address", "remote_port", "pid")]


class ProcessIdentity(NamedTuple):
    created: str
    owner: str
    executable: Path


def _function(library, name, restype, *argtypes):
    function = getattr(library, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


@lru_cache
def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _function(kernel, "GetCurrentProcess", wt.HANDLE)
    _function(kernel, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    _function(kernel, "CloseHandle", wt.BOOL, wt.HANDLE)
    _function(kernel, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
    _function(kernel, "WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
    _function(kernel, "GetProcessTimes", wt.BOOL, wt.HANDLE, *([ctypes.POINTER(wt.FILETIME)] * 4))
    _function(kernel, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR,
              ctypes.POINTER(wt.DWORD))
    _function(kernel, "CreateDirectoryW", wt.BOOL, wt.LPCWSTR, ctypes.POINTER(_SecurityAttributes))
    return kernel


@lru_cache
def _advapi():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _function(advapi, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
    _function(advapi, "GetTokenInformation", wt.BOOL, wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
              wt.DWORD, ctypes.POINTER(wt.DWORD))
    _function(advapi, "ConvertSidToStringSidW", wt.BOOL, ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR))
    _function(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wt.BOOL, wt.LPCWSTR,
              wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD))
    _function(advapi, "GetNamedSecurityInfoW", wt.DWORD, wt.LPCWSTR, ctypes.c_int, wt.DWORD,
              *([ctypes.POINTER(ctypes.c_void_p)] * 5))
    _function(advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW", wt.BOOL, ctypes.c_void_p,
              wt.DWORD, wt.DWORD, ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(wt.DWORD))
    return advapi


@contextmanager
def _handle(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield value
    finally:
        _kernel().CloseHandle(value)


def _sid_text(sid):
    text = wt.LPWSTR()
    if not _advapi().ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        _kernel().LocalFree(text)


def _process_owner(handle):
    advapi = _advapi()
    token = wt.HANDLE()
    if not advapi.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    with _handle(token):
        size = wt.DWORD()
        advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise OSError("Недоступен SID процесса")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])


def owner_sid():
    return _process_owner(_kernel().GetCurrentProcess())


def process_identity(pid):
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        return None
    kernel = _kernel()
    try:
        with _handle(kernel.OpenProcess(_QUERY_LIMITED | _SYNCHRONIZE, False, pid)) as handle:
            if kernel.WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT:
                return None
            times = [wt.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            size = wt.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                return None
            created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
            return ProcessIdentity(str(created), _process_owner(handle), Path(name.value))
    except OSError:
        return None


def tcp_listeners(port):
    """IPv4 listener address and PID from the Windows TCP table."""
    ip_helper = ctypes.WinDLL("iphlpapi", use_last_error=True)
    table = _function(ip_helper, "GetExtendedTcpTable", wt.DWORD, ctypes.c_void_p,
                      ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG)
    length = wt.DWORD()
    status = table(None, ctypes.byref(length), False, socket.AF_INET, 3, 0)
    for _ in range(3):
        if status != _INSUFFICIENT_BUFFER or not 4 <= length.value <= 16 * 1024 * 1024:
            break
        buffer = ctypes.create_string_buffer(length.value)
        status = table(buffer, ctypes.byref(length), False, socket.AF_INET, 3, 0)
        if status == 0:
            count = wt.DWORD.from_buffer(buffer).value
            if count > (len(buffer) - 4) // ctypes.sizeof(_TcpRow):
                break
            rows = (_TcpRow * count).from_buffer(buffer, 4)
            return {(socket.inet_ntoa(struct.pack("=I", row.address)), row.pid)
                    for row in rows if socket.ntohs(row.port & 0xffff) == port}
    raise OSError("Таблица TCP listener недоступна")


def _dacl(path):
    advapi = _advapi()
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    flags = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
    result = advapi.GetNamedSecurityInfoW(str(path), _FILE_OBJECT, flags, ctypes.byref(owner), None,
                                         None, None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        text = wt.LPWSTR()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return _sid_text(owner), text.value
        finally:
            _kernel().LocalFree(text)
    finally:
        _kernel().LocalFree(descriptor)


def check_private_path(path, owner=None):
    path = checked_path(path)
    owner = owner_sid() if owner is None else owner
    actual_owner, dacl = _dacl(path)
    if actual_owner != owner:
        raise PermissionError("Каталог или файл принадлежит другой identity")
    aces = re.findall(r"\(([^()]*)\)", dacl)
    if not aces or not re.fullmatch(r"D:(?:P|AI|AR)*(?:\([^()]*\))+", dacl):
        raise PermissionError("Требуется закрытый Windows ACL")
    allowed = {owner, _SYSTEM, _ADMINISTRATORS, "SY", "BA", "OW"}
    for ace in aces:
        fields = ace.split(";")
        if len(fields) != 6 or fields[0] != "A" or fields[-1] not in allowed:
            raise PermissionError("Windows ACL допускает другую identity")
    return path


def private_directory(path):
    """Создать с приватным ACL либо проверить существующий без изменения прав."""
    path = checked_path(path)
    if path == Path(path.anchor):
        raise PermissionError("Корень диска не является приватным каталогом")
    owner = owner_sid()
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise NotADirectoryError(current)
    if missing:
        descriptor = ctypes.c_void_p()
        sddl = f"O:{owner}D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)"
        if not _advapi().ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            for directory in reversed(missing):
                checked_path(directory)
                if not _kernel().CreateDirectoryW(str(directory), ctypes.byref(attributes)):
                    error = ctypes.get_last_error()
                    if error != 183:
                        raise ctypes.WinError(error)
                check_private_path(directory, owner)
        finally:
            _kernel().LocalFree(descriptor)
    if not path.is_dir():
        raise NotADirectoryError(path)
    return check_private_path(path, owner)
