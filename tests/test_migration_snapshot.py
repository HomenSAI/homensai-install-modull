"""Fail-closed live snapshot: реальные деревья/handles, без hooks или SQLite."""
import os
from pathlib import Path
import shutil

import pytest

from homensai_install.errors import InstallerError
from homensai_install import migration_snapshot as snapshot
from shared.datahome import PERSISTENT_ROOTS


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux ProductAdapter secure dir_fd backend")


@pytest.fixture
def trees(tmp_path):
    live = {}
    for name in PERSISTENT_ROOTS:
        root = tmp_path / "live" / name
        root.mkdir(parents=True, mode=0o700)
        live[name] = root
    (live["data"] / "empty-dir").mkdir(mode=0o700)
    for name, relative, content in (("data", "payload.bin", b"\x00\xffsource snapshot"),
            ("data", "reference-adapter.sqlite3", b"product ledger"),
            ("services", "operation.lock", b"held lock"), ("config", "settings.json", b"settings"),
            ("runtime", "runtime.bin", b"runtime recovery"), ("backup", "prior.txt", b"backup metadata")):
        path = live[name] / relative
        path.write_bytes(content)
        path.chmod(0o600)
    journal = live["services"] / ("update-" + "a" * 32 + ".json")
    journal.write_bytes(b"live current phase")
    journal.chmod(0o644)
    restored = tmp_path / "restored"
    shutil.copytree(tmp_path / "live", restored / "roots")
    (restored / "roots/services" / journal.name).write_bytes(b"backup earlier phase")
    return live, restored, journal


def run(trees):
    live, restored, journal = trees
    return snapshot.assert_live_snapshot_matches(live, restored, own_update_journal=journal)


def refused(trees):
    with pytest.raises(InstallerError) as caught:
        run(trees)
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert caught.value.context["details"] == {"reason": "migration_live_snapshot_not_verified"}
    assert str(trees[1]) not in str(caught.value)


def test_matches_bytes_dirs_with_distinct_inodes_and_only_exact_journal_content_excluded(trees):
    live, restored, journal = trees
    assert live["data"].stat().st_ino != (restored / "roots/data").stat().st_ino
    before = {str(p): p.read_bytes() for root in (*live.values(), restored) for p in root.rglob("*") if p.is_file()}
    assert run(trees) is True
    assert before == {str(p): p.read_bytes() for root in (*live.values(), restored) for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("side", ["live", "restored"])
@pytest.mark.parametrize("change", ["changed_bytes", "missing_file", "extra_file", "extra_empty_dir",
    "missing_empty_dir", "lock_changed", "product_ledger_changed", "neighbor_journal", "nested_journal",
    "missing_root", "missing_journal", "journal_directory", "file_hardlink", "journal_hardlink",
    "file_symlink", "directory_symlink", "journal_symlink", "fifo", "shared_writable_file", "shared_writable_journal"])
def test_every_nonexception_difference_and_unsafe_entry_is_rejected(trees, tmp_path, side, change):
    live, restored, journal = trees
    roots = live if side == "live" else {name: restored / "roots" / name for name in live}
    data, services = roots["data"], roots["services"]
    payload = data / "payload.bin"
    own = services / journal.name
    if change == "changed_bytes": payload.write_bytes(b"different")
    elif change == "missing_file": payload.unlink()
    elif change == "extra_file": (data / "unknown").write_bytes(b"extra")
    elif change == "extra_empty_dir": (data / "unknown-dir").mkdir()
    elif change == "missing_empty_dir": (data / "empty-dir").rmdir()
    elif change == "lock_changed": (services / "operation.lock").write_bytes(b"changed")
    elif change == "product_ledger_changed": (data / "reference-adapter.sqlite3").write_bytes(b"changed")
    elif change == "neighbor_journal": (services / ("update-" + "b" * 32 + ".json")).write_bytes(b"not excluded")
    elif change == "nested_journal":
        (services / "nested").mkdir()
        (services / "nested" / journal.name).write_bytes(b"not excluded")
    elif change == "missing_root": shutil.rmtree(roots["config"])
    elif change == "missing_journal": own.unlink()
    elif change == "journal_directory": own.unlink(); own.mkdir()
    elif change == "file_hardlink": os.link(payload, tmp_path / "linked")
    elif change == "journal_hardlink": os.link(own, tmp_path / "linked")
    elif change == "file_symlink": payload.unlink(); payload.symlink_to(tmp_path / "outside")
    elif change == "directory_symlink": (data / "empty-dir").rmdir(); (data / "empty-dir").symlink_to(tmp_path, target_is_directory=True)
    elif change == "journal_symlink": own.unlink(); own.symlink_to(tmp_path / "outside")
    elif change == "fifo": payload.unlink(); os.mkfifo(payload, mode=0o600)
    elif change == "shared_writable_file": payload.chmod(0o666)
    elif change == "shared_writable_journal": own.chmod(0o666)
    refused(trees)


@pytest.mark.parametrize("change", ["uppercase_token", "short_token", "wrong_prefix", "nested", "other_root",
    "missing_mapping_root", "extra_mapping_root", "overlapping_roots", "extra_restored_root", "relative_candidate"])
def test_only_exact_five_root_mapping_and_direct_services_journal_are_accepted(trees, change):
    live, restored, journal = trees
    if change == "uppercase_token": journal = journal.with_name("update-" + "A" * 32 + ".json")
    elif change == "short_token": journal = journal.with_name("update-aaaa.json")
    elif change == "wrong_prefix": journal = journal.with_name("arbitrary-" + "a" * 32 + ".json")
    elif change == "nested": journal = journal.parent / "nested" / journal.name
    elif change == "other_root": journal = live["data"] / journal.name
    elif change == "missing_mapping_root": live = {key: value for key, value in live.items() if key != "backup"}
    elif change == "extra_mapping_root": live = {**live, "unknown": restored.parent / "unknown"}
    elif change == "overlapping_roots": live = {**live, "backup": live["data"] / "empty-dir"}
    elif change == "extra_restored_root": (restored / "roots/unknown").mkdir()
    elif change == "relative_candidate": restored = Path("relative")
    refused((live, restored, journal))


def test_fd_open_substitution_before_file_read_is_rejected(trees, monkeypatch):
    live, _, _ = trees
    payload = live["data"] / "payload.bin"
    real_open = os.open
    fired = []
    def substitute(path, flags, *args, **kwargs):
        if path == "payload.bin" and not fired:
            fired.append(True)
            payload.rename(payload.with_name("previous"))
            payload.write_bytes(b"replacement")
            payload.chmod(0o600)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(snapshot.os, "open", substitute)
    # supports_dir_fd stores function objects, so preserve the capability when
    # instrumenting only the syscall boundary in this test.
    monkeypatch.setattr(snapshot.os, "supports_dir_fd", {*os.supports_dir_fd, substitute})
    refused(trees)
    assert fired


def test_file_change_during_fd_read_is_rejected(trees, monkeypatch):
    live, _, _ = trees
    payload = live["data"] / "payload.bin"
    real_read = os.read
    fired = []
    def change(handle, size):
        block = real_read(handle, size)
        if block == b"\x00\xffsource snapshot" and not fired:
            fired.append(True)
            payload.write_bytes(b"same file changed after read")
        return block
    monkeypatch.setattr(snapshot.os, "read", change)
    refused(trees)
    assert fired


def test_previously_scanned_root_drift_during_backup_scan_is_rejected(trees, monkeypatch):
    live, restored, _ = trees
    real_scan = snapshot._scan
    fired = []
    def change(root, label, journal_name):
        result = real_scan(root, label, journal_name)
        if root == restored / "roots/data" and not fired:
            fired.append(True)
            (live["data"] / "payload.bin").write_bytes(b"drift after first live pass")
        return result
    monkeypatch.setattr(snapshot, "_scan", change)
    refused(trees)
    assert fired


def test_unsupported_safe_directory_backend_is_rejected(trees, monkeypatch):
    monkeypatch.setattr(snapshot.os, "supports_dir_fd", set())
    refused(trees)


def test_ancestor_substitution_cannot_follow_symlink(trees, tmp_path, monkeypatch):
    live, _, _ = trees
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "foreign-secret").write_bytes(b"must never read this")
    real_open, real_read = os.open, os.read
    fired = []
    def substitute(path, flags, *args, **kwargs):
        if path == "data" and not fired:
            fired.append(True)
            live["data"].rename(live["data"].with_name("previous-data"))
            live["data"].symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)
    def watch(handle, size):
        block = real_read(handle, size)
        assert block != b"must never read this"
        return block
    monkeypatch.setattr(snapshot.os, "open", substitute)
    monkeypatch.setattr(snapshot.os, "read", watch)
    monkeypatch.setattr(snapshot.os, "supports_dir_fd", {*os.supports_dir_fd, substitute})
    refused(trees)
    assert fired


def test_hardlink_arriving_during_file_read_is_rejected(trees, tmp_path, monkeypatch):
    live, _, _ = trees
    real_read = os.read
    fired = []
    def link(handle, size):
        block = real_read(handle, size)
        if block == b"\x00\xffsource snapshot" and not fired:
            fired.append(True)
            os.link(live["data"] / "payload.bin", tmp_path / "new-hardlink")
        return block
    monkeypatch.setattr(snapshot.os, "read", link)
    refused(trees)
    assert fired


def test_extra_restored_root_arriving_after_inventory_is_rejected(trees, monkeypatch):
    _, restored, _ = trees
    real_scan = snapshot._scan
    fired = []
    def change(root, label, journal_name):
        result = real_scan(root, label, journal_name)
        if root == restored / "roots/data" and not fired:
            fired.append(True)
            (restored / "roots/unknown").mkdir(mode=0o700)
        return result
    monkeypatch.setattr(snapshot, "_scan", change)
    refused(trees)
    assert fired


def test_same_unexpected_neighbor_journal_is_compared_instead_of_excluded(trees):
    live, restored, journal = trees
    name = "update-" + "b" * 32 + ".json"
    (live["services"] / name).write_bytes(b"same neighbor")
    (restored / "roots/services" / name).write_bytes(b"same neighbor")
    assert run(trees) is True
    (live["services"] / name).write_bytes(b"changed neighbor")
    refused(trees)
