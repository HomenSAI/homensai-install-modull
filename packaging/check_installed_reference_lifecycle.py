"""Installed public reference SDK preparation/fresh/update с точным SDK Git input.

Новый отдельный профиль, старый maintenance harness не меняет. Все продуктовые
действия идут через public InstallerAPI и trusted NativeEngine opt-in хоста.
Evidence экспортируется до выхода, поэтому Docker runner можно запускать --rm.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import zipfile

from check_installed_sdk import canonical, check_wheel, package_files, sha, write_json
from check_installed_prerequisites import check_installed_dependency, security_wheels
from check_installed_product_adapter import check_reference_wheel, fixture, reference_files


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--sdk-revision", required=True)
    parser.add_argument("--source-git-dir", type=Path)
    parser.add_argument("--profile", choices=("preparation", "fresh", "update-readiness", "update", "update-lost-ack", "update-tampered-backup", "update-missing-ledger"), default="preparation")
    args = parser.parse_args(argv)
    work, output = args.work_dir.resolve(), args.receipt.absolute()
    exported = output.with_name(output.stem + "-evidence")
    if (work.exists() or output.exists() or exported.exists() or work.is_relative_to(ROOT) or ROOT.is_relative_to(work)
            or re.fullmatch(r"[0-9a-f]{40}", args.sdk_revision) is None):
        parser.error("Нужны новые внешние пути и полный Git SHA")
    work.mkdir(mode=0o700, parents=True)
    (work / "reports").mkdir(mode=0o700)
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PIP_NO_INPUT": "1",
                   "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}
    git = ["git", "-c", "safe.directory=" + str(ROOT)]
    if args.source_git_dir:
        git += ["--git-dir=" + str(args.source_git_dir.resolve(strict=True)), "--work-tree=" + str(ROOT)]
    receipt = {"schema_version": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "FAIL",
        "profile": args.profile, "production_ready": False, "gate_closed": False, "author": "Serhii Khomenko",
        "commands": [], "scope": "installed public SDK reference " + args.profile + ", Linux/native/user DEV container",
        "sdk_source_revision": args.sdk_revision, "environment": {"python": sys.version, "uid": os.getuid(),
            "network_downloads": False, "venv_system_site_packages": False, "work_dir": str(work),
            "runtime_closure": "signed copied DEV ELF; borrowed container stdlib/libpython/OS libraries"},
        "not_run": [("Adapter restore/rollback/repair/uninstall" if args.profile in {"update", "update-lost-ack", "update-tampered-backup", "update-missing-ledger"} else
                     "Adapter update/restore/rollback/repair/uninstall"), "Clean Ubuntu/Windows/Docker/service/reboot",
                    "Standalone runtime/security/license closure", "GUI", "Production signing", "Full G1-G5"],
        "harness_files": {name: sha((ROOT / "packaging" / name).read_bytes()) for name in
            ("check_installed_reference_lifecycle.py", "installed_reference_lifecycle_client.py", "check_installed_product_adapter.py",
             "check_installed_sdk.py", "check_installed_prerequisites.py")}}
    code = 1

    def command(argv, cwd=work):
        record = {"argv": [str(item) for item in argv], "cwd": str(cwd)}
        receipt["commands"].append(record)
        completed = subprocess.run(record["argv"], cwd=cwd, env=environment, capture_output=True, text=True, timeout=180)
        log = completed.stdout + completed.stderr
        (work / "reports" / ("command-" + str(len(receipt["commands"])) + ".log")).write_text(log, encoding="utf-8")
        record.update(exit_code=completed.returncode, log_sha256=sha(log.encode()))
        if completed.returncode:
            raise RuntimeError("Команда " + str(len(receipt["commands"])) + ": " + log[-2500:])
        return completed.stdout

    try:
        receipt["sdk_source_tree"] = command([*git, "rev-parse", args.sdk_revision + "^{tree}"], ROOT).strip()
        receipt["reference_source_head"] = command([*git, "rev-parse", "HEAD"], ROOT).strip()
        receipt["reference_source_status"] = command([*git, "status", "--porcelain"], ROOT).splitlines()
        archive = work / "sdk-source.zip"
        command([*git, "archive", "--format=zip", "--output=" + str(archive), args.sdk_revision, "--",
                 "homensai_install", "shared", "pyproject.toml", "INSTALLER_API.md"], ROOT)
        receipt["sdk_source_archive_sha256"] = sha(archive.read_bytes())
        snapshot = work / "sdk-source"
        with zipfile.ZipFile(archive) as zipped:
            assert all(not name.startswith("/") and ".." not in PurePosixPath(name).parts for name in zipped.namelist())
            zipped.extractall(snapshot)
        ref_snapshot = work / "reference-source"
        shutil.copytree(ROOT / "reference", ref_snapshot / "reference",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build", "dist"))
        expected, ref_expected = package_files(snapshot), reference_files(ref_snapshot)
        receipt.update(sdk_source_package_files=expected, reference_source_files=ref_expected)
        wheels = work / "wheels"
        wheels.mkdir()
        for source in (snapshot, ref_snapshot / "reference"):
            command([sys.executable, "-I", "-B", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-cache-dir",
                     "--no-build-isolation", "--wheel-dir", wheels, source])
        wheel, = wheels.glob("homensai_install_module-*.whl")
        reference_wheel, = wheels.glob("homensai_reference-*.whl")
        check_wheel(wheel, expected)
        check_reference_wheel(reference_wheel, ref_expected)
        receipt["wheel"] = {"name": wheel.name, "sha256": sha(wheel.read_bytes()), "exact_files_checked": len(expected)}
        receipt["reference_wheel"] = {"name": reference_wheel.name, "sha256": sha(reference_wheel.read_bytes()), "exact_files_checked": len(ref_expected)}
        dependencies, lock = security_wheels(args.wheelhouse.resolve(), work / "security-wheels")
        receipt["dependencies"] = dependencies
        receipt["security_lock_sha256"] = sha(lock.read_bytes())
        venv = work / "environment"
        command([sys.executable, "-I", "-B", "-m", "venv", venv])
        python = venv / "bin/python"
        command([python, "-I", "-B", "-c", "import importlib.util;assert all(importlib.util.find_spec(n) is None for n in ('homensai_install','cryptography','homensai_reference'))"])
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir",
                 "--require-hashes", "--only-binary=:all:", "--find-links", lock.parent, "-r", lock])
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir", wheel])
        command([python, "-I", "-B", "-m", "pip", "check"])
        site = Path(command([python, "-I", "-B", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"]).strip())
        check_wheel(wheel, expected, site)
        for dependency in dependencies:
            dependency["installed_verification"] = check_installed_dependency(lock.parent / dependency["filename"], site)
        config, _, product_site = fixture(work, reference_wheel, command)
        check_reference_wheel(reference_wheel, ref_expected, product_site)
        payload = work / "payload"
        descriptor = json.loads((payload / "installer/adapter.json").read_text())
        descriptor["capabilities"].update({name: {"mutating": mutating, "idempotency": "application" if mutating else "read_only"}
                                            for name, mutating in (("migration_plan", False), ("migration_apply", True))})
        descriptor["environment_allowlist"].append("HOMENSAI_CONFIG_HOME")
        write_json(payload / "installer/adapter.json", descriptor)
        manifest = json.loads((work / "manifest.json").read_text())
        if args.profile in {"fresh", "update-readiness", "update", "update-lost-ack", "update-tampered-backup", "update-missing-ledger"}:
            (payload / "forbidden_legacy_migration.py").write_text("import os\nfrom pathlib import Path\n(Path(os.environ['HOMENSAI_DATA_HOME'])/'FORBIDDEN_LEGACY_MIGRATION').write_text('unexpected legacy hook')\nraise SystemExit(87)\n", encoding="utf-8")
            manifest["migrations"]["cmd"] = ["python", "-I", "-B", "forbidden_legacy_migration.py"]
            write_json(work / "manifest.json", manifest)
        for path in payload.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o700 if path.read_bytes().startswith(b"\x7fELF") else 0o600)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        private = Ed25519PrivateKey.generate()
        key_id = "reference-lifecycle-dev"
        write_json(work / "trust.json", {"schema_version": 1, "keys": {key_id:
            base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}})
        options = json.loads((work / "options.json").read_text())
        original_inventory = json.loads((work / "inventory.json").read_text())
        bundle_options, signed_bundles = {}, {}
        for version in (1, 2, 3):
            source = payload if version == 1 else work / ("payload-v" + str(version))
            if version != 1:
                shutil.copytree(payload, source)
            profile = json.loads((source / "reference-profile.json").read_text())
            profile.update(product_version=str(version) + ".0.0", database_schema=version)
            write_json(source / "reference-profile.json", profile)
            files = {path.relative_to(source).as_posix(): sha(path.read_bytes()) for path in sorted(source.rglob("*")) if path.is_file()}
            inventory = {**original_inventory, "release": "reference-v" + str(version), "files": files,
                         "manifest_sha256": sha(canonical(manifest))}
            inventory_path, signature_path = (work / ("inventory-v" + str(version) + ".json"), work / ("signature-v" + str(version) + ".json"))
            write_json(inventory_path, inventory)
            write_json(signature_path, {"algorithm": "ed25519", "key_id": key_id,
                "signature": base64.b64encode(private.sign(canonical(inventory))).decode()})
            version_options = {**options, "payload_dir": str(source), "release": inventory["release"],
                               "inventory_file": str(inventory_path), "signature_file": str(signature_path)}
            option_path = work / ("options-v" + str(version) + ".json")
            write_json(option_path, version_options)
            bundle_options[str(version)] = str(option_path)
            signed_bundles[str(version)] = files
        del private
        receipt["signed_bundles"] = signed_bundles
        config.update(site_packages=str(site), checkout=str(ROOT), client_report=str(work / "client-report.json"),
                      bundle_options=bundle_options, profile="update" if args.profile in {"update-lost-ack", "update-tampered-backup", "update-missing-ledger"} else args.profile,
                      fault_profile={"update-lost-ack": "lost_ack", "update-tampered-backup": "tampered_backup", "update-missing-ledger": "missing_ledger"}.get(args.profile),
                      runtime_sha256=signed_bundles["1"]["runtime/bin/python"])
        config_path = work / "client-config.json"
        write_json(config_path, config)
        config_path.chmod(0o600)
        client = work / "external_client.py"
        shutil.copy2(ROOT / "packaging/installed_reference_lifecycle_client.py", client)
        command([python, "-I", "-B", client, config_path])
        receipt["client"] = json.loads((work / "client-report.json").read_text())
        assert receipt["client"]["status"] == "PASS"
        check_wheel(wheel, expected, site)
        for version in (1, 2, 3):
            source = payload if version == 1 else work / ("payload-v" + str(version))
            assert signed_bundles[str(version)] == {path.relative_to(source).as_posix(): sha(path.read_bytes()) for path in sorted(source.rglob("*")) if path.is_file()}
            check_reference_wheel(reference_wheel, ref_expected, source / product_site.relative_to(payload))
        for dependency in dependencies:
            assert dependency["installed_verification"] == check_installed_dependency(lock.parent / dependency["filename"], site)
        assert ref_expected == reference_files(ROOT)
        assert receipt["harness_files"] == {name: sha((ROOT / "packaging" / name).read_bytes()) for name in receipt["harness_files"]}
        receipt.update(status="PASS", exact_inputs_and_installed_bytes_unchanged=True)
        code = 0
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[-4000:]}
        report_path = work / "client-report.json"
        if report_path.exists():
            receipt["client"] = json.loads(report_path.read_text())
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        exported.mkdir(mode=0o700)
        for directory in ("reports", "wheels"):
            if (work / directory).exists():
                shutil.copytree(work / directory, exported / directory)
        receipt["exported_evidence"] = str(exported)
        receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(output, receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(output), "profile": args.profile}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
