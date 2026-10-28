"""Native discovery не обращается к постороннему Docker/WSL на хосте."""
import json
from pathlib import Path
import sys

import pytest

from homensai_install.engine import NativeEngine
from homensai_install import prerequisites


@pytest.mark.skipif(sys.platform != "linux", reason="Native user engine executes on Linux")
def test_native_discovery_does_not_execute_available_container_tools(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(prerequisites, "_program", lambda name, *_: tmp_path / name)

    def forbidden(_runner, argv, **_kwargs):
        calls.append(argv)
        raise AssertionError("Native discovery attempted unrelated container probe")

    monkeypatch.setattr(prerequisites.SafeProbeRunner, "run", forbidden)
    engine = NativeEngine()
    monkeypatch.setattr(engine, "_port_state", lambda *_: {})
    manifest = json.loads((Path(__file__).resolve().parents[1] / "examples/rent.manifest.json").read_text())
    manifest["paths"] = {key: key for key in ("app", "releases", "data", "backup", "services", "config", "runtime")}
    root = tmp_path / "new-installation"
    result = engine.discover(manifest, {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"},
                             {"install_root": str(root), "modes": [manifest["modes"][0]["name"]]})
    assert result["read_only"] is True and result["installation"] is None
    assert calls == [] and not root.exists()
    for name in ("docker_engine", "docker_compose"):
        component = result["environment_inventory"]["components"][name]
        assert component["status"] == "manual"
        assert component["reason"] == "container_tools_not_inspected"
        assert component["version"] is None


def test_inventory_can_skip_container_probes_without_claiming_readiness(tmp_path, monkeypatch):
    def forbidden(*_args):
        raise AssertionError("Unselected program must not be discovered or run")

    monkeypatch.setattr(prerequisites, "_program", forbidden)
    result = prerequisites.collect_inventory(paths=[tmp_path], probe_container_tools=False,
        platform_info={"os": "windows", "arch": "x64"})
    for name in ("docker_engine", "docker_compose", "wsl"):
        assert result["components"][name]["status"] == "manual"
    checks = prerequisites.check_requirements([{"component_id": "docker", "kind": "docker_engine"}], result)
    assert checks[0]["ready"] is False and checks[0]["reason"] == "container_tools_not_inspected"
