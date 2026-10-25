"""Offline dependency closure and exact-byte receipt checks."""
import copy
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hinstall_build_lock", ROOT / "tools/check_build_lock.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


@pytest.fixture
def snapshot():
    return checker.read_snapshot(ROOT / "packaging/build-dependencies.json")


def render(snapshot):
    lines = sorted(checker.OPTIONS)
    for item in snapshot["packages"]:
        marker = " ; " + item["marker"] if item.get("marker") else ""
        hashes = " ".join("--hash=sha256:" + h for h in sorted({f["sha256"] for f in item["files"]}))
        lines.append(f"{item['name']}=={item['version']}{marker} {hashes}")
    return "\n".join(lines) + "\n"


def package(snapshot, name):
    return next(p for p in snapshot["packages"] if p["name"] == name)


def test_repository_lock_exactly_matches_official_metadata_snapshot(snapshot):
    result = checker.validate(snapshot, (ROOT / "requirements-build.lock").read_text(encoding="utf-8"))
    assert result["pinned_packages"] == 17
    assert result["selected_wheel_files"] == 22
    assert {(v["target"], v["python"], len(v["active_packages"])) for v in result["matrix"]} == {
        ("linux-x64", "3.14.3", 14), ("linux-x64", "3.14.9", 14),
        ("windows-x64", "3.14.3", 17), ("windows-x64", "3.14.9", 17)}
    assert result["license_approval"] is False
    assert result["runtime_acceptance"] == "NOT_CHECKED"


def test_missing_windows_transitive_dependency_is_detected(snapshot):
    snapshot["packages"] = [p for p in snapshot["packages"] if p["name"] != "colorama"]
    with pytest.raises(checker.InvalidLock, match="Missing active dependency: colorama"):
        checker.validate(snapshot, render(snapshot))


def test_version_conflict_is_detected_in_real_dependency_graph(snapshot):
    item = package(snapshot, "build")
    item["requires_dist"] = ["packaging>=999"]
    for wheel in item["files"]:
        wheel["wheel_requires_dist"] = ["packaging>=999"]
    with pytest.raises(checker.InvalidLock, match="Dependency version conflict"):
        checker.validate(snapshot, render(snapshot))


def test_unreachable_package_cannot_be_smuggled_into_linux_lock(snapshot):
    package(snapshot, "colorama")["marker"] = None
    with pytest.raises(checker.InvalidLock, match="Unnecessary or unreachable"):
        checker.validate(snapshot, render(snapshot))


@pytest.mark.parametrize("replacement", ["build>=1.6.1", "build==1.6.*", "build[uv]==1.6.1",
                                           "build @ https://example.invalid/build.whl"])
def test_lock_rejects_unpinned_ranges_extras_and_direct_urls(snapshot, replacement):
    text = render(snapshot).replace("build==1.6.1", replacement)
    with pytest.raises(ValueError):
        checker.parse_lock(text)


def test_extra_index_and_digest_drift_are_rejected(snapshot):
    with pytest.raises(checker.InvalidLock, match="pip option"):
        checker.parse_lock(render(snapshot) + "\n--extra-index-url https://example.invalid/simple\n")
    digest = snapshot["packages"][0]["files"][0]["sha256"]
    with pytest.raises(checker.InvalidLock, match="file hashes differ"):
        checker.validate(snapshot, render(snapshot).replace(digest, "0" * 64))


@pytest.mark.parametrize("change", ["yanked", "unverified", "different_host", "url_credentials", "wrong_identity", "free_threaded"])
def test_unapproved_file_variants_are_rejected(snapshot, change):
    item = package(snapshot, "cffi")["files"][0]
    if change == "yanked":
        item["yanked"] = True
    elif change == "unverified":
        item["download_verified_now"] = False
    elif change == "different_host":
        item["url"] = item["url"].replace("files.pythonhosted.org", "example.invalid")
    elif change == "url_credentials":
        item["url"] = item["url"].replace("https://", "https://user:secret@")
    else:
        old = item["filename"]
        item["filename"] = old.replace("cffi-2.1.1", "unexpected-2.1.1") if change == "wrong_identity" else old.replace("cp314-cp314", "cp314-cp314t")
        item["url"] = item["url"].replace(old, item["filename"])
    with pytest.raises(ValueError):
        checker.validate(snapshot, render(snapshot))


def test_index_and_wheel_metadata_dependency_disagreement_is_detected(snapshot):
    package(snapshot, "build")["files"][0]["wheel_requires_dist"].append("unexpected-package")
    with pytest.raises(checker.InvalidLock, match="dependency declarations differ"):
        checker.validate(snapshot, render(snapshot))


def test_python_patch_markers_are_evaluated_and_not_ignored(snapshot):
    item = package(snapshot, "build")
    dependency = 'unknown-package; python_full_version >= "3.14.9"'
    item["requires_dist"].append(dependency)
    for wheel in item["files"]:
        wheel["wheel_requires_dist"].append(dependency)
    assert checker.check_closure(snapshot, "linux-x64", "3.14.3")
    with pytest.raises(checker.InvalidLock, match="Missing active dependency: unknown-package"):
        checker.check_closure(snapshot, "linux-x64", "3.14.9")


def test_duplicate_snapshot_keys_fail_closed(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(checker.InvalidLock, match="Duplicate JSON"):
        checker.read_snapshot(path)


def test_cached_wheel_bytes_and_top_level_metadata_are_checked_without_execution(tmp_path):
    path = tmp_path / "fixture-1.0-py3-none-any.whl"
    metadata = b"Metadata-Version: 2.4\nName: fixture\nVersion: 1.0\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fixture-1.0.dist-info/METADATA", metadata)
        archive.writestr("fixture/_vendor/another-1.0.dist-info/METADATA", b"not top-level")
        archive.writestr("fixture/__init__.py", 'raise RuntimeError("must not execute")')
    raw = path.read_bytes()
    value = {"packages": [{"files": [{"filename": path.name, "size": len(raw),
              "sha256": hashlib.sha256(raw).hexdigest(),
              "wheel_metadata_sha256": hashlib.sha256(metadata).hexdigest()}]}]}
    assert checker.verify_wheel_cache(value, tmp_path) == {"status": "PASS", "wheel_files_verified": 1, "code_executed": False}
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with pytest.raises(checker.InvalidLock, match="SHA-256 mismatch"):
        checker.verify_wheel_cache(value, tmp_path)


def test_cached_wheel_metadata_digest_cannot_be_substituted(tmp_path):
    path = tmp_path / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fixture-1.dist-info/METADATA", "test")
    raw = path.read_bytes()
    value = {"packages": [{"files": [{"filename": path.name, "size": len(raw),
              "sha256": hashlib.sha256(raw).hexdigest(), "wheel_metadata_sha256": "0" * 64}]}]}
    with pytest.raises(checker.InvalidLock, match="METADATA digest mismatch"):
        checker.verify_wheel_cache(value, tmp_path)
