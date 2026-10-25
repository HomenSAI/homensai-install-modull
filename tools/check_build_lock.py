"""Offline build-lock checks; no download, installation or license approval.

The reviewed lock and provenance snapshot are trusted repository inputs. This
checks their agreement and PEP 508 closure, not the trustworthiness of PyPI or
the author of a wheel. Optional cached-wheel checks compare actual bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import stat
import sys
import sysconfig
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT = 2 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HASH_OPTION = re.compile(r"\s+--hash=sha256:([0-9a-f]{64})(?=\s|$)")
OPTIONS = {"--index-url https://pypi.org/simple", "--only-binary=:all:", "--require-hashes"}
TARGETS = {
    "linux-x64": {"os_name": "posix", "sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64"},
    "windows-x64": {"os_name": "nt", "sys_platform": "win32", "platform_system": "Windows", "platform_machine": "AMD64"},
}


class InvalidLock(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise InvalidLock(message)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def read_snapshot(path):
    raw = Path(path).read_bytes()
    _require(len(raw) <= MAX_INPUT, "Snapshot is too large")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(InvalidLock("Non-finite JSON")))


def parse_lock(text):
    """Only exact pins, optional PEP 508 markers and SHA-256 are accepted."""
    _require(len(text.encode("utf-8")) <= MAX_INPUT, "Lock is too large")
    lines, pending = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            _require(not pending, "Interrupted requirement continuation")
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
        else:
            lines.append(pending + line)
            pending = ""
    _require(not pending, "Unterminated requirement continuation")
    options, result = set(), {}
    for line in lines:
        if line.startswith("--"):
            _require(line in OPTIONS and line not in options, "Unexpected or duplicate pip option")
            options.add(line)
            continue
        hashes = HASH_OPTION.findall(line)
        _require(hashes and len(set(hashes)) == len(hashes), "Missing or duplicate wheel hashes")
        requirement = Requirement(HASH_OPTION.sub("", line).strip())
        specs = list(requirement.specifier)
        _require(not requirement.url and not requirement.extras and len(specs) == 1,
                 "Only exact dependency pins without extras or URLs are supported")
        spec = specs[0]
        _require(spec.operator == "==" and "*" not in spec.version, "Dependency must have one exact version")
        name = canonicalize_name(requirement.name)
        _require(name not in result, "Duplicate dependency pin")
        result[name] = {"version": str(Version(spec.version)),
                        "marker": str(requirement.marker) if requirement.marker else None,
                        "hashes": set(hashes)}
    _require(options == OPTIONS, "All required pip safety options must be present")
    _require(result, "Empty dependency lock")
    return result


def marker_environment(target, python_version="3.14.9"):
    _require(target in TARGETS, "Unsupported target")
    _require(Version(python_version).release[:2] == (3, 14), "Only CPython 3.14 is locked")
    return {**TARGETS[target], "python_version": "3.14", "python_full_version": python_version,
            "implementation_name": "cpython", "implementation_version": python_version,
            "platform_python_implementation": "CPython", "platform_release": "",
            "platform_version": "", "extra": ""}


def _active(marker, environment):
    return not marker or Requirement("placeholder; " + marker).marker.evaluate(environment)


def _requirements_identity(values):
    _require(isinstance(values, list) and len(values) <= 1000, "Invalid Requires-Dist list")
    result = set()
    for value in values:
        _require(isinstance(value, str) and len(value) <= 8192, "Invalid Requires-Dist value")
        requirement = Requirement(value)
        result.add((canonicalize_name(requirement.name), str(requirement.specifier),
                    str(requirement.marker) if requirement.marker else None,
                    requirement.url, tuple(sorted(requirement.extras))))
    return result


def _wheel_targets(filename):
    _, _, _, tags = parse_wheel_filename(filename)
    result = set()
    for tag in tags:
        interpreter, abi = tag.interpreter, tag.abi
        compatible = ((abi == "none" and interpreter == "py3")
                      or (abi == "abi3" and re.fullmatch(r"cp3[0-9]+", interpreter)
                          and 2 <= int(interpreter[3:]) <= 14)
                      or (interpreter == "cp314" and abi == "cp314"))
        if not compatible:
            continue
        if tag.platform == "any":
            result.update(TARGETS)
        elif tag.platform == "win_amd64":
            result.add("windows-x64")
        elif tag.platform.startswith("manylinux") and tag.platform.endswith("_x86_64"):
            result.add("linux-x64")
    return result


def active_packages(snapshot, target, python_version="3.14.9"):
    environment = marker_environment(target, python_version)
    return {p["name"]: p for p in snapshot["packages"] if _active(p.get("marker"), environment)}


def check_closure(snapshot, target, python_version):
    environment = marker_environment(target, python_version)
    active = active_packages(snapshot, target, python_version)
    visited, queue = set(), list(snapshot["roots"])
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        _require(name in active, f"Missing active dependency: {name} on {target}")
        package = active[name]
        visited.add(name)
        _require(SpecifierSet(package.get("requires_python") or "").contains(python_version),
                 f"Python version is unsupported by {name}")
        files = [item for item in package["files"] if target in item["targets"]]
        _require(files, f"No compatible wheel: {name} on {target}")
        for item in files:
            _require(SpecifierSet(item.get("requires_python") or "").contains(python_version),
                     f"Python version is unsupported by wheel: {name}")
            for value in item["wheel_requires_dist"]:
                dependency = Requirement(value)
                if dependency.marker and not dependency.marker.evaluate(environment):
                    continue
                _require(not dependency.url and not dependency.extras,
                         "Active URL dependencies or extras require a separately reviewed lock")
                child = canonicalize_name(dependency.name)
                _require(child in active, f"Missing active dependency: {child} required by {name} on {target}")
                _require(dependency.specifier.contains(active[child]["version"]),
                         f"Dependency version conflict: {child} required by {name}")
                queue.append(child)
    _require(visited == set(active), "Unnecessary or unreachable package in lock")
    return sorted(visited)


def validate(snapshot, lock_text):
    _require(isinstance(snapshot, dict) and snapshot.get("schema_version") == 1, "Invalid snapshot schema")
    _require(snapshot.get("python") == "3.14-standard", "Unsupported Python build flavor")
    _require(set(snapshot.get("targets", {})) == set(TARGETS), "Unsupported target scope")
    for target, facts in TARGETS.items():
        _require(all(snapshot["targets"][target].get(key) == value for key, value in facts.items()),
                 "Target marker environment does not match reviewed scope")
    packages = snapshot.get("packages")
    _require(isinstance(packages, list) and 0 < len(packages) <= 1000, "Invalid package list")
    pins, names, total_files = parse_lock(lock_text), set(), 0
    for package in packages:
        name, version = package["name"], package["version"]
        _require(name == canonicalize_name(name) and name not in names, "Noncanonical or duplicate package name")
        names.add(name)
        _require(name in pins and str(Version(version)) == pins[name]["version"], "Lock and snapshot versions differ")
        marker = str(Requirement("placeholder; " + package["marker"]).marker) if package.get("marker") else None
        _require(marker == pins[name]["marker"], "Lock and snapshot markers differ")
        _require(package["metadata_url"] == f"https://pypi.org/pypi/{name}/{version}/json", "Nonofficial PyPI metadata URL")
        _require(bool(SHA256.fullmatch(package["metadata_sha256"])), "Missing raw metadata digest")
        metadata_dependencies = _requirements_identity(package["requires_dist"])
        files = package["files"]
        _require(isinstance(files, list) and 0 < len(files) <= 20, "Invalid wheel list")
        hashes, filenames = set(), set()
        for item in files:
            filename = item["filename"]
            _require(isinstance(filename, str) and "/" not in filename and "\\" not in filename
                     and filename.endswith(".whl") and filename not in filenames, "Invalid or duplicate wheel filename")
            filenames.add(filename)
            wheel_name, wheel_version, _, _ = parse_wheel_filename(filename)
            _require(canonicalize_name(wheel_name) == name and str(wheel_version) == version, "Wheel identity mismatch")
            url = urlsplit(item["url"])
            _require(url.scheme == "https" and url.hostname == "files.pythonhosted.org" and url.port in (None, 443)
                     and not url.username and not url.password and not url.query and not url.fragment
                     and unquote(url.path.rsplit("/", 1)[-1]) == filename, "Nonofficial or ambiguous wheel URL")
            _require(type(item["size"]) is int and 0 < item["size"] <= 256 * 1024 * 1024, "Invalid wheel size")
            _require(bool(SHA256.fullmatch(item["sha256"])) and bool(SHA256.fullmatch(item["wheel_metadata_sha256"])),
                     "Invalid wheel or METADATA digest")
            _require(item["yanked"] is False and item["download_verified_now"] is True, "Unverified or yanked wheel")
            actual_targets = _wheel_targets(filename)
            _require(actual_targets and set(item["targets"]) == actual_targets, "Wheel tags are incompatible with target scope")
            _require(_requirements_identity(item["wheel_requires_dist"]) == metadata_dependencies,
                     "Wheel METADATA and index dependency declarations differ")
            hashes.add(item["sha256"])
            total_files += 1
        _require(hashes == pins[name]["hashes"], "Lock and snapshot file hashes differ")
    _require(set(pins) == names, "Lock and snapshot package sets differ")
    roots = snapshot.get("roots")
    _require(isinstance(roots, list) and len(set(roots)) == len(roots) and set(roots) <= names,
             "Invalid root dependencies")
    versions = snapshot.get("python_full_versions_checked")
    _require(isinstance(versions, list) and versions and all(isinstance(v, str) for v in versions),
             "Missing Python versions for dependency checks")
    matrix = []
    for target in TARGETS:
        for version in versions:
            matrix.append({"target": target, "python": version,
                           "active_packages": check_closure(snapshot, target, version)})
    return {"status": "PASS", "check": "offline_lock_consistency_and_dependency_closure",
            "pinned_packages": len(packages), "selected_wheel_files": total_files,
            "matrix": matrix, "license_approval": False, "runtime_acceptance": "NOT_CHECKED"}


def verify_wheel_cache(snapshot, directory):
    """Read and hash cached wheels only; never extract or import their code."""
    directory = Path(directory)
    count = 0
    for package in snapshot["packages"]:
        for item in package["files"]:
            path = directory / item["filename"]
            info = path.lstat()
            _require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "Wheel cache entry must be a regular file")
            _require(info.st_size == item["size"], "Cached wheel size mismatch")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            _require(digest.hexdigest() == item["sha256"], "Cached wheel SHA-256 mismatch")
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if len(n.split("/")) == 2 and n.endswith(".dist-info/METADATA")]
                _require(len(names) == 1 and archive.getinfo(names[0]).file_size <= MAX_INPUT, "Invalid top-level wheel METADATA")
                _require(hashlib.sha256(archive.read(names[0])).hexdigest() == item["wheel_metadata_sha256"],
                         "Cached wheel METADATA digest mismatch")
            count += 1
    return {"status": "PASS", "wheel_files_verified": count, "code_executed": False}


def check_installed(snapshot):
    _require(platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 14)
             and not sysconfig.get_config_var("Py_GIL_DISABLED"), "Installed Python is outside lock scope")
    _require(platform.machine().lower() in ("x86_64", "amd64"), "Installed architecture is outside lock scope")
    target = {"linux": "linux-x64", "win32": "windows-x64"}.get(sys.platform)
    _require(target is not None, "Installed platform is outside lock scope")
    version = platform.python_version()
    check_closure(snapshot, target, version)
    active = active_packages(snapshot, target, version)
    # Source builds create local *.egg-info in the checkout. Those are build
    # outputs, not installed distributions in this interpreter's isolated venv.
    # Restrict metadata inspection to its own installation paths, not cwd.
    sites = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    installed = {canonicalize_name(d.metadata["Name"]): d.version
                 for d in importlib.metadata.distributions(path=sites)}
    for name, package in active.items():
        _require(installed.get(name) == package["version"], f"Installed version differs from lock: {name}")
    _require(set(installed) <= set(active) | {"pip"}, "Unexpected distribution in build environment")
    return {"status": "PASS", "target": target, "python": version,
            "packages": {name: installed[name] for name in sorted(active)},
            "bootstrap_pip_version": installed.get("pip"),
            "note": "pip is a recorded bootstrap tool, not a redistributed runtime dependency"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "packaging/build-dependencies.json")
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements-build.lock")
    parser.add_argument("--wheel-dir", type=Path)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args(argv)
    try:
        snapshot = read_snapshot(args.snapshot)
        result = validate(snapshot, args.lock.read_text(encoding="utf-8"))
        if args.wheel_dir:
            result["cached_wheels"] = verify_wheel_cache(snapshot, args.wheel_dir)
        if args.installed:
            result["installed_environment"] = check_installed(snapshot)
    except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "FAIL", "reason": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
