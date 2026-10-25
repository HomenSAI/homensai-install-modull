"""Офлайн-приёмка signed synthetic prerequisites из установленного SDK/CLI.

Новый wheel, чистый venv без system-site-packages, pinned security wheels и
независимые внешние клиенты. Требуются Linux x64, build-env CPython 3.14 и
локальный wheelhouse по requirements-build.lock. Настоящие OS installers,
runtime продукта, GUI/frozen/Windows/Docker этим профилем не принимаются.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import zipfile

from check_installed_sdk import canonical, check_wheel, package_files, run, sha, write_json


ROOT = Path(__file__).resolve().parents[1]
SECURITY_PACKAGES = ("cryptography", "cffi", "pycparser")
PHASES = ("audit_guard", "tampered", "manual_prepare", "manual_restore", "auto_prepare",
          "auto_restore", "cancel_prepare", "cancel_restore", "cli_prepare", "cli_restore")


class MissingInput(RuntimeError):
    pass


def security_wheels(wheelhouse, destination):
    """Выбрать совместимые wheel bytes, уже закреплённые общим lock-файлом."""
    from packaging.tags import sys_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    records = {}
    lock = (ROOT / "requirements-build.lock").read_text(encoding="utf-8")
    for line in lock.replace("\\\n", " ").splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+)==([^\s;]+)\s+(.*)$", line.strip())
        if match and canonicalize_name(match[1]) in SECURITY_PACKAGES:
            name = canonicalize_name(match[1])
            assert name not in records, "Повтор dependency в lock"
            hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", match[3])
            assert hashes, "Security dependency не закреплена hash"
            records[name] = (match[2], set(hashes))
    if set(records) != set(SECURITY_PACKAGES):
        raise MissingInput("Security closure отсутствует в requirements-build.lock")
    tags = {tag: index for index, tag in enumerate(sys_tags())}
    destination.mkdir()
    selected = []
    for name in SECURITY_PACKAGES:
        version, allowed = records[name]
        candidates = []
        for path in wheelhouse.glob("*.whl"):
            candidate, wheel_version, _, wheel_tags = parse_wheel_filename(path.name)
            ranks = [tags[tag] for tag in wheel_tags if tag in tags]
            if candidate == name and str(wheel_version) == version and ranks:
                candidates.append((min(ranks), path.name, path))
        if not candidates:
            raise MissingInput("Нет совместимого pinned wheel: " + name + "==" + version)
        path = min(candidates)[2]
        digest = sha(path.read_bytes())
        if digest not in allowed:
            raise RuntimeError("Wheel hash не совпадает с requirements-build.lock: " + path.name)
        copied = destination / path.name
        shutil.copy2(path, copied)
        assert sha(copied.read_bytes()) == digest
        selected.append({"name": name, "version": version, "filename": path.name, "sha256": digest})
    requirements = destination / "security.lock"
    requirements.write_text("".join(item["name"] + "==" + item["version"] +
        " --hash=sha256:" + item["sha256"] + "\n" for item in selected), encoding="utf-8")
    return selected, requirements


def check_installed_dependency(wheel, installed):
    """Проверить bytes dependency, включая extension module и metadata."""
    checked = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith("/") or name.endswith(".dist-info/RECORD"):
                continue  # pip дописывает installation RECORD.
            parts = PurePosixPath(name).parts
            assert parts and not name.startswith("/") and ".." not in parts
            assert not any(part.endswith(".data") for part in parts), "Неподдержанный wheel relocation"
            expected = sha(archive.read(name))
            assert sha((installed / name).read_bytes()) == expected, "Изменён установленный dependency: " + name
            checked[name] = expected
    return {"exact_files_checked": len(checked), "files_digest": sha(canonical(checked))}


def fixture(work):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    payload = work / "payload"
    for name in ("installer", "licenses"):
        (payload / name).mkdir(parents=True)
    (payload / "runtime.txt").write_text("NON_EXECUTABLE synthetic payload; no product runtime.\n", encoding="utf-8")
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text(encoding="utf-8"))
    manifest.update(product_id="installed-prerequisite-contract", product_name="Signed synthetic prerequisite contract")
    target = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
    entries, artifacts = [], {}
    sentinel = "synthetic-token-never-public"
    for name, depends, required in (("base", [], True), ("client", ["base"], True), ("metrics", [], False)):
        text = "Synthetic installed terms for " + name + "; no third party software.\n"
        (payload / "licenses" / (name + ".txt")).write_text(text, encoding="utf-8")
        content = ("# synthetic artifact " + name + "; NEVER EXECUTED\n").encode()
        url = "https://prerequisites.example.invalid/" + name + "?token=" + sentinel
        artifacts[name] = {"url": url, "content_base64": base64.b64encode(content).decode()}
        entries.append({"requirement": {"component_id": name, "kind": name, "required": required,
            "minimum_version": "1.0.0", "scope": "host"}, "depends_on": depends, "ownership": "shared",
            "adapter_id": "install-" + name, "artifacts": [{"artifact_id": name + "-package", "version": "1.0.0",
                "url": url, "sha256": sha(content), "size": len(content), "license_ref": name + "-terms"}],
            "license": {"license_id": name + "-terms", "version": "2026-01", "text_path": "licenses/" + name + ".txt",
                "text_sha256": sha(text.encode()), "locale": "en", "decision": "allowed",
                "acceptance_required": True, "conditions": []}})
    descriptor = {"schema_version": 1, "catalog_id": "installed-prerequisites", "catalog_version": "1.0.0",
                  "product_id": manifest["product_id"], "target": target, "entries": entries}
    (payload / "installer/prerequisites.json").write_bytes(canonical(descriptor))
    files = {path.relative_to(payload).as_posix(): sha(path.read_bytes())
             for path in sorted(payload.rglob("*")) if path.is_file()}
    inventory = {"schema_version": 1, "product_id": manifest["product_id"], "release": "installed-prerequisites-dev",
        "target": {key: target[key] for key in ("os", "arch", "mode")}, "files": files,
        "development": True, "manifest_sha256": sha(canonical(manifest)), "runtime_executable": "runtime.txt"}
    private = Ed25519PrivateKey.generate()
    signature_bytes = private.sign(canonical(inventory))
    private.public_key().verify(signature_bytes, canonical(inventory))
    signature = {"algorithm": "ed25519", "key_id": "installed-prerequisites-dev",
                 "signature": base64.b64encode(signature_bytes).decode()}
    trust = {"schema_version": 1, "keys": {signature["key_id"]:
        base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}}
    del private
    bad = {**signature, "signature": base64.b64encode(bytes([signature_bytes[0] ^ 1]) + signature_bytes[1:]).decode()}
    for name, value in (("manifest", manifest), ("signed-inventory", inventory), ("signature", signature),
                        ("bad-signature", bad), ("trust", trust)):
        write_json(work / (name + ".json"), value)
    options = {"payload_dir": str(payload), "inventory_file": str(work / "signed-inventory.json"),
               "signature_file": str(work / "signature.json"), "runtime_executable": "runtime.txt",
               "release": inventory["release"], "install_root": str(work / "install-root"),
               "work_dir": str(work / "product-recovery")}
    write_json(work / "options.json", options)
    initial = {"platform": {"os": "linux", "arch": "x64", "release": "synthetic"},
        "identity": {"uid": os.getuid(), "scope": "current_process", "elevated": False},
        "resources": {"cpu_count": 2}, "ports": [], "disks": [],
        "components": {name: {"status": "missing", "version": None, "scope": "host"} for name in artifacts}}
    for case in ("manual", "auto", "cancel", "cli", "tampered"):
        directory = work / "cases" / case
        directory.mkdir(parents=True)
        write_json(directory / "inventory.json", initial)
        write_json(directory / "ledger.json", {"installer_calls": [], "download_calls": [], "discovery_calls": 0})
    return {"target": target, "artifacts": artifacts, "redaction_sentinel": sentinel,
            "product_consent_refs": ["product-eula:" + sha(b"Synthetic product acceptance only")],
            "manifest": str(work / "manifest.json"), "options": str(work / "options.json"),
            "trust": str(work / "trust.json"), "bad_signature": str(work / "bad-signature.json")}, files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True, help="Новый каталог вне checkout; старые попытки не удаляются")
    parser.add_argument("--receipt", type=Path, required=True, help="Новая JSON квитанция, в том числе при FAIL")
    parser.add_argument("--wheelhouse", type=Path, required=True, help="Локальные wheels из requirements-build.lock")
    parser.add_argument("--previous-receipt", type=Path, action="append", default=[], help="Сохранить ссылку/hash предыдущей попытки")
    args = parser.parse_args()
    work, receipt_path = args.work_dir.resolve(), args.receipt.absolute()
    if work.is_relative_to(ROOT) or ROOT.is_relative_to(work) or work.exists() or receipt_path.exists():
        parser.error("Нужны новый work-dir вне checkout и новый receipt; предыдущие файлы сохраняются")
    work.mkdir(parents=True)
    (work / "reports").mkdir()
    receipt = {"schema_version": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "author": "Serhii Khomenko", "status": "FAIL", "production_ready": False, "gate_closed": False,
        "scope": "installed signed synthetic prerequisite SDK/CLI lifecycle, Linux x64",
        "commands": [], "previous_attempts": [], "clients": [],
        "harness_sha256": sha(Path(__file__).read_bytes()),
        "client_sha256": sha((ROOT / "packaging/installed_prerequisites_client.py").read_bytes()),
        "shared_harness_sha256": sha((ROOT / "packaging/check_installed_sdk.py").read_bytes()),
        "requirements_build_lock_sha256": sha((ROOT / "requirements-build.lock").read_bytes()),
        "environment": {"python": sys.version, "platform": platform.platform(), "work_dir": str(work),
                        "venv_system_site_packages": False, "network_downloads": False},
        "not_run": ["Actual OS prerequisite installers", "Windows/Docker/service/reboot/relogin", "Process power-loss",
                    "Product install/update/restore/rollback", "Executable product runtime", "Frozen/GUI/HTTP transport",
                    "Production registry/signing/licence closure", "Full G1/release acceptance"]}
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PIP_NO_INPUT": "1",
                   "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}

    def command(argv, *, cwd=work):
        record = {"argv": [str(item) for item in argv], "cwd": str(cwd), "status": "RUNNING", "expected_exit_code": 0}
        receipt["commands"].append(record)
        try:
            output = run(argv, cwd=cwd, env=environment)
        except Exception as error:
            record.update(status="FAIL", error_type=type(error).__name__, error=str(error)[-4000:])
            raise
        record.update(status="PASS", exit_code=0, stdout_sha256=sha(output.encode()))
        (work / "reports" / ("command-" + str(len(receipt["commands"])) + ".log")).write_text(output, encoding="utf-8")
        return output

    exit_code = 1
    try:
        if sys.version_info[:2] != (3, 14) or sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise MissingInput("Профиль требует CPython 3.14 Linux x64 и соответствующий pinned wheelhouse")
        for previous in args.previous_receipt:
            content = previous.read_bytes()
            value = json.loads(content)
            receipt["previous_attempts"].append({"path": str(previous.absolute()), "sha256": sha(content),
                                                  "status": value["status"], "scope": value["scope"]})
        receipt["source_head"] = command(["git", "rev-parse", "HEAD"], cwd=ROOT).strip()
        receipt["source_tree"] = command(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).strip()
        receipt["source_status"] = command(["git", "status", "--porcelain"], cwd=ROOT).splitlines()
        environment["SOURCE_DATE_EPOCH"] = command(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT).strip()
        receipt["environment"]["source_date_epoch"] = environment["SOURCE_DATE_EPOCH"]
        receipt["environment"]["build_versions"] = {name: importlib.metadata.version(name)
            for name in ("pip", "setuptools", "packaging", "cryptography")}
        snapshot = work / "build-source"
        snapshot.mkdir()
        for name in ("homensai_install", "shared"):
            shutil.copytree(ROOT / name, snapshot / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build", "dist"))
        for name in ("pyproject.toml", "INSTALLER_API.md"):
            shutil.copy2(ROOT / name, snapshot / name)
        expected = package_files(snapshot)
        receipt["source_package_files"], receipt["source_package_digest"] = expected, sha(canonical(expected))
        wheels = work / "wheels"
        wheels.mkdir()
        command([sys.executable, "-I", "-B", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-cache-dir",
                 "--no-build-isolation", "--wheel-dir", wheels, snapshot])
        wheel, = wheels.glob("homensai_install_module-*.whl")
        check_wheel(wheel, expected)
        receipt["wheel"] = {"name": wheel.name, "sha256": sha(wheel.read_bytes()), "exact_files_checked": len(expected)}
        dependencies, security_lock = security_wheels(args.wheelhouse.resolve(), work / "security-wheels")
        receipt["dependencies"] = dependencies
        receipt["security_install_lock_sha256"] = sha(security_lock.read_bytes())
        venv = work / "environment"
        command([sys.executable, "-I", "-B", "-m", "venv", venv])
        python = venv / "bin/python"
        command([python, "-I", "-B", "-c", "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('homensai_install', 'cryptography', 'cffi', 'pycparser')); print('no checkout or dependency fallback')"])
        receipt["missing_package_no_fallback"] = True
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir",
                 "--require-hashes", "--only-binary=:all:", "--find-links", security_lock.parent, "-r", security_lock])
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir", wheel])
        command([python, "-I", "-B", "-m", "pip", "check"])
        installed = Path(command([python, "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).strip())
        check_wheel(wheel, expected, installed)
        for item in dependencies:
            item["installed_verification"] = check_installed_dependency(security_lock.parent / item["filename"], installed)
        client = work / "external_client.py"
        shutil.copy2(ROOT / "packaging/installed_prerequisites_client.py", client)
        config, payload_before = fixture(work)
        config.update(checkout=str(ROOT), site_packages=str(installed), work=str(work), dependencies=dependencies)
        config_path = work / "client-config.json"
        write_json(config_path, config)
        receipt["fixture"] = {"development_signature": True, "files": payload_before,
                              "runtime": "NON_EXECUTABLE_SYNTHETIC_PLACEHOLDER", "private_key_saved": False}
        for phase in PHASES:
            try:
                command([python, "-I", "-B", client, config_path, phase])
            finally:
                report_path = work / "reports" / (phase + ".json")
                if report_path.exists():
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    receipt["clients"].append({**report, "report_sha256": sha(report_path.read_bytes())})
            assert report["status"] == "PASS"
        assert len({report["pid"] for report in receipt["clients"]}) == len(PHASES), "Клиенты не независимы"
        after = {path.relative_to(work / "payload").as_posix(): sha(path.read_bytes())
                 for path in sorted((work / "payload").rglob("*")) if path.is_file()}
        assert after == payload_before, "Signed payload изменён"
        receipt["fixture"]["payload_unchanged"] = True
        check_wheel(wheel, expected, installed)
        for item in dependencies:
            assert check_installed_dependency(security_lock.parent / item["filename"], installed) == item["installed_verification"]
        receipt["installed_package_and_dependency_bytes_unchanged"] = True
        receipt["ordinary_client_denied_attempts"] = sum(len(report["denied_attempts"])
            for report in receipt["clients"] if report["phase"] != "audit_guard")
        assert receipt["ordinary_client_denied_attempts"] == 0
        receipt["cli_commands"] = sum(len(report["observation"].get("commands", [])) for report in receipt["clients"])
        receipt["no_product_effects"] = True
        receipt["status"], exit_code = "PASS", 0
    except Exception as error:
        receipt["status"] = "BLOCKED" if isinstance(error, MissingInput) else "FAIL"
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[-4000:]}
    receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(receipt_path, receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(receipt_path),
                      "clients": len(receipt["clients"]), "cli_commands": receipt.get("cli_commands", 0)}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
