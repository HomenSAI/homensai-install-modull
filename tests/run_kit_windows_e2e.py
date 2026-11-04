#!/usr/bin/env python3
"""Сквозная проверка комплекта на Windows 10 в тестовой ВМ (dockur/windows + KVM).

Собирает на хосте единые комплекты (Linux + Windows) вымышленного продукта —
версии 1, 2, 3 и 3 с отказом здоровья — и кладёт их в общую папку ВМ. Агент
в ВМ (OEM-сценарий, администратор) выполняет задание PowerShell: отказ при
junction ProgramData/HomenSAI на каталог Administrators, отказ без
обязательного секрета, установку служб WinSW с доставкой секретов, смену секрета, обновление 1→2, обновление с откатом, обновление 2→3, удаление с
сохранением данных, переустановку и полное удаление. Сеть ВМ для проверки не
нужна. Требуется --run и запущенная ВМ с агентом (см. docs/KIT.md).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
JOB = r'''$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$base = 'C:\hsi\e2e'
$log = '\\host.lan\Data\jobs\@NAME@.log'
function Write-Output($message) { Add-Content -Path $log -Value $message -Encoding UTF8 }
function Fail($message) { Write-Output "E2E FAIL: $message"; Write-Output (Get-Service 'homensai.homensai-kit-demo.*' -ErrorAction SilentlyContinue | Format-Table -AutoSize | Out-String); exit 1 }
function Step($message) { Write-Output "=== $message" }
function Secrets() { try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:18431/secrets').Content } catch { return '' } }
function Health() { try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:18431/healthz').Content } catch { return '' } }
function Notes() { try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:18431/notes').Content } catch { return '' } }
function WaitRelease($release) {
    for ($i = 0; $i -lt 120; $i++) { if ((Health) -match ('"release": "' + $release + '"')) { return $true }; Start-Sleep -Milliseconds 500 }
    return $false
}
function Fingerprint($text) {
    $hash = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($text))
    return (-join ($hash | ForEach-Object { $_.ToString('x2') })).Substring(0, 12)
}
function WaitToken($token) {
    $expected = '"token": "' + (Fingerprint $token) + '"'
    for ($i = 0; $i -lt 120; $i++) { $h = Secrets; if ($h.Contains($expected) -and $h.Contains('"session_key": true')) { return $true }; Start-Sleep -Milliseconds 500 }
    return $false
}
function Setup($kit, [string[]]$arguments) {
    $cmd = Join-Path $kit 'setup.cmd'
    & cmd.exe /c $cmd @arguments 2>&1 | ForEach-Object { Write-Output "    $_" }
    return $LASTEXITCODE
}
# Тестовая ВМ: убрать остатки прежних прогонов (службы, в том числе со старой схемой ID, состояние, каталог установки).
foreach ($s in Get-Service 'homensai.homensai-kit-demo.*', 'homensai-kit-demo-*' -ErrorAction SilentlyContinue) { Stop-Service $s.Name -Force -ErrorAction SilentlyContinue; & sc.exe delete $s.Name | Out-Null }
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*kit demo*" -or $_.Path -like "*HomenSAI*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
foreach ($p in "C:\ProgramData\HomenSAI\.state\homensai-kit-demo", "C:\ProgramData\HomenSAI\.secrets\homensai-kit-demo", "C:\Программы HomenSAI") { if (Test-Path $p) { & takeown.exe /F $p /R /D Y | Out-Null; & icacls.exe $p /grant "*S-1-5-32-544:(OI)(CI)F" /T /Q | Out-Null; Remove-Item -Recurse -Force $p } }
Remove-Item -Recurse -Force $base -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $base | Out-Null
foreach ($name in '1', '2', '3', '3bad') {
    New-Item -ItemType Directory -Force "$base\$name" | Out-Null
    tar -xzf "\\host.lan\Data\kits\homensai-kit-demo-$name.tar.gz" -C "$base\$name"
    if ($LASTEXITCODE -ne 0) { Fail "extract $name" }
}
$K1 = "$base\1\homensai-kit-demo-1"; $K2 = "$base\2\homensai-kit-demo-2"
$K3 = "$base\3\homensai-kit-demo-3"; $KB = "$base\3bad\homensai-kit-demo-3bad"
$root = 'C:\Программы HomenSAI\kit demo'
$stored = 'C:\ProgramData\HomenSAI\.state\homensai-kit-demo\installer'
$secrets = 'C:\ProgramData\HomenSAI\.secrets\homensai-kit-demo'

Step 'junction C:\ProgramData\HomenSAI на каталог Administrators отклоняется до изменений'
$hsi = 'C:\ProgramData\HomenSAI'; $decoy = 'C:\ProgramData\HomenSAI-e2e-decoy'
foreach ($p in $hsi, $decoy) { if (Test-Path $p) { & takeown.exe /F $p /R /D Y | Out-Null; & icacls.exe $p /grant "*S-1-5-32-544:(OI)(CI)F" /T /Q | Out-Null; Remove-Item -Recurse -Force $p } }
New-Item -ItemType Directory $decoy | Out-Null
& icacls.exe $decoy /setowner '*S-1-5-32-544' /Q | Out-Null
& cmd.exe /c mklink /J $hsi $decoy | Out-Null
if (-not ((Get-Item $hsi -Force).Attributes -match 'ReparsePoint')) { Fail 'junction не создан' }
$env:KITDEMO_TOKEN = 'e2e-token-1'
$out = & cmd.exe /c (Join-Path $K1 'setup.cmd') --yes --language en install --root $root 2>&1 | Out-String
$code = $LASTEXITCODE
Remove-Item Env:KITDEMO_TOKEN
Write-Output "    $($out.Trim())"
if ($code -eq 0) { Fail 'установка через junction должна отказать' }
if (-not $out.Contains('The state or service directory is unsafe')) { Fail 'ожидался отказ kit.unsafe_state' }
if (Get-ChildItem -Force -Recurse $decoy) { Fail 'в цель junction ничего не должно попасть' }
if (Test-Path (Split-Path $root)) { Fail 'корень установки не должен создаваться' }
& cmd.exe /c rmdir $hsi
Remove-Item -Recurse -Force $decoy
if (Test-Path $hsi) { Fail 'junction не удалён' }

Step 'установка без обязательного секрета отклоняется до изменений'
Remove-Item Env:KITDEMO_TOKEN -ErrorAction SilentlyContinue
if ((Setup $K1 @('--yes', '--language', 'ru', 'install', '--root', $root)) -eq 0) { Fail 'установка без секрета' }
if (Test-Path $root) { Fail 'отказ из-за секрета должен быть до изменений' }

Step 'установка версии 1'
$env:KITDEMO_TOKEN = 'e2e-token-1'
if ((Setup $K1 @('--yes', '--language', 'ru', 'install', '--root', $root)) -ne 0) { Fail 'install v1' }
Remove-Item Env:KITDEMO_TOKEN
if (-not (WaitRelease '1')) { Fail 'health v1' }
if (-not (WaitToken 'e2e-token-1')) { Fail ('секреты не доставлены службе: ' + (Secrets)) }
if (-not ((Get-Content "$secrets\secrets.env" -Raw) -match 'KITDEMO_SESSION_KEY=')) { Fail 'сгенерированный секрет' }
$acl = (Get-Acl "$secrets\secrets.env").Access | Where-Object { $_.IdentityReference -notmatch 'SYSTEM|Administrators|Администраторы' }
if ($acl) { Fail ('файл секретов доступен не только SYSTEM и Administrators: ' + ($acl | Out-String)) }
if (Get-ChildItem -Recurse -File "$root\data", "$root\config", "C:\ProgramData\HomenSAI\.state\homensai-kit-demo\state.json" | Select-String -Pattern 'e2e-token-1' -SimpleMatch -ErrorAction SilentlyContinue) { Fail 'значение секрета вне файла секретов' }
if ((Notes) -ne '[]') { Fail ('новая база должна быть пустой: ' + (Notes)) }
if (-not ((Get-Content "$root\data\deps.json" -Raw) -match '"sizeof_int": 4')) { Fail 'двоичная зависимость cffi' }
$service = Get-Service 'homensai.homensai-kit-demo.web'
if ($service.Status -ne 'Running' -or $service.StartType -ne 'Automatic') { Fail 'служба web' }
if ((Setup $stored @('--language', 'en', 'status')) -ne 0) { Fail 'status из сохранённого установщика' }
if ((Setup $K1 @('--yes', 'install')) -eq 0) { Fail 'повторная установка должна отказать' }
$acl = (Get-Acl $root).Access | Where-Object { $_.IdentityReference -match 'Users|Пользователи' }
if ($acl) { Fail 'каталог установки доступен пользователям' }

Step 'обновление без пароля отклоняется'
Remove-Item Env:HOMENSAI_ARCHIVE_PASSWORD -ErrorAction SilentlyContinue
if ((Setup $K2 @('--yes', 'update')) -eq 0) { Fail 'update без пароля' }
if (-not (WaitRelease '1')) { Fail 'v1 после отказа' }

Step 'обновление 1 -> 2 с миграцией'
$env:HOMENSAI_ARCHIVE_PASSWORD = 'synthetic-e2e-password'
if ((Setup $K2 @('--yes', '--language', 'ru', 'update')) -ne 0) { Fail 'update 1->2' }
if (-not (WaitRelease '2')) { Fail 'health v2' }
if (-not ((Notes) -match 'migrated notes-2')) { Fail ('миграция notes-2: ' + (Notes)) }

Step 'обновление с отказом здоровья -> откат'
$code = Setup $KB @('--yes', '--language', 'ru', 'update')
if ($code -ne 4) { Fail "ожидался код отката 4, получен $code" }
if (-not (WaitRelease '2')) { Fail 'после отката должна работать v2' }
if ((Notes) -match 'migrated notes-3') { Fail 'миграция отменённого обновления осталась' }

Step 'обновление 2 -> 3 после отката'
if ((Setup $K3 @('--yes', '--language', 'de', 'update')) -ne 0) { Fail 'update 2->3' }
if (-not (WaitRelease '3')) { Fail 'health v3' }
if (-not ((Notes) -match 'migrated notes-3')) { Fail 'миграция notes-3' }
Remove-Item Env:HOMENSAI_ARCHIVE_PASSWORD

Step 'смена секрета командой secrets и перезапуск служб'
if ((Setup $stored @('--language', 'ru', 'secrets')) -ne 0) { Fail 'перечень секретов' }
$env:KITDEMO_TOKEN = 'e2e-token-2'
if ((Setup $stored @('--yes', '--language', 'ru', 'secrets', 'KITDEMO_TOKEN')) -ne 0) { Fail 'secrets set' }
Remove-Item Env:KITDEMO_TOKEN
if (-not (WaitToken 'e2e-token-2')) { Fail ('новый секрет не доставлен: ' + (Secrets)) }

Step 'удаление с сохранением данных'
if ((Setup $stored @('--yes', '--language', 'ru', 'uninstall')) -ne 0) { Fail 'uninstall keep' }
if (Get-Service 'homensai.homensai-kit-demo.web' -ErrorAction SilentlyContinue) { Fail 'служба должна быть удалена' }
if (Health) { Fail 'служба должна быть остановлена' }
if (-not (Test-Path "$root\data\kitdemo.sqlite3")) { Fail 'данные должны сохраниться' }
if (-not ((Get-Content "$secrets\secrets.env" -Raw) -match 'KITDEMO_TOKEN=e2e-token-2')) { Fail 'секреты должны сохраниться' }

Step 'переустановка той же версии на сохранённых данных'
if ((Setup $K2 @('--yes', 'install')) -eq 0) { Fail 'переустановка другой версии должна отказать' }
if ((Setup $K3 @('--yes', '--language', 'ru', 'install')) -ne 0) { Fail 'reinstall v3' }
if (-not (WaitRelease '3')) { Fail 'health после переустановки' }
if (-not (WaitToken 'e2e-token-2')) { Fail 'секреты после переустановки' }
if (-not ((Notes) -match 'migrated notes-3')) { Fail 'данные после переустановки' }

Step 'полное удаление'
if ((Setup $stored @('--yes', 'uninstall', '--purge')) -ne 0) { Fail 'purge' }
if (Test-Path "$root\data") { Fail 'данные должны быть удалены' }
if (Test-Path $secrets) { Fail 'секреты должны быть удалены' }
if (Get-Service 'homensai.homensai-kit-demo.*' -ErrorAction SilentlyContinue) { Fail 'службы после purge' }
Write-Output 'E2E OK'
exit 0
'''


def build_kits(inputs: Path, output: Path, python: str):
    keys = output / 'keys'
    subprocess.run([python, '-m', 'shared.kit', 'keygen', str(keys)], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    variants = {'1': (['notes-1'], False), '2': (['notes-1', 'notes-2'], False),
                '3': (['notes-1', 'notes-2', 'notes-3'], False), '3bad': (['notes-1', 'notes-2', 'notes-3'], True)}
    linux = next((inputs / 'pbs').glob('*-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'))
    windows = next((inputs / 'pbs').glob('*-x86_64-pc-windows-msvc-install_only_stripped.tar.gz'))
    for version, (migrations, broken) in variants.items():
        project = output / ('project-' + version)
        shutil.copytree(ROOT / 'examples/kit_demo', project, ignore=shutil.ignore_patterns('wheelhouse', 'inputs'))
        shutil.copytree(inputs / 'wheelhouse', project / 'installer/wheelhouse')
        target = project / 'installer/inputs'
        target.mkdir()
        for item in (linux, windows, inputs / 'winsw/WinSW-x64.exe', inputs / 'winsw/LICENSE.txt'):
            shutil.copyfile(item, target / item.name)
        config = json.loads((project / 'homensai-kit.json').read_text())
        config['migrations'] = migrations
        config['targets'] = ['linux-x86_64', 'windows-x86_64']
        config['runtime']['windows'] = {
            'archive': 'installer/inputs/' + windows.name,
            'sha256': 'c1876a9795d3a94ee49a1499a69cd91e26ca3e2829593f93fa6e4db90d05402b',
            'origin': 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/'
                      'cpython-3.14.10%2B20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz',
            'python_version': '3.14.10'}
        config['winsw'] = {'exe': 'installer/inputs/WinSW-x64.exe', 'license': 'installer/inputs/LICENSE.txt',
                           'sha256': '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'}
        (project / 'homensai-kit.json').write_text(json.dumps(config))
        if broken:
            (project / 'src/kitdemo/FAIL_HEALTH').write_text('synthetic health failure\n')
        subprocess.run([python, '-m', 'shared.kit', 'build', '--config', str(project / 'homensai-kit.json'),
                        '--version', version, '--signing-key', str(keys / 'publisher-ed25519.pem'),
                        '--output', str(output / 'kits')], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--inputs', type=Path, default=Path.home() / '.cache/homensai-kit-inputs')
    parser.add_argument('--shared', type=Path, default=Path.home() / '.cache/homensai-winvm/shared',
                        help='каталог, подключённый в ВМ как \\\\host.lan\\Data')
    parser.add_argument('--kits', type=Path, help='готовые комплекты (пропустить сборку)')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()
    jobs = args.shared / 'jobs'
    alive = jobs / 'agent.alive'
    if not alive.exists() or time.time() - alive.stat().st_mtime > 60:
        print('Агент ВМ не отвечает (нет свежего agent.alive)', file=sys.stderr)
        return 2
    kits = args.shared / 'kits'
    if args.kits is None:
        work = Path.home() / '.cache/homensai-kit-windows-e2e'
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        build_kits(args.inputs, work, args.python)
        source = work / 'kits'
    else:
        source = args.kits
    shutil.rmtree(kits, ignore_errors=True)
    kits.mkdir()
    for item in source.glob('*.tar.gz'):
        shutil.copyfile(item, kits / item.name)
    name = 'kit-e2e-' + str(int(time.time()))
    for suffix in ('.out', '.rc'):
        (jobs / (name + suffix)).unlink(missing_ok=True)
    (jobs / (name + '.ps1')).write_bytes(b'\xef\xbb\xbf' + JOB.replace('@NAME@', name).replace('\n', '\r\n').encode('utf-8'))
    deadline = time.monotonic() + args.timeout
    rc = jobs / (name + '.rc')
    while not rc.exists():
        if time.monotonic() > deadline:
            print('Время ожидания задания истекло', file=sys.stderr)
            return 1
        time.sleep(5)
    time.sleep(2)
    log = jobs / (name + '.log')
    print(log.read_text(encoding='utf-8-sig', errors='replace') if log.exists() else '(журнал отсутствует)')
    if (jobs / (name + '.out')).exists():
        print((jobs / (name + '.out')).read_text(encoding='utf-8', errors='replace'))
    code = int(rc.read_text(encoding='ascii', errors='ignore').strip().lstrip('\ufeff') or 1)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
