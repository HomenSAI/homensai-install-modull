"""SQLite sidecar removal is normal; missing/unsafe main state fails closed.

Real owned SQLite files with a deterministic unlink at the metadata check.
No external process, network or OS installer is used.
"""
import errno
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from homensai_install.prerequisite_state import PrerequisiteStore
from test_prerequisites_execution import bundle, coordinator


def file_for(store, suffix):
    path = Path(str(store.path) + suffix)
    if suffix:
        path.write_bytes(b"synthetic SQLite sidecar")
        path.chmod(0o600)
    return path


def prevent_database_open(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsafe state reached sqlite3.connect")
    monkeypatch.setattr("homensai_install.prerequisite_state.sqlite3.connect", forbidden)


@pytest.mark.parametrize("name", ["fresh-state", "state with #% and кириллица"])
def test_fresh_store_creates_main_before_checking_private_files(tmp_path, monkeypatch, name):
    checked = []
    original = PrerequisiteStore._check_files

    def check(store):
        # The existing constructor creates main before its first connection.
        assert store.path.is_file()
        checked.append(store.path)
        return original(store)

    monkeypatch.setattr(PrerequisiteStore, "_check_files", check)
    directory = tmp_path / name
    assert not directory.exists()
    store = PrerequisiteStore(directory)
    assert checked and store.get("missing") is None
    assert PrerequisiteStore(directory).get("missing") is None


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_sidecar_removed_at_lstat_keeps_public_status_readable(bundle, monkeypatch, suffix):
    subject = coordinator(bundle)
    api = InstallerAPI(bundle["tmp_path"] / "api-state", prerequisites=subject)
    plan = api.prerequisites_plan({"base": "manual", "client": "manual"})
    api.prerequisites_start("state-race", plan, plan["required_consents"])
    before = api.prerequisites_status("state-race")
    sidecar = file_for(subject.store, suffix)
    original = Path.lstat
    removed = []

    def remove_before_lstat(path, *args, **kwargs):
        if path == sidecar and not removed:
            # Old code has just observed exists() == True. SQLite commit may
            # now remove its sidecar before this metadata read.
            path.unlink()
            removed.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", remove_before_lstat)
    assert api.prerequisites_status("state-race") == before
    assert removed == [sidecar] and not sidecar.exists()
    assert bundle["calls"] == [] and bundle["transport"].calls == []


def test_main_removed_at_lstat_is_a_storage_failure(bundle, monkeypatch):
    subject = coordinator(bundle)
    api = InstallerAPI(bundle["tmp_path"] / "api-state", prerequisites=subject)
    plan = api.prerequisites_plan({"base": "manual", "client": "manual"})
    api.prerequisites_start("main-race", plan, plan["required_consents"])
    main = subject.store.path
    original = Path.lstat
    removed = []

    def remove_before_lstat(path, *args, **kwargs):
        if path == main and not removed:
            path.unlink()
            removed.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", remove_before_lstat)
    with pytest.raises(InstallerError) as caught:
        api.prerequisites_status("main-race")
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert removed == [main] and not main.exists()


def test_missing_expected_main_is_not_recreated(tmp_path):
    store = PrerequisiteStore(tmp_path / "state")
    store.path.unlink()
    with pytest.raises(InstallerError) as caught:
        store.get("missing")
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert not store.path.exists()


@pytest.mark.parametrize("name", ["state", "state with #% and кириллица"])
def test_main_removed_after_check_before_connect_is_not_recreated(tmp_path, monkeypatch, name):
    store = PrerequisiteStore(tmp_path / name)
    original = sqlite3.connect
    removed = []

    def remove_before_connect(*args, **kwargs):
        # A successful metadata check must not authorize implicit recreation.
        store.path.unlink()
        removed.append(store.path)
        return original(*args, **kwargs)

    monkeypatch.setattr("homensai_install.prerequisite_state.sqlite3.connect", remove_before_connect)
    with pytest.raises(InstallerError) as caught:
        store.get("missing")
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert removed == [store.path] and not store.path.exists()


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.parametrize("unsafe", ["permissions", "hardlink", "symlink", "directory", "owner"])
def test_existing_unsafe_state_never_reaches_sqlite(tmp_path, monkeypatch, suffix, unsafe):
    if os.name == "nt" and unsafe in {"permissions", "owner"}:
        pytest.skip("POSIX metadata refusal; Windows ACL acceptance is separate")
    store = PrerequisiteStore(tmp_path / "state")
    path = file_for(store, suffix)
    if unsafe == "permissions":
        path.chmod(0o644)
    elif unsafe == "hardlink":
        (tmp_path / "other-link").hardlink_to(path)
    elif unsafe == "symlink":
        path.unlink()
        outside = tmp_path / "other-state"
        outside.write_bytes(b"must not be opened")
        path.symlink_to(outside)
    elif unsafe == "directory":
        path.unlink()
        path.mkdir()
    else:
        original = Path.lstat

        def wrong_owner(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            if candidate == path:
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", wrong_owner)
    prevent_database_open(monkeypatch)
    with pytest.raises(InstallerError) as caught:
        store.get("missing")
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_other_lstat_errors_are_not_treated_as_absent(tmp_path, monkeypatch, suffix, error_number):
    store = PrerequisiteStore(tmp_path / "state")
    path = file_for(store, suffix)
    original = Path.lstat

    def unavailable(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError(error_number, "synthetic private storage detail")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unavailable)
    prevent_database_open(monkeypatch)
    with pytest.raises((InstallerError, OSError)) as caught:
        store.get("missing")
    if isinstance(caught.value, InstallerError):
        assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    else:
        assert caught.value.errno == error_number


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink of an open SQLite sidecar")
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_unlinked_sidecar_metadata_with_zero_links_is_normal_commit(tmp_path, monkeypatch, suffix):
    store = PrerequisiteStore(tmp_path / "state")
    sidecar = file_for(store, suffix)
    descriptor = os.open(sidecar, os.O_RDONLY)
    original, observed = Path.lstat, []
    def unlinked_metadata(path, *args, **kwargs):
        if path == sidecar and not observed:
            path.unlink()
            info = os.fstat(descriptor)
            assert info.st_nlink == 0 and stat.S_ISREG(info.st_mode)
            observed.append(info)
            return info
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", unlinked_metadata)
    try:
        assert store.get("missing") is None
        assert observed and not sidecar.exists()
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink of an open authoritative database")
def test_zero_link_main_database_is_never_a_transient_sidecar(tmp_path, monkeypatch):
    store = PrerequisiteStore(tmp_path / "state")
    descriptor = os.open(store.path, os.O_RDONLY)
    original, observed = Path.lstat, []
    def missing_main_metadata(path, *args, **kwargs):
        if path == store.path and not observed:
            path.unlink()
            observed.append(True)
            return os.fstat(descriptor)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", missing_main_metadata)
    prevent_database_open(monkeypatch)
    try:
        with pytest.raises(InstallerError):
            store.get("missing")
    finally:
        os.close(descriptor)
    assert not store.path.exists()


@pytest.mark.parametrize("unsafe", ["permissions", "owner", "type", "two_links", "fresh_permissions", "fresh_owner", "fresh_type", "fresh_two_links", "four_zero", "fresh_eacces", "fresh_eio"])
def test_zero_link_exception_requires_safe_metadata_and_confirmed_absence(tmp_path, monkeypatch, unsafe):
    if os.name == "nt" and unsafe in {"permissions", "owner", "fresh_permissions", "fresh_owner"}:
        pytest.skip("POSIX owner/mode refusal; Windows ACL acceptance is separate")
    store = PrerequisiteStore(tmp_path / "state")
    sidecar = file_for(store, "-journal")
    original, observed = Path.lstat, []
    def suspicious_metadata(path, *args, **kwargs):
        if path != sidecar:
            return original(path, *args, **kwargs)
        if not observed:
            values = list(original(path, *args, **kwargs)); values[3] = 0
            if unsafe == "permissions": values[0] |= 0o044
            elif unsafe == "owner": values[4] = os.geteuid() + 1
            elif unsafe == "type": values[0] = stat.S_IFLNK | 0o600
            elif unsafe == "two_links": values[3] = 2
            observed.append(True)
            if not unsafe.startswith("fresh_") and unsafe != "four_zero": sidecar.unlink()
            return os.stat_result(values)
        if unsafe == "fresh_eacces": raise PermissionError(errno.EACCES, "synthetic metadata denial")
        if unsafe == "fresh_eio": raise OSError(errno.EIO, "synthetic metadata I/O failure")
        values = list(original(path, *args, **kwargs))
        if unsafe == "fresh_permissions": values[0] |= 0o044
        elif unsafe == "fresh_owner": values[4] = os.geteuid() + 1
        elif unsafe == "fresh_type": values[0] = stat.S_IFLNK | 0o600
        elif unsafe == "fresh_two_links": values[3] = 2
        elif unsafe == "four_zero": values[3] = 0
        if unsafe in {"fresh_permissions", "fresh_owner", "fresh_type", "fresh_two_links", "four_zero"}:
            observed.append(True)
            return os.stat_result(values)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", suspicious_metadata)
    prevent_database_open(monkeypatch)
    with pytest.raises(InstallerError):
        store.get("missing")
    if unsafe == "four_zero":
        assert len(observed) == 4, "Re-observation must remain bounded"


def test_new_private_sidecar_after_zero_link_observation_is_fully_rechecked(tmp_path, monkeypatch):
    store = PrerequisiteStore(tmp_path / "state")
    sidecar = file_for(store, "-journal")
    original, observed = Path.lstat, []
    def old_unlinked_metadata(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == sidecar and not observed:
            values = list(info); values[3] = 0
            observed.append(True)
            return os.stat_result(values)
        return info
    monkeypatch.setattr(Path, "lstat", old_unlinked_metadata)
    store._check_files()  # Only metadata; synthetic sidecar is not a valid SQLite journal.
    assert observed and sidecar.stat().st_nlink == 1
