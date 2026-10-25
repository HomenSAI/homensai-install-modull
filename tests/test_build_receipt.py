"""A receipt records observed bytes and never invents a past verification."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hinstall_build_receipt", ROOT / "tools/build_receipt.py")
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)


def test_runtime_archive_verified_now_does_not_claim_historical_provenance(tmp_path):
    path = tmp_path / "runtime.tar.gz"
    path.write_bytes(b"synthetic archive bytes only")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    value = receipt.runtime_archive_receipt(path, digest, "https://example.invalid/runtime.tar.gz")
    assert value["archive_bytes_verified_now"] is True
    assert value["historical_download_origin"] == "NOT_PROVEN"
    assert value["extracted_runtime_tree"] == "NOT_COMPARED"
    assert value["publisher_attestation"] == "NOT_CHECKED"


@pytest.mark.parametrize("url", ["http://example.invalid/archive", "https://user:password@example.invalid/archive", "file:///tmp/archive"])
def test_runtime_source_url_rejects_credentials_and_non_https(tmp_path, url):
    path = tmp_path / "runtime.tar.gz"
    path.write_bytes(b"test")
    with pytest.raises(receipt.InvalidReceipt, match="HTTPS URL"):
        receipt.runtime_archive_receipt(path, hashlib.sha256(b"test").hexdigest(), url)


def test_runtime_archive_rejects_changed_bytes_and_missing_expected_digest(tmp_path):
    path = tmp_path / "runtime.tar.gz"
    path.write_bytes(b"changed")
    with pytest.raises(receipt.InvalidReceipt, match="SHA-256 mismatch"):
        receipt.runtime_archive_receipt(path, hashlib.sha256(b"original").hexdigest(), "https://example.invalid/archive")
    with pytest.raises(receipt.InvalidReceipt, match="reviewed SHA-256"):
        receipt.runtime_archive_receipt(path, None, "https://example.invalid/archive")


def git_fixture(root, dirty=False):
    def inspect(requested_root, arguments):
        assert requested_root == root
        if arguments == ["rev-parse", "HEAD"]:
            return ("a" * 40 + "\n").encode()
        if arguments[0] == "status":
            return b"?? new-file.py\0" if dirty else b""
        if arguments[0] == "ls-files":
            return b"source.py\0"
        raise AssertionError(arguments)
    return inspect


def test_source_snapshot_hash_changes_with_tracked_content(tmp_path, monkeypatch):
    path = tmp_path / "source.py"
    path.write_bytes(b"version=1\n")
    monkeypatch.setattr(receipt, "_git", git_fixture(tmp_path))
    first = receipt.source_snapshot(tmp_path)
    path.write_bytes(b"version=2\n")
    second = receipt.source_snapshot(tmp_path)
    assert first["tracked_tree_sha256"] != second["tracked_tree_sha256"]
    assert first["dirty"] is False and first["untracked_files_included"] is False
    assert first["tracked_files"][0]["sha256"] == hashlib.sha256(b"version=1\n").hexdigest()


def test_dirty_source_is_refused_unless_explicit_dev_scope(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_bytes(b"test")
    monkeypatch.setattr(receipt, "_git", git_fixture(tmp_path, dirty=True))
    with pytest.raises(receipt.InvalidReceipt, match="dirty"):
        receipt.source_snapshot(tmp_path)
    value = receipt.source_snapshot(tmp_path, allow_dirty_dev=True)
    assert value["dirty"] and value["allow_dirty_dev"]
    assert value["untracked_files_included"] is False


def test_git_inspection_uses_fixed_argv_and_never_shell(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(receipt.shutil, "which", lambda name: "git-test-path" if name == "git" else None)
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": b"safe"})()
    monkeypatch.setattr(receipt.subprocess, "run", run)
    assert receipt._git(tmp_path, ["rev-parse", "HEAD"]) == b"safe"
    argv, kwargs = calls[0]
    assert argv == ["git-test-path", "--no-optional-locks", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"]
    assert kwargs["shell"] is False and kwargs["cwd"] == tmp_path
    assert "env" not in kwargs


def test_receipt_is_written_as_one_complete_json_document(tmp_path):
    path = tmp_path / "output" / "receipt.json"
    receipt.write_receipt(path, {"test_fixture": True, "production_approved": False})
    assert json.loads(path.read_text(encoding="utf-8")) == {"test_fixture": True, "production_approved": False}
    assert list(path.parent.glob(".receipt-*.tmp")) == []
