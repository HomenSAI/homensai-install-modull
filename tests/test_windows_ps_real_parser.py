"""Проверки PowerShell Core на Linux; не замена запуску на Windows."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from shared.manifest import load_manifest
from shared.windows_ps import _COMMON, render_windows

ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which('pwsh')
pytestmark = pytest.mark.skipif(os.name != 'posix' or PWSH is None,
                                reason='Нужен PowerShell Core на POSIX')


def test_generated_windows_scripts_parse_with_powershell_core(tmp_path):
    payload = b'synthetic WinSW bytes, never executable'
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['winsw'] = {'version': '2.12.0', 'sha256': hashlib.sha256(payload).hexdigest()}
    outputs = render_windows(manifest, install_root="C:\\Demo\\Owner's $data; &(literal)`",
                             modes=('autonomous',), winsw_payload=payload)
    scripts = tmp_path / 'generated'
    scripts.mkdir()
    for name, content in outputs.items():
        if name.endswith('.ps1'):
            (scripts / name).write_bytes(content.encode('utf-8'))
    parser = tmp_path / 'parse.ps1'
    parser.write_text("""param([string]$Directory)
$ErrorActionPreference = 'Stop'
foreach ($file in Get-ChildItem -LiteralPath $Directory -Filter '*.ps1') {
    $tokens = $null; $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count -ne 0) { Write-Error 'Parse failed'; exit 1 }
}
""", encoding='utf-8')
    result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(parser), str(scripts)],
                            cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    assert result.returncode == 0, 'PowerShell Core не принял синтаксис созданных сценариев'


def test_wrapper_executes_only_when_xml_matches(tmp_path):
    executable = tmp_path / 'demo.exe'
    executable.write_bytes(b'#!/bin/sh\nprintf called > called-marker\n')
    executable.chmod(0o700)
    config = tmp_path / 'demo.xml'
    config.write_bytes(b'<service>synthetic</service>\n')
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    harness = tmp_path / 'wrapper.ps1'
    harness.write_text("param([string]$Services, [string]$WinSWHash, [string]$ExpectedHash)\n" + _COMMON + """
$ConfigHashes = @{ 'demo' = $ExpectedHash }
$marker = Join-Path $Services 'called-marker'
Invoke-Wrapper 'demo' 'start'
if (-not (Test-Path -LiteralPath $marker)) { exit 10 }
Remove-Item -LiteralPath $marker
[IO.File]::WriteAllText((Join-Path $Services 'demo.xml'), '<service>tampered</service>')
$rejected = $false
try { Invoke-Wrapper 'demo' 'start' } catch { $rejected = $true }
if (-not $rejected -or (Test-Path -LiteralPath $marker)) { exit 11 }
exit 0
""", encoding='utf-8')
    result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(harness),
                             str(tmp_path), executable_hash, config_hash], cwd=tmp_path,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
    assert result.returncode == 0, 'Тестовая оболочка не подтвердила проверку XML'
    assert config.read_text() == '<service>tampered</service>'
    assert not (tmp_path / 'called-marker').exists()
