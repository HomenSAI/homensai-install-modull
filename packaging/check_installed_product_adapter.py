"""Офлайн-приёмка установленного ProductAdapter Linux/native/user maintenance.

Собирает SDK и настоящий reference wheel, устанавливает в новые отдельные venv,
проверяет подписанные ELF runtime bytes, процесс, HTTP, SQLite и полный encrypted
backup. Bootstrap установки — legacy; adapter fresh/update этим профилем НЕ принят.
Product venv использует stdlib/libpython DEV-контейнера, не standalone runtime.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import zipfile

from check_installed_sdk import canonical, check_wheel, package_files, sha, write_json
from check_installed_prerequisites import MissingInput, check_installed_dependency, security_wheels


ROOT = Path(__file__).resolve().parents[1]
PHASES = ("guard", "bootstrap", "start", "backup", "archive", "stop", "replay", "unsupported")


def reference_files(root):
    directory = root / "reference/src"
    return {path.relative_to(directory).as_posix(): sha(path.read_bytes())
            for path in sorted(directory.rglob("*.py"))}


def check_reference_wheel(wheel, expected, installed=None):
    with zipfile.ZipFile(wheel) as archive:
        names = {name for name in archive.namelist() if name.startswith("homensai_reference/") and not name.endswith("/")}
        assert names == expected.keys(), "Reference wheel file set отличается от исходников"
        for name, digest in expected.items():
            assert sha(archive.read(name)) == digest, "Reference wheel bytes отличаются от исходников"
            if installed is not None:
                assert sha((installed / name).read_bytes()) == digest, "Установленный reference изменён"


def fixture(work, reference_wheel, command):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    payload = work / "payload"
    payload.mkdir(mode=0o700)
    runtime = payload / "runtime"
    command([sys.executable, "-I", "-B", "-m", "venv", "--copies", "--without-pip", runtime])
    # venv создаёт lib64 -> lib. SDK запрещает любые payload symlinks.
    for path in runtime.rglob("*"):
        if path.is_symlink():
            if path.name != "lib64" or path.readlink() != Path("lib"):
                raise AssertionError("Неожиданная ссылка product runtime")
            path.unlink()
    python = runtime / "bin/python"
    assert python.read_bytes().startswith(b"\x7fELF") and not python.is_symlink()
    command([sys.executable, "-I", "-B", "-m", "pip", "--python", python, "install", "--no-index", "--no-deps",
             "--no-compile", "--no-cache-dir", reference_wheel])
    product_site = Path(command([python, "-I", "-B", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"]).strip())
    for name in ("vendor", "locks", "legal", "installer"):
        (payload / name).mkdir(mode=0o700)
    shutil.copy2(reference_wheel, payload / "vendor" / reference_wheel.name)
    for locale in ("ru", "en", "de"):
        shutil.copy2(ROOT / "reference/legal" / (locale + ".txt"), payload / "legal" / (locale + ".txt"))
    for target in ("linux", "windows"):
        (payload / "locks" / (target + ".lock")).write_text(
            "homensai-reference==1.0.0 --hash=sha256:" + sha(reference_wheel.read_bytes()) + "\n", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    profile = {"schema_version": 1, "product_version": "1.0.0", "database_schema": 1,
               "health_failure": False, "migration_failure": False, "require_api_key": True}
    write_json(payload / "reference-profile.json", profile)
    write_json(payload / "adapter-config.json", {"product_id": "homensai-reference", "port": port})
    hooks = {"describe": False, "operation_status": False, "quiesce": True, "readiness": False}
    descriptor = {"schema_version": 1, "protocol_version": "1.0", "product_id": "homensai-reference",
        "adapter_id": "homensai-reference", "adapter_version": "0.1.0",
        "command": ["runtime/bin/python", "-I", "-B", "-m", "homensai_reference.adapter"],
        "environment_allowlist": ["HOMENSAI_DATA_HOME"],
        "capabilities": {name: {"mutating": mutating, "idempotency": "application" if mutating else "read_only"}
                         for name, mutating in hooks.items()}}
    write_json(payload / "installer/adapter.json", descriptor)
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text(encoding="utf-8"))
    manifest.update(product_id="homensai-reference", product_name="Installed HomenSAI Reference DEV")
    manifest["paths"] = {key: key for key in ("app", "releases", "data", "backup", "services", "config", "runtime")}
    manifest["runtime"]["lock_files"] = {name: {"file": "locks/" + name + ".lock",
        "sha256": sha((payload / "locks" / (name + ".lock")).read_bytes())} for name in ("linux", "windows")}
    manifest["runtime"]["vendor_wheels"] = [{"file": "vendor/" + reference_wheel.name, "sha256": sha(reference_wheel.read_bytes())}]
    manifest["components"] = [{"name": "web", "cmd": ["python", "-I", "-B", "-m", "homensai_reference.server"], "after": [],
        "heartbeat": {"file": "web.json", "interval": 1, "timeout": 3}, "restart": {"delay": 1, "max": 1, "window": 3}}]
    manifest["modes"] = [{"name": "local", "components": ["web"]}]
    manifest["mutual_exclusion"] = []
    manifest["web"] = {"host": "127.0.0.1", "port": port, "healthz": "/healthz"}
    manifest["secrets"] = [{"name": "REFERENCE_API_KEY", "required": True, "contexts": ["service", "admin"], "same_value": True}]
    manifest["migrations"]["cmd"] = ["python", "-I", "-B", "-m", "homensai_reference.migrate"]
    manifest["backup"]["cmd"] = ["python", "-I", "-B", "-m", "homensai_reference.archive"]
    manifest["restore"]["cmd"] = list(manifest["backup"]["cmd"])
    manifest["hooks"] = {"pre_start": [], "health_checks": [list(manifest["backup"]["cmd"])], "post_update": []}
    manifest["update"]["health_wait"] = 8
    files = {path.relative_to(payload).as_posix(): sha(path.read_bytes()) for path in sorted(payload.rglob("*")) if path.is_file()}
    target = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
    inventory = {"schema_version": 1, "product_id": manifest["product_id"], "release": "reference-v1",
        "target": {key: target[key] for key in ("os", "arch", "mode")}, "files": files,
        "development": True, "manifest_sha256": sha(canonical(manifest)), "runtime_executable": "runtime/bin/python"}
    private = Ed25519PrivateKey.generate()
    signed = private.sign(canonical(inventory))
    private.public_key().verify(signed, canonical(inventory))
    signature = {"algorithm": "ed25519", "key_id": "installed-reference-dev", "signature": base64.b64encode(signed).decode()}
    trust = {"schema_version": 1, "keys": {signature["key_id"]:
             base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}}
    del private
    for name, value in (("manifest", manifest), ("inventory", inventory), ("signature", signature), ("trust", trust)):
        write_json(work / (name + ".json"), value)
    options = {"install_root": str(work / "Установка с пробелами"), "work_dir": str(work / "recovery"),
        "payload_dir": str(payload), "inventory_file": str(work / "inventory.json"),
        "signature_file": str(work / "signature.json"), "runtime_executable": "runtime/bin/python", "release": "reference-v1",
        "eula_digest": files["legal/ru.txt"], "eula_locale": "ru", "backup_password_ref": "acceptance.archive",
        "secret_refs": {"REFERENCE_API_KEY": "acceptance.product-key"}}
    write_json(work / "options.json", options)
    return {"port": port, "target": target, "root": options["install_root"], "work": str(work),
            "manifest": str(work / "manifest.json"), "options": str(work / "options.json"), "trust": str(work / "trust.json"),
            "archive_password": secrets.token_hex(32), "product_secret": secrets.token_hex(32)}, files, product_site


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True, help="Новый внешний каталог, вне checkout")
    parser.add_argument("--receipt", type=Path, required=True, help="Новая JSON квитанция; FAIL тоже сохраняется")
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--checkpoint", default="development")
    parser.add_argument("--source-git-dir", type=Path, help="Read-only gitdir для linked worktree, смонтированного в Docker")
    parser.add_argument("--previous-receipt", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    work, receipt_path = args.work_dir.resolve(), args.receipt.absolute()
    if work.is_relative_to(ROOT) or ROOT.is_relative_to(work) or work.exists() or receipt_path.exists():
        parser.error("Нужны новый внешний work-dir и новый receipt, старые попытки сохраняются")
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
        parser.error("Нужен Linux x64 DEV runner")
    work.mkdir(mode=0o700, parents=True)
    (work / "reports").mkdir(mode=0o700)
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PIP_NO_INPUT": "1",
                   "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}
    receipt = {"schema_version": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "author": "Serhii Khomenko", "checkpoint_id": args.checkpoint, "status": "FAIL", "production_ready": False,
        "gate_closed": False, "scope": "installed reference ProductAdapter maintenance Linux/native/user in DEV container",
        "task_ids": ["HIM-024", "HIM-036", "HIM-050", "HIM-063", "HIM-072"],
        "commands": [], "clients": [], "previous_attempts": [],
        "environment": {"python": sys.version, "platform": platform.platform(), "work_dir": str(work),
            "uid": os.getuid(), "venv_system_site_packages": False, "network_downloads": False,
            "runtime_closure": "signed copied DEV ELF in product venv; borrowed container stdlib/libpython/OS libraries"},
        "harness_files": {name: sha((ROOT / "packaging" / name).read_bytes()) for name in
            ("check_installed_product_adapter.py", "installed_product_adapter_client.py", "check_installed_sdk.py", "check_installed_prerequisites.py")},
        "not_run": ["ProductAdapter fresh install/update/migration/restore/rollback/repair/reinstall/uninstall",
                    "Clean Ubuntu24.04/Windows11 host", "Product Docker/services/reboot", "GUI consent/browser",
                    "Standalone bundled runtime/security/license closure", "Production signing", "Full G1/G2/G3/G4/G5"]}
    config = None
    exit_code = 1
    git = ["git", "-c", "safe.directory=" + str(ROOT)]
    if args.source_git_dir is not None:
        git.extend(["--git-dir=" + str(args.source_git_dir.resolve(strict=True)), "--work-tree=" + str(ROOT)])

    def command(argv, *, cwd=work):
        record = {"argv": [str(item) for item in argv], "cwd": str(cwd), "expected_exit_code": 0}
        receipt["commands"].append(record)
        completed = subprocess.run(record["argv"], cwd=cwd, env=environment, text=True, capture_output=True, timeout=150)
        log = completed.stdout + completed.stderr
        record.update(exit_code=completed.returncode, status="PASS" if completed.returncode == 0 else "FAIL", log_sha256=sha(log.encode()))
        (work / "reports" / ("command-" + str(len(receipt["commands"])) + ".log")).write_text(log, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError("Команда " + str(len(receipt["commands"])) + " завершилась " + str(completed.returncode) + ": " + log[-2500:])
        return completed.stdout

    try:
        for previous in args.previous_receipt:
            previous = previous.resolve(strict=True)
            previous_data = json.loads(previous.read_text(encoding="utf-8"))
            receipt["previous_attempts"].append({"path": str(previous), "sha256": sha(previous.read_bytes()), "status": previous_data["status"]})
        receipt["source_head"] = command([*git, "rev-parse", "HEAD"], cwd=ROOT).strip()
        receipt["source_tree"] = command([*git, "rev-parse", "HEAD^{tree}"], cwd=ROOT).strip()
        receipt["source_status"] = command([*git, "status", "--porcelain"], cwd=ROOT).splitlines()
        receipt["requirements_build_lock_sha256"] = sha((ROOT / "requirements-build.lock").read_bytes())
        environment["SOURCE_DATE_EPOCH"] = command([*git, "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT).strip()
        receipt["environment"]["build_versions"] = {name: importlib.metadata.version(name) for name in ("pip", "setuptools", "packaging", "cryptography")}
        snapshot = work / "build-source"
        snapshot.mkdir()
        for name in ("homensai_install", "shared", "reference"):
            shutil.copytree(ROOT / name, snapshot / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build", "dist"))
        for name in ("pyproject.toml", "INSTALLER_API.md"):
            shutil.copy2(ROOT / name, snapshot / name)
        expected, reference_expected = package_files(snapshot), reference_files(snapshot)
        receipt.update(source_package_files=expected, source_package_digest=sha(canonical(expected)),
                       reference_source_files=reference_expected, reference_source_digest=sha(canonical(reference_expected)))
        wheels = work / "wheels"
        wheels.mkdir()
        for source in (snapshot, snapshot / "reference"):
            command([sys.executable, "-I", "-B", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-cache-dir",
                     "--no-build-isolation", "--wheel-dir", wheels, source])
        wheel, = wheels.glob("homensai_install_module-*.whl")
        reference_wheel, = wheels.glob("homensai_reference-*.whl")
        check_wheel(wheel, expected)
        check_reference_wheel(reference_wheel, reference_expected)
        receipt["wheel"] = {"name": wheel.name, "sha256": sha(wheel.read_bytes()), "exact_files_checked": len(expected)}
        receipt["reference_wheel"] = {"name": reference_wheel.name, "sha256": sha(reference_wheel.read_bytes()), "exact_files_checked": len(reference_expected)}
        dependencies, security_lock = security_wheels(args.wheelhouse.resolve(), work / "security-wheels")
        receipt["dependencies"] = dependencies
        receipt["security_install_lock_sha256"] = sha(security_lock.read_bytes())
        venv = work / "environment"
        command([sys.executable, "-I", "-B", "-m", "venv", venv])
        python = venv / "bin/python"
        command([python, "-I", "-B", "-c", "import importlib.util;assert all(importlib.util.find_spec(n) is None for n in ('homensai_install','cryptography','cffi','pycparser','homensai_reference'));print('no source or dependency fallback')"])
        receipt["missing_package_no_fallback"] = True
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir",
                 "--require-hashes", "--only-binary=:all:", "--find-links", security_lock.parent, "-r", security_lock])
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir", wheel])
        command([python, "-I", "-B", "-m", "pip", "check"])
        installed = Path(command([python, "-I", "-B", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"]).strip())
        check_wheel(wheel, expected, installed)
        for item in dependencies:
            item["installed_verification"] = check_installed_dependency(security_lock.parent / item["filename"], installed)
        config, payload_before, product_site = fixture(work, reference_wheel, command)
        check_reference_wheel(reference_wheel, reference_expected, product_site)
        product_origins = json.loads(command([work / "payload/runtime/bin/python", "-I", "-B", "-c",
            "import json,sys,homensai_reference;print(json.dumps({'module':homensai_reference.__file__,'prefix':sys.prefix,'base_prefix':sys.base_prefix}))"]))
        assert Path(product_origins["module"]).is_relative_to(product_site)
        assert Path(product_origins["prefix"]) == work / "payload/runtime"
        receipt["reference_runtime_import"] = product_origins
        config.update(checkout=str(ROOT), site_packages=str(installed), runtime_sha256=payload_before["runtime/bin/python"])
        config_path = work / "client-config.json"
        write_json(config_path, config)
        config_path.chmod(0o600)
        client = work / "external_client.py"
        shutil.copy2(ROOT / "packaging/installed_product_adapter_client.py", client)
        receipt["fixture"] = {"development_signature": True, "private_key_saved": False, "files": payload_before,
            "runtime_executable": "runtime/bin/python", "bootstrap": "legacy NativeEngine install; NOT ProductAdapter fresh"}
        for phase in PHASES:
            try:
                command([python, "-I", "-B", client, config_path, phase])
            finally:
                report_path = work / "reports" / (phase + ".json")
                if report_path.exists():
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    receipt["clients"].append({**report, "report_sha256": sha(report_path.read_bytes())})
            assert report["status"] == "PASS"
        assert len({report["pid"] for report in receipt["clients"]}) == len(PHASES)
        assert receipt["clients"][0]["denied_attempts"] == ["checkout_read", "external_network", "unowned_subprocess"]
        assert not any(report["denied_attempts"] for report in receipt["clients"][1:])
        payload_after = {path.relative_to(work / "payload").as_posix(): sha(path.read_bytes())
                         for path in sorted((work / "payload").rglob("*")) if path.is_file()}
        assert payload_before == payload_after, "Signed payload изменён"
        assert expected == package_files(ROOT) and reference_expected == reference_files(ROOT), "Исходники изменились во время проверки"
        assert receipt["harness_files"] == {name: sha((ROOT / "packaging" / name).read_bytes()) for name in receipt["harness_files"]}, "Harness изменился во время проверки"
        check_wheel(wheel, expected, installed)
        check_reference_wheel(reference_wheel, reference_expected, product_site)
        for item in dependencies:
            assert check_installed_dependency(security_lock.parent / item["filename"], installed) == item["installed_verification"]
        receipt.update(installed_package_and_dependency_bytes_unchanged=True, signed_payload_unchanged=True,
                       source_inputs_unchanged=True, status="PASS")
        exit_code = 0
    except Exception as error:
        receipt["status"] = "BLOCKED" if isinstance(error, MissingInput) else "FAIL"
        message = str(error)[-4000:]
        if config:
            for secret in (config["archive_password"], config["product_secret"]):
                message = message.replace(secret, "[REDACTED]")
        receipt["error"] = {"type": type(error).__name__, "message": message}
    finally:
        # Каждый клиент останавливает свой процесс через SDK; при раннем FAIL
        # bootstrap helper делает отдельный bounded cleanup только этого root.
        if config and config.get("site_packages"):
            try:
                cleanup = work / "cleanup.py"
                cleanup.write_text("import json,sys\nfrom pathlib import Path\nfrom homensai_install.api.v1 import InstallerAPI\n"
                    "c=json.loads(Path(sys.argv[1]).read_text());o=json.loads(Path(c['options']).read_text());m=json.loads(Path(c['manifest']).read_text())\n"
                    "a=InstallerAPI.from_bootstrap(Path(c['work'])/'cleanup-state',trust_config=c['trust'],allow_development=True)\n"
                    "p=a.plan('stop',m,c['target'],options=o)\n"
                    "r=a.submit(p['plan_id'],p['plan_digest'],[*p['required_consents'],p['review_consent_ref']],idempotency_key='cleanup',background=False)\n"
                    "assert r['status']=='SUCCEEDED'\n", encoding="utf-8")
                command([work / "environment/bin/python", "-I", "-B", cleanup, work / "client-config.json"])
                receipt["cleanup"] = "PASS: SDK stop of owned reference root"
            except Exception as error:
                receipt["cleanup"] = "FAIL: " + type(error).__name__
                receipt["status"], exit_code = "FAIL", 1
        receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(receipt_path, receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(receipt_path), "clients": len(receipt["clients"])}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
