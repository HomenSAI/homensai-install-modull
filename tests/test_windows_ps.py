"""Только тексты и локальные tmp_path; PowerShell/WinSW не исполняются."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest

from shared import ctl
from shared.manifest import ManifestError, load_manifest
from shared.windows_ps import render_windows


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
GOLDEN = Path(__file__).with_name("golden")
PAYLOAD = b"synthetic WinSW bytes, never executable"
SECRET = "SYNTHETIC-PRIVATE-VALUE-step8"
ROOT = "C:\\Windows Demo\\Owner's $data; &(literal)`"


@pytest.fixture(autouse=True)
def forbid_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Генератор не исполняет процессы")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.fixture
def manifest():
    value = load_manifest(EXAMPLES / "rent.manifest.json")
    value["winsw"] = {"version": "2.12.0", "sha256": hashlib.sha256(PAYLOAD).hexdigest()}
    return value


def render(value, **kwargs):
    return render_windows(value, install_root=kwargs.pop("install_root", ROOT),
                          modes=kwargs.pop("modes", ("autonomous",)),
                          winsw_payload=kwargs.pop("winsw_payload", PAYLOAD), **kwargs)


@pytest.mark.parametrize("action", ["install", "secrets", "rollback"])
def test_golden_text(manifest, action, tmp_path):
    output = render(manifest)[action + ".ps1"]
    target = tmp_path / (action + ".ps1")
    target.write_bytes(output.encode("utf-8"))
    assert target.read_bytes() == (GOLDEN / ("windows-" + action + ".ps1")).read_bytes()


def test_deterministic_no_input_mutation_or_files(manifest, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = deepcopy(manifest)
    first = render(manifest)
    manifest["components"].reverse()
    manifest["secrets"].reverse()
    manifest["paths"] = dict(reversed(list(manifest["paths"].items())))
    assert render(manifest) == first
    manifest["components"].reverse()
    manifest["secrets"].reverse()
    assert manifest == before
    assert list(tmp_path.iterdir()) == []
    assert set(first) == {a + ".ps1" for a in (
        "install", "start", "stop", "status", "uninstall", "rollback", "secrets")} | {
            "homensai-rent-demo-" + c + ".xml" for c in ("web", "bot", "scheduler")}


def test_directories_quoting_acl_and_operation_lock(manifest):
    outputs = render(manifest)
    install = outputs["install.ps1"]
    assert "C:\\Windows Demo\\Owner''s $data; &(literal)`\\demo-layout\\data'" in install
    assert "$Secrets = 'C:\\Windows Demo\\Owner''s $data; &(literal)`\\demo-layout\\data\\secrets'" in install
    assert "SetAccessRuleProtection($true, $false)" in install
    assert "New-Object Security.AccessControl.DirectorySecurity" in install
    assert "New-Object Security.AccessControl.FileSecurity" in install
    assert "icacls.exe" in install and "'/inheritance:r' '/grant:r'" in install
    assert "S-1-5-18" in install and "S-1-5-32-544" in install
    assert "Protect-Directory $Data\n" in install and "Protect-Directory $Secrets\n" in install
    assert "Protect-Directory $App" not in install
    for name, text in outputs.items():
        if not name.endswith(".ps1"):
            continue
        assert "$lock.Lock(2147483647, 1)" in text
        assert "$lock.Unlock(2147483647, 1)" in text and "$lock.Dispose()" in text
        assert "ReparsePoint" in text and "EnumerateFileSystemEntries" in text
        assert "Remove-Item" not in text  # Includes persistent lock file.
        assert text.startswith("\ufeff#requires -Version 5.1\n")
        assert all(operator not in text for operator in ("??", "?.", "&&", "||", "ForEach-Object -Parallel"))


@pytest.mark.parametrize("scope", ["CurrentUser", "LocalMachine"])
def test_secrets_names_only_and_dpapi(manifest, monkeypatch, scope):
    for name in ("BACKUP_PASSWORD", "BOT_TOKEN", "ENCRYPTION_KEY"):
        monkeypatch.setenv(name, SECRET)
    outputs = render(manifest, dpapi_scope=scope)
    assert SECRET not in "".join(outputs.values())
    text = outputs["secrets.ps1"]
    assert "$SecretNames = @('BACKUP_PASSWORD', 'BOT_TOKEN', 'ENCRYPTION_KEY')" in text
    assert "Read-Host -Prompt $name -AsSecureString" in text
    assert "[Security.Cryptography.DataProtectionScope]::" + scope in text
    assert "[Security.Cryptography.ProtectedData]::Protect($plain, $null, $Scope)" in text
    assert "$stream.Write($cipher, 0, $cipher.Length)" in text
    assert "[IO.FileMode]::CreateNew" in text and "ZeroFreeBSTR" in text
    assert "[Array]::Clear($plain, 0, $plain.Length)" in text
    assert "PtrToString" not in text and "Write-Output $" not in text
    assert "Start-Transcript" not in text and "ConvertFrom-SecureString" not in text
    assert "[IO.File]::WriteAllBytes($target, $plain)" not in text


def test_winsw_verified_before_copy_registration_and_every_use(manifest):
    outputs = render(manifest)
    text = outputs["install.ps1"]
    assert text.index("Assert-Pin $source") < text.index("[IO.File]::Copy")
    assert text.index("[IO.File]::Copy") < text.index("Invoke-Wrapper $id 'install'")
    wrapper = text[text.index("function Invoke-Wrapper"):text.index("function Stop-Components")]
    assert wrapper.index("Assert-Pin $exe") < wrapper.index("& $exe $Action")
    assert "Get-FileHash -LiteralPath $Path -Algorithm SHA256" in text
    for name in ("web", "bot", "scheduler"):
        xml = ET.fromstring(outputs["homensai-rent-demo-" + name + ".xml"])
        assert xml.findtext("startmode") == "Automatic"
        assert xml.findtext("delayedAutoStart") == "true"
        assert xml.findtext("workingdirectory") == ROOT + "\\demo-layout\\app"
    assert "$StartOrder = @('homensai-rent-demo-web', 'homensai-rent-demo-bot', 'homensai-rent-demo-scheduler')" in text
    assert "$StopOrder = @('homensai-rent-demo-scheduler', 'homensai-rent-demo-bot', 'homensai-rent-demo-web')" in text


def test_status_uninstall_and_rollback_contract(manifest):
    outputs = render(manifest)
    assert "Get-Service -Name $id" in outputs["status.ps1"]
    assert "Invoke-Wrapper $id 'uninstall'" in outputs["uninstall.ps1"]
    body = outputs["rollback.ps1"].split("$locked = $true\n", 1)[1]
    steps = ["Invoke-VerifiedPort $VerifyRecovery", "Stop-Components",
             "Invoke-VerifiedPort $BackupBeforeRollback", "Invoke-VerifiedPort $RestoreRecovery",
             "Invoke-VerifiedPort $SwitchPreviousRelease", "Start-Components", "Invoke-VerifiedPort $Health"]
    assert [body.index(step) for step in steps] == sorted(body.index(step) for step in steps)
    assert "[Parameter(Mandatory=$true)][scriptblock]$BackupBeforeRollback" in outputs["rollback.ps1"]
    assert "$result -isnot [bool] -or -not $result" in outputs["rollback.ps1"]
    assert body.count("Stop-Components") == 2


@pytest.mark.parametrize("payload", [None, b"mismatch", "not bytes"])
def test_missing_or_wrong_pin_payload(manifest, payload):
    with pytest.raises(ManifestError, match="WinSW"):
        render(manifest, winsw_payload=payload)


def test_both_original_examples_rejected_without_native_contract():
    for name in ("rent", "jobpilot"):
        with pytest.raises(ManifestError):
            render(load_manifest(EXAMPLES / (name + ".manifest.json")))


@pytest.mark.parametrize("hostile", ["a;whoami", "a';&whoami", "$(whoami)", "a`n", "a\n", "../bad", "a%PATH%", "a&b", "-option"])
@pytest.mark.parametrize("field", ["product_id", "component", "secret"])
def test_hostile_identifiers_are_rejected(manifest, hostile, field):
    if field == "component":
        manifest["components"][0]["name"] = hostile
    elif field == "secret":
        manifest["secrets"][0]["name"] = hostile
    else:
        manifest[field] = hostile
    with pytest.raises(ManifestError) as error:
        render(manifest)
    assert hostile not in str(error.value)


@pytest.mark.parametrize("hostile", ["C:relative", "relative", "C:\\", "\\rooted", "\\\\server\\share", "/posix", "C:\\safe\\..\\escape", "C:\\bad.\\file", "C:\\NUL.txt", "C:\\CONIN$", "C:\\a:stream", "C:\\bad?name", "C:\\bad*name", 'C:\\bad"name', "C:\\bad\nname", "C:\\bad\x00", "C:\\%TEMP%", "C:\\smart\u2019quote", "C:\\line\u2028break", "C:\\bad\\", "C:\\double\\\\segment"])
def test_hostile_install_roots(manifest, hostile):
    with pytest.raises(ManifestError):
        render(manifest, install_root=hostile)


@pytest.mark.parametrize("hostile", ["data/NUL", "data/bad?name", "data/bad*name", "data/%TEMP%", "data/../escape", "C:drive", "data/bad./leaf", "data/line\u2028end"])
def test_hostile_manifest_paths(manifest, hostile):
    manifest["paths"]["data"] = hostile
    with pytest.raises(ManifestError):
        render(manifest)


@pytest.mark.parametrize("location", ["services", "backup", "app", "releases", "data/child"])
def test_overlapping_directories(manifest, location):
    manifest["paths"]["data"] = "demo-layout/" + location
    if location == "data/child":
        manifest["paths"]["backup"] = "demo-layout/data"
    with pytest.raises(ManifestError):
        render(manifest)


@pytest.mark.parametrize("path", ["operation.lock", "operation.lock/child", "homensai-rent-demo-web.exe", "homensai-rent-demo-web.xml/child", "active.json/child", "NUL"])
def test_management_file_collisions(manifest, path):
    manifest["update"]["state_file"] = path
    with pytest.raises(ManifestError):
        render(manifest)


@pytest.mark.parametrize("scope", ["localmachine", "Bad;whoami", "CurrentUser\n", "", None])
def test_invalid_dpapi_scope(manifest, scope):
    with pytest.raises(ManifestError):
        render(manifest, dpapi_scope=scope)


@pytest.mark.parametrize("modes", [(), ("unknown",), ("autonomous", "platform"), ("platform", "platform")])
def test_modes_rejected(manifest, modes):
    with pytest.raises(ManifestError):
        render(manifest, modes=modes)


def test_service_argv_remains_xml_data(manifest):
    hostile = "x'; & whoami; $(oops) ` & < > \\\" tail\\"
    manifest["components"][0]["cmd"].append(hostile)
    outputs = render(manifest)
    xml = outputs["homensai-rent-demo-web.xml"]
    assert "&lt;" in xml and "&amp;" in xml
    assert "$(oops)" in ET.fromstring(xml).findtext("arguments")
    assert hostile not in outputs["install.ps1"]  # Embedded XML is base64 data.


def test_empty_secret_descriptors_and_platform_mode(manifest):
    manifest["secrets"] = []
    outputs = render(manifest, modes=("platform",))
    assert "$SecretNames = @()" in outputs["secrets.ps1"]
    assert [name for name in outputs if name.endswith(".xml")] == ["homensai-rent-demo-core-server.xml"]


def cli(capsys, manifest_path, output, binary, *args):
    code = ctl.main(["--manifest", str(manifest_path), "render-windows", "--json",
                     "--install-root", ROOT, "--mode", "autonomous", "--winsw-file", str(binary),
                     "--output-dir", str(output), *args])
    captured = capsys.readouterr()
    assert not captured.err and SECRET not in captured.out
    return code, json.loads(captured.out)


def test_cli_writes_text_only_and_refuses_overwrite(manifest, tmp_path, capsys):
    public = tmp_path / "public.json"
    public.write_text(json.dumps(manifest), encoding="utf-8")
    binary = tmp_path / "public.bin"
    binary.write_bytes(PAYLOAD)
    output = tmp_path / "rendered"
    code, response = cli(capsys, public, output, binary, "--dpapi-scope", "CurrentUser")
    assert code == 0 and response["ok"]
    files = {p.name: p.read_bytes() for p in output.iterdir()}
    assert files == {name: text.encode("utf-8") for name, text in render(manifest, dpapi_scope="CurrentUser").items()}
    code, response = cli(capsys, public, output, binary)
    assert code == 2 and not response["ok"]
    assert files == {p.name: p.read_bytes() for p in output.iterdir()}


@pytest.mark.parametrize("example", ["rent", "jobpilot"])
def test_cli_original_examples_fail_without_writes(tmp_path, capsys, example):
    binary = tmp_path / "public.bin"
    binary.write_bytes(PAYLOAD)
    output = tmp_path / "rendered"
    code, response = cli(capsys, EXAMPLES / (example + ".manifest.json"), output, binary)
    assert code == 2 and not response["ok"] and not output.exists()


def test_manifest_secret_values_rejected_without_leak(manifest, tmp_path, capsys):
    manifest["secrets"][0]["value"] = SECRET
    path = tmp_path / "public.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    binary = tmp_path / "public.bin"
    binary.write_bytes(PAYLOAD)
    output = tmp_path / "rendered"
    assert cli(capsys, path, output, binary)[0] == 2
    assert not output.exists()
