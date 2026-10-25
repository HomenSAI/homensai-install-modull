"""Единый facade выбирает backend, сохраняет выбор и не подменяет Docker native.

Fake engines доказывают routing и journal contracts. Настоящий Linux lifecycle
через этот router проверяется отдельно в integration_tests.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.backends import BackendRouter, configured_backends
from homensai_install.engine import EngineError, NativeEngine
from homensai_install.errors import InstallerError


ROOT = Path(__file__).resolve().parents[1]
NATIVE = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
DOCKER = {**NATIVE, "mode": "docker"}


class RecordingEngine:
    def __init__(self, name, *, status="SUCCEEDED"):
        self.name, self.final = name, status
        self.calls = []
        self.inventory = {"fingerprint_data": {"resource": name, "revision": 1}}

    def capabilities(self, target):
        self.calls.append(("capabilities", deepcopy(target)))
        return {"actions": {"install": {"supported": True}}, "production_ready": False}

    def discover(self, manifest, target, options):
        self.calls.append(("discover", deepcopy(target)))
        return deepcopy(self.inventory)

    def plan(self, action, manifest, target, inventory, options):
        self.calls.append(("plan", deepcopy(target)))
        return {"steps": [{"id": self.name}], "required_consents": []}

    def execute(self, plan, emit, canceled):
        self.calls.append(("execute", deepcopy(plan["target"])))
        emit({"type": "step.completed", "step_id": self.name, "status": "SUCCEEDED"})
        return {"status": self.final, "backend": self.name}

    def resume(self, plan, previous, emit, canceled):
        self.calls.append(("resume", deepcopy(plan["target"])))
        return {"status": "SUCCEEDED", "backend": self.name}


def manifest():
    return json.loads((ROOT / "examples/rent.manifest.json").read_text())


def submit(api, plan):
    return api.submit(plan["plan_id"], plan["plan_digest"],
                      [plan["review_consent_ref"], *plan["required_consents"]],
                      idempotency_key="reviewed-operation", background=False)


@pytest.mark.parametrize("target", [NATIVE, DOCKER])
def test_facade_dispatches_review_execute_and_restart_to_same_backend(tmp_path, target):
    native, docker = RecordingEngine("native"), RecordingEngine("docker")
    api = InstallerAPI(tmp_path / "state", engine=BackendRouter(native=native, docker=docker))
    selected, other = (native, docker) if target["mode"] == "native" else (docker, native)
    before = selected.discover({}, target, {})
    assert api._engine().discover({}, target, {}) == before  # fingerprint shape stays exact
    plan = api.plan("install", manifest(), target, options={"install_root": str(tmp_path / "app")})
    assert plan["engine_plan"]["backend_routing"]["target"] == target
    operation = submit(api, plan)
    assert operation["status"] == "SUCCEEDED"
    assert operation["result"]["backend"] == selected.name
    restarted = InstallerAPI(api.state_dir, engine=BackendRouter(native=native, docker=docker))
    assert submit(restarted, plan)["operation_id"] == operation["operation_id"]
    assert [name for name, _ in selected.calls].count("execute") == 1
    assert other.calls == []


def test_paused_docker_operation_resumes_only_with_explicit_docker_backend(tmp_path):
    native, docker = RecordingEngine("native"), RecordingEngine("docker", status="RECOVERY_REQUIRED")
    api = InstallerAPI(tmp_path / "state", engine=BackendRouter(native=native, docker=docker))
    plan = api.plan("install", manifest(), DOCKER, options={"install_root": str(tmp_path / "app")})
    operation = submit(api, plan)
    assert operation["status"] == "RECOVERY_REQUIRED"
    missing = InstallerAPI(api.state_dir, engine=BackendRouter(native=native))
    refused = missing.resume(operation["operation_id"], background=False)
    assert refused["status"] == "RECOVERY_REQUIRED"
    assert refused["error"]["code"] == "CAPABILITY_UNSUPPORTED"
    assert native.calls == []
    restarted = InstallerAPI(api.state_dir, engine=BackendRouter(native=native, docker=docker))
    resumed = restarted.resume(operation["operation_id"], background=False)
    assert resumed["status"] == "SUCCEEDED"
    assert [name for name, _ in docker.calls].count("execute") == 1
    assert [name for name, _ in docker.calls].count("resume") == 1


@pytest.mark.parametrize("target", [DOCKER, {**NATIVE, "scope": "service"}])
def test_missing_backend_is_unavailable_without_native_fallback(tmp_path, target):
    native = RecordingEngine("native")
    api = InstallerAPI(tmp_path / "never-created", engine=BackendRouter(native=native))
    caps = api.capabilities(target)
    assert caps["backend_configured"] is False
    assert not any(item["supported"] for item in caps["actions"].values())
    for function in (lambda: api.discover(manifest(), target),
                     lambda: api.plan("install", manifest(), target)):
        with pytest.raises(InstallerError) as caught:
            function()
        assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    assert native.calls == []
    assert not api.state_dir.exists()


def test_changed_mode_or_scope_cannot_reuse_reviewed_binding():
    native, docker = RecordingEngine("native"), RecordingEngine("docker")
    router = BackendRouter(native=native, docker=docker)
    detail = router.plan("install", {}, DOCKER, {}, {})
    stale = {"target": NATIVE, "engine_plan": detail}
    with pytest.raises(EngineError) as caught:
        router.execute(stale)
    assert caught.value.code == "PLAN_STALE"
    assert not any(name in {"execute", "resume"} for name, _ in native.calls + docker.calls)


def test_legacy_native_resume_remains_compatible_but_docker_needs_review():
    native, docker = RecordingEngine("native"), RecordingEngine("docker")
    router = BackendRouter(native=native, docker=docker)
    assert router.resume({"target": NATIVE, "engine_plan": {}}, {})["status"] == "SUCCEEDED"
    with pytest.raises(EngineError) as caught:
        router.resume({"target": DOCKER, "engine_plan": {}}, {})
    assert caught.value.code == "PLAN_STALE"
    assert docker.calls == []


def test_backend_cannot_replace_router_binding():
    engine = RecordingEngine("native")
    engine.plan = lambda *args: {"backend_routing": {"id": "docker:user"}}
    with pytest.raises(EngineError) as caught:
        BackendRouter(native=engine).plan("install", {}, NATIVE, {}, {})
    assert caught.value.code == "INTEGRITY_FAILED"


def test_default_sdk_reports_unconfigured_docker_without_touching_state(tmp_path):
    api = InstallerAPI(tmp_path / "state")
    caps = api.capabilities(DOCKER)
    assert caps["backend_configured"] is False
    assert api.describe()["backends"]["implicit_fallback"] is False
    assert not api.state_dir.exists()


def test_cli_keeps_bootstrap_failures_in_machine_error_contract(tmp_path, capsys):
    from homensai_install.cli import main
    missing = tmp_path / "not-a-config.json"
    assert main(["api", "capabilities", "--mode", "docker", "--docker-cli-config", str(missing)]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["error"]["code"] == "INVALID_REQUEST"
    assert str(missing) not in json.dumps(response)
    assert main(["api", "capabilities", "--mode", "docker"]) == 0
    caps = json.loads(capsys.readouterr().out)["result"]
    assert caps["backend_configured"] is False


def test_local_bootstrap_passes_only_pinned_config_and_preserves_both_engines(tmp_path, monkeypatch):
    import homensai_install.docker_cli as cli
    import homensai_install.docker_engine as docker_module
    constructed = []
    def runner(**kwargs):
        constructed.append(kwargs)
        return object()
    verifier, secrets = object(), object()
    def docker_factory(**kwargs):
        assert kwargs["verifier"] is verifier and kwargs["secret_provider"] is secrets
        return RecordingEngine("docker")
    monkeypatch.setattr(cli, "DockerCLI", runner)
    monkeypatch.setattr(docker_module, "DockerEngine", docker_factory)
    config = {"executable": "/usr/bin/docker", "sha256": "1" * 64,
              "endpoint": "unix:///run/docker.sock", "config_dir": str(tmp_path / "docker")}
    file = tmp_path / "bootstrap.json"
    file.write_text(json.dumps(config))
    router = configured_backends(verifier=verifier, secret_provider=secrets, docker_cli_config=file)
    assert constructed == [config]
    assert router.capabilities(DOCKER)["backend_configured"] is True
    assert router.capabilities(NATIVE)["backend_configured"] is True


@pytest.mark.parametrize("value", [[], {"executable": "docker"},
    {"executable": "/usr/bin/docker", "sha256": "1" * 64,
     "endpoint": "unix:///run/docker.sock", "config_dir": "/tmp/docker", "argv": ["rm"]}])
def test_invalid_supplied_bootstrap_fails_instead_of_silently_disabling_docker(tmp_path, value):
    file = tmp_path / "bootstrap.json"
    file.write_text(json.dumps(value))
    with pytest.raises(EngineError) as caught:
        configured_backends(docker_cli_config=file)
    assert caught.value.code == "INVALID_REQUEST"


def test_native_refuses_existing_docker_ownership_before_any_effect(tmp_path):
    root = tmp_path / "product"
    (root / ".hinstall").mkdir(parents=True)
    receipt = root / ".hinstall/docker-installation.json"
    receipt.write_text("{}")
    product = manifest()
    with pytest.raises(EngineError) as caught:
        NativeEngine().discover(product, NATIVE, {"install_root": str(root), "modes": [product["modes"][0]["name"]]})
    assert caught.value.code == "OPERATION_CONFLICT"
    assert receipt.read_text() == "{}"
    assert not (root / ".hinstall/installation.json").exists()
