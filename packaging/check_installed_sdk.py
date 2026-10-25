"""Офлайн-проверка минимального public SDK/CLI contract из установленного wheel.

Сборка, venv, подпись development fixture и клиент находятся вне checkout.
Нужны существующие setuptools, pip, cryptography; скачиваний нет. Payload
содержит настоящий reference wheel и явный неисполняемый runtime placeholder.
Это планирование, не приёмка install/runtime/Windows/Docker или полного G1.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(argv, *, cwd, env, expected=0):
    value = subprocess.run([str(item) for item in argv], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=120)
    if value.returncode != expected:
        raise RuntimeError(f"Command failed ({value.returncode}): {argv}\n{value.stdout[-3000:]}\n{value.stderr[-3000:]}")
    return value.stdout


def package_files(root):
    config = tomllib.loads((root / "pyproject.toml").read_text())
    names = set()
    for package in ("homensai_install", "shared"):
        names.update(path.relative_to(root).as_posix() for path in (root / package).rglob("*.py"))
        for pattern in config["tool"]["setuptools"]["package-data"][package]:
            names.update(path.relative_to(root).as_posix() for path in (root / package).glob(pattern) if path.is_file())
    return {name: sha((root / name).read_bytes()) for name in sorted(names)}


def check_wheel(wheel, expected, installed=None):
    with zipfile.ZipFile(wheel) as archive:
        names = {name for name in archive.namelist() if name.startswith(("homensai_install/", "shared/")) and not name.endswith("/")}
        if names != expected.keys():
            raise AssertionError("Wheel package file set differs from source inputs")
        for name, digest in expected.items():
            if sha(archive.read(name)) != digest:
                raise AssertionError("Wheel differs from source: " + name)
            if installed is not None and sha((installed / name).read_bytes()) != digest:
                raise AssertionError("Installed file differs from wheel: " + name)


def fixture(work, reference_wheel):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    payload = work / "payload"
    for directory in ("runtime", "vendor", "locks", "legal"):
        (payload / directory).mkdir(parents=True, exist_ok=True)
    # Deliberately not executable: no Python 3.14 runtime is claimed by this check.
    (payload / "runtime/PLANNING_ONLY.txt").write_text("Synthetic signed planning fixture; NOT AN EXECUTABLE RUNTIME.\n")
    shutil.copy2(reference_wheel, payload / "vendor" / reference_wheel.name)
    shutil.copytree(ROOT / "reference/legal", payload / "legal", dirs_exist_ok=True)
    wheel_digest = sha(reference_wheel.read_bytes())
    for target in ("windows", "linux"):
        (payload / "locks" / (target + ".lock")).write_text(
            "homensai-reference==1.0.0 --hash=sha256:" + wheel_digest + "\n")
    marker = work / "PRODUCT_HOOK_EXECUTED"
    (payload / "forbidden_hook.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('unexpected execution')\n")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text())
    manifest.update(product_id="homensai-reference", product_name="Reference signed planning fixture")
    manifest["paths"] = {name: name for name in manifest["paths"]}
    manifest["runtime"]["lock_files"] = {name: {"file": "locks/" + name + ".lock",
        "sha256": sha((payload / "locks" / (name + ".lock")).read_bytes())} for name in ("windows", "linux")}
    manifest["runtime"]["vendor_wheels"] = [{"file": "vendor/" + reference_wheel.name, "sha256": wheel_digest}]
    manifest["components"] = [{**manifest["components"][0], "cmd": ["python", "-m", "homensai_reference.server"]}]
    manifest["modes"] = [{"name": "local", "components": ["web"]}]
    manifest["mutual_exclusion"] = []
    manifest["secrets"] = []
    manifest["web"] = {"host": "127.0.0.1", "port": port, "healthz": "/healthz"}
    for name, module in (("migrations", "migrate"), ("backup", "archive"), ("restore", "archive")):
        manifest[name]["cmd"] = ["python", "-m", "homensai_reference." + module]
    manifest["hooks"] = {name: [["python", "forbidden_hook.py"]] for name in ("pre_start", "health_checks", "post_update")}
    files = {path.relative_to(payload).as_posix(): sha(path.read_bytes()) for path in sorted(payload.rglob("*")) if path.is_file()}
    target = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
    inventory = {"schema_version": 1, "product_id": manifest["product_id"], "release": "reference-plan-dev",
                 "target": {key: target[key] for key in ("os", "arch", "mode")}, "files": files,
                 "development": True, "manifest_sha256": sha(canonical(manifest)),
                 "runtime_executable": "runtime/PLANNING_ONLY.txt"}
    key = Ed25519PrivateKey.generate()
    signed = key.sign(canonical(inventory))
    key.public_key().verify(signed, canonical(inventory))
    signature = {"algorithm": "ed25519", "key_id": "installed-contract-dev", "signature": base64.b64encode(signed).decode()}
    trust = {"schema_version": 1, "keys": {signature["key_id"]: base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}}
    del key
    for name, value in (("manifest", manifest), ("inventory", inventory), ("signature", signature), ("trust", trust)):
        write_json(work / (name + ".json"), value)
    options = {"install_root": str(work / "install-root"), "work_dir": str(work / "recovery-root"),
               "payload_dir": str(payload), "inventory_file": str(work / "inventory.json"),
               "signature_file": str(work / "signature.json"), "runtime_executable": inventory["runtime_executable"],
               "release": inventory["release"], "eula_digest": files["legal/ru.txt"], "eula_locale": "ru"}
    write_json(work / "options.json", options)
    return target, port, files, marker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True, help="Новый отдельный каталог вне checkout")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.absolute()
    if work.resolve().is_relative_to(ROOT) or ROOT.is_relative_to(work.resolve()):
        parser.error("work-dir must be outside the checkout")
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
        parser.error("This acceptance profile currently requires Linux x64")
    work.mkdir(parents=True, exist_ok=False)
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    baseline = run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=environment).strip()
    snapshot = work / "build-source"
    snapshot.mkdir()
    for directory in ("homensai_install", "shared", "reference"):
        shutil.copytree(ROOT / directory, snapshot / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build", "dist"))
    for name in ("pyproject.toml", "INSTALLER_API.md"):
        shutil.copy2(ROOT / name, snapshot / name)
    expected = package_files(snapshot)
    wheel_dir = work / "wheels"
    wheel_dir.mkdir()
    # PEP 517 backend is already present; no index, dependencies, cache or isolation download.
    build_log = run([sys.executable, "-I", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-cache-dir",
                     "--no-build-isolation", "--wheel-dir", wheel_dir, snapshot], cwd=work, env=environment)
    reference_log = run([sys.executable, "-I", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-cache-dir",
                         "--no-build-isolation", "--ignore-requires-python", "--wheel-dir", wheel_dir, snapshot / "reference"],
                        cwd=work, env=environment)
    (work / "build.log").write_text(build_log + reference_log)
    wheel, = wheel_dir.glob("homensai_install_module-*.whl")
    reference_wheel, = wheel_dir.glob("homensai_reference-*.whl")
    check_wheel(wheel, expected)
    # A wheel with changed resource bytes must fail the same actual verification.
    wrong = work / "wrong-resource.whl"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(wrong, "w") as output:
        for name in source.namelist():
            output.writestr(name, source.read(name) + (b" " if name == "shared/manifest.schema.json" else b""))
    try:
        check_wheel(wrong, expected)
    except AssertionError:
        wrong_rejected = True
    else:
        raise AssertionError("Wrong resource wheel was accepted")
    venv = work / "environment"
    run([sys.executable, "-I", "-m", "venv", venv], cwd=work, env=environment)
    python = venv / "bin/python"
    missing = run([python, "-I", "-c", "import importlib.util; assert importlib.util.find_spec('homensai_install') is None; print('no checkout fallback')"], cwd=work, env=environment)
    run([python, "-I", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir", wheel], cwd=work, env=environment)
    site = Path(run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], cwd=work, env=environment).strip())
    check_wheel(wheel, expected, site)
    client = work / "external_client.py"
    shutil.copy2(ROOT / "packaging/installed_sdk_client.py", client)
    target, port, payload_before, marker = fixture(work, reference_wheel)
    config = {"checkout": str(ROOT), "site_packages": str(site), "port": port,
              "sdk_state": str(work / "sdk-state"), "manifest": str(work / "manifest.json"),
              "options": str(work / "options.json"), "trust": str(work / "trust.json"),
              "target": target, "hook_marker": str(marker), "entrypoint": str(venv / "bin/hinstall"),
              "client_report": str(work / "client-report.json")}
    config_path = work / "client-config.json"
    write_json(config_path, config)
    run([python, "-I", "-B", client, config_path, "sdk"], cwd=work, env=environment)
    sdk = json.loads(Path(config["client_report"]).read_text())
    cli_reports = []
    common = ["--json", "--state-dir", str(work / "cli-state"), "--trust-config", config["trust"], "--allow-development"]
    inputs = ["--manifest", config["manifest"], "--options", config["options"], "--os", "linux", "--arch", "x64", "--mode", "native", "--scope", "user"]
    commands = [("describe", ["api", "describe"], 0), ("validate", ["validate", "--manifest", config["manifest"]], 0),
                ("discover", ["discover", *inputs], 0), ("plan", ["plan", "install", *inputs], 0),
                ("unsupported", ["plan", "modify_components", *inputs], 2),
                ("prerequisites-capabilities", ["prerequisites", "capabilities"], 0),
                ("prerequisites-unconfigured", ["prerequisites", "operations"], 2)]
    for name, command, code in commands:
        raw = run([python, "-I", "-B", client, config_path, "cli", *common, *command], cwd=work, env=environment, expected=code)
        envelope = json.loads(raw)
        report = json.loads(Path(config["client_report"]).read_text())
        if name in {"unsupported", "prerequisites-unconfigured"}:
            assert envelope["error"]["code"] == "CAPABILITY_UNSUPPORTED"
        else:
            assert "error" not in envelope
            result = envelope["result"]
            if name == "describe":
                assert result == sdk["description"]
            if name == "prerequisites-capabilities":
                assert result["configured"] is False and result["production_ready"] is False
            if name == "validate":
                assert result["manifest_digest"] == sdk["manifest_digest"]
            if name == "plan":
                assert result["engine_plan"] == sdk["engine_plan"]
                assert result["target"] == sdk["target"]
                assert result["required_consents"] == sdk["required_consents"]
        cli_reports.append({"command": name, "exit_code": report["exit_code"], "denied_attempts": report["denied_attempts"],
                            "loopback_port_probes": report["loopback_port_probes"], "module_count": len(report["module_origins"])})
    payload_after = {p.relative_to(work / "payload").as_posix(): sha(p.read_bytes()) for p in sorted((work / "payload").rglob("*")) if p.is_file()}
    assert payload_before == payload_after
    assert not marker.exists() and not (work / "install-root").exists() and not (work / "recovery-root").exists()
    receipt = {"schema_version": 1, "checked_at_utc": datetime.now(timezone.utc).isoformat(), "status": "PASS",
               "scope": "installed SDK/CLI Linux x64 signed synthetic reference planning contract",
               "production_ready": False, "gate_closed": False, "source_baseline": baseline,
               "source_package_files": expected, "source_package_digest": sha(canonical(expected)),
               "harness_sha256": sha(Path(__file__).read_bytes()), "client_sha256": sha(client.read_bytes()),
               "wheel": {"name": wheel.name, "sha256": sha(wheel.read_bytes()), "exact_files_checked": len(expected)},
               "reference_wheel": {"name": reference_wheel.name, "sha256": sha(reference_wheel.read_bytes()), "installed_or_executed": False},
               "environment": {"python": sys.version, "platform": platform.platform(), "venv_system_site_packages": False,
                               "external_work_directory": str(work), "pip_network_flags": "--no-index --no-deps --no-cache-dir --no-build-isolation"},
               "fixture": {"development": True, "ed25519_signature_verified_by_publisher": True,
                           "files": payload_before, "payload_unchanged": True, "runtime": "NON_EXECUTABLE_PLANNING_PLACEHOLDER",
                           "installer_payload_verification": "deferred until execute; execute was not called"},
               "sdk": sdk, "cli": cli_reports, "wrong_resource_wheel_rejected": wrong_rejected,
               "missing_package_no_checkout_fallback": missing.strip() == "no checkout fallback",
               "no_product_effects": True, "network_traffic_or_child_process_attempts_in_clients": 0,
               "not_run": ["product install/update/restore/rollback", "Python 3.14 reference runtime execution",
                           "Windows", "Docker", "service integration", "GUI consent", "production signing",
                           "clean machine dependency/license closure", "full G1 and release gates",
                           "installed registered prerequisite coordinator lifecycle", "installed prerequisite HTTP/GUI transport"]}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.receipt, receipt)
    print(json.dumps({"status": "PASS", "receipt": str(args.receipt), "wheel_sha256": receipt["wheel"]["sha256"],
                      "exact_files_checked": len(expected), "cli_commands": len(cli_reports)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
