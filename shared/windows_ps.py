# HomenS.AI · © 2026 Serhii Khomenko
"""Детерминированные тексты Windows PowerShell 5.1; без эффектов над установкой.

v2, явный mode, проверенные байты WinSW. Rollback требует доверенных портов
продукта при будущем исполнении; готового native-установщика здесь нет.
"""

from __future__ import annotations

import base64
from itertools import combinations
import ntpath
from pathlib import PureWindowsPath
import re

from shared.manifest import ManifestError, validate_manifest
from shared.services import component_order, render_services


def _quote(value: str) -> str:
    # PowerShell распознаёт также типографские кавычки и разделители строк.
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
           or c in '\u2018\u2019\u201a\u201b\u201c\u201d\u2028\u2029\ufffe\uffff' for c in value):
        raise ManifestError("Windows: недопустимые символы текста")
    return "'" + value.replace("'", "''") + "'"


def _path(value: str, base: PureWindowsPath | None = None) -> PureWindowsPath:
    if type(value) is not str or not value:
        raise ManifestError("Windows: требуется путь")
    _quote(value)
    text = value.replace("/", "\\")
    path = PureWindowsPath(text)
    if (text.startswith("\\") or "%" in text or ":" in text[2:] or ".." in text.split("\\")
            or (path.drive and not path.is_absolute())
            or any(ntpath.isreserved(part) or part in ("", ".", "..")
                   for part in text.split("\\")[1 if path.drive else 0:])):
        raise ManifestError("Windows: небезопасный путь")
    if not path.is_absolute():
        if base is None:
            raise ManifestError("Windows: требуется абсолютный корень")
        path = base / path
    if not re.fullmatch(r"[A-Za-z]:", path.drive) or path == PureWindowsPath(path.anchor):
        raise ManifestError("Windows: требуется локальный путь вне корня диска")
    return path


def _overlap(a: PureWindowsPath, b: PureWindowsPath) -> bool:
    return a == b or a in b.parents or b in a.parents


_COMMON = r'''
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Assert-SafePath([string]$Path) {
    $node = [IO.Path]::GetFullPath($Path)
    while ($node) {
        if (Test-Path -LiteralPath $node) {
            $attributes = [IO.File]::GetAttributes($node)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Reparse point refused.'
            }
        }
        $node = [IO.Path]::GetDirectoryName($node)
    }
}

function New-SafeDirectory([string]$Path) {
    Assert-SafePath $Path
    [void][IO.Directory]::CreateDirectory($Path)
    Assert-SafePath $Path
}

function Protect-Directory([string]$Path) {
    Assert-SafePath $Path
    # Walk explicitly: never recurse through a junction before checking it.
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push($Path)
    $nodes = New-Object 'Collections.Generic.List[string]'
    while ($pending.Count -gt 0) {
        $item = $pending.Pop()
        Assert-SafePath $item
        $nodes.Add($item)
        if ([IO.Directory]::Exists($item)) {
            foreach ($child in [IO.Directory]::EnumerateFileSystemEntries($item)) {
                $pending.Push($child)
            }
        }
    }
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    # Fresh protected DACL removes ALL pre-existing explicit/inherited grants.
    foreach ($item in $nodes) {
        $directory = [IO.Directory]::Exists($item)
        if ($directory) {
            $acl = New-Object Security.AccessControl.DirectorySecurity
            $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        } else {
            $acl = New-Object Security.AccessControl.FileSecurity
            $inherit = [Security.AccessControl.InheritanceFlags]::None
        }
        $acl.SetOwner($owner)
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($sid in @($owner.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique) {
            $identity = New-Object Security.Principal.SecurityIdentifier($sid)
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $identity, 'FullControl', $inherit, 'None', 'Allow')
            [void]$acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $item -AclObject $acl
    }
    $icacls = Join-Path ([Environment]::SystemDirectory) 'icacls.exe'
    & $icacls $Path '/inheritance:r' '/grant:r' ('*' + $owner.Value + ':(OI)(CI)F') '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '/Q' > $null 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'ACL failed.' }
}

function Assert-Pin([string]$Path) {
    Assert-SafePath $Path
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ine $WinSWHash) { throw 'WinSW pin mismatch.' }
}

function Invoke-Wrapper([string]$Id, [string]$Action) {
    $exe = Join-Path $Services ($Id + '.exe')
    Assert-Pin $exe
    Assert-SafePath (Join-Path $Services ($Id + '.xml'))
    & $exe $Action > $null 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'WinSW failed.' }
}

function Stop-Components {
    foreach ($id in $StopOrder) { Invoke-Wrapper $id 'stop' }
}

function Start-Components {
    foreach ($id in $StartOrder) { Invoke-Wrapper $id 'start' }
}

function Invoke-VerifiedPort([scriptblock]$Port) {
    $result = & $Port
    if ($result -isnot [bool] -or -not $result) { throw 'Product port not verified.' }
}
'''

_SECRET = r'''
    New-SafeDirectory $Data
    Protect-Directory $Data
    New-SafeDirectory $Secrets
    Protect-Directory $Secrets
    Add-Type -AssemblyName System.Security
    foreach ($name in $SecretNames) {
        $secure = $null
        $plain = $null
        $cipher = $null
        $bstr = [IntPtr]::Zero
        $stream = $null
        try {
            $target = Join-Path $Secrets ($name + '.' + $ScopeName + '.dpapi')
            Assert-SafePath $target
            if (Test-Path -LiteralPath $target) { throw 'Secret already exists.' }
            $secure = Read-Host -Prompt $name -AsSecureString
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            $length = [Runtime.InteropServices.Marshal]::ReadInt32($bstr, -4)
            $plain = New-Object byte[] $length
            [Runtime.InteropServices.Marshal]::Copy($bstr, $plain, 0, $length)
            $cipher = [Security.Cryptography.ProtectedData]::Protect($plain, $null, $Scope)
            $stream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $stream.Write($cipher, 0, $cipher.Length)
            $stream.Flush($true)
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
            if ($null -ne $plain) { [Array]::Clear($plain, 0, $plain.Length) }
            if ($null -ne $cipher) { [Array]::Clear($cipher, 0, $cipher.Length) }
            if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
            if ($null -ne $secure) { $secure.Dispose() }
        }
    }
'''

_ROLLBACK_PARAMS = '''[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][scriptblock]$VerifyRecovery,
    [Parameter(Mandatory=$true)][scriptblock]$BackupBeforeRollback,
    [Parameter(Mandatory=$true)][scriptblock]$RestoreRecovery,
    [Parameter(Mandatory=$true)][scriptblock]$SwitchPreviousRelease,
    [Parameter(Mandatory=$true)][scriptblock]$Health
)
'''

_ROLLBACK = r'''
    # Trusted product ports: return exactly one Boolean true, no logs/secrets.
    # Backup port must create and verify a NEW full encrypted before-rollback.
    Invoke-VerifiedPort $VerifyRecovery
    Stop-Components
    Invoke-VerifiedPort $BackupBeforeRollback
    Invoke-VerifiedPort $RestoreRecovery
    Invoke-VerifiedPort $SwitchPreviousRelease
    Start-Components
    try { Invoke-VerifiedPort $Health } catch {
        Stop-Components
        throw 'Health failed.'
    }
'''


def render_windows(manifest: dict, *, install_root: str, modes: tuple[str, ...],
                   winsw_payload: bytes | None = None,
                   dpapi_scope: str = "LocalMachine") -> dict[str, str]:
    """Вернуть PS1/XML; не читать ФС продукта/env, не запускать процессы.

    WinSW.exe поставляется вызывающей стороной рядом со скриптами; pin
    проверяется сейчас по байтам и текстом снова перед каждым будущим вызовом.
    v1 не дополняется догадками о native-команде, путях и именах секретов.
    """
    manifest = validate_manifest(manifest)
    order = component_order(manifest, modes)
    if dpapi_scope not in ("CurrentUser", "LocalMachine"):
        raise ManifestError("Windows: недопустимый scope DPAPI")
    root = _path(install_root)
    paths = {key: _path(manifest["paths"][key], root)
             for key in ("app", "releases", "data", "backup", "services", "config", "runtime")}
    if any(_overlap(a, b) for a, b in combinations(paths.values(), 2)):
        raise ManifestError("Windows: каталоги пересекаются")
    # IDs используются как SCM names, leaf paths и argv: только строгий алфавит.
    for name in [manifest["product_id"], *(item["name"] for item in manifest["components"])]:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name) is None:
            raise ManifestError("Windows: недопустимый идентификатор")
    secret_names = sorted(item["name"] for item in manifest["secrets"])
    if any(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name) is None
           or ntpath.isreserved(name) for name in secret_names):
        raise ManifestError("Windows: недопустимое имя секрета")
    xml = render_services(manifest, platform="windows", modes=modes,
                          install_root=str(root), winsw_payload=winsw_payload)
    services = paths["services"]
    state_paths = [_path(manifest["update"][field], services)
                   for field in ("pointer_file", "state_file")]
    if _overlap(*state_paths):
        raise ManifestError("Windows: служебные пути пересекаются")
    for target in state_paths:
        if any(_overlap(target, services / leaf) for leaf in [
                "operation.lock", "secrets", *xml,
                *(name.removesuffix(".xml") + ".exe" for name in xml)]):
            raise ManifestError("Windows: служебные пути пересекаются")
    ids = [manifest["product_id"] + "-" + name for name in order]
    array = lambda values: "@(" + ", ".join(_quote(str(v)) for v in values) + ")"
    constants = "\n".join("$" + key + " = " + _quote(str(value)) for key, value in {
        "App": paths["app"], "Data": paths["data"], "Services": services,
        "Secrets": paths["data"] / "secrets", "WinSWHash": manifest["winsw"]["sha256"],
        "ScopeName": dpapi_scope,
    }.items()) + "\n"
    constants += "$Directories = " + array(paths.values()) + "\n"
    constants += "$StartOrder = " + array(ids) + "\n"
    constants += "$StopOrder = " + array(reversed(ids)) + "\n"
    constants += "$SecretNames = " + array(secret_names) + "\n"
    constants += "Add-Type -AssemblyName System.Security\n"
    constants += "$Scope = [Security.Cryptography.DataProtectionScope]::" + dpapi_scope + "\n"
    install = "    foreach ($directory in $Directories) { New-SafeDirectory $directory }\n"
    install += "    Protect-Directory $Data\n    New-SafeDirectory $Secrets\n    Protect-Directory $Secrets\n"
    install += "    $source = Join-Path $PSScriptRoot 'WinSW.exe'\n    Assert-Pin $source\n"
    # Preflight every destination before any registration or file replacement.
    for filename in xml:
        for leaf in (filename, filename.removesuffix(".xml") + ".exe"):
            install += "    $target = Join-Path $Services " + _quote(leaf) + "\n"
            install += "    Assert-SafePath $target\n    if (Test-Path -LiteralPath $target) { throw 'Service files already exist.' }\n"
        install += "    if (Get-Service -Name " + _quote(filename.removesuffix(".xml")) + " -ErrorAction SilentlyContinue) { throw 'Service already exists.' }\n"
    for filename, content in xml.items():
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        exe = filename.removesuffix(".xml") + ".exe"
        install += "    [IO.File]::Copy($source, (Join-Path $Services " + _quote(exe) + "), $false)\n"
        install += "    [IO.File]::WriteAllBytes((Join-Path $Services " + _quote(filename) + "), [Convert]::FromBase64String(" + _quote(encoded) + "))\n"
    install += "    foreach ($id in $StartOrder) { Invoke-Wrapper $id 'install' }\n"
    bodies = {
        "install": install,
        "start": "    Start-Components\n",
        "stop": "    Stop-Components\n",
        "status": "    foreach ($id in $StartOrder) {\n        $service = Get-Service -Name $id -ErrorAction SilentlyContinue\n        if ($null -eq $service) { Write-Output ($id + ': absent') }\n        else { Write-Output ($id + ': ' + $service.Status) }\n    }\n",
        "uninstall": "    Stop-Components\n    foreach ($id in $StopOrder) { Invoke-Wrapper $id 'uninstall' }\n    # Preserve ALL data, secrets, backups, releases, runtime and service files.\n",
        "rollback": _ROLLBACK,
        "secrets": _SECRET,
    }
    result = {}
    for action, body in bodies.items():
        header = "#requires -Version 5.1\n# HomenS.AI - generated text only; not acceptance-tested.\n"
        if action == "rollback":
            header += _ROLLBACK_PARAMS
        else:
            header += "[CmdletBinding()]\nparam()\n"
        text = header + _COMMON + "\n" + constants
        text += "$lock = $null\n$locked = $false\ntry {\n"
        text += "    foreach ($directory in $Directories) { Assert-SafePath $directory }\n"
        if action == "install":
            text += "    New-SafeDirectory $Services\n"
        text += "    $lockPath = Join-Path $Services 'operation.lock'\n    Assert-SafePath $lockPath\n"
        text += "    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)\n"
        text += "    $lock.Lock(2147483647, 1)\n    $locked = $true\n" + body
        text += "} catch {\n    Write-Error 'Windows operation failed; no sensitive details emitted.' -ErrorAction Continue\n    exit 1\n} finally {\n    if ($null -ne $lock) {\n        if ($locked) { $lock.Unlock(2147483647, 1) }\n        $lock.Dispose()\n    }\n}\n"
        # PS5.1 reads UTF-8 without BOM as ANSI; BOM is part of deterministic text.
        result[action + ".ps1"] = "\ufeff" + text
    result.update(xml)
    return result
