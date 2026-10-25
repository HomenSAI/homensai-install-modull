"""Воспроизводимая browser-проверка prerequisite uploads из нового installed wheel.

Linux x64 / CPython3.14 / локальный pinned wheelhouse / Chromium+Playwright.
Все данные синтетические; сценарий не подтверждает реальные OS installers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

from check_installed_sdk import canonical, check_wheel, package_files, sha, write_json
from check_installed_prerequisites import security_wheels, check_installed_dependency

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--node-path", required=True)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    if work.exists() or work.is_relative_to(ROOT):
        parser.error("Требуется новый work-dir вне checkout")
    if args.receipt.exists():
        parser.error("Старую квитанцию нельзя перезаписывать")
    work.mkdir(mode=0o700, parents=True)
    reports = work / "reports"
    reports.mkdir(mode=0o700)
    environment = {"PATH": os.defpath + ":/usr/local/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1", "NODE_PATH": args.node_path, "HINSTALL_TEST_BROWSER": str(args.browser.resolve())}
    receipt = {"schema_version": 1, "status": "RUNNING", "checkpoint": "CP-20261025-020", "task_ids": ["HIM-033", "HIM-029", "HIM-035", "HIM-102", "HIM-062", "HIM-063"], "scenario_ids": ["ACC-05", "ACC-07", "ACC-08", "ACC-16", "ACC-23"], "started_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "Installed wheel + настоящий Chromium Linux x64 в Docker; signed synthetic fixture; ru/en/de. Не Windows11/Ubuntu clean-host/native frozen/OS installer/full lifecycle.", "production_ready": False, "commands": [], "environment": {"python": sys.version, "platform": platform.platform(), "browser_path": str(args.browser), "venv_system_site_packages": False}, "work_dir": str(work)}
    def command(argv, *, cwd=work):
        argv = [str(value) for value in argv]
        record = {"argv": argv, "cwd": str(cwd), "started_at_utc": datetime.now(timezone.utc).isoformat()}
        receipt["commands"].append(record)
        output = reports / ("command-" + str(len(receipt["commands"])) + ".log")
        start = time.monotonic()
        with output.open("w", encoding="utf-8") as log:
            result = subprocess.run(argv, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=240)
        record.update(exit_code=result.returncode, duration_seconds=time.monotonic()-start, log=str(output), log_sha256=sha(output.read_bytes()))
        if result.returncode:
            raise RuntimeError("Команда завершилась с ошибкой: " + output.name + ": " + output.read_text(encoding="utf-8")[-3000:])
        return output.read_text(encoding="utf-8")
    server = None
    server_log = None
    exit_code = 1
    try:
        if sys.platform != "linux" or sys.version_info[:2] != (3, 14) or platform.machine() not in {"x86_64", "amd64"}:
            raise RuntimeError("Требуется CPython3.14 Linux x64")
        receipt["source_commit"] = command(["git", "rev-parse", "HEAD"], cwd=ROOT).strip()
        receipt["source_tree"] = command(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).strip()
        receipt["source_status"] = command(["git", "status", "--porcelain=v1"], cwd=ROOT).splitlines()
        receipt["source_diff_sha256"] = sha(command(["git", "diff", "--binary", "HEAD"], cwd=ROOT).encode())
        snapshot = work / "build-source"
        snapshot.mkdir()
        for name in ("homensai_install", "shared"):
            shutil.copytree(ROOT / name, snapshot / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        for name in ("pyproject.toml", "INSTALLER_API.md"):
            shutil.copy2(ROOT / name, snapshot / name)
        expected = package_files(snapshot)
        receipt["source_package_files"] = expected
        receipt["source_package_digest"] = sha(canonical(expected))
        wheels = work / "wheels"
        wheels.mkdir()
        command([sys.executable, "-I", "-B", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-cache-dir", "--no-build-isolation", "--wheel-dir", wheels, snapshot])
        wheel, = wheels.glob("homensai_install_module-*.whl")
        check_wheel(wheel, expected)
        receipt["wheel"] = {"name": wheel.name, "sha256": sha(wheel.read_bytes()), "exact_files_checked": len(expected)}
        dependencies, requirements = security_wheels(args.wheelhouse.resolve(), work / "security-wheels")
        receipt["dependencies"] = dependencies
        command([sys.executable, "-I", "-B", "-m", "venv", work / "environment"])
        python = work / "environment/bin/python"
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir", "--require-hashes", "--find-links", requirements.parent, "-r", requirements])
        command([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir", wheel])
        command([python, "-I", "-B", "-m", "pip", "check"])
        installed = Path(command([python, "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).strip())
        check_wheel(wheel, expected, installed)
        before_dependencies = {item["filename"]: check_installed_dependency(requirements.parent / item["filename"], installed) for item in dependencies}
        scripts = ["prerequisite_upload_browser_fixture.py", "check_prerequisite_upload_browser.cjs"]
        receipt["scripts"] = {}
        for name in scripts:
            shutil.copy2(ROOT / "packaging" / name, work / name)
            receipt["scripts"][name] = sha((work / name).read_bytes())
        template = work / "manifest-template.json"
        shutil.copy2(ROOT / "examples/rent.manifest.json", template)
        receipt["manifest_template_sha256"] = sha(template.read_bytes())
        fixture = work / "fixture"
        server_argv = [str(python), "-I", "-B", str(work / scripts[0]), "--work-dir", str(fixture), "--manifest-template", str(template), "--checkout", str(ROOT)]
        server_log = (reports / "fixture-server.log").open("w", encoding="utf-8")
        receipt["server"] = {"argv": server_argv, "cwd": str(work)}
        server = subprocess.Popen(server_argv, cwd=work, env=environment, stdout=server_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic()+30
        while not (fixture / "url.txt").exists():
            if server.poll() is not None or time.monotonic() > deadline:
                server_log.flush()
                raise RuntimeError("Browser fixture не запустился: " + (reports / "fixture-server.log").read_text(encoding="utf-8")[-3000:])
            time.sleep(.1)
        command([shutil.which("node"), work / scripts[1], fixture / "url.txt", fixture, reports / "browser"])
        receipt["browser"] = json.loads((reports / "browser/browser-upload.json").read_text(encoding="utf-8"))
        check_wheel(wheel, expected, installed)
        after_dependencies = {item["filename"]: check_installed_dependency(requirements.parent / item["filename"], installed) for item in dependencies}
        assert before_dependencies == after_dependencies
        assert package_files(ROOT) == expected, "Package source изменился во время browser прогона"
        receipt["installed_package_and_dependencies_unchanged"] = True
        receipt["package_source_unchanged"] = True
        receipt["status"] = "PASS"
        exit_code = 0
    except Exception as error:
        receipt["status"] = "FAIL"
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[-4000:]}
        browser_receipt = reports / "browser/browser-upload.json"
        if browser_receipt.exists():
            receipt["browser"] = json.loads(browser_receipt.read_text(encoding="utf-8"))
    finally:
        if server is not None:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait(timeout=5)
            receipt["fixture_server_stopped"] = True
        if server_log is not None:
            server_log.close()
            receipt["server_log_sha256"] = sha((reports / "fixture-server.log").read_bytes())
        receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.receipt, receipt)
        print(json.dumps({"status": receipt["status"], "receipt": str(args.receipt)}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
