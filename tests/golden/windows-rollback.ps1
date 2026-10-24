#requires -Version 5.1
# HomenS.AI - generated text only; not acceptance-tested.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][scriptblock]$VerifyRecovery,
    [Parameter(Mandatory=$true)][scriptblock]$BackupBeforeRollback,
    [Parameter(Mandatory=$true)][scriptblock]$RestoreRecovery,
    [Parameter(Mandatory=$true)][scriptblock]$SwitchPreviousRelease,
    [Parameter(Mandatory=$true)][scriptblock]$Health
)

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

$App = 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\app'
$Data = 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\data'
$Services = 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\services'
$Secrets = 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\data\secrets'
$WinSWHash = '83710c9befb7bd25d147ebfaf4df5146e97f25e1d4ba96913e235353d5bab977'
$ScopeName = 'LocalMachine'
$Directories = @('C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\app', 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\releases', 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\data', 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\backup', 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\services', 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\config', 'C:\Windows Demo\Owner''s $data; &(literal)`\demo-layout\runtime')
$StartOrder = @('homensai-rent-demo-web', 'homensai-rent-demo-bot', 'homensai-rent-demo-scheduler')
$StopOrder = @('homensai-rent-demo-scheduler', 'homensai-rent-demo-bot', 'homensai-rent-demo-web')
$SecretNames = @('BACKUP_PASSWORD', 'BOT_TOKEN', 'ENCRYPTION_KEY')
Add-Type -AssemblyName System.Security
$Scope = [Security.Cryptography.DataProtectionScope]::LocalMachine
$lock = $null
$locked = $false
try {
    foreach ($directory in $Directories) { Assert-SafePath $directory }
    $lockPath = Join-Path $Services 'operation.lock'
    Assert-SafePath $lockPath
    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
    $lock.Lock(2147483647, 1)
    $locked = $true

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
} catch {
    Write-Error 'Windows operation failed; no sensitive details emitted.' -ErrorAction Continue
    exit 1
} finally {
    if ($null -ne $lock) {
        if ($locked) { $lock.Unlock(2147483647, 1) }
        $lock.Dispose()
    }
}
