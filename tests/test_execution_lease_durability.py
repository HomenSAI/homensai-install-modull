"""Fault and process-exit properties for the independent durable domain."""

import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time

import pytest

import homensai_install
from homensai_install.errors import InstallerError


SOURCE = Path(homensai_install.__file__).resolve().parents[1]
BINDING = hashlib.sha256(b"synthetic reviewed operation binding").hexdigest()
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux private files/OS lease contract")


def domain_at(directory):
    from homensai_install.execution_lease import ExecutionDomain
    return ExecutionDomain(directory, owner_id="synthetic-owner")


@pytest.mark.parametrize("entry", ["execution.lock", "execution.sqlite3"])
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "public-mode"])
def test_untrusted_lease_files_fail_closed_without_mutating_target(tmp_path, entry, attack):
    domain = domain_at(tmp_path / "domain")
    handle = domain.reserve("product", "first", BINDING)
    handle.close(release=True)
    target = domain.directory / entry
    original = target.read_bytes()
    retained = tmp_path / "retained"
    if attack == "symlink":
        target.rename(retained)
        target.symlink_to(retained)
    elif attack == "hardlink":
        os.link(target, retained)
    else:
        target.chmod(0o644)
    assert domain.busy() is None
    with pytest.raises(InstallerError) as caught:
        domain.reserve("prerequisite", "other", hashlib.sha256(b"other").hexdigest())
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert target.read_bytes() == original
    if retained.exists():
        assert retained.read_bytes() == original


@pytest.mark.parametrize("entry", ["execution.lock", "execution.sqlite3"])
def test_removed_authoritative_file_is_not_silently_recreated(tmp_path, entry):
    domain = domain_at(tmp_path / "domain")
    handle = domain.reserve("product", "first", BINDING)
    handle.close()
    target = domain.directory / entry
    target.rename(tmp_path / "preserved")
    assert domain.busy() is None
    with pytest.raises(InstallerError) as caught:
        domain.reserve("product", "first", BINDING, resume=True)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert not target.exists()


def test_owner_mismatch_cannot_reuse_domain(tmp_path):
    from homensai_install.execution_lease import ExecutionDomain
    domain = domain_at(tmp_path / "domain")
    handle = domain.reserve("product", "first", BINDING)
    handle.close()
    other = ExecutionDomain(domain.directory, owner_id="different-original-owner")
    assert other.busy() is None
    with pytest.raises(InstallerError) as caught:
        other.reserve("product", "first", BINDING, resume=True)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert domain.busy() is True


def test_stale_fencing_token_cannot_delete_newer_durable_claim(tmp_path):
    domain = domain_at(tmp_path / "domain")
    handle = domain.reserve("product", "first", BINDING)
    # Fault injection represents a stale owner presenting an obsolete token.
    # Normal contenders cannot write this row while its OS lock is held.
    with sqlite3.connect(domain.path) as db:
        old = json.loads(db.execute("SELECT body FROM claim WHERE id=1").fetchone()[0])
        current = {**old, "token": "b" * 32}
        assert current["token"] != handle.token
        db.execute("UPDATE claim SET body=? WHERE id=1", (json.dumps(current),))
    with pytest.raises(InstallerError) as caught:
        handle.close(release=True)
    assert caught.value.code == "OPERATION_CONFLICT"
    with sqlite3.connect(domain.path) as db:
        assert json.loads(db.execute("SELECT body FROM claim WHERE id=1").fetchone()[0]) == current
    assert domain.busy() is True


def test_sqlite_failure_rolls_back_release_and_keeps_durable_intent(tmp_path):
    domain = domain_at(tmp_path / "domain")
    handle = domain.reserve("product", "first", BINDING)
    with sqlite3.connect(domain.path) as db:
        db.execute("CREATE TRIGGER deny_release BEFORE DELETE ON claim BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END")
    with pytest.raises(InstallerError) as caught:
        handle.close(release=True)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert domain.busy() is True
    with sqlite3.connect(domain.path) as db:
        assert json.loads(db.execute("SELECT body FROM claim WHERE id=1").fetchone()[0])["token"] == handle.token


@pytest.mark.parametrize("phase", ["RESERVED", "QUEUED", "ACTIVE"])
def test_independent_process_exit_requires_exact_explicit_recovery(tmp_path, phase):
    script = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from homensai_install.execution_lease import ExecutionDomain
domain = ExecutionDomain(sys.argv[2], owner_id="synthetic-owner")
handle = domain.reserve("product", "original-operation", sys.argv[3])
if sys.argv[4] in {"QUEUED", "ACTIVE"}: handle.queued()
if sys.argv[4] == "ACTIVE": handle.active()
print(handle.token, flush=True)
os._exit(73)
'''
    directory = tmp_path / "domain"
    child = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                            str(SOURCE), str(directory), BINDING, phase],
                           cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert child.returncode == 73, child.stderr
    token = child.stdout.strip()
    assert len(token) == 32
    domain = domain_at(directory)
    assert domain.busy() is True
    with pytest.raises(InstallerError) as caught:
        domain.reserve("prerequisite", "other-operation", hashlib.sha256(b"other").hexdigest())
    assert caught.value.code == "OPERATION_CONFLICT"
    if phase != "RESERVED":
        with pytest.raises(InstallerError) as caught:
            domain.reserve("product", "original-operation", BINDING)
        assert caught.value.code == "OPERATION_CONFLICT"
    recovered = domain.reserve("product", "original-operation" if phase != "RESERVED" else "new-proposed-id",
                               BINDING, resume=phase != "RESERVED")
    try:
        assert recovered.operation_id == "original-operation" and recovered.token != token
        assert recovered.uncertain is (phase == "ACTIVE")
    finally:
        recovered.close()
    assert domain.busy() is True


@pytest.mark.parametrize("spawn_mode", ["exec", "fork"])
def test_live_external_child_does_not_inherit_lock_but_keeps_unknown_claim(tmp_path, spawn_mode):
    script = r'''
import os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from homensai_install.execution_lease import ExecutionDomain
domain = ExecutionDomain(Path(sys.argv[2]) / "domain", owner_id="synthetic-owner")
handle = domain.reserve("product", "controller", sys.argv[3]); handle.queued(); handle.active()
child_code = """
import sys,time
from pathlib import Path
root=Path(sys.argv[1]); (root/'child-alive').write_text('alive')
deadline=time.monotonic()+15
while not (root/'child-release').exists() and time.monotonic()<deadline: time.sleep(.01)
(root/'child-done').write_text('done')
"""
if sys.argv[4] == "exec":
    child=subprocess.Popen([sys.executable, "-I", "-B", "-c", child_code, sys.argv[2]],
                           close_fds=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = child.pid
else:
    child_pid = os.fork()
    if child_pid == 0:
        os.close(0); os.close(1); os.close(2)
        sys.argv = ["synthetic-child", sys.argv[2]]
        exec(child_code)
        os._exit(0)
deadline=time.monotonic()+5
while not (Path(sys.argv[2])/'child-alive').exists() and time.monotonic()<deadline: time.sleep(.01)
assert (Path(sys.argv[2])/'child-alive').exists()
print(child_pid, flush=True)
os._exit(73)
'''
    child = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(SOURCE), str(tmp_path), BINDING, spawn_mode],
                           cwd=tmp_path, text=True, capture_output=True, timeout=7)
    try:
        assert child.returncode == 73, child.stderr
        assert (tmp_path / "child-alive").exists() and not (tmp_path / "child-done").exists()
        domain = domain_at(tmp_path / "domain")
        with pytest.raises(InstallerError) as caught:
            domain.reserve("prerequisite", "cannot-run", hashlib.sha256(b"foreign").hexdigest())
        assert caught.value.code == "OPERATION_CONFLICT"
        # Exact explicit recovery can acquire the OS lock while the external
        # child is still alive: the descriptor was not inherited through exec.
        recovered = domain.reserve("product", "controller", BINDING, resume=True)
        try:
            assert recovered.uncertain is True and not (tmp_path / "child-done").exists()
        finally:
            recovered.close()
        assert domain.busy() is True
    finally:
        (tmp_path / "child-release").write_text("release", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not (tmp_path / "child-done").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    assert (tmp_path / "child-done").exists()
    assert domain_at(tmp_path / "domain").busy() is True
