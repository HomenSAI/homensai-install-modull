"""Только синтетические байты/окружение и проверка текстов, без служб."""

import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from shared.manifest import ManifestError, load_manifest, validate_manifest
from shared.services import component_order, render_services, verify_winsw


PAYLOAD = b"synthetic-not-an-executable"


@pytest.fixture
def manifest():
    value = load_manifest(Path(__file__).resolve().parents[1] / "examples/rent.manifest.json")
    value["winsw"] = {"version": "2.12.0", "sha256": hashlib.sha256(PAYLOAD).hexdigest()}
    return value


def render(manifest, platform="linux", **kwargs):
    return render_services(manifest, platform=platform, modes=("autonomous",),
                           install_root="C:\\Demo & App" if platform == "windows" else "/srv/demo app", **kwargs)


def test_topological_selection_and_mutual_exclusion(manifest):
    manifest["components"].reverse()
    assert component_order(manifest, ("autonomous",)) == ("web", "bot", "scheduler")
    assert component_order(manifest, ("platform",)) == ("core-server",)
    for modes in ((), ("unknown",), ("autonomous", "platform"), ("platform", "platform")):
        with pytest.raises(ManifestError):
            component_order(manifest, modes)


@pytest.mark.parametrize("payload,pin,valid", [
    (PAYLOAD, hashlib.sha256(PAYLOAD).hexdigest(), True),
    (PAYLOAD + b"x", hashlib.sha256(PAYLOAD).hexdigest(), False),
    (PAYLOAD, "a" * 63, False), (PAYLOAD, "A" * 64, False),
    (PAYLOAD, None, False), (None, "a" * 64, False),
])
def test_pure_digest(payload, pin, valid):
    assert verify_winsw(payload, pin) is valid


@pytest.mark.parametrize("pin", [None, "bad", "A" * 64, "a" * 64 + "\n", 12])
def test_pin_schema(manifest, pin):
    manifest["winsw"]["sha256"] = pin
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("mutation", ["missing", "mismatch", "version", "extra"])
def test_windows_refuses_unverified_payload(manifest, mutation):
    if mutation == "missing":
        del manifest["winsw"]
    elif mutation == "mismatch":
        manifest["winsw"]["sha256"] = "a" * 64
    elif mutation == "version":
        manifest["winsw"]["version"] = "latest"
    else:
        manifest["winsw"]["url"] = "https://example.invalid/"
    with pytest.raises(ManifestError):
        render(manifest, "windows", winsw_payload=PAYLOAD)


def test_windows_requires_bytes_even_with_pin(manifest):
    with pytest.raises(ManifestError):
        render(manifest, "windows")


def test_xml_escaping_arguments_dependencies_and_limits(manifest):
    manifest["components"][0]["cmd"] = ["python", 'a&<b>"c', "space arg", "C:\\end\\"]
    output = render(manifest, "windows", winsw_payload=PAYLOAD)
    root = ET.fromstring(output["homensai-rent-demo-web.xml"])
    assert root.findtext("executable") == "python"
    assert root.findtext("arguments") == '"a&<b>\\"c" "space arg" "C:\\end\\\\"'
    assert root.findtext("workingdirectory") == "C:\\Demo & App\\demo-layout\\app"
    assert "&amp;" in output["homensai-rent-demo-web.xml"]
    assert root.findtext("startmode") == "Automatic"
    assert root.findtext("delayedAutoStart") == "true"
    assert root.findtext("resetfailure") == "600 sec"
    assert [node.attrib for node in root.findall("onfailure")] == [
        {"action": "restart", "delay": "60 sec"},
        {"action": "restart", "delay": "60 sec"}, {"action": "none"}]
    bot = ET.fromstring(output["homensai-rent-demo-bot.xml"])
    assert bot.findtext("depend") == "homensai-rent-demo-web"


def test_unit_escaping_dependencies_restart_and_absolute_path(manifest):
    manifest["components"][0]["cmd"] = ["python", 'a"b\\c %i $X ${X}; &']
    output = render(manifest)
    unit = output["homensai-rent-demo-web.service"]
    assert 'ExecStart="python" "a\\"b\\\\c %%i $$X $${X}; &"\n' in unit
    assert 'WorkingDirectory="/srv/demo app/demo-layout/app"\n' in unit
    for line in ("RestartSec=60", "StartLimitIntervalSec=600", "StartLimitBurst=3"):
        assert line in unit
    bot = output["homensai-rent-demo-bot.service"]
    assert "After=homensai-rent-demo-web.service\n" in bot
    assert "Requires=homensai-rent-demo-web.service\n" in bot
    assert list(output) == [f"homensai-rent-demo-{name}.service" for name in ("web", "bot", "scheduler")]


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_no_environment_secret_values(manifest, monkeypatch, platform):
    for secret in manifest["secrets"]:
        monkeypatch.setenv(secret["name"], "SYNTHETIC-SECRET-DO-NOT-EMIT")
    manifest["secrets"].append({"name": "ADMIN_ONLY", "required": True,
                                "contexts": ["admin"], "same_value": False})
    output = "\n".join(render(manifest, platform, winsw_payload=PAYLOAD).values())
    assert "SYNTHETIC-SECRET-DO-NOT-EMIT" not in output
    assert "ADMIN_ONLY" not in output
    assert "BOT_TOKEN" in output
    assert "127.0.0.1" in output and "8820" in output
    assert "0.0.0.0" not in output
    if platform == "windows":
        assert 'value="%BOT_TOKEN%"' in output
    else:
        assert "PassEnvironment=BACKUP_PASSWORD BOT_TOKEN ENCRYPTION_KEY" in output


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_rejects_public_bind_and_env_override(manifest, platform):
    manifest["web"]["host"] = "0.0.0.0"
    with pytest.raises(ManifestError):
        render(manifest, platform, winsw_payload=PAYLOAD)
    manifest["web"]["host"] = "127.0.0.1"
    manifest["secrets"][0]["name"] = "HOMENSAI_WEB_HOST"
    with pytest.raises(ManifestError):
        render(manifest, platform, winsw_payload=PAYLOAD)


@pytest.mark.parametrize("value", ["%USERPROFILE%", "\ud800", "\uffff"])
def test_rejects_windows_interpolation_or_invalid_xml(manifest, value):
    manifest["components"][0]["cmd"].append(value)
    with pytest.raises(ManifestError):
        render(manifest, "windows", winsw_payload=PAYLOAD)


def test_v1_cannot_carry_pin():
    value = load_manifest(Path(__file__).resolve().parents[1] / "examples/jobpilot.manifest.json")
    value["winsw"] = {"version": "2.12.0", "sha256": "a" * 64}
    with pytest.raises(ManifestError):
        validate_manifest(value)


@pytest.mark.parametrize("executable", ["-python", "+python", "@python", ":python", "!python", "|python", "bin/python"])
def test_rejects_systemd_executable_prefix_or_relative_path(manifest, executable):
    manifest["components"][0]["cmd"][0] = executable
    with pytest.raises(ManifestError):
        render(manifest)
