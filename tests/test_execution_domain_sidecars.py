"""Read-only domain probes can overlap the owner's SQLite commit cleanup."""
from contextlib import contextmanager
import os
from pathlib import Path
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
