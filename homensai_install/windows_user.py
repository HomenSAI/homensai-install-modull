"""Системные операции для пользовательской установки Windows."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes as wt
from functools import lru_cache
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
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
_JOB_QUERY = 4
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x400
_JOB_LIST_ATTRIBUTE = 0x0002000D
_HANDLE_LIST_ATTRIBUTE = 0x00020002


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wt.BOOL)]


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, wt.DWORD) for name in
                ("state", "address", "port", "remote_address", "remote_port", "pid")]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("reserved", wt.LPWSTR), ("desktop", wt.LPWSTR),
                ("title", wt.LPWSTR), ("x", wt.DWORD), ("y", wt.DWORD), ("width", wt.DWORD),
                ("height", wt.DWORD), ("columns", wt.DWORD), ("rows", wt.DWORD),
                ("fill", wt.DWORD), ("flags", wt.DWORD), ("show", wt.WORD),
                ("reserved_size", wt.WORD), ("reserved_bytes", ctypes.c_void_p),
                ("stdin", wt.HANDLE), ("stdout", wt.HANDLE), ("stderr", wt.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("process", wt.HANDLE), ("thread", wt.HANDLE),
                ("pid", wt.DWORD), ("thread_id", wt.DWORD)]


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
    _function(kernel, "CreateJobObjectW", wt.HANDLE, ctypes.POINTER(_SecurityAttributes), wt.LPCWSTR)
    _function(kernel, "OpenJobObjectW", wt.HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
    _function(kernel, "QueryInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int,
              ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
    _function(kernel, "InitializeProcThreadAttributeList", wt.BOOL, ctypes.c_void_p, wt.DWORD,
              wt.DWORD, ctypes.POINTER(ctypes.c_size_t))
    _function(kernel, "UpdateProcThreadAttribute", wt.BOOL, ctypes.c_void_p, wt.DWORD, ctypes.c_size_t,
              ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
    _function(kernel, "DeleteProcThreadAttributeList", None, ctypes.c_void_p)
    _function(kernel, "CreateProcessW", wt.BOOL, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p,
              ctypes.c_void_p, wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
              ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation))
    _function(kernel, "GetExitCodeProcess", wt.BOOL, wt.HANDLE, ctypes.POINTER(wt.DWORD))
    _function(kernel, "ProcessIdToSessionId", wt.BOOL, wt.DWORD, ctypes.POINTER(wt.DWORD))
    _function(kernel, "DuplicateHandle", wt.BOOL, wt.HANDLE, wt.HANDLE, wt.HANDLE,
              ctypes.POINTER(wt.HANDLE), wt.DWORD, wt.BOOL, wt.DWORD)
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
    _function(advapi, "GetAclInformation", wt.BOOL, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_int)
    _function(advapi, "GetAce", wt.BOOL, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p))
    return advapi


@contextmanager
def _handle(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield value
    finally:
        _kernel().CloseHandle(value)


@contextmanager
def _security_attributes(sddl):
    descriptor = ctypes.c_void_p()
    if not _advapi().ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        _kernel().LocalFree(descriptor)


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


def session_id():
    value = wt.DWORD()
    if not _kernel().ProcessIdToSessionId(os.getpid(), ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.value


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
    acl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    flags = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
    result = advapi.GetNamedSecurityInfoW(str(path), _FILE_OBJECT, flags, ctypes.byref(owner), None,
                                         ctypes.byref(acl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        size_information = (wt.DWORD * 3)()
        if not acl.value or not advapi.GetAclInformation(acl, size_information, ctypes.sizeof(size_information), 2):
            raise PermissionError("Нет проверяемого Windows ACL")
        identities = set()
        for index in range(size_information[0]):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(acl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.string_at(ace, 4)
            ace_type, ace_flags, ace_size = struct.unpack("=BBH", header)
            if ace_type != 0 or ace_size < 16:
                raise PermissionError("Неподдерживаемая запись Windows ACL")
            identities.add((_sid_text(ace.value + 8), ace_flags))
        return _sid_text(owner), identities
    finally:
        _kernel().LocalFree(descriptor)


def check_private_path(path, owner=None):
    path = checked_path(path)
    owner = owner_sid() if owner is None else owner
    actual_owner, identities = _dacl(path)
    if actual_owner != owner:
        raise PermissionError("Каталог или файл принадлежит другой identity")
    if not identities:
        raise PermissionError("Требуется закрытый Windows ACL")
    allowed = {owner, _SYSTEM, _ADMINISTRATORS, "S-1-3-4"}
    for sid, flags in identities:
        if sid == "S-1-3-0" and flags & 0x08:  # Creator Owner, только наследование.
            continue
        if sid not in allowed:
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
        sddl = f"O:{owner}D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)"
        with _security_attributes(sddl) as attributes:
            for directory in reversed(missing):
                checked_path(directory)
                if not _kernel().CreateDirectoryW(str(directory), ctypes.byref(attributes)):
                    error = ctypes.get_last_error()
                    if error != 183:
                        raise ctypes.WinError(error)
                check_private_path(directory, owner)
    if not path.is_dir():
        raise NotADirectoryError(path)
    return check_private_path(path, owner)


def _job_name(launch_id):
    if type(launch_id) is not str or not re.fullmatch(r"[a-f0-9]{32}", launch_id):
        raise ValueError("Некорректный идентификатор Windows job")
    return "Local\\HomenSAI-" + launch_id


@contextmanager
def open_job(launch_id):
    kernel = _kernel()
    job = kernel.OpenJobObjectW(_JOB_QUERY, False, _job_name(launch_id))
    if not job and ctypes.get_last_error() == 2:
        yield None
        return
    with _handle(job) as handle:
        yield handle


def job_members(job):
    kernel = _kernel()
    for size in (8, 64, 256, 1024, 4096):
        header = 2 * ctypes.sizeof(wt.DWORD)
        data = ctypes.create_string_buffer(header + size * ctypes.sizeof(ctypes.c_size_t))
        if kernel.QueryInformationJobObject(job, 3, data, len(data), None):
            total, count = (wt.DWORD * 2).from_buffer(data)
            if total == count and count <= size:
                return set((ctypes.c_size_t * count).from_buffer(data, header))
        elif ctypes.get_last_error() != 234:
            raise ctypes.WinError(ctypes.get_last_error())
    raise OSError("Процессы Windows job нельзя подтвердить")


def job_pids(launch_id):
    """None означает, что kernel job не удалось открыть по имени."""
    with open_job(launch_id) as job:
        return job_members(job) if job is not None else None


class WindowsChild:
    """Handle конкретного запуска, без сигналов через повторно используемый PID."""

    def __init__(self, pid, handle):
        self.pid = pid
        self._process = handle

    def __del__(self):
        if self._process:
            _kernel().CloseHandle(self._process)

    def wait(self, timeout):
        kernel = _kernel()
        state = kernel.WaitForSingleObject(self._process, max(0, round(timeout * 1000)))
        if state == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("Windows component", timeout)
        code = wt.DWORD()
        if state != 0 or not kernel.GetExitCodeProcess(self._process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def poll(self):
        try:
            return self.wait(0)
        except subprocess.TimeoutExpired:
            return None


def create_job_child(launch_id, argv, cwd, env):
    """Создать процесс сразу внутри job. Дерево сохраняется после закрытия SDK."""
    kernel = _kernel()
    with _security_attributes(f"D:P(A;;GA;;;{owner_sid()})(A;;GA;;;SY)") as attributes:
        job = kernel.CreateJobObjectW(ctypes.byref(attributes), _job_name(launch_id))
        already_exists = ctypes.get_last_error() == 183
    with _handle(job):
        if already_exists:
            raise OSError("Windows job уже существует")
        inherited = wt.HANDLE()
        current = kernel.GetCurrentProcess()
        if not kernel.DuplicateHandle(current, job, current, ctypes.byref(inherited), _JOB_QUERY, True, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        with _handle(inherited):
            return _create_in_job(kernel, job, inherited, argv, cwd, env)


def _create_in_job(kernel, job, inherited, argv, cwd, env):
    size = ctypes.c_size_t()
    kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    data = ctypes.create_string_buffer(size.value)
    if not kernel.InitializeProcThreadAttributeList(data, 2, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        job_handle = wt.HANDLE(job)
        if not kernel.UpdateProcThreadAttribute(data, 0, _JOB_LIST_ATTRIBUTE, ctypes.byref(job_handle),
                                                ctypes.sizeof(job_handle), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        # Ровно один query handle удерживает имя job после закрытия мастера.
        if not kernel.UpdateProcThreadAttribute(data, 0, _HANDLE_LIST_ATTRIBUTE, ctypes.byref(inherited),
                                                ctypes.sizeof(inherited), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = _StartupInfoEx()
        startup.startup.size = ctypes.sizeof(startup)
        startup.attributes = ctypes.addressof(data)
        process = _ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0")
        flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT | subprocess.CREATE_NO_WINDOW
        if not kernel.CreateProcessW(argv[0], command, None, None, True, flags, environment,
                                     str(cwd), ctypes.byref(startup), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(process.thread)
        return WindowsChild(process.pid, process.process)
    finally:
        kernel.DeleteProcThreadAttributeList(data)
