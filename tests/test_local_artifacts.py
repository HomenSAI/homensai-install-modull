"""Staging source-проверки: синтетические файлы, без installers и сети."""
import hashlib
import io
import os
from pathlib import Path
import stat
import threading

import pytest

from homensai_install.errors import InstallerError
from homensai_install.local_artifacts import LocalArtifactUploads, MAX_UPLOAD_BYTES, MAX_UPLOADS


CONTENT = b"synthetic local prerequisite bytes"
BINDING = {"catalog_digest": "signed", "owner_id": "synthetic-owner"}


def artifact(content=CONTENT):
    return {"artifact_id": "base-package", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def allocated(tmp_path):
    store = LocalArtifactUploads(tmp_path / "state")
    record = store.create("base", artifact(), binding=BINDING)
    return store, record["upload_id"]


def test_upload_is_exact_hash_verified_private_and_never_uses_filename(tmp_path):
    store, upload_id = allocated(tmp_path)
    record = store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    assert record == {"upload_id": upload_id, "component_id": "base", "artifact_id": "base-package",
                      "size": len(CONTENT), "sha256": hashlib.sha256(CONTENT).hexdigest(),
                      "state": "verified", "verified": True}
    path = Path(store.paths({"base-package": upload_id}, binding=BINDING)["base-package"])
    assert path.read_bytes() == CONTENT and str(tmp_path) not in str(record)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("content", [b"", CONTENT[:-1], b"X" * len(CONTENT)])
def test_incomplete_or_wrong_hash_fails_and_removes_partial(tmp_path, content):
    store, upload_id = allocated(tmp_path)
    with pytest.raises(InstallerError) as error:
        store.receive(upload_id, io.BytesIO(content), len(CONTENT), binding=BINDING)
    assert error.value.code == "INTEGRITY_FAILED"
    assert store.status(upload_id)["state"] == "failed"
    assert list(store.directory.iterdir()) == []


@pytest.mark.parametrize("length", [0, -1, len(CONTENT) - 1, len(CONTENT) + 1, True])
def test_wrong_length_rejected_before_stream_read(tmp_path, length):
    store, upload_id = allocated(tmp_path)
    class NeverRead:
        def read(self, _):
            pytest.fail("invalid framing read body")
    with pytest.raises(InstallerError):
        store.receive(upload_id, NeverRead(), length, binding=BINDING)
    assert not list(store.directory.iterdir())
    assert store.status(upload_id)["state"] == "pending"


def test_session_and_catalog_binding_are_not_browser_controlled(tmp_path):
    store, upload_id = allocated(tmp_path)
    with pytest.raises(InstallerError) as error:
        store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding={**BINDING, "owner_id": "other"})
    assert error.value.code == "PLAN_STALE"
    second = LocalArtifactUploads(tmp_path / "state")
    with pytest.raises(InstallerError):
        second.status(upload_id)


@pytest.mark.parametrize("upload_id", ["../file", "upload_" + "x" * 32, "", None, 42])
def test_identifiers_never_become_arbitrary_paths(tmp_path, upload_id):
    store = LocalArtifactUploads(tmp_path / "state")
    with pytest.raises(InstallerError):
        store.status(upload_id)
    assert not store.parent.exists()


def test_limit_applies_before_storage_and_to_session_sum(tmp_path):
    store = LocalArtifactUploads(tmp_path / "state")
    for size in (MAX_UPLOAD_BYTES + 1, 0, True):
        with pytest.raises(InstallerError):
            store.create("base", {**artifact(), "size": size}, binding=BINDING)
    assert not store.parent.exists()
    store.create("base", {**artifact(), "size": MAX_UPLOAD_BYTES}, binding=BINDING)
    with pytest.raises(InstallerError):
        store.create("base", artifact(), binding=BINDING)


def test_count_limit_includes_canceled_history(tmp_path):
    store = LocalArtifactUploads(tmp_path / "state")
    for _ in range(MAX_UPLOADS):
        record = store.create("base", artifact(), binding=BINDING)
        store.cancel(record["upload_id"])
    with pytest.raises(InstallerError):
        store.create("base", artifact(), binding=BINDING)


def test_pending_cancel_prevents_late_content(tmp_path):
    store, upload_id = allocated(tmp_path)
    assert store.cancel(upload_id)["state"] == "canceled"
    with pytest.raises(InstallerError):
        store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    assert not list(store.directory.iterdir())


def test_cancel_during_receive_preserves_canceled_state_and_cleans_partial(tmp_path):
    store, upload_id = allocated(tmp_path)
    class CancelingStream(io.BytesIO):
        def read(self, size):
            store.cancel(upload_id)
            return super().read(size)
    with pytest.raises(InstallerError):
        store.receive(upload_id, CancelingStream(CONTENT), len(CONTENT), binding=BINDING)
    assert store.status(upload_id)["state"] == "canceled"
    assert not list(store.directory.iterdir())


def test_verified_cancel_removes_only_unbound_bytes(tmp_path):
    store, upload_id = allocated(tmp_path)
    store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    store.cancel(upload_id)
    assert not list(store.directory.iterdir())
    with pytest.raises(InstallerError):
        store.paths({"base-package": upload_id}, binding=BINDING)


def test_cancel_cannot_delete_a_plan_retained_file(tmp_path):
    store, upload_id = allocated(tmp_path)
    store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    paths = store.paths({"base-package": upload_id}, binding=BINDING)
    with pytest.raises(InstallerError) as error:
        store.cancel(upload_id)
    assert error.value.code == "OPERATION_CONFLICT"
    assert Path(paths["base-package"]).read_bytes() == CONTENT


def test_replay_cannot_overwrite_a_verified_upload(tmp_path):
    store, upload_id = allocated(tmp_path)
    store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    with pytest.raises(InstallerError):
        store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    assert store.status(upload_id)["verified"]


def test_changed_staged_file_is_rechecked_before_plan_binding(tmp_path):
    store, upload_id = allocated(tmp_path)
    store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    path = next(store.directory.iterdir())
    path.write_bytes(b"X" * len(CONTENT))
    with pytest.raises(InstallerError) as error:
        store.paths({"base-package": upload_id}, binding=BINDING)
    assert error.value.code == "INTEGRITY_FAILED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/mode and actual symlink profile")
@pytest.mark.parametrize("substitution", ["symlink", "hardlink", "mode", "directory"])
def test_unsafe_staged_inode_cannot_bind_or_be_unlinked_as_original(tmp_path, substitution):
    store, upload_id = allocated(tmp_path)
    store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    path = next(store.directory.iterdir())
    other = tmp_path / "other"
    other.write_bytes(CONTENT)
    other.chmod(0o600)
    if substitution == "mode":
        path.chmod(0o644)
    else:
        path.unlink()
        if substitution == "symlink": path.symlink_to(other)
        elif substitution == "hardlink": os.link(other, path)
        else: path.mkdir()
    with pytest.raises((InstallerError, ValueError)):
        store.paths({"base-package": upload_id}, binding=BINDING)
    assert other.read_bytes() == CONTENT


def test_temporary_selection_releases_only_its_pin_after_failure(tmp_path):
    store, upload_id = allocated(tmp_path)
    store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    with pytest.raises(RuntimeError):
        with store.selection({"base-package": upload_id}, binding=BINDING):
            with pytest.raises(InstallerError):
                store.cancel(upload_id)
            raise RuntimeError("synthetic plan failure")
    assert store.cancel(upload_id)["state"] == "canceled"
    assert not list(store.directory.iterdir())


def test_failed_second_selection_preserves_older_retained_plan(tmp_path):
    store, upload_id = allocated(tmp_path)
    store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    with store.selection({"base-package": upload_id}, binding=BINDING):
        store.retain({"base-package": upload_id})
    with pytest.raises(RuntimeError):
        with store.selection({"base-package": upload_id}, binding=BINDING):
            raise RuntimeError("second plan failed")
    with pytest.raises(InstallerError):
        store.cancel(upload_id)
    assert store.status(upload_id)["verified"]


def test_failed_cleanup_keeps_byte_reservation_and_primary_error(tmp_path, monkeypatch):
    import homensai_install.local_artifacts as uploads
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", len(CONTENT))
    store, upload_id = allocated(tmp_path)
    original_remove = store._remove
    def locked(_):
        raise PermissionError("synthetic locked staging file")
    monkeypatch.setattr(store, "_remove", locked)
    with pytest.raises(InstallerError) as primary:
        store.receive(upload_id, io.BytesIO(b"X" * len(CONTENT)), len(CONTENT), binding=BINDING)
    assert primary.value.code == "INTEGRITY_FAILED", "Cleanup must not mask the original hash failure"
    assert sum(path.stat().st_size for path in store.directory.iterdir()) == len(CONTENT)
    with pytest.raises(InstallerError):
        store.create("base", artifact(), binding=BINDING)
    monkeypatch.setattr(store, "_remove", original_remove)
    store.cancel(upload_id)  # Explicit retry may release quota only after deletion.
    assert not list(store.directory.iterdir())
    assert store.create("base", artifact(), binding=BINDING)["state"] == "pending"


def test_repeated_active_cancel_keeps_file_and_quota_until_receive_finishes(tmp_path, monkeypatch):
    import homensai_install.local_artifacts as uploads
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", len(CONTENT))
    store, upload_id = allocated(tmp_path)
    class CancelingStream(io.BytesIO):
        def read(self, size):
            store.cancel(upload_id)
            store.cancel(upload_id)
            assert len(list(store.directory.iterdir())) == 1, "An active writer owns its staged file until finalization"
            with pytest.raises(InstallerError):
                store.create("base", artifact(), binding=BINDING)
            return super().read(size)
    with pytest.raises(InstallerError) as canceled:
        store.receive(upload_id, CancelingStream(CONTENT), len(CONTENT), binding=BINDING)
    assert canceled.value.code == "OPERATION_CONFLICT"
    assert store.status(upload_id)["state"] == "canceled"
    assert not list(store.directory.iterdir())
    assert store.create("base", artifact(), binding=BINDING)["state"] == "pending"


def test_created_file_with_unknown_identity_does_not_release_quota(tmp_path, monkeypatch):
    import homensai_install.local_artifacts as uploads
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", len(CONTENT))
    store, upload_id = allocated(tmp_path)
    with monkeypatch.context() as failing:
        def unreadable_identity(_):
            raise OSError("synthetic fstat failure after exclusive create")
        failing.setattr(uploads.os, "fstat", unreadable_identity)
        with pytest.raises(InstallerError) as error:
            store.receive(upload_id, io.BytesIO(CONTENT), len(CONTENT), binding=BINDING)
    assert error.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert len(list(store.directory.iterdir())) == 1
    with pytest.raises(InstallerError):
        store.cancel(upload_id)  # Identity is unknown; do not unlink an unchecked inode.
    with pytest.raises(InstallerError):
        store.create("base", artifact(), binding=BINDING)
