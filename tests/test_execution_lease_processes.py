"""Независимые OS processes для общей product/prerequisite блокировки.

Real interpreter exits, private journals and file locks; signed synthetic
prerequisites and fake product engine. This is not platform installation QA.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

import homensai_install
from homensai_install.errors import InstallerError


SOURCE = Path(homensai_install.__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("test_execution_lease_process_worker.py")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Independent Linux synthetic lease contract")


def message(process):
    ready, _, _ = select.select([process.stdout], [], [], 8)
    assert ready, "Own synthetic child did not reach its expected barrier"
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def command(directory, action, kind, phase="effect", mode="synchronous"):
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(WORKER), str(SOURCE), str(directory),
         action, kind, phase, mode], cwd=directory, text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert len(lines) == 1, lines
    return lines[0]


@contextmanager
def held(directory, kind, phase="effect", mode="synchronous"):
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(WORKER), str(SOURCE), str(directory),
         "hold", kind, phase, mode], cwd=directory, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        observed = message(process)
        assert observed["event"] == "gate", observed
        assert observed["kind"] == kind and observed["phase"] == phase
        assert observed["pid"] != os.getpid()
        yield process, observed
    finally:
        if process.poll() is None:
            try:
                process.stdin.write("release\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()  # Only the Popen child created by this test.
            process.communicate(timeout=5)


@pytest.mark.parametrize("first", ["product", "prerequisite"])
@pytest.mark.parametrize("mode", ["synchronous", "background"])
def test_independent_product_and_prerequisite_controllers_exclude_each_other(tmp_path, first, mode):
    second = "prerequisite" if first == "product" else "product"
    with held(tmp_path, first, mode=mode) as (process, observed):
        assert observed["busy"] is True
        contender = command(tmp_path, "contend", second)
        assert contender["event"] == "error" and contender["code"] == "OPERATION_CONFLICT"
        assert contender["effect"] is False and contender["calls"] == []
        assert not (tmp_path / "contender" / "effect-entered").exists()
        process.stdin.write("release\n")
        process.stdin.flush()
        result = message(process)
        assert result["event"] == "result", result
        assert result["result"]["status"] == ("SUCCEEDED" if first == "product" else "READY")
        assert result["busy"] is False
        assert process.wait(timeout=5) == 0
    after = command(tmp_path, "contend", second)
    assert after["event"] == "result", after
    assert after["result"]["status"] == ("SUCCEEDED" if second == "product" else "READY")
    assert after["effect"] is True


@pytest.mark.parametrize("first", ["product", "prerequisite"])
@pytest.mark.parametrize("phase", ["enqueue", "queued", "terminal"])
def test_same_lease_covers_enqueue_handoff_and_final_observer(tmp_path, first, phase):
    second = "prerequisite" if first == "product" else "product"
    with held(tmp_path, first, phase=phase, mode="background") as (_, observed):
        assert observed["busy"] is True
        contender = command(tmp_path, "contend", second)
        assert contender["event"] == "error" and contender["code"] == "OPERATION_CONFLICT"
        assert contender["effect"] is False and contender["calls"] == []
        if phase in {"enqueue", "queued"}:
            assert not (tmp_path / "holder" / "effect-entered").exists()
        else:
            assert (tmp_path / "holder" / "effect-entered").exists()


@pytest.mark.parametrize("first", ["product", "prerequisite"])
def test_lost_ack_duplicate_is_read_only_while_first_process_executes(tmp_path, first):
    with held(tmp_path, first, mode="background") as (_, observed):
        effect_path = tmp_path / "holder" / "effect-entered"
        before = effect_path.stat().st_mtime_ns
        duplicate = command(tmp_path, "duplicate", first, mode="background")
        assert duplicate["event"] == "result", duplicate
        assert duplicate["result"]["operation_id"] == observed["operation_id"]
        assert duplicate["calls"] == []
        assert duplicate["busy"] is True
        assert effect_path.stat().st_mtime_ns == before


@pytest.mark.parametrize("first", ["product", "prerequisite"])
@pytest.mark.parametrize("phase", ["queued", "effect"])
def test_real_controller_exit_keeps_durable_claim_and_replay_does_not_execute(tmp_path, first, phase):
    second = "prerequisite" if first == "product" else "product"
    with held(tmp_path, first, phase=phase, mode="background") as (process, observed):
        process.stdin.write("crash\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 73
    effect_path = tmp_path / "holder" / "effect-entered"
    before = effect_path.stat().st_mtime_ns if effect_path.exists() else None
    duplicate = command(tmp_path, "duplicate", first, mode="background")
    assert duplicate["event"] == "result", duplicate
    assert duplicate["result"]["operation_id"] == observed["operation_id"]
    assert duplicate["calls"] == [] and duplicate["busy"] is True
    assert (effect_path.stat().st_mtime_ns if effect_path.exists() else None) == before
    contender = command(tmp_path, "contend", second)
    assert contender["event"] == "error" and contender["code"] == "OPERATION_CONFLICT"
    assert contender["effect"] is False
    if phase == "effect":
        canceled = command(tmp_path, "cancel", first)
        assert canceled["event"] == "result" and canceled["busy"] is True
        after_cancel = command(tmp_path, "contend", second)
        assert after_cancel["event"] == "error" and after_cancel["code"] == "OPERATION_CONFLICT"
        assert after_cancel["effect"] is False


@pytest.mark.parametrize("first", ["product", "prerequisite"])
def test_exact_start_recovers_reserved_gap_with_original_id(tmp_path, first):
    with held(tmp_path, first, phase="enqueue", mode="background") as (process, observed):
        process.stdin.write("crash\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 73
    assert not (tmp_path / "holder" / "effect-entered").exists()
    retry = command(tmp_path, "duplicate", first)
    assert retry["event"] == "result", retry
    assert retry["result"]["operation_id"] == observed["operation_id"]
    assert retry["result"]["status"] == ("SUCCEEDED" if first == "product" else "READY")
    assert retry["effect"] is True and retry["busy"] is False


@pytest.mark.parametrize("first", ["product", "prerequisite"])
def test_terminal_write_is_not_automatic_release_after_controller_exit(tmp_path, first):
    second = "prerequisite" if first == "product" else "product"
    with held(tmp_path, first, phase="terminal", mode="background") as (process, observed):
        process.stdin.write("crash\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 73
    duplicate = command(tmp_path, "duplicate", first, mode="background")
    assert duplicate["event"] == "result", duplicate
    assert duplicate["result"]["operation_id"] == observed["operation_id"]
    assert duplicate["result"]["status"] == ("SUCCEEDED" if first == "product" else "READY")
    assert duplicate["busy"] is True and duplicate["calls"] == []
    contender = command(tmp_path, "contend", second)
    assert contender["event"] == "error" and contender["code"] == "OPERATION_CONFLICT"
    assert contender["effect"] is False
    effect = tmp_path / "holder" / "effect-entered"
    before = effect.stat().st_mtime_ns
    recovered = command(tmp_path, "resume", first)
    assert recovered["event"] == "result", recovered
    assert recovered["result"]["operation_id"] == observed["operation_id"]
    assert recovered["result"]["status"] == ("SUCCEEDED" if first == "product" else "READY")
    assert recovered["busy"] is False and recovered["calls"] == []
    assert effect.stat().st_mtime_ns == before, "Explicit terminal recovery repeated an effect"
    after = command(tmp_path, "contend", second)
    assert after["event"] == "result" and after["effect"] is True


def test_domain_and_capabilities_are_lazy_and_busy_does_not_expose_private_claim(tmp_path):
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.execution_lease import ExecutionDomain
    from test_public_api import Engine

    directory = tmp_path / "must-stay-absent"
    domain = ExecutionDomain(directory, owner_id="synthetic-owner")
    api = InstallerAPI(tmp_path / "api-absent", engine=Engine(tmp_path), execution_domain=domain)
    api.describe()
    api.capabilities()
    assert domain.busy() is False
    assert not directory.exists() and not api.state_dir.exists()
    handle = domain.reserve("product", "private-operation-id", hashlib.sha256(b"binding").hexdigest())
    try:
        assert domain.busy() is True
        assert isinstance(domain.busy(), bool)
        other = ExecutionDomain(directory, owner_id="synthetic-owner")
        with pytest.raises(InstallerError) as caught:
            other.reserve("prerequisite", "different-operation", hashlib.sha256(b"other").hexdigest())
        public = json.dumps(caught.value.to_dict(locale="en"))
        assert "private-operation-id" not in public and handle.token not in public and str(directory) not in public
    finally:
        handle.close(release=True)
