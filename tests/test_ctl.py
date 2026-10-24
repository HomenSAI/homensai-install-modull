"""CLI через main(argv); только tmp_path и вымышленные данные, без исполнения."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from shared import ctl
from shared.datahome import resolve_datahome
from shared.manifest import load_manifest
from shared.update_plan import UpdatePlan, UpdatePorts


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SECRET = "SYNTHETIC-CLI-SECRET-DO-NOT-EMIT"
PAYLOAD = b"synthetic-not-a-real-winsw-executable"


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("CLI не должен запускать процессы")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


def call(capsys, manifest, *arguments, code=0):
    result = ctl.main(["--manifest", str(manifest), *map(str, arguments), "--json"])
    captured = capsys.readouterr()
    assert result == code, captured
    assert captured.err == ""
    value = json.loads(captured.out)
    assert value["code"] == code and value["ok"] == (code == 0)
    assert SECRET not in captured.out
    return value["data"]


@pytest.fixture
def rent(tmp_path):
    manifest = load_manifest(EXAMPLES / "rent.manifest.json")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def v1_paths(root):
    return ["--install-root", root, "--data-home", root / "data",
            "--app-dir", root / "app", "--releases-dir", root / "releases"]


@pytest.mark.parametrize("example", ["jobpilot", "rent"])
def test_validate_examples(capsys, example):
    data = call(capsys, EXAMPLES / f"{example}.manifest.json", "validate")
    assert data["schema_version"] == (1 if example == "jobpilot" else 2)


def test_human_output_and_json_before_command(capsys):
    manifest = str(EXAMPLES / "jobpilot.manifest.json")
    assert ctl.main(["--manifest", manifest, "validate"]) == 0
    assert "Манифест корректен" in capsys.readouterr().out
    assert ctl.main(["--json", "--manifest", manifest, "validate"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]


@pytest.mark.parametrize("arguments", [[], [SECRET], ["--manifest", SECRET],
    ["--manifest", "missing", "validate", "--password", SECRET],
    ["--manifest", "missing", "plan-install", "--platform", SECRET]])
def test_usage_errors_are_json_and_never_echo_argv(capsys, arguments):
    assert ctl.main([*arguments, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == "" and SECRET not in captured.out
    assert json.loads(captured.out)["code"] == 2


def test_help_returns_code(capsys):
    assert ctl.main(["--help"]) == 0
    assert "archive-restore" in capsys.readouterr().out


def test_human_error_goes_to_stderr(capsys):
    assert ctl.main(["--manifest", SECRET, "unknown"]) == 2
    captured = capsys.readouterr()
    assert not captured.out and SECRET not in captured.err
    assert "Неверные аргументы" in captured.err


@pytest.mark.parametrize("body", ['{"password":"' + SECRET + '"}',
    '{"schema_version":NaN}', '{"schema_version":1,"schema_version":1}', '{', '[]'])
def test_invalid_manifest_without_values(tmp_path, capsys, body):
    manifest = tmp_path / "invalid.json"
    manifest.write_text(body, encoding="utf-8")
    call(capsys, manifest, "validate", code=2)


def test_missing_file_is_operational_error(tmp_path, capsys):
    call(capsys, tmp_path / "missing.json", "validate", code=1)


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_rent_plan_only_lists_selected_files(tmp_path, capsys, platform):
    root = tmp_path / "not-created"
    before = list(tmp_path.iterdir())
    data = call(capsys, EXAMPLES / "rent.manifest.json", "plan-install",
                "--install-root", root, "--platform", platform, "--mode", "autonomous")
    assert data["dry_run"] is True
    assert len(data["directories"]) == 7 and len(data["files"]) == 3
    suffix = ".xml" if platform == "windows" else ".service"
    assert [Path(path).name for path in data["files"]] == [
        "homensai-rent-demo-" + name + suffix for name in ("web", "bot", "scheduler")]
    assert list(tmp_path.iterdir()) == before and not root.exists()


def test_jobpilot_plan_requires_explicit_adapter_paths(tmp_path, capsys, monkeypatch):
    root = tmp_path / "installation"
    monkeypatch.setenv("JOBPILOT_DATA_HOME", SECRET)
    call(capsys, EXAMPLES / "jobpilot.manifest.json", "plan-install", "--platform", "docker",
         "--install-root", root, code=2)
    data = call(capsys, EXAMPLES / "jobpilot.manifest.json", "plan-install", "--platform", "docker",
                *v1_paths(root))
    assert len(data["directories"]) == 3
    assert {Path(path).name for path in data["files"]} == {"Dockerfile", "compose.yml"}
    assert not root.exists()


def test_human_plan_is_russian_text_without_writes(tmp_path, capsys):
    assert ctl.main(["--manifest", str(EXAMPLES / "rent.manifest.json"), "plan-install",
                     "--install-root", str(tmp_path), "--platform", "linux", "--mode", "platform"]) == 0
    output = capsys.readouterr().out
    assert "Каталоги:" in output and "Файлы:" in output
    assert "homensai-rent-demo-core-server.service" in output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("modes", [[], ["unknown"], ["autonomous", "platform"],
                                  ["platform", "platform"]])
def test_plan_invalid_modes_does_not_write(tmp_path, capsys, modes):
    arguments = [part for mode in modes for part in ("--mode", mode)]
    call(capsys, EXAMPLES / "rent.manifest.json", "plan-install", "--install-root", tmp_path,
         "--platform", "linux", *arguments, code=2)
    assert not list(tmp_path.iterdir())


def test_v1_host_services_and_v2_docker_fail(tmp_path, capsys):
    call(capsys, EXAMPLES / "jobpilot.manifest.json", "plan-install", "--platform", "linux",
         *v1_paths(tmp_path), code=2)
    output = tmp_path / "output"
    call(capsys, EXAMPLES / "rent.manifest.json", "render-docker", "--output-dir", output, code=2)
    assert not output.exists()


def test_v2_rejects_ignored_legacy_paths(tmp_path, capsys):
    call(capsys, EXAMPLES / "rent.manifest.json", "plan-install", "--install-root", tmp_path,
         "--data-home", SECRET, "--platform", "linux", "--mode", "platform", code=2)


def test_render_docker_and_no_overwrite(tmp_path, capsys):
    output = tmp_path / "docker"
    data = call(capsys, EXAMPLES / "jobpilot.manifest.json", "render-docker", "--output-dir", output)
    assert len(data["files"]) == 2
    assert "USER 10001:10001" in (output / "Dockerfile").read_text(encoding="utf-8")
    assert "127.0.0.1:8765:8765" in (output / "compose.yml").read_text(encoding="utf-8")
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    call(capsys, EXAMPLES / "jobpilot.manifest.json", "render-docker", "--output-dir", output, code=2)
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_render_services_pin_and_secret_names(tmp_path, capsys, rent, monkeypatch, platform):
    path, manifest = rent
    manifest["winsw"] = {"version": "2.12.0", "sha256": hashlib.sha256(PAYLOAD).hexdigest()}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    binary = tmp_path / "synthetic.bin"
    binary.write_bytes(PAYLOAD)
    for name in ("BOT_TOKEN", "BACKUP_PASSWORD", "ENCRYPTION_KEY"):
        monkeypatch.setenv(name, SECRET)
    output = tmp_path / "units"
    args = ["--winsw-file", binary] if platform == "windows" else []
    target = str(tmp_path / "install") if (platform == "windows") == (os.name == "nt") else (
        "C:\\Demo" if platform == "windows" else "/srv/demo")
    data = call(capsys, path, "render-services", "--install-root", target,
                "--platform", platform, "--mode", "autonomous", "--output-dir", output, *args)
    assert len(data["files"]) == 3
    text = "".join(p.read_text(encoding="utf-8") for p in output.iterdir())
    assert SECRET not in text and "BOT_TOKEN" in text and "127.0.0.1" in text
    call(capsys, path, "render-services", "--install-root", target, "--platform", platform,
         "--mode", "autonomous", "--output-dir", output, *args, code=2)


@pytest.mark.parametrize("mutation", ["missing-pin", "missing-file", "mismatch"])
def test_windows_render_requires_verified_pin(tmp_path, capsys, rent, mutation):
    path, manifest = rent
    binary = tmp_path / "synthetic.bin"
    binary.write_bytes(PAYLOAD)
    if mutation != "missing-pin":
        manifest["winsw"] = {"version": "2.12.0", "sha256": "a" * 64}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    args = [] if mutation == "missing-file" else ["--winsw-file", binary]
    output = tmp_path / "output"
    target = str(tmp_path / "install") if os.name == "nt" else "C:\\Demo"
    call(capsys, path, "render-services", "--platform", "windows", "--install-root", target,
         "--mode", "platform", "--output-dir", output, *args, code=2)
    assert not output.exists()


@pytest.mark.parametrize("example", ["jobpilot", "rent"])
def test_archive_roundtrip_and_wrong_product(tmp_path, capsys, example):
    path = EXAMPLES / f"{example}.manifest.json"
    manifest = load_manifest(path)
    root = tmp_path / "install"
    if example == "jobpilot":
        home = resolve_datahome(manifest, root, environ={manifest["data_home_env"]: str(root / "data")},
                                app_dir=root / "app", releases_dir=root / "releases")
        args = v1_paths(root)
    else:
        home = resolve_datahome(manifest, root)
        args = ["--install-root", root]
    for directory in home.roots.values():
        directory.mkdir(parents=True)
        (directory / "empty").mkdir()
        (directory / "synthetic.txt").write_text(SECRET, encoding="utf-8")
    archive = tmp_path / "snapshot.zip"
    created = call(capsys, path, "archive-create", *args, "--archive", archive)
    assert created["encrypted"] is False and created["file_count"] == len(home.roots)
    verified = call(capsys, path, "archive-verify", "--archive", archive)
    assert verified == created
    other = EXAMPLES / ("rent.manifest.json" if example == "jobpilot" else "jobpilot.manifest.json")
    call(capsys, other, "archive-verify", "--archive", archive, code=1)
    target = tmp_path / "recovery"
    target.mkdir()
    restored = call(capsys, path, "archive-restore", *args, "--archive", archive, "--target", target)
    assert restored == created
    for name in home.roots:
        assert (target / "roots" / name / "synthetic.txt").read_text(encoding="utf-8") == SECRET
        assert (target / "roots" / name / "empty").is_dir()
    call(capsys, path, "archive-restore", *args, "--archive", archive, "--target", target, code=1)
    before = archive.read_bytes()
    call(capsys, path, "archive-create", *args, "--archive", archive, code=1)
    assert archive.read_bytes() == before


def test_archive_refuses_missing_roots_and_corrupt_zip(tmp_path, capsys):
    path = EXAMPLES / "rent.manifest.json"
    archive = tmp_path / "corrupt.zip"
    call(capsys, path, "archive-create", "--install-root", tmp_path,
         "--archive", archive, code=1)
    assert not archive.exists()
    archive.write_bytes(SECRET.encode())
    call(capsys, path, "archive-verify", "--archive", archive, code=1)
    target = tmp_path / "target"
    target.mkdir()
    call(capsys, path, "archive-restore", "--install-root", tmp_path / "install",
         "--archive", archive, "--target", target, code=1)
    assert not list(target.iterdir())


@pytest.mark.parametrize("location", ["app", "releases", "data", "backup", "services", "config", "runtime", "parent"])
def test_restore_refuses_live_tree_even_when_empty(tmp_path, capsys, location):
    manifest = EXAMPLES / "rent.manifest.json"
    home = resolve_datahome(load_manifest(manifest), tmp_path / "install")
    locations = {"app": home.app, "releases": home.releases, **home.roots,
                 "parent": tmp_path / "install"}
    target = locations[location]
    target.mkdir(parents=True)
    archive = tmp_path / "not-read.zip"
    archive.write_bytes(b"invalid but must not be read")
    call(capsys, manifest, "archive-restore", "--install-root", tmp_path / "install",
         "--archive", archive, "--target", target, code=2)
    assert not list(target.iterdir())


@pytest.mark.parametrize("unsafe", ["../escape", "folder/../escape", "bad. /file", "bad./file",
                                    "//server/share/file", "C:relative", "bad:stream", "bad\nname",
                                    "NUL", "CON.json", "folder/COM1.txt", "bad?name", "bad*name"])
def test_unsafe_manifest_paths_never_reach_loader(tmp_path, capsys, monkeypatch, unsafe):
    monkeypatch.chdir(tmp_path)
    def forbidden(*args):
        raise AssertionError("Небезопасный путь не должен читаться")
    monkeypatch.setattr(ctl, "load_manifest", forbidden)
    call(capsys, unsafe, "validate", code=2)


@pytest.mark.parametrize("command", ["render-docker", "archive-create", "archive-verify", "archive-restore"])
def test_unsafe_output_or_archive_paths(tmp_path, capsys, command):
    args = ["--output-dir", str(tmp_path) + "/../escape"] if command == "render-docker" else [
        "--archive", str(tmp_path) + "/../escape.zip"]
    if command in ("archive-create", "archive-restore"):
        args.extend(v1_paths(tmp_path / "install"))
    if command == "archive-restore":
        args.extend(["--target", tmp_path / "recovery"])
    call(capsys, EXAMPLES / "jobpilot.manifest.json", command, *args, code=2)
    assert not list(tmp_path.iterdir())


def make_plan(manifest, root):
    return UpdatePlan(manifest, root, ctl.UnimplementedUpdatePorts(), release="new",
                      modes=("autonomous",))


def checkpoint(plan):
    return {"version": 1, "transaction": "a" * 32, "plan": plan.digest,
            "old_release": "old", "release": "new", "direction": "forward", "index": 0,
            "failed_phase": None, "archive_ready": False, "outcome": "running"}


def status(capsys, path, root, code=0, extra=()):
    return call(capsys, path, "update-plan-status", "--install-root", root,
                "--release", "new", "--mode", "autonomous", *extra, code=code)


def test_status_absent_has_no_side_effects(tmp_path, capsys, rent, monkeypatch):
    path, manifest = rent
    root = tmp_path / "not-created"
    def forbidden(*args):
        raise AssertionError("Чтение статуса не запускает транзакцию")
    monkeypatch.setattr(UpdatePlan, "run", forbidden)
    assert status(capsys, path, root) == {"state": None}
    assert not root.exists()


@pytest.mark.parametrize("direction,index,archive_ready,outcome,failed", [
    ("forward", 0, False, "running", None), ("forward", 2, True, "running", None),
    ("forward", 9, True, "updated", None),
    ("rollback", 2, True, "running", "health"),
    ("rollback", 6, True, "rolled_back", "health")])
def test_status_reads_valid_checkpoint_without_writes(tmp_path, capsys, rent,
                                                     direction, index, archive_ready, outcome, failed):
    path, manifest = rent
    plan = make_plan(manifest, tmp_path)
    state = checkpoint(plan)
    state.update(direction=direction, index=index, archive_ready=archive_ready, outcome=outcome,
                 failed_phase=failed)
    plan.state_path.parent.mkdir(parents=True)
    plan.state_path.write_text(json.dumps(state), encoding="utf-8")
    before = plan.state_path.read_bytes()
    assert status(capsys, path, tmp_path) == {"state": state}
    assert plan.state_path.read_bytes() == before and not plan.lock_path.exists()


@pytest.mark.parametrize("mutation", ["invalid-json", "extra-secret", "duplicate", "plan", "index", "invariant"])
def test_status_rejects_corruption_without_echoing(tmp_path, capsys, rent, mutation):
    path, manifest = rent
    plan = make_plan(manifest, tmp_path)
    state = checkpoint(plan)
    if mutation == "extra-secret":
        state["password"] = SECRET
    elif mutation == "plan":
        state["plan"] = SECRET
    elif mutation == "index":
        state["index"] = 100
    elif mutation == "invariant":
        state["archive_ready"] = True
    body = json.dumps(state)
    if mutation == "invalid-json":
        body = SECRET
    elif mutation == "duplicate":
        body = body[:-1] + ',"outcome":"' + SECRET + '"}'
    plan.state_path.parent.mkdir(parents=True)
    plan.state_path.write_text(body, encoding="utf-8")
    status(capsys, path, tmp_path, code=1)
    assert plan.state_path.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("extra,ids", [(["--no-migrations"], ()),
    (["--migration-id", "one", "--migration-id", "two"], ("one", "two"))])
def test_status_uses_original_migration_order(tmp_path, capsys, rent, extra, ids):
    path, manifest = rent
    plan = UpdatePlan(manifest, tmp_path, ctl.UnimplementedUpdatePorts(), release="new",
                      modes=("autonomous",), migration_ids=ids)
    state = checkpoint(plan)
    plan.state_path.parent.mkdir(parents=True)
    plan.state_path.write_text(json.dumps(state), encoding="utf-8")
    assert status(capsys, path, tmp_path, extra=extra)["state"] == state
    status(capsys, path, tmp_path, code=1)


@pytest.mark.parametrize("operation", [name for name in UpdatePorts.__dict__ if not name.startswith("_")])
def test_all_update_ports_explicitly_unimplemented(operation):
    with pytest.raises(ctl.UnimplementedPortError, match="не реализованы") as error:
        getattr(ctl.UnimplementedUpdatePorts(), operation)(SECRET)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("operation", ["build", "start", "stop"])
def test_all_docker_ports_explicitly_unimplemented(operation):
    with pytest.raises(ctl.UnimplementedPortError, match="не реализован") as error:
        getattr(ctl.UnimplementedDockerPorts(), operation)(SECRET)
    assert SECRET not in str(error.value)


def test_io_exception_does_not_leak(tmp_path, capsys, monkeypatch):
    def failed(*args, **kwargs):
        raise PermissionError(SECRET)
    monkeypatch.setattr(ctl, "_write_rendered", failed)
    call(capsys, EXAMPLES / "jobpilot.manifest.json", "render-docker",
         "--output-dir", tmp_path / "output", code=1)


def test_reparse_output_is_rejected_without_privileges(tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace
    output = tmp_path / "output"
    output.mkdir()
    original = Path.lstat
    def simulated(self, *args, **kwargs):
        info = original(self, *args, **kwargs)
        if self == output:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", simulated)
    call(capsys, EXAMPLES / "jobpilot.manifest.json", "render-docker",
         "--output-dir", output, code=2)
    assert not list(output.iterdir())


def test_render_preflights_all_outputs_before_first_write(tmp_path, capsys):
    output = tmp_path / "docker"
    output.mkdir()
    existing = output / "compose.yml"
    existing.write_bytes(b"keep this result")
    call(capsys, EXAMPLES / "jobpilot.manifest.json", "render-docker",
         "--output-dir", output, code=2)
    assert existing.read_bytes() == b"keep this result"
    assert not (output / "Dockerfile").exists()


def test_relative_manifest_and_output_paths(tmp_path, capsys, monkeypatch):
    manifest = load_manifest(EXAMPLES / "jobpilot.manifest.json")
    (tmp_path / "public.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    call(capsys, "public.json", "render-docker", "--output-dir", "output")
    assert (tmp_path / "output" / "Dockerfile").is_file()


def test_install_root_must_be_absolute(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    call(capsys, EXAMPLES / "rent.manifest.json", "plan-install", "--install-root", "relative",
         "--platform", "linux", "--mode", "platform", code=2)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("root", ["//server/share", "relative", "/safe/../escape", "/bad./app"])
def test_service_target_root_is_safe_even_for_foreign_os(tmp_path, capsys, root):
    output = tmp_path / "output"
    call(capsys, EXAMPLES / "rent.manifest.json", "render-services", "--install-root", root,
         "--platform", "linux", "--mode", "platform", "--output-dir", output, code=2)
    assert not output.exists()


def test_legacy_status_rejected_without_creating_state(tmp_path, capsys):
    status(capsys, EXAMPLES / "jobpilot.manifest.json", tmp_path, code=2)
    assert not list(tmp_path.iterdir())


def test_symlink_manifest_is_rejected(tmp_path, capsys):
    link = tmp_path / "manifest-link.json"
    try:
        link.symlink_to(EXAMPLES / "jobpilot.manifest.json")
    except (OSError, NotImplementedError):
        pytest.skip("ОС не разрешает создание symlink без дополнительных привилегий")
    call(capsys, link, "validate", code=2)
