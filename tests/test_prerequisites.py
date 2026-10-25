"""Read-only probes, явные планы и настоящие ограниченные subprocess.

Docker/Windows responses здесь — fixtures, не установленная acceptance-среда.
Сеть запрещена общим conftest; проверка портов внедряется явно.
"""
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from homensai_install import prerequisites as module
from homensai_install.errors import InstallerError
from homensai_install.prerequisites import (
    DependencyRequirement, ProbeResult, SafeProbeRunner, apply_prerequisite_plan,
    check_requirements, collect_inventory, manual_instructions, plan_prerequisites,
)


class FakeRunner:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def run(self, argv, *, timeout):
        self.calls.append(list(argv))
        return self.responses.get(tuple(argv[1:]), ProbeResult(1, "private raw probe failure"))


@pytest.fixture
def absent_programs(monkeypatch):
    monkeypatch.setattr(module, "_program", lambda *_: None)


def inventory(status="present", version="27.3.1", **component_changes):
    component = {"status": status, "version": version, "scope": "host", "evidence": []}
    component.update(component_changes)
    value = {"platform": {"os": "linux"}, "components": {"docker_engine": component}}
    value["fingerprint"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def requirement(**changes):
    value = {"component_id": "runtime", "kind": "docker_engine", "minimum_version": "27.0.0"}
    value.update(changes)
    return value


def test_free_space_observation_does_not_make_every_plan_stale(tmp_path, absent_programs, monkeypatch):
    disk = {"path": str(tmp_path), "status": "available", "exists": True,
            "free_bytes": 1000000, "total_bytes": 2000000, "parent_writable": True}
    monkeypatch.setattr(module, "_disk", lambda _: dict(disk))
    first = collect_inventory(paths=[tmp_path])
    disk["free_bytes"] -= 4096  # The installer's own journal may allocate a block.
    second = collect_inventory(paths=[tmp_path])
    assert first["fingerprint"] == second["fingerprint"]
    assert first["disks"][0]["free_bytes"] != second["disks"][0]["free_bytes"]
    disk["parent_writable"] = False
    assert collect_inventory(paths=[tmp_path])["fingerprint"] != second["fingerprint"]


def test_inventory_is_read_only_and_does_not_export_environment_values(tmp_path, absent_programs):
    selected = tmp_path / "not-created" / "data"
    env = {
        "PATH": "/usr/bin:relative::/bin", "DATABASE_PASSWORD": "db-secret-fixture",
        "HTTPS_PROXY": "https://user:proxy-secret-fixture@proxy.example",
        "TEMP": "temp-secret-fixture", "HOME": "/home/test-person",
        "DOCKER_HOST": "ssh://remote-secret-fixture.example",
    }
    before = set(tmp_path.iterdir())
    result = collect_inventory(paths=[selected], ports=[8080], environ=env,
        port_probe=lambda value: {"port": value, "status": "in_use"})
    encoded = json.dumps(result)
    assert not selected.exists() and set(tmp_path.iterdir()) == before
    for secret in ("db-secret-fixture", "proxy-secret-fixture", "temp-secret-fixture", "test-person", "remote-secret-fixture"):
        assert secret not in encoded
    assert "DATABASE_PASSWORD" not in result["environment"]["process"]["variables"]
    path = result["environment"]["process"]["variables"]["PATH"]
    assert path["contains_relative"] and path["contains_empty"] and path["entry_count"] == 4
    assert not any(item["value_exposed"] for item in result["environment"]["process"]["variables"].values())
    assert result["ports"][0]["status"] == "in_use"
    assert result["disks"][0]["free_bytes"] > 0 and not result["disks"][0]["exists"]
    assert result["evidence_level"] == "simulated"
    assert not result["capabilities"]["automatic_package_install"]
    assert result["environment"]["service"]["status"] == "not_inspected"


def test_docker_probe_uses_local_endpoint_and_keeps_only_structured_version(tmp_path):
    docker = tmp_path / "docker"
    docker.touch()
    arguments = ("--host", "unix:///var/run/docker.sock", "version", "--format", "{{json .}}")
    runner = FakeRunner({arguments: ProbeResult(0, json.dumps({"Server": {"Version": "27.3.1", "Secret": "hidden-engine-secret"},
                                                                  "Client": {"Context": "private-context"}})),
                         ("compose", "version", "--short"): ProbeResult(0, "v2.32.0\n")})
    result = collect_inventory(program_paths={"docker": docker}, runner=runner,
        platform_info={"os": "linux", "arch": "x86_64"}, environ={"DOCKER_HOST": "ssh://remote.example"})
    assert result["components"]["docker_engine"]["status"] == "present"
    assert result["components"]["docker_engine"]["version"] == "27.3.1"
    assert result["components"]["docker_compose"]["version"] == "2.32.0"
    assert runner.calls[0][1:3] == ["--host", "unix:///var/run/docker.sock"]
    assert result["platform"]["arch"] == "x64"
    assert "hidden-engine-secret" not in json.dumps(result) and "private-context" not in json.dumps(result)


def test_unavailable_or_unrecognized_engine_is_manual_not_present(tmp_path):
    docker = tmp_path / "docker"
    docker.touch()
    result = collect_inventory(program_paths={"docker": docker}, runner=FakeRunner({}),
        platform_info={"os": "linux"}, environ={})
    assert result["components"]["docker_engine"]["status"] == "manual"
    assert result["components"]["docker_compose"]["status"] == "missing"
    assert "private raw probe failure" not in json.dumps(result)


def test_simulated_windows_wsl_reports_reboot_without_performing_it(tmp_path):
    wsl = tmp_path / "wsl.exe"
    wsl.touch()
    runner = FakeRunner({("--status",): ProbeResult(3010), ("--version",): ProbeResult(0, "WSL version: 2.5.1.0")})
    result = collect_inventory(program_paths={"wsl": wsl}, runner=runner,
        platform_info={"os": "windows", "arch": "amd64", "release": "fixture-only"}, environ={})
    assert result["components"]["wsl"]["status"] == "reboot_required"
    assert result["evidence_level"] == "simulated"
    assert not result["capabilities"]["automatic_reboot"]
    assert {tuple(call[1:]) for call in runner.calls} == {("--status",), ("--version",)}
    assert all("--install" not in call for call in runner.calls)


def test_controller_database_is_not_a_product_database(absent_programs):
    found = collect_inventory(environ={})
    checks = check_requirements([{"component_id": "database", "kind": "sqlite", "scope": "product"}], found)
    assert checks[0]["status"] == "unsupported"
    assert checks[0]["reason"] == "component_scope_does_not_prove_requirement"
    controller = check_requirements([{"component_id": "catalog", "kind": "sqlite", "scope": "controller"}], found)
    assert controller[0]["ready"]


def test_linux_inside_wsl_is_distinguished_from_windows_host(absent_programs):
    found = collect_inventory(platform_info={"os": "linux", "release": "6.6.87.2-microsoft-standard-WSL2"},
                              environ={"WSL_DISTRO_NAME": "private-distribution-name"})
    assert found["wsl_environment"]["marker_detected"]
    assert not found["wsl_environment"]["host_windows_access_proven"]
    assert found["components"]["wsl"]["status"] == "unsupported"
    assert "private-distribution-name" not in json.dumps(found)


@pytest.mark.parametrize("version,minimum,maximum,status", [
    ("9.10.0", "9.9.0", None, "present"), ("9.9.0", "9.10.0", None, "incompatible"),
    ("27.3.2", None, "27.3.1", "incompatible"), ("v27.3.1+vendor", "27.3", None, "present"),
    ("28.0.0-rc1", "27.0", None, "manual"), (None, "27.0", None, "manual"),
])
def test_version_checks_are_numeric_and_unknown_is_manual(version, minimum, maximum, status):
    checks = check_requirements([requirement(minimum_version=minimum, maximum_version=maximum)], inventory(version=version))
    assert checks[0]["status"] == status


@pytest.mark.parametrize("changes", [{"minimum_version": "latest"}, {"component_id": None},
    {"minimum_version": "28.0", "maximum_version": "27.0"}, {"required": "yes"}, {"scope": []}])
def test_malformed_requirements_fail_validation(changes):
    with pytest.raises(ValueError):
        DependencyRequirement.from_dict(requirement(**changes))


def test_duplicate_ids_and_unimplemented_detector_are_explicit():
    with pytest.raises(ValueError):
        check_requirements([requirement(), requirement()], inventory())
    result = check_requirements([requirement(kind="postgresql")], inventory())
    assert result[0]["status"] == "unsupported" and not result[0]["ready"]
    assert result[0]["reason"] == "detector_not_implemented"


def test_ready_and_missing_plans_need_explicit_review_consent():
    for value, expected in ((inventory(), "READY"), (inventory(status="missing", version=None), "AWAITING_USER")):
        plan = plan_prerequisites([requirement()], value)
        with pytest.raises(InstallerError) as caught:
            apply_prerequisite_plan(plan, inventory=value, consents=[])
        assert caught.value.code == "CONSENT_REQUIRED"
        result = apply_prerequisite_plan(plan, inventory=value, consents=plan["required_consents"])
        assert result["status"] == expected and not result["system_changes_performed"]
        assert result["ready"] == (expected == "READY")


def test_tampered_stale_or_automatic_install_plan_is_rejected():
    value = inventory(status="missing", version=None)
    plan = plan_prerequisites([requirement()], value)
    tampered = copy.deepcopy(plan)
    tampered["actions"][0]["strategy"] = "shell"
    with pytest.raises(InstallerError) as caught:
        apply_prerequisite_plan(tampered, inventory=value, consents=plan["required_consents"])
    assert caught.value.code == "INTEGRITY_FAILED"
    with pytest.raises(InstallerError) as caught:
        apply_prerequisite_plan(plan, inventory=inventory(), consents=plan["required_consents"])
    assert caught.value.code == "PLAN_STALE"
    with pytest.raises(InstallerError) as caught:
        apply_prerequisite_plan(plan, inventory=value, consents=plan["required_consents"], automatic=True)
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"


@pytest.mark.parametrize("detected,expected", [("reboot_required", "REBOOT_REQUIRED"), ("relogin_required", "RELOGIN_REQUIRED")])
def test_reboot_and_relogin_are_states_not_automatic_actions(detected, expected):
    value = inventory(status=detected)
    plan = plan_prerequisites([requirement()], value)
    result = apply_prerequisite_plan(plan, inventory=value, consents=plan["required_consents"])
    assert result["status"] == expected and not result["ready"] and not result["system_changes_performed"]


def test_optional_missing_component_does_not_block_selected_required_components():
    value = inventory(status="missing")
    plan = plan_prerequisites([requirement(required=False)], value)
    result = apply_prerequisite_plan(plan, inventory=value, consents=plan["required_consents"])
    assert result["ready"] and result["checks"][0]["status"] == "missing"


@pytest.mark.parametrize("locale", ["ru", "en", "de"])
def test_manual_guidance_uses_primary_docs_without_executable_commands(locale):
    result = manual_instructions("docker_engine", locale, "windows")
    assert result["locale"] == locale and len(result["steps"]) == 4
    assert result["official_url"] == "https://docs.docker.com/desktop/setup/install/windows-install/"
    assert result["contains_executable_commands"] is False


def test_real_probe_uses_exact_argv_and_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("HINSTALL_TEST_SECRET", "not-to-inherit")
    monkeypatch.setenv("PYTHONPATH", "/not/to/inherit")
    code = "import os,json;print(json.dumps({key:os.environ.get(key) for key in ('HINSTALL_TEST_SECRET','PYTHONPATH','LD_PRELOAD')}))"
    executable = str(Path(sys.executable).resolve())
    runner = SafeProbeRunner({executable: [("-c", code)]})
    result = runner.run([executable, "-c", code], timeout=3)
    assert result.returncode == 0 and json.loads(result.stdout) == {"HINSTALL_TEST_SECRET": None, "PYTHONPATH": None, "LD_PRELOAD": None}
    with pytest.raises(InstallerError) as caught:
        runner.run([executable, "-c", "print('unlisted')"], timeout=3)
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"


def test_real_probe_output_and_runtime_are_bounded():
    executable = str(Path(sys.executable).resolve())
    flood, hang = "import sys;sys.stdout.write('x'*1000000)", "import time;time.sleep(10)"
    runner = SafeProbeRunner({executable: [("-c", flood), ("-c", hang)]}, max_output_bytes=1024)
    flooded = runner.run([executable, "-c", flood], timeout=2)
    assert flooded.output_limited and len(flooded.stdout.encode()) <= 1024
    started = time.monotonic()
    timed = runner.run([executable, "-c", hang], timeout=.1)
    assert timed.timed_out and time.monotonic() - started < 2


def test_symlink_disk_path_does_not_establish_safe_install_location(tmp_path, absent_programs):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    result = collect_inventory(paths=[alias / "future"], environ={})
    assert result["disks"][0]["status"] == "unsafe"
    assert not (actual / "future").exists()


def test_invalid_port_is_rejected_before_probe(absent_programs):
    called = []
    with pytest.raises(ValueError):
        collect_inventory(ports=[0], port_probe=lambda port: called.append(port))
    assert not called
