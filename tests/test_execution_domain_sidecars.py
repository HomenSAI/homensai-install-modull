"""Read-only domain probes can overlap the owner's SQLite commit cleanup."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading

import pytest

import homensai_install.execution_lease as execution
from homensai_install.errors import InstallerError


@pytest.mark.parametrize("probe", ["pending", "prerequisites_busy"])
def test_read_probe_survives_journal_removed_before_private_open(tmp_path, monkeypatch, probe):
    domain = execution.ExecutionDomain(tmp_path / "domain", owner_id="synthetic-owner")
    lease = domain.reserve("prerequisite", "prerequisite-one", "b" * 64)
    journal = Path(str(domain.path) + "-journal")
    updated, commit_allowed, writer_finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_db, original_open = domain._db, execution._private_file

    @contextmanager
    def gated_commit(**kwargs):
        with original_db(**kwargs) as db:
            yield db
            if kwargs.get("write") and threading.current_thread().name == "synthetic-domain-writer":
                updated.set()
                assert commit_allowed.wait(5)

    def disappearing_sidecar(path, flags):
        if (Path(path) == journal and updated.is_set() and not writer_finished.is_set()
                and threading.current_thread().name != "synthetic-domain-writer"):
            assert journal.exists()
            commit_allowed.set()
            assert writer_finished.wait(5)
            assert not journal.exists()
        return original_open(path, flags)

    def write():
        try:
            lease.queued()
        except BaseException as error:
            errors.append(error)
        finally:
            writer_finished.set()

    monkeypatch.setattr(domain, "_db", gated_commit)
    monkeypatch.setattr(execution, "_private_file", disappearing_sidecar)
    worker = threading.Thread(target=write, name="synthetic-domain-writer")
    try:
        worker.start()
        assert updated.wait(5) and journal.exists()
        if probe == "pending":
            assert domain.pending("prerequisite", "prerequisite-one", "b" * 64) is True
        else:
            assert domain.prerequisites_busy() is True
        assert not errors
    finally:
        commit_allowed.set()
        worker.join(5)
        assert not worker.is_alive()
        lease.close(release=True)


@pytest.mark.parametrize("unsafe", ["missing_main", "sidecar_permissions", "sidecar_symlink", "sidecar_hardlink"])
def test_transient_absence_policy_does_not_allow_unsafe_authoritative_files(tmp_path, monkeypatch, unsafe):
    if os.name == "nt" and unsafe == "sidecar_permissions":
        pytest.skip("POSIX file mode check; actual Windows ACL acceptance is separate")
    domain = execution.ExecutionDomain(tmp_path / "domain", owner_id="synthetic-owner")
    lease = domain.reserve("product", "product-one", "b" * 64)
    lease.close(release=True)
    sidecar = Path(str(domain.path) + "-journal")
    if unsafe == "missing_main":
        domain.path.unlink()
    elif unsafe == "sidecar_permissions":
        sidecar.write_bytes(b"private fixture")
        sidecar.chmod(0o644)
    else:
        original = tmp_path / "fixture-file"
        original.write_bytes(b"private fixture")
        original.chmod(0o600)
        if unsafe == "sidecar_symlink":
            sidecar.symlink_to(original)
        else:
            os.link(original, sidecar)

    def forbidden_connect(*args, **kwargs):
        pytest.fail("Unsafe or missing main state reached SQLite")

    monkeypatch.setattr(execution.sqlite3, "connect", forbidden_connect)
    with pytest.raises(InstallerError) as caught:
        with domain._db():
            pytest.fail("Unsafe state was accepted")
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    if unsafe == "missing_main":
        assert not domain.path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink of open SQLite metadata")
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_zero_link_private_sidecar_after_open_is_reobserved(tmp_path, monkeypatch, suffix):
    main = tmp_path / "state.sqlite3"
    main.write_bytes(b"synthetic main"); main.chmod(0o600)
    sidecar = Path(str(main) + suffix)
    sidecar.write_bytes(b"synthetic sidecar"); sidecar.chmod(0o600)
    original, observed = os.fstat, []
    def unlinked_after_open(descriptor):
        info = original(descriptor)
        if (info.st_dev, info.st_ino) == sidecar_identity and not observed:
            sidecar.unlink()
            info = original(descriptor)
            assert info.st_nlink == 0
            observed.append(True)
        return info
    sidecar_identity = (sidecar.stat().st_dev, sidecar.stat().st_ino)
    monkeypatch.setattr(execution.os, "fstat", unlinked_after_open)
    execution._check_sqlite_files(main)
    assert observed and not sidecar.exists()


@pytest.mark.parametrize("unsafe", ["main", "mode", "owner", "type", "hardlink", "four_zero"])
def test_unlinked_retry_preserves_private_file_and_main_refusals(tmp_path, monkeypatch, unsafe):
    if os.name == "nt" and unsafe in {"mode", "owner"}:
        pytest.skip("POSIX owner/mode profile; Windows ACL acceptance is separate")
    main = tmp_path / "state.sqlite3"
    main.write_bytes(b"synthetic main"); main.chmod(0o600)
    sidecar = Path(str(main) + "-journal")
    sidecar.write_bytes(b"synthetic sidecar"); sidecar.chmod(0o600)
    target = main if unsafe == "main" else sidecar
    target_identity = (target.stat().st_dev, target.stat().st_ino)
    original, observed = os.fstat, []
    def unsafe_unlinked_metadata(descriptor):
        info = original(descriptor)
        if (info.st_dev, info.st_ino) != target_identity:
            return info
        values = list(info); values[3] = 0
        if observed:
            if unsafe == "mode": values[0] |= 0o044
            elif unsafe == "owner": values[4] = os.geteuid() + 1
            elif unsafe == "type": values[0] = stat.S_IFLNK | 0o600
            elif unsafe == "hardlink": values[3] = 2
        observed.append(True)
        return os.stat_result(values)
    monkeypatch.setattr(execution.os, "fstat", unsafe_unlinked_metadata)
    with pytest.raises(InstallerError):
        execution._check_sqlite_files(main)
    assert len(observed) == (1 if unsafe == "main" else 4 if unsafe == "four_zero" else 2)


def test_generic_private_file_keeps_rejecting_zero_links_for_locks(tmp_path, monkeypatch):
    path = tmp_path / "controller.lock"
    path.write_bytes(b"synthetic lock"); path.chmod(0o600)
    original = os.fstat
    def zero_links(descriptor):
        values = list(original(descriptor)); values[3] = 0
        return os.stat_result(values)
    monkeypatch.setattr(execution.os, "fstat", zero_links)
    with pytest.raises(InstallerError):
        execution._private_file(path, os.O_RDONLY)
