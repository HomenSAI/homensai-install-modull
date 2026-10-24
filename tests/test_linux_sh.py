"""Только проверка текстов; shell/systemd/useradd никогда не запускаются."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from shared import ctl
from shared.linux_sh import render_linux
from shared.manifest import ManifestError, load_manifest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rent.manifest.json"
GOLDEN = Path(__file__).parent / "golden" / "linux"
SENTINEL = "synthetic-secret-never-rendered-8b"


@pytest.fixture
def manifest():
    return load_manifest(EXAMPLE)


@pytest.fixture(autouse=True)
def forbid_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Генератор не должен запускать процессы")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


def render(manifest, **kwargs):
    return render_linux(manifest, modes=("platform",), install_root="/srv/demo", **kwargs)


def test_golden_all_texts(manifest, tmp_path):
    result = render(manifest)
    assert set(result) == {path.name for path in GOLDEN.iterdir()}
    for name, content in result.items():
        assert content == (GOLDEN / name).read_text(encoding="utf-8")
        assert content.endswith("\n") and "\r" not in content
        destination = tmp_path / name
        destination.write_text(content, encoding="utf-8", newline="\n")
        assert destination.read_bytes() == content.encode("utf-8")


def test_render_deterministic_no_mutation_or_io(manifest, monkeypatch):
    before = deepcopy(manifest)
    expected = render(manifest)
    def forbidden(*args, **kwargs):
        raise AssertionError("Генератор читает только уже загруженный публичный контракт")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    assert render(manifest) == expected and manifest == before


def test_service_order_and_uninstall_retention(manifest):
    result = render_linux(manifest, modes=("autonomous",), install_root="/srv/demo")
    assert len(result) == 9
    script = result["install.sh"]
    assert script.index('"$bundle_dir"/homensai-rent-demo-web.service ') < script.index(
        '"$bundle_dir"/homensai-rent-demo-bot.service ') < script.index(
        '"$bundle_dir"/homensai-rent-demo-scheduler.service ')
    uninstall = result["uninstall.sh"]
    assert uninstall.index("stop -- homensai-rent-demo-scheduler") < uninstall.index(
        "stop -- homensai-rent-demo-bot") < uninstall.index("stop -- homensai-rent-demo-web")
    assert "rm -r" not in uninstall and "userdel" not in uninstall
    assert "/srv/demo" not in uninstall and "service.env" not in uninstall
    assert "After=homensai-rent-demo-web.service" in result["homensai-rent-demo-bot.service"]


def test_env_protection_and_no_secret_values(manifest, monkeypatch):
    for item in manifest["secrets"]:
        monkeypatch.setenv(item["name"], SENTINEL)
    result = render(manifest)
    text = "".join(result.values())
    assert SENTINEL not in text and "PassEnvironment=" not in text
    assert "--shell /usr/sbin/nologin" in text and "--no-create-home" in text
    assert "User=homensai\nGroup=homensai\nUMask=0077" in text
    assert 'EnvironmentFile="/etc/homensai-rent-demo/service.env"' in text
    script = result["install.sh"]
    assert "install -d -m 0700 -o root -g root -- /etc/homensai-rent-demo" in script
    assert "install -m 0600 -o root -g root -- /dev/null" in script
    assert "chmod 0600 -- /etc/homensai-rent-demo/service.env" in script
    for name in ("BOT_TOKEN", "BACKUP_PASSWORD", "ENCRYPTION_KEY"):
        assert "'# " + name + "'" in script
        assert name + "=" not in text
    assert "systemctl" not in script and " eval " not in text
    assert "--property=Id,LoadState,ActiveState,SubState" in result["status.sh"]
    assert "Environment" not in result["status.sh"] and "journalctl" not in result["status.sh"]


def test_admin_only_secret_name_excluded(manifest):
    manifest["secrets"].append({"name": "ADMIN_ONLY", "required": True,
                                "contexts": ["admin"], "same_value": False})
    assert "ADMIN_ONLY" not in "".join(render(manifest).values())


@pytest.mark.parametrize("root", ["/srv/кириллица и пробелы", "/srv/a'b\"c;$(id)`whoami`%n$HOME",
                                   "/srv/a&b|c>d<e*(f)?[g]#h"])
def test_hostile_root_shell_round_trip(manifest, root):
    result = render_linux(manifest, modes=("platform",), install_root=root)
    script = result["install.sh"]
    directory_lines = [line for line in script.splitlines() if line.startswith("install -d ")]
    directories = [shlex.split(line)[-1] for line in directory_lines]
    assert root + "/demo-layout/app" in directories
    for line in directory_lines:
        words = shlex.split(line)
        assert words[:2] == ["install", "-d"] and len(words) == 10 and words[-2] == "--"
    unit = result["homensai-rent-demo-core-server.service"]
    working = '"' + (root + "/demo-layout/app").replace('"', '\\"').replace("%", "%%") + '"'
    assert "WorkingDirectory=" + working in unit


def test_hostile_environment_file_two_quoting_contexts(manifest):
    env = "/etc/a'b\";$(id)`id`%n$HOME/private.env"
    result = render(manifest, environment_file=env)
    chmod = next(line for line in result["install.sh"].splitlines() if line.startswith("chmod "))
    assert shlex.split(chmod) == ["chmod", "0600", "--", env]
    override = result["homensai-rent-demo-core-server.service.override.conf"]
    assert 'EnvironmentFile="/etc/a\'b\\";$(id)`id`%%n$HOME/private.env"' in override


def test_hostile_argv_is_systemd_literal(manifest):
    argv = ["/usr/bin/python3", 'a"b\\c $HOME ${TOKEN} %n ; `id` $(id)', "кириллица и пробелы"]
    manifest["components"][-1]["cmd"] = argv
    text = render(manifest)["homensai-rent-demo-core-server.service"]
    assert 'ExecStart="/usr/bin/python3" "a\\"b\\\\c $$HOME $${TOKEN} %%n ; `id` $$(id)" "кириллица и пробелы"' in text
    assert "sh -c" not in text


@pytest.mark.parametrize("user", ["root", "0", "-x", "a b", "a;id", "$(id)", "a\nUser=root", "a%u", "a'", "a" * 32])
def test_hostile_account_rejected(manifest, user):
    with pytest.raises(ManifestError):
        render(manifest, user=user)


@pytest.mark.parametrize("root", ["/", "relative", "//host/share", "/a/../b", "/a/./b", "/a\nb",
    "/a\x00b", "/a\rb", "/a\tb", "C:/app", "/a\\b", "/a\u2028b", "/a\u0085b", "/a\ud800b"])
def test_unsafe_root_rejected(manifest, root):
    with pytest.raises(ManifestError):
        render_linux(manifest, modes=("platform",), install_root=root)


@pytest.mark.parametrize("env", ["/service.env", "/etc/service.env", "/etc/systemd/system/evil.env",
    "/srv/demo/demo-layout/app/private/env", "/srv/demo/demo-layout/releases/private/env",
    "/srv/demo/demo-layout/data/env", "/srv/demo/demo-layout/config/env",
    "/srv/demo/demo-layout/app/../env", "/etc/a\nb/env", "relative/env"])
def test_unsafe_env_location_rejected(manifest, env):
    with pytest.raises(ManifestError):
        render(manifest, environment_file=env)


@pytest.mark.parametrize("cmd", [["-python"], ["+python"], ["|python"], ["bin/python"],
    ["$PYTHON"], ["/bin/$PYTHON"], ["python", "a\nb"], ["python", "a\u2028b"]])
def test_unsafe_command_rejected(manifest, cmd):
    manifest["components"][-1]["cmd"] = cmd
    with pytest.raises(ManifestError):
        render(manifest)


@pytest.mark.parametrize("modes", [(), ("unknown",), ("platform", "platform"), ("platform", "autonomous")])
def test_bad_modes_rejected(manifest, modes):
    with pytest.raises(ManifestError):
        render_linux(manifest, modes=modes, install_root="/srv/demo")


def test_v1_rejected():
    with pytest.raises(ManifestError):
        render(load_manifest(EXAMPLE.with_name("jobpilot.manifest.json")))


@pytest.mark.parametrize("mutation", ["overlap", "absolute-overlap", "wrong-os", "secret-value", "env-collision"])
def test_bad_manifest_rejected(manifest, mutation):
    if mutation == "overlap":
        manifest["paths"]["backup"] = manifest["paths"]["data"] + "/backup"
    elif mutation == "absolute-overlap":
        manifest["paths"] = {key: "/srv/" + key for key in manifest["paths"]}
        manifest["paths"]["data"] = "/etc/systemd/system/data"
    elif mutation == "wrong-os":
        manifest["paths"] = {key: "C:/Demo/" + key for key in manifest["paths"]}
    elif mutation == "secret-value":
        manifest["secrets"][0]["value"] = SENTINEL
    else:
        manifest["secrets"][0]["name"] = "HOMENSAI_WEB_HOST"
    with pytest.raises(ManifestError) as error:
        render(manifest)
    assert SENTINEL not in str(error.value)


def test_absolute_paths(manifest):
    manifest["paths"] = {key: "/srv/" + key for key in manifest["paths"]}
    assert 'WorkingDirectory="/srv/app"' in render(manifest)["homensai-rent-demo-core-server.service"]


def test_cli_bundle_no_execution_and_no_overwrite(tmp_path, capsys):
    output = tmp_path / "linux"
    args = ["--manifest", str(EXAMPLE), "render-linux", "--install-root", "/srv/demo",
            "--mode", "platform", "--output-dir", str(output), "--json"]
    assert ctl.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    assert len(result["data"]["files"]) == 5
    assert before == {name: text.encode("utf-8") for name, text in render(load_manifest(EXAMPLE)).items()}
    assert ctl.main(args) == 2
    capsys.readouterr()
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}


def test_cli_invalid_target_does_not_write(tmp_path, capsys):
    output = tmp_path / "linux"
    assert ctl.main(["--manifest", str(EXAMPLE), "render-linux", "--install-root", "/a\nb",
                     "--mode", "platform", "--output-dir", str(output), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not output.exists()
