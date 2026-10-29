"""Installed public reference SDK preparation/fresh/update с точным SDK Git input.

Новый отдельный профиль, старый maintenance harness не меняет. Все продуктовые
действия идут через public InstallerAPI и trusted NativeEngine opt-in хоста.
Evidence экспортируется до выхода, поэтому Docker runner можно запускать --rm.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import importlib.metadata
import os
import platform
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
MAINTENANCE_PROFILES = {"repair", "reinstall"}
RECOVERY_PROFILES = {
    "recovery-restore": ("restore", None),
    "recovery-rollback": ("rollback", None),
    "recovery-lost-ack": ("rollback", "lost_ack"),
    "recovery-missing-ledger": ("rollback", "missing_ledger"),
    "uninstall-restore": ("restore", None),
    "recovery-root-resume": ("restore", None),
    "recovery-stage-resume": ("restore", None),
}
UPDATE_FAULTS = {
    "update-lost-ack": "lost_ack",
    "update-tampered-backup": "tampered_backup",
    "update-missing-ledger": "missing_ledger",
    "update-foreign-backup": "foreign_backup",
    "update-live-data-drift": "live_data_drift",
    "update-candidate-health": "candidate_health",
    "update-candidate-cleanup": "candidate_cleanup",
}


_STAGES = {"setup", "source_snapshot", "wheel_build", "sdk_environment", "product_fixture",
           "product_import_probe", "signed_payloads", "installed_client", "verify_inputs"}
_REASONS = {"command_failed", "process_signal", "python_shared_library_missing", "shared_library_missing",
            "python_stdlib_missing", "python_module_missing", "execution_permission_denied",
            "executable_format_invalid", "python_assertion_failed", "python_ssl_import_failed", "python_sqlite_import_failed"}


def _command_failure_reason(returncode, log):
    """Fixed buckets only: stdout never reproduces a command, path or output."""
    value = log.lower()
    if "error while loading shared libraries:" in value:
        return "python_shared_library_missing" if "libpython" in value else "shared_library_missing"
    if "no module named 'encodings'" in value or "failed to get the python codec" in value:
        return "python_stdlib_missing"
    if "importerror:" in value or "modulenotfounderror:" in value:
        if "_ssl" in value:
            return "python_ssl_import_failed"
        if "_sqlite3" in value:
            return "python_sqlite_import_failed"
    if "modulenotfounderror:" in value or "no module named" in value:
        return "python_module_missing"
    if "permission denied" in value or "operation not permitted" in value:
        return "execution_permission_denied"
    if "exec format error" in value:
        return "executable_format_invalid"
    if "assertionerror" in value:
        return "python_assertion_failed"
    return "process_signal" if returncode < 0 else "command_failed"


def _safe_failure_summary(error, commands, stage, *, client_report=None):
    last = commands[-1] if commands else {}
    code = last.get("exit_code")
    code = code if type(code) is int else None
    if code is not None and code != 0:
        reason = last.get("failure_reason_class")
        reason = reason if reason in _REASONS else "command_failed"
        error_class = "command_failed"
    elif isinstance(error, subprocess.TimeoutExpired):
        reason, error_class = "command_timeout", "command_timeout"
    elif isinstance(error, ModuleNotFoundError):
        reason, error_class = "python_module_missing", "python_import"
    elif isinstance(error, AssertionError):
        reason = ("unexpected_runtime_symlink" if str(error) == "Неожиданная ссылка product runtime"
                  else "validation_assertion_failed")
        error_class = "validation_assertion"
    elif isinstance(error, OSError):
        reason, error_class = "filesystem_operation_failed", "filesystem_error"
    else:
        reason, error_class = "harness_operation_failed", "unexpected_error"
    result = {"stage": stage if stage in _STAGES else "setup", "command_index": len(commands) or None,
              "exit_code": code, "reason_class": reason, "error_class": error_class}
    if stage == "installed_client":
        result["client"] = _safe_client_summary(client_report)
    return result


_CLIENT_TYPES = frozenset({"InstallerError", "AssertionError", "RuntimeError", "ValueError", "OSError",
    "FileNotFoundError", "PermissionError", "TimeoutExpired", "ModuleNotFoundError", "ImportError",
    "JSONDecodeError", "OperationalError", "KeyError", "TypeError"})
_CLIENT_ACTIONS = frozenset({"install", "update", "restore", "rollback", "start", "stop", "backup",
    "repair", "reinstall", "uninstall"})
_CLIENT_STATUSES = frozenset({"PASS", "FAIL", "SUCCEEDED", "FAILED", "RECOVERY_REQUIRED", "FAILED_ROLLED_BACK",
    "QUEUED", "RUNNING", "CANCELED", "AWAITING_USER", "WAITING_REBOOT"})


def _catalog_error_codes():
    try:
        values = json.loads((ROOT / "homensai_install/catalog/errors.v1.json").read_text(encoding="utf-8"))["errors"]
        return frozenset(key for key in values if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", key))
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset()


_CATALOG_ERROR_CODES = _catalog_error_codes()


def _fixed(value, allowed):
    return value if type(value) is str and value in allowed else None


def _safe_client_summary(report):
    """Только catalog codes, фиксированные types/actions и вычисленный counter."""
    if type(report) is not dict:
        return {"available": False}
    error = report.get("error")
    error = error if type(error) is dict else {}
    observations = report.get("observations")
    observations = observations if type(observations) is list else []
    actions = report.get("actions")
    actions = actions if type(actions) is list else []
    selected = [report["primary_last_action"]] if "primary_last_action" in report else actions[-1024:]
    last = None
    for item in reversed(selected):
        if type(item) is dict and _fixed(item.get("action"), _CLIENT_ACTIONS) is not None:
            last = {"action": item["action"], "status": _fixed(item.get("status"), _CLIENT_STATUSES),
                    "error_code": _fixed(item.get("error_code"), _CATALOG_ERROR_CODES)}
            break
    lines = error.get("own_source_lines")
    lines = [number for number in lines[:16] if type(number) is int and 1 <= number <= 10000] if type(lines) is list else []
    return {"available": True, "status": _fixed(report.get("status"), _CLIENT_STATUSES),
        "error_code": _fixed(error.get("code"), _CATALOG_ERROR_CODES),
        "error_type": _fixed(error.get("type"), _CLIENT_TYPES), "own_source_lines": lines,
        "passed_cases": min(1024, sum(type(item) is dict and item.get("status") == "PASS" for item in observations)),
        "last_safe_action": last}


def _read_client_report(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None
        result = json.loads(raw)
        return result if type(result) is dict else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def _probe_product_imports(work, command):
    # Pure imports before signing/native actions, with the same sanitized
    # command environment. No host loader variables or allowlist changes.
    return command([work / "payload/runtime/bin/python", "-I", "-B", "-c",
        "import ssl,sqlite3;import homensai_reference.adapter;print('product_imports_ok')"], cwd=work / "payload")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--sdk-revision", required=True)
    parser.add_argument("--source-git-dir", type=Path)
    parser.add_argument("--profile", choices=("preparation", "fresh", "update-readiness", "update", *UPDATE_FAULTS, *RECOVERY_PROFILES, *sorted(MAINTENANCE_PROFILES)), default="preparation")
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
        "commands": [], "scope": "installed public SDK reference " + args.profile + ", Linux/native/user DEV runner",
        "sdk_source_revision": args.sdk_revision, "environment": {"python": sys.version, "platform": platform.platform(), "uid": os.getuid(),
            "build_versions": {name: importlib.metadata.version(name) for name in ("pip", "setuptools", "packaging", "cryptography")},
            "network_downloads": False, "venv_system_site_packages": False, "work_dir": str(work),
            "runtime_closure": "signed copied DEV ELF; borrowed runner stdlib/libpython/OS libraries"},
        "not_run": [("Adapter restore/rollback and the other maintenance action" if args.profile in MAINTENANCE_PROFILES else
                     "Adapter repair/reinstall" if args.profile in {"uninstall-restore", "recovery-root-resume", "recovery-stage-resume"} else
                     "Adapter repair/reinstall/uninstall" if args.profile in RECOVERY_PROFILES else
                     "Adapter restore/rollback/repair/uninstall" if args.profile in {"update", *UPDATE_FAULTS} else
                     "Adapter update/restore/rollback/repair/uninstall"), "Clean Ubuntu/Windows/Docker/service/reboot",
                    "Standalone runtime/security/license closure", "GUI", "Production signing", "Full G1-G5"],
        "harness_files": {name: sha((ROOT / "packaging" / name).read_bytes()) for name in
            ("check_installed_reference_lifecycle.py", "installed_reference_lifecycle_client.py", "check_installed_product_adapter.py",
             "check_installed_sdk.py", "check_installed_prerequisites.py")}}
    code = 1
    stage = "source_snapshot"

    def command(argv, cwd=work):
        record = {"argv": [str(item) for item in argv], "cwd": str(cwd)}
        receipt["commands"].append(record)
        completed = subprocess.run(record["argv"], cwd=cwd, env=environment, capture_output=True, text=True, timeout=180)
        log = completed.stdout + completed.stderr
        (work / "reports" / ("command-" + str(len(receipt["commands"])) + ".log")).write_text(log, encoding="utf-8")
        record.update(exit_code=completed.returncode, log_sha256=sha(log.encode()))
        if completed.returncode:
            record["failure_reason_class"] = _command_failure_reason(completed.returncode, log)
            raise RuntimeError("Команда " + str(len(receipt["commands"])) + ": " + log[-2500:])
        return completed.stdout

    try:
        environment["SOURCE_DATE_EPOCH"] = command([*git, "show", "-s", "--format=%ct", args.sdk_revision], ROOT).strip()
        receipt["environment"]["source_date_epoch"] = environment["SOURCE_DATE_EPOCH"]
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
        stage = "wheel_build"
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
        stage = "sdk_environment"
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
        stage = "product_fixture"
        config, _, product_site = fixture(work, reference_wheel, command)
        check_reference_wheel(reference_wheel, ref_expected, product_site)
        stage = "product_import_probe"
        _probe_product_imports(work, command)
        stage = "signed_payloads"
        payload = work / "payload"
        descriptor = json.loads((payload / "installer/adapter.json").read_text())
        descriptor["capabilities"].update({name: {"mutating": mutating, "idempotency": "application" if mutating else "read_only"}
                                            for name, mutating in (("migration_plan", False), ("migration_apply", True))})
        if args.profile in {*RECOVERY_PROFILES, *MAINTENANCE_PROFILES}:
            descriptor["capabilities"].update({name: {"mutating": True, "idempotency": "application"}
                                               for name in ("restore", "post_restore")})
        descriptor["environment_allowlist"].append("HOMENSAI_CONFIG_HOME")
        write_json(payload / "installer/adapter.json", descriptor)
        manifest = json.loads((work / "manifest.json").read_text())
        if args.profile in {"fresh", "update-readiness", "update", *UPDATE_FAULTS, *RECOVERY_PROFILES, *MAINTENANCE_PROFILES}:
            (payload / "forbidden_legacy_migration.py").write_text("import os\nfrom pathlib import Path\n(Path(os.environ['HOMENSAI_DATA_HOME'])/'FORBIDDEN_LEGACY_MIGRATION').write_text('unexpected legacy hook')\nraise SystemExit(87)\n", encoding="utf-8")
            manifest["migrations"]["cmd"] = ["python", "-I", "-B", "forbidden_legacy_migration.py"]
            if args.profile in {*RECOVERY_PROFILES, *MAINTENANCE_PROFILES}:
                (payload / "forbidden_legacy_restore.py").write_text("import os\nfrom pathlib import Path\n(Path(os.environ['HOMENSAI_DATA_HOME'])/'FORBIDDEN_LEGACY_RESTORE').write_text('unexpected legacy restore hook')\nraise SystemExit(88)\n", encoding="utf-8")
                manifest["restore"]["cmd"] = ["python", "-I", "-B", "forbidden_legacy_restore.py"]
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
                      bundle_options=bundle_options, profile=("recovery" if args.profile in RECOVERY_PROFILES else
                          "update" if args.profile in UPDATE_FAULTS else args.profile),
                      recovery_action=RECOVERY_PROFILES.get(args.profile, (None, None))[0],
                      recovery_fault=RECOVERY_PROFILES.get(args.profile, (None, None))[1],
                      uninstall_before_restore=args.profile in {"uninstall-restore", "recovery-root-resume", "recovery-stage-resume"},
                      recovery_root_resume=args.profile == "recovery-root-resume",
                      recovery_stage_resume=args.profile == "recovery-stage-resume",
                      fault_profile=UPDATE_FAULTS.get(args.profile),
                      runtime_sha256=signed_bundles["1"]["runtime/bin/python"])
        config_path = work / "client-config.json"
        write_json(config_path, config)
        config_path.chmod(0o600)
        client = work / "external_client.py"
        shutil.copy2(ROOT / "packaging/installed_reference_lifecycle_client.py", client)
        stage = "installed_client"
        command([python, "-I", "-B", client, config_path])
        receipt["client"] = json.loads((work / "client-report.json").read_text())
        assert receipt["client"]["status"] == "PASS"
        stage = "verify_inputs"
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
        report = _read_client_report(work / "client-report.json")
        receipt["safe_failure"] = _safe_failure_summary(error, receipt["commands"], stage, client_report=report)
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[-4000:]}
        if report is not None:
            receipt["client"] = report
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        exported.mkdir(mode=0o700)
        for directory in ("reports", "wheels"):
            if (work / directory).exists():
                shutil.copytree(work / directory, exported / directory)
        receipt["exported_evidence"] = str(exported)
        receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(output, receipt)
    summary = {"status": receipt["status"], "profile": args.profile}
    if receipt["status"] == "PASS":
        summary["receipt"] = str(output)
    else:
        summary["failure"] = receipt.get("safe_failure", {
            "stage": "setup", "command_index": None, "exit_code": None,
            "reason_class": "harness_operation_failed", "error_class": "unexpected_error"})
    print(json.dumps(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
