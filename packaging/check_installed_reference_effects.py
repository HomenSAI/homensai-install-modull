"""Installed reference migration hooks: настоящий nonzero до/после COMMIT.

SDK собирается из точного Git commit, reference — из проверяемых текущих bytes.
Trusted exact-request authorizer синтетический. Это hook/transport acceptance,
не настоящий engine fresh/update и не проверка архивного proof.
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
FAULT_WRAPPER = '''import json,sys
from homensai_reference import adapter
request=json.load(sys.stdin)
if request['hook']=='migration_apply' and request['operation_id']=='op_pre':sys.exit(78)
result=adapter.execute(request)
response={key:request[key] for key in ('protocol_version','product_id','request_id','request_digest','hook')}
response.update(status='ok',result=result)
print(adapter.canonical(response),flush=True)
if request['hook']=='migration_apply' and request['operation_id']=='op_post':sys.exit(79)
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--sdk-revision", required=True, help="Точный полный Git SHA SDK с default-deny authorizer")
    parser.add_argument("--source-git-dir", type=Path)
    args = parser.parse_args(argv)
    work, output = args.work_dir.resolve(), args.receipt.absolute()
    if (work.exists() or output.exists() or work.is_relative_to(ROOT) or ROOT.is_relative_to(work)
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
        "production_ready": False, "gate_closed": False, "author": "Serhii Khomenko", "commands": [],
        "scope": "installed reference migration hook transport with synthetic trusted authorizer; actual subprocess nonzero faults",
        "sdk_source_revision": args.sdk_revision, "environment": {"python": sys.version, "uid": os.getuid(),
            "network_downloads": False, "venv_system_site_packages": False, "work_dir": str(work),
            "runtime_closure": "signed copied DEV ELF; borrowed container stdlib/libpython/OS libraries"},
        "authorizer": "SYNTHETIC exact-request fixture, NOT real archive/fresh proof",
        "not_run": ["Actual engine fresh/update", "Real backup/fresh receipt authorization", "Process kill/power loss",
                    "Standalone runtime", "Clean Ubuntu/Windows/Docker/services", "GUI", "Production signing/licenses", "Full G1-G5"],
        "harness_files": {name: sha((ROOT / "packaging" / name).read_bytes()) for name in
            ("check_installed_reference_effects.py", "installed_reference_effects_client.py", "check_installed_product_adapter.py",
             "check_installed_sdk.py", "check_installed_prerequisites.py")}}
    code = 1

    def command(argv, cwd=work):
        record = {"argv": [str(item) for item in argv], "cwd": str(cwd)}
        receipt["commands"].append(record)
        completed = subprocess.run(record["argv"], cwd=cwd, env=environment, capture_output=True, text=True, timeout=150)
        text = completed.stdout + completed.stderr
        (work / "reports" / ("command-" + str(len(receipt["commands"])) + ".log")).write_text(text, encoding="utf-8")
        record.update(exit_code=completed.returncode, log_sha256=sha(text.encode()))
        if completed.returncode:
            raise RuntimeError("Команда " + str(len(receipt["commands"])) + ": " + text[-2500:])
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
        environment_path = work / "environment"
        command([sys.executable, "-I", "-B", "-m", "venv", environment_path])
        python = environment_path / "bin/python"
        command([python, "-I", "-B", "-c", "import importlib.util;assert all(importlib.util.find_spec(n) is None for n in ('homensai_install','cryptography','homensai_reference'))"])
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir",
                 "--require-hashes", "--only-binary=:all:", "--find-links", lock.parent, "-r", lock])
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir", wheel])
        site = Path(command([python, "-I", "-B", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"]).strip())
        check_wheel(wheel, expected, site)
        for dependency in dependencies:
            dependency["installed_verification"] = check_installed_dependency(lock.parent / dependency["filename"], site)
        config, _, product_site = fixture(work, reference_wheel, command)
        payload = work / "payload"
        descriptor = json.loads((payload / "installer/adapter.json").read_text())
        descriptor["capabilities"].update({name: {"mutating": mutating, "idempotency": "application" if mutating else "read_only"}
                                            for name, mutating in (("migration_plan", False), ("migration_apply", True))})
        descriptor["environment_allowlist"].append("HOMENSAI_CONFIG_HOME")
        descriptor["command"] = ["runtime/bin/python", "-I", "-B", "fault_entry.py"]
        write_json(payload / "installer/adapter.json", descriptor)
        (payload / "fault_entry.py").write_text(FAULT_WRAPPER, encoding="utf-8")
        profile = json.loads((payload / "reference-profile.json").read_text())
        profile.update(product_version="2.0.0", database_schema=2)
        write_json(payload / "reference-profile.json", profile)
        # Source Windows bind permissions не подходят direct executable boundary.
        for path in payload.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o700 if path.read_bytes().startswith(b"\x7fELF") else 0o600)
        inventory = json.loads((work / "inventory.json").read_text())
        inventory["release"] = "reference-v2"
        inventory["files"] = {path.relative_to(payload).as_posix(): sha(path.read_bytes()) for path in sorted(payload.rglob("*")) if path.is_file()}
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        private = Ed25519PrivateKey.generate()
        signature = {"algorithm": "ed25519", "key_id": "reference-effect-fault-dev",
                     "signature": base64.b64encode(private.sign(canonical(inventory))).decode()}
        trust = {"schema_version": 1, "keys": {signature["key_id"]:
            base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}}
        del private
        for name, value in (("inventory", inventory), ("signature", signature), ("trust", trust)):
            write_json(work / (name + ".json"), value)
        options = json.loads((work / "options.json").read_text())
        options["release"] = "reference-v2"
        write_json(work / "options.json", options)
        receipt["signed_payload_files"] = inventory["files"]
        # Явный legacy product DB bootstrap, отдельно от adapter fresh path.
        for case in ("denied", "pre", "post"):
            command([payload / "runtime/bin/python", "-I", "-B", "-c",
                "import sys;from pathlib import Path;from homensai_reference.migrate import migrate;from homensai_reference.profile import DEFAULT_PROFILE;migrate(Path(sys.argv[1]),Path(sys.argv[2]),profile=DEFAULT_PROFILE)",
                work / ("data-" + case), work / ("config-" + case)])
        receipt["bootstrap"] = "explicit legacy reference migrate v1 fixture; NOT adapter fresh install"
        config.update(site_packages=str(site), checkout=str(ROOT), client_report=str(work / "client-report.json"))
        config_path = work / "client-config.json"
        write_json(config_path, config)
        config_path.chmod(0o600)
        client = work / "external_client.py"
        shutil.copy2(ROOT / "packaging/installed_reference_effects_client.py", client)
        command([python, "-I", "-B", client, config_path])
        receipt["client"] = json.loads((work / "client-report.json").read_text())
        assert receipt["client"]["status"] == "PASS"
        check_wheel(wheel, expected, site)
        check_reference_wheel(reference_wheel, ref_expected, product_site)
        for dependency in dependencies:
            assert dependency["installed_verification"] == check_installed_dependency(lock.parent / dependency["filename"], site)
        assert ref_expected == reference_files(ROOT)
        assert inventory["files"] == {path.relative_to(payload).as_posix(): sha(path.read_bytes())
            for path in sorted(payload.rglob("*")) if path.is_file()}
        assert receipt["harness_files"] == {name: sha((ROOT / "packaging" / name).read_bytes()) for name in receipt["harness_files"]}
        receipt.update(status="PASS", exact_inputs_and_installed_bytes_unchanged=True)
        code = 0
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[-4000:]}
    receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(output)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
