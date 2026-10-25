"""Real owned Linux child/zombie; no product, network or daemon execution.

Run in the actual PID/proc namespace. A default sandbox with a different proc
view cannot provide this evidence and is explicitly rejected by the fixture.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from homensai_install.api import v1
from homensai_install.api.journal import now


pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not hasattr(os, "WNOWAIT"),
                                reason="Requires Linux waitid(WNOWAIT) and proc")


@pytest.fixture
def owned_child():
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c",
        "import os,sys; sys.stdin.buffer.read(1); os._exit(0)"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
        assert int(fields[1]) == os.getpid(), "Requires matching actual PID/proc namespace"
        owner = {"pid": process.pid, "process_start": v1._process_start(process.pid)}
        assert owner["process_start"] is not None
        yield process, owner
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()  # Only this fixture's still-owned Popen child.
            process.wait(timeout=5)


def leave_unreaped(process):
    process.stdin.close()
    deadline = time.monotonic() + 5
    while True:
        outcome = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        if outcome is not None:
            assert outcome.si_status == 0
            break
        assert time.monotonic() < deadline, "Owned child did not exit"
        time.sleep(0.01)
    fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
    assert int(fields[1]) == os.getpid() and fields[0] == "Z"
    assert int(fields[17]) == 1


def test_live_owner_matches_birth_and_rejects_an_old_instance(owned_child):
    process, owner = owned_child
    assert v1._owner_alive(owner) is True
    assert v1._owner_alive({**owner, "process_start": owner["process_start"] + "-old-instance"}) is False
    assert process.poll() is None


def test_unreaped_owned_zombie_is_dead_even_though_signal_zero_succeeds(owned_child):
    process, owner = owned_child
    leave_unreaped(process)
    os.kill(process.pid, 0)
    assert v1._process_start(process.pid) == owner["process_start"]
    assert v1._owner_alive(owner) is False
    process.wait(timeout=5)
    assert v1._owner_alive(owner) is False


def test_zombie_owner_marks_api_recovery_once_without_engine_effects(tmp_path, owned_child):
    process, owner = owned_child
    class NoEffects:
        calls = 0
        def execute(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("Status must never execute or resume an operation")
        resume = execute
    engine = NoEffects()
    api = v1.InstallerAPI(tmp_path / "state", engine=engine)
    operation = {"api_version": v1.API_VERSION, "operation_id": "op_" + uuid.uuid4().hex,
        "plan_id": "plan_" + uuid.uuid4().hex, "plan_digest": "a" * 64,
        "action": "install", "status": "RUNNING", "cancel_requested": False,
        "last_sequence": 0, "created_at": now(), "updated_at": now(), "owner": owner,
        "consent_refs": [], "attempt": 1, "result": None, "error": None}
    journal = api._journal(create=True)
    journal.create_operation(operation, "liveness-fixture", "b" * 64, "c" * 64)
    assert api.status(operation["operation_id"])["status"] == "RUNNING"
    leave_unreaped(process)
    recovered = api.status(operation["operation_id"])
    assert recovered["operation_id"] == operation["operation_id"]
    assert recovered["status"] == "RECOVERY_REQUIRED" and recovered["attempt"] == 1
    assert api.status(operation["operation_id"])["last_sequence"] == recovered["last_sequence"]
    events = api.events(operation["operation_id"])["events"]
    assert sum(event["type"] == "operation.interrupted" for event in events) == 1
    assert engine.calls == 0


@pytest.mark.parametrize("failure", [PermissionError("restricted"), OSError("unavailable")])
def test_signal_access_failure_stays_conservatively_alive(monkeypatch, failure):
    def fail(*args):
        raise failure
    monkeypatch.setattr(v1.os, "kill", fail)
    assert v1._owner_alive({"pid": 123, "process_start": "100"}) is True


@pytest.mark.parametrize("failure", [PermissionError("restricted"), OSError("unavailable"),
                                    UnicodeError("unreadable")])
def test_unreadable_proc_is_not_evidence_of_owner_death(monkeypatch, failure):
    monkeypatch.setattr(v1.os, "kill", lambda *args: None)
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(v1.Path, "read_text", fail)
    assert v1._owner_alive({"pid": 123, "process_start": "100"}) is True


def test_malformed_proc_keeps_owner_unknown_and_alive(monkeypatch):
    monkeypatch.setattr(v1.os, "kill", lambda *args: None)
    monkeypatch.setattr(v1.Path, "read_text", lambda *args, **kwargs: "123 (incomplete stat)")
    assert v1._owner_alive({"pid": 123, "process_start": "100"}) is True


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_dead_state_with_additional_threads_does_not_claim_group_exit(monkeypatch, state):
    monkeypatch.setattr(v1.os, "kill", lambda *args: None)
    monkeypatch.setattr(v1, "_process_identity", lambda pid: (state, "100", 2))
    assert v1._owner_alive({"pid": 123, "process_start": "100"}) is True
