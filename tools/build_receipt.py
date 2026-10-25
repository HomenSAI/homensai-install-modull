"""Write an unsigned local source-build receipt without reading environment secrets.

This records observed inputs and outputs. It is not a SLSA attestation, a
reproducible-build proof, a license approval, or an installer acceptance result.
No download, source execution, package installation, signing or upload occurs.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_build_lock import check_installed, read_snapshot, validate

MAX_FILE = 2 * 1024 * 1024 * 1024
INPUTS = (
    "requirements-build.lock", "pyproject.toml", "reference/pyproject.toml",
    "packaging/build-dependencies.json", "packaging/ci-toolchain.json",
    "packaging/runtime-source-receipt.json", "tools/check_build_lock.py",
    "tools/build_receipt.py", ".github/workflows/source-tests.yml",
)


class InvalidReceipt(ValueError):
    pass


def digest_file(path):
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_size > MAX_FILE:
        raise InvalidReceipt("Receipt inputs must be bounded regular files without symlinks")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise InvalidReceipt("Input changed while hashing; retry after the build is idle")
    return {"sha256": digest.hexdigest(), "size": before.st_size}


def _git(root, arguments):
    executable = shutil.which("git")
    if not executable:
        raise InvalidReceipt("Git is required to identify source inputs")
    result = subprocess.run([executable, "--no-optional-locks", "-c", "core.fsmonitor=false", *arguments],
                            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=30, shell=False)
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise InvalidReceipt("Read-only Git source inspection failed")
    return result.stdout


def source_snapshot(root, *, allow_dirty_dev=False):
    root = Path(root).resolve(strict=True)
    commit = _git(root, ["rev-parse", "HEAD"]).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise InvalidReceipt("Invalid source commit identifier")
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all", "-z"])
    dirty = bool(status)
    if dirty and not allow_dirty_dev:
        raise InvalidReceipt("Source tree is dirty; commit reviewed inputs or explicitly use --allow-dirty-dev")
    names = _git(root, ["ls-files", "--cached", "-z"]).decode("utf-8").split("\0")
    files = []
    for name in sorted(set(n for n in names if n)):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or not relative.parts:
            raise InvalidReceipt("Unexpected source filename")
        path = root.joinpath(*relative.parts)
        if not path.resolve(strict=True).is_relative_to(root):
            raise InvalidReceipt("Source file resolves outside repository")
        files.append({"path": name, **digest_file(path)})
    after = _git(root, ["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all", "-z"])
    if after != status:
        raise InvalidReceipt("Source state changed during receipt creation")
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"commit": commit, "dirty": dirty, "allow_dirty_dev": allow_dirty_dev,
            "tracked_file_count": len(files), "tracked_tree_sha256": hashlib.sha256(canonical).hexdigest(),
            "tracked_files": files, "untracked_files_included": False,
            "scope": "Git-tracked regular files as observed; dirty DEV inputs are not a release source attestation"}


def runtime_archive_receipt(path, expected_sha256, source_url):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise InvalidReceipt("Runtime archive requires an independently reviewed SHA-256")
    url = urlsplit(source_url or "")
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise InvalidReceipt("Runtime source must be an HTTPS URL without credentials or fragments")
    observed = digest_file(path)
    if observed["sha256"] != expected_sha256:
        raise InvalidReceipt("Runtime archive SHA-256 mismatch")
    return {"filename": Path(path).name, **observed, "declared_source_url": source_url,
            "archive_bytes_verified_now": True, "historical_download_origin": "NOT_PROVEN",
            "extracted_runtime_tree": "NOT_COMPARED", "publisher_attestation": "NOT_CHECKED"}


def create_receipt(root, artifact_dirs, *, allow_dirty_dev=False, runtime_archive=None,
                   runtime_sha256=None, runtime_source_url=None):
    root = Path(root).resolve(strict=True)
    snapshot = read_snapshot(root / "packaging/build-dependencies.json")
    lock_check = validate(snapshot, (root / "requirements-build.lock").read_text(encoding="utf-8"))
    installed = check_installed(snapshot)
    source = source_snapshot(root, allow_dirty_dev=allow_dirty_dev)
    inputs = [{"path": name, **digest_file(root / name)} for name in INPUTS]
    artifacts = []
    for directory in artifact_dirs:
        directory = Path(directory).resolve(strict=True)
        wheels = sorted(directory.glob("*.whl"))
        if not wheels:
            raise InvalidReceipt("Each artifact directory must contain a built wheel")
        for path in wheels:
            label = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
            artifacts.append({"path": label, "kind": "wheel", **digest_file(path)})
    if not artifacts or len({a["path"] for a in artifacts}) != len(artifacts):
        raise InvalidReceipt("No artifacts or duplicate artifact identifiers")
    runtime_args = (runtime_archive, runtime_sha256, runtime_source_url)
    if any(value is not None for value in runtime_args) and not all(value is not None for value in runtime_args):
        raise InvalidReceipt("Runtime archive, expected digest and source URL must be supplied together")
    runtime = runtime_archive_receipt(*runtime_args) if runtime_archive is not None else {"status": "NOT_PROVIDED"}
    return {"schema_version": 1, "kind": "unsigned-local-source-build-receipt",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "producer": "tools/build_receipt.py", "source": source, "inputs": inputs,
            "toolchain": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                          "platform": sys.platform, "machine": platform.machine(),
                          "gil_disabled": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
                          "installed_environment": installed},
            "lock_check": lock_check, "artifacts": artifacts, "runtime_input": runtime,
            "assertions": {"inputs_outputs_observed_together": True, "build_execution_attested": False,
                           "reproducible_rebuild": "NOT_CHECKED", "environment_secrets_collected": False,
                           "production_signature": "NOT_PRESENT", "license_gate": "NOT_RUN",
                           "native_installer_acceptance": "NOT_RUN", "docker_acceptance": "NOT_RUN"}}


def write_receipt(path, value):
    path = Path(path)
    if path.is_symlink():
        raise InvalidReceipt("Receipt output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".receipt-", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, action="append", required=True)
    parser.add_argument("--allow-dirty-dev", action="store_true")
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--runtime-sha256")
    parser.add_argument("--runtime-source-url")
    args = parser.parse_args(argv)
    try:
        receipt = create_receipt(ROOT, args.artifact_dir, allow_dirty_dev=args.allow_dirty_dev,
                                 runtime_archive=args.runtime_archive, runtime_sha256=args.runtime_sha256,
                                 runtime_source_url=args.runtime_source_url)
        write_receipt(args.output, receipt)
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAIL", "reason": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "RECORDED", "source_commit": receipt["source"]["commit"],
                      "dirty": receipt["source"]["dirty"], "artifacts": receipt["artifacts"],
                      "assertions": receipt["assertions"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
