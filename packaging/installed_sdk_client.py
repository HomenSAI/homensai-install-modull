"""Внешний клиент установленного SDK; только публичные imports продукта.

Запускается check_installed_sdk.py из отдельного venv и каталога вне checkout.
Не предназначен для установки продукта: проверяет только минимальный контракт.
"""
from __future__ import annotations

import importlib.metadata
from importlib import resources
import json
import os
from pathlib import Path
import runpy
import ssl  # Загружается до установки запретов; socket остаётся настоящим классом.
import sys


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    mode = sys.argv[2]
    checkout = Path(config["checkout"])
    installed = Path(config["site_packages"])
    denied = []
    probes = []

    def guard(event, arguments):
        if event == "open" and isinstance(arguments[0], (str, bytes)):
            path = Path(os.fsdecode(arguments[0])).absolute()
            if path.is_relative_to(checkout):
                denied.append("checkout_read")
                raise AssertionError("Installed client tried to read the source checkout")
        if event == "socket.bind":
            if arguments[1] != ("127.0.0.1", config["port"]):
                denied.append(event)
                raise AssertionError("Only the isolated loopback port probe is allowed")
            probes.append(event)
        elif event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                       "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg",
                       "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec",
                       "os.fork", "os.forkpty"}:
            denied.append(event)
            raise AssertionError("Network traffic and child processes are forbidden: " + event)

    sys.addaudithook(guard)
    assert not any(Path(item or os.getcwd()).absolute().is_relative_to(checkout) for item in sys.path)
    assert not Path.cwd().is_relative_to(checkout)
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.errors import InstallerError

    state = Path(config["sdk_state"])
    manifest = json.loads(Path(config["manifest"]).read_text())
    options = json.loads(Path(config["options"]).read_text())
    target = config["target"]
    result = {}
    exit_code = 0
    if mode == "cli":
        sys.argv = [config["entrypoint"], *sys.argv[3:]]
        try:
            runpy.run_path(config["entrypoint"], run_name="__main__")
        except SystemExit as error:
            exit_code = error.code or 0
    else:
        api = InstallerAPI.from_bootstrap(state, trust_config=config["trust"], allow_development=True)
        description = api.describe()
        exported = json.loads(resources.files("homensai_install").joinpath("api/resources/discovery.json").read_text())
        exported.pop("documentation")
        assert description == exported
        assert description["production_ready"] is False
        assert importlib.metadata.version("homensai-install-module") == description["module_version"]
        for name in description["schemas"]:
            assert isinstance(api.schema(name), dict)
        validated = api.validate(manifest)
        assert validated["valid"] is True
        assert validated["normalized_manifest"]["native_complete"] is True
        discovered = api.discover(manifest, target, options)
        assert discovered["installation"] is None
        assert discovered["read_only"] is True
        assert not state.exists(), "describe/validate/discover must not create the journal"
        plan = api.plan("install", manifest, target, options=options)
        assert plan["engine_plan"]["backend_routing"]["id"] == "native:user"
        assert [step["id"] for step in plan["engine_plan"]["steps"]] == [
            "verify", "prepare", "stage", "migrate", "activate", "start", "health"]
        assert api.get_plan(plan["plan_id"]) == plan
        assert InstallerAPI.from_bootstrap(state, trust_config=config["trust"], allow_development=True).get_plan(plan["plan_id"]) == plan
        assert api.operations_list()["operations"] == []
        errors = []
        for action, selected, values, expected in [
            ("modify_components", target, options, "CAPABILITY_UNSUPPORTED"),
            ("install", {**target, "mode": "docker"}, options, "CAPABILITY_UNSUPPORTED"),
            ("invented_action", target, options, "INVALID_REQUEST"),
            ("install", target, {**options, "trust_config": config["trust"]}, "INVALID_REQUEST"),
        ]:
            try:
                api.plan(action, manifest, selected, options=values)
            except InstallerError as error:
                assert error.code == expected, (error.code, expected)
                errors.append(error.code)
            else:
                raise AssertionError("Unsupported action or request trust injection was accepted")
        result = {"description": description, "manifest_digest": validated["manifest_digest"],
                  "engine_plan": plan["engine_plan"], "required_consents": plan["required_consents"],
                  "target": plan["target"], "negative_error_codes": errors,
                  "journal_reload": True, "operations": [], "schemas": list(description["schemas"])}

    origins = {}
    for name, module in list(sys.modules.items()):
        if name == "shared" or name.startswith(("homensai_install", "shared.")):
            filename = getattr(module, "__file__", None)
            if filename:
                path = Path(filename).resolve()
                assert path.is_relative_to(installed), (name, str(path))
                origins[name] = path.relative_to(installed).as_posix()
    assert origins and not denied, denied
    assert not Path(options["install_root"]).exists()
    assert not Path(options["work_dir"]).exists()
    assert not Path(config["hook_marker"]).exists()
    result.update({"module_origins": origins, "denied_attempts": denied,
                   "loopback_port_probes": len(probes), "exit_code": exit_code})
    Path(config["client_report"]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
