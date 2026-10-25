"""Durable measured bytes with signed synthetic inputs, never OS installers."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from homensai_install.downloads import ArtifactSpec, DownloadManager
from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from homensai_install.prerequisite_progress import MAX_BYTE_CHECKPOINTS, ProgressRecorder
from homensai_install.trust import SignedPayloadVerifier
from cryptography.hazmat.primitives import serialization
from test_downloads import DATA, Response, Transport, manager, spec
from test_prerequisites_execution import bundle, coordinator, resign


def bigger_base(bundle, content=DATA):
    raw = bundle["descriptor"]["entries"][0]["artifacts"][0]
    bundle["transport"].files[raw["url"]] = content
    raw.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
    resign(bundle)
    bundle["downloads"] = DownloadManager(bundle["tmp_path"] / "cache", trusted_hosts=["downloads.example.org"],
        transport=bundle["transport"], retries=0, chunk_size=1024)
    return raw


def downloading(bundle):
    bigger_base(bundle)
    subject = coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "manual"})
    with subject.store.lock():
        operation = subject._prepare_start("progress-operation", plan, plan["required_consents"])
        operation["status"] = "RUNNING"
        operation["steps"]["base"]["state"] = "DOWNLOADING"
        operation = subject._save(operation, "test_download_boundary", create=True)
    return subject, operation


def binding(operation, transfer="a" * 32):
    return dict(plan_digest=operation["plan_digest"], component_id="base", artifact_id="base-package", transfer_id=transfer)


def progress_event(kind="download.preparing", **changes):
    return {"type": kind, "completed": 0, "total": len(DATA), "unit": "bytes", "attempt": 0,
            "source": "unknown", "resumed": False, **changes}


def begin(subject, operation, transfer="a" * 32, previous=None):
    return subject.store.record_progress(operation["operation_id"], **binding(operation, transfer),
        event=progress_event(), start=True, previous_transfer_id=previous)


@pytest.mark.parametrize("source", ["network", "cache", "partial"])
def test_downloader_mandatory_writer_error_is_never_retry_or_success(tmp_path, source):
    transport = Transport(Response(DATA, headers={"ETag": '"v1"'}), Response(DATA))
    subject = manager(tmp_path, transport)
    if source != "network":
        subject.cache_dir.mkdir(mode=0o700)
        (subject.cache_dir / (spec().sha256 + (".artifact" if source == "cache" else ".part"))).write_bytes(DATA)
    observed = []

    def broken(event):
        if event["type"] in {"download.bytes", "download.cache_hit", "download.verified"}:
            raise OSError("private-path-and-secret-must-not-escape")

    with pytest.raises(InstallerError) as error:
        subject.fetch(spec(), persist_progress=broken, progress=observed.append)
    assert error.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert "private-path" not in str(error.value.to_dict())
    assert len(transport.calls) == (1 if source == "network" else 0)
    assert not any(event["type"] in {"download.retry", "download.verified", "download.cache_hit"} for event in observed)


def test_downloader_observer_failure_does_not_replace_mandatory_writer(tmp_path):
    events = []

    def broken_observer(event):
        raise RuntimeError("synthetic UI failure")

    result = manager(tmp_path, Transport(Response(DATA))).fetch(spec(), persist_progress=events.append, progress=broken_observer)
    assert result["verified"] and events[-1]["type"] == "download.verified"
    assert events[-1]["completed"] == len(DATA) and events[-1]["attempt"] == 1


def test_snapshot_event_cancel_and_stale_controller_save_preserve_each_other(bundle):
    subject, operation = downloading(bundle)
    old = deepcopy(operation)
    begin(subject, operation)
    subject.store.request_cancel(operation["operation_id"])
    snapshot, event = subject.store.record_progress(operation["operation_id"], **binding(operation),
        event=progress_event("download.started", attempt=1, completed=1024, source="network"))
    assert snapshot["percent"] == 25 and event["sequence"] == snapshot["sequence"]
    saved, _ = subject.store.save(old, "stale_controller_snapshot")
    assert saved["cancel_requested"] is True and saved["download_progress"] == snapshot
    assert subject.store.has_claim() and saved["ready"] is False
    events = subject.events(operation["operation_id"], limit=1000)
    assert [event["sequence"] for event in events] == list(range(1, saved["revision"] + 1))


def test_progress_transaction_rolls_back_snapshot_when_event_insert_fails(bundle):
    subject, operation = downloading(bundle)
    begin(subject, operation)
    before, old_events = subject._get(operation["operation_id"]), subject.events(operation["operation_id"])
    with subject.store.connection(write=True) as db:
        db.execute("CREATE TRIGGER fail_progress BEFORE INSERT ON events "
                   "WHEN json_extract(NEW.body,'$.type')='prerequisite_download' "
                   "BEGIN SELECT RAISE(ABORT,'synthetic event write failure'); END")
    with pytest.raises(InstallerError) as error:
        subject.store.record_progress(operation["operation_id"], **binding(operation),
            event=progress_event("download.started", attempt=1, completed=1024, source="network"))
    assert error.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert subject._get(operation["operation_id"]) == before
    assert subject.events(operation["operation_id"]) == old_events


def test_old_transfer_old_attempt_and_wrong_plan_cannot_replace_current_progress(bundle):
    subject, operation = downloading(bundle)
    begin(subject, operation)
    begin(subject, operation, "b" * 32, previous="a" * 32)
    current = binding(operation, "b" * 32)
    subject.store.record_progress(operation["operation_id"], **current,
        event=progress_event("download.started", attempt=2, completed=512, source="network", resumed=True))
    before = subject._get(operation["operation_id"])
    for identity, event in (
        (binding(operation), progress_event("download.bytes", attempt=1, completed=2048, source="network")),
        (current, progress_event("download.bytes", attempt=1, completed=2048, source="network")),
        ({**current, "plan_digest": "f" * 64}, progress_event("download.bytes", attempt=2, completed=2048, source="network")),
    ):
        assert subject.store.record_progress(operation["operation_id"], **identity, event=event) == (None, None)
    assert subject._get(operation["operation_id"]) == before


@pytest.mark.parametrize("changes", [
    {"completed": True}, {"completed": -1}, {"completed": len(DATA) + 1}, {"completed": 1.5},
    {"total": True}, {"total": len(DATA) + 1}, {"total": 2**53}, {"attempt": True},
    {"attempt": 8}, {"source": "https://secret.example/a?token=private"}, {"resumed": 1},
    {"unit": "fake"}, {"type": {}},
])
def test_malformed_counters_cannot_become_durable_progress(bundle, changes):
    subject, operation = downloading(bundle)
    begin(subject, operation)
    before = subject._get(operation["operation_id"])
    with pytest.raises(InstallerError) as error:
        subject.store.record_progress(operation["operation_id"], **binding(operation),
            event=progress_event("download.started", **{"attempt": 1, "source": "network", **changes}))
    assert error.value.code == "INVALID_REQUEST"
    assert subject._get(operation["operation_id"]) == before


def test_unknown_callback_fields_are_not_in_snapshot_or_events(bundle):
    subject, operation = downloading(bundle)
    begin(subject, operation)
    snapshot, _ = subject.store.record_progress(operation["operation_id"], **binding(operation),
        event=progress_event("download.started", attempt=1, source="network", completed=1024,
                             url="https://example.org?token=never-public", path="/private/name", etag="private-etag"))
    encoded = json.dumps(subject.status(operation["operation_id"]))
    assert "never-public" not in encoded and "private-etag" not in encoded and "/private/name" not in encoded
    assert snapshot["artifact_sha256"] == operation["plan"]["components"][0]["artifacts"][0]["sha256"]


def test_fresh_coordinator_and_independent_process_observe_committed_mid_download(bundle):
    bigger_base(bundle)
    entered, release = threading.Event(), threading.Event()
    results = []

    def observe(event):
        snapshot = event.get("download_progress", {})
        if snapshot.get("completed") == 1024 and snapshot.get("state") == "transferring":
            entered.set()
            assert release.wait(10)

    subject = coordinator(bundle, event_callback=observe)
    api = InstallerAPI(bundle["tmp_path"] / "product-state", prerequisites=subject)
    plan = api.prerequisites_plan({"base": "auto", "client": "manual"})
    worker = threading.Thread(target=lambda: results.append(api.prerequisites_start("live-progress", plan, plan["required_consents"])))
    worker.start()
    try:
        assert entered.wait(10)
        fresh_api = InstallerAPI(bundle["tmp_path"] / "product-state", prerequisites=coordinator(bundle))
        fresh = fresh_api.prerequisites_status("live-progress")
        assert fresh["download_progress"]["completed"] == 1024 and fresh["download_progress"]["percent"] == 25
        child = subprocess.run([sys.executable, "-I", "-B", "-c",
            "import json,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "r=json.loads(c.execute('select body from operations where id=?',(sys.argv[2],)).fetchone()[0]); "
            "print(json.dumps(r['download_progress'],sort_keys=True))",
            str(subject.store.path), "live-progress"], cwd=bundle["tmp_path"], capture_output=True, text=True, timeout=10, check=True)
        assert json.loads(child.stdout) == fresh["download_progress"]
        cursor, delivered = 0, []
        while page := fresh_api.prerequisites_events("live-progress", after_sequence=cursor, limit=2)["events"]:
            delivered.extend(page)
            cursor = page[-1]["sequence"]
        assert cursor == fresh["revision"]
        assert [event["sequence"] for event in delivered] == list(range(1, cursor + 1))
        assert bundle["calls"] == []
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and results[0]["status"] == "AWAITING_USER"
    assert results[0]["download_progress"]["verified"] and not results[0]["ready"]


def test_cancel_mid_transfer_remains_canceled_and_never_runs_installer(bundle):
    bigger_base(bundle)
    subject = None

    def observe(event):
        if event.get("download_progress", {}).get("state") == "transferring" and event["download_progress"]["completed"]:
            coordinator(bundle).cancel(event["operation_id"])

    subject = coordinator(bundle, event_callback=observe)
    plan = subject.plan({"base": "auto", "client": "manual"})
    result = subject.start("cancel-progress", plan, consents=plan["required_consents"])
    assert result["status"] == "CANCELED" and not bundle["calls"]
    progress = result["download_progress"]
    assert progress["state"] == "canceled" and progress["percent"] is None and not progress["verified"]
    assert progress["completed"] == 1024


@pytest.mark.parametrize("when", ["download.bytes", "download.staged_verified"])
def test_mandatory_storage_failure_before_installer_is_not_hidden(bundle, monkeypatch, when):
    bigger_base(bundle)
    subject = coordinator(bundle)
    original = subject.store.record_progress

    def fail_at_boundary(*args, **kwargs):
        if kwargs["event"]["type"] == when:
            raise OSError("synthetic full disk; private details")
        return original(*args, **kwargs)

    monkeypatch.setattr(subject.store, "record_progress", fail_at_boundary)
    plan = subject.plan({"base": "auto", "client": "manual"})
    result = subject.start("storage-failure", plan, consents=plan["required_consents"])
    assert result["error"]["code"] == "STATE_STORAGE_UNAVAILABLE" and result["status"] == "AWAITING_USER"
    assert not bundle["calls"] and result["steps"]["base"]["attempt"] == 0
    assert result["download_progress"]["percent"] != 100
    assert "private details" not in json.dumps(result)


def test_complete_bad_hash_never_reaches_verified_or_false_hundred(bundle):
    raw = bigger_base(bundle)
    bundle["transport"].files[raw["url"]] = b"x" * raw["size"]
    captured = []
    subject = coordinator(bundle, event_callback=captured.append)
    plan = subject.plan({"base": "auto", "client": "manual"})
    result = subject.start("bad-hash", plan, consents=plan["required_consents"])
    projections = [event["download_progress"] for event in captured if "download_progress" in event]
    assert any(item["completed"] == raw["size"] and item["state"] == "verifying" for item in projections)
    assert all(item["percent"] != 100 and not item["verified"] for item in projections)
    assert result["download_progress"]["state"] == "failed" and not bundle["calls"]


def test_corrupt_offline_cache_never_claims_verified_or_contacts_transport(bundle):
    raw = bigger_base(bundle)
    bundle["downloads"].cache_dir.mkdir(mode=0o700)
    (bundle["downloads"].cache_dir / (raw["sha256"] + ".artifact")).write_bytes(b"corrupt")
    subject = coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "manual"}, offline=True)
    result = subject.start("offline-corrupt", plan, consents=plan["required_consents"])
    assert result["error"]["code"] == "PREREQUISITE_MISSING"
    assert result["download_progress"]["state"] == "failed" and result["download_progress"]["percent"] is None
    assert not result["download_progress"]["verified"] and not bundle["transport"].calls and not bundle["calls"]


@pytest.mark.parametrize("source", ["cache", "partial", "local"])
def test_verified_sources_are_measured_and_staged_before_hundred(bundle, source):
    raw = bigger_base(bundle)
    kwargs = {"offline": True}
    if source == "local":
        path = bundle["tmp_path"] / "local-synthetic.py"
        kwargs["local_artifacts"] = {"base-package": str(path)}
    else:
        bundle["downloads"].cache_dir.mkdir(mode=0o700)
        path = bundle["downloads"].cache_dir / (raw["sha256"] + (".artifact" if source == "cache" else ".part"))
    path.write_bytes(DATA)
    captured = []
    subject = coordinator(bundle, event_callback=captured.append)
    plan = subject.plan({"base": "auto", "client": "manual"}, **kwargs)
    result = subject.start("verified-source", plan, consents=plan["required_consents"])
    progress = result["download_progress"]
    assert progress["source"] == source and progress["state"] == "verified" and progress["percent"] == 100
    assert progress["completed"] == len(DATA) and not result["ready"]
    assert not bundle["transport"].calls and bundle["calls"] == ["base"]
    projections = [event["download_progress"] for event in captured if "download_progress" in event]
    assert any(item["state"] == "verifying" and item["percent"] is None for item in projections)


def test_staged_copy_corruption_is_failure_even_after_source_verifies(bundle, monkeypatch):
    bigger_base(bundle)
    subject = coordinator(bundle)
    original = subject.downloads.verify

    def verify(spec, path):
        if Path(path).name == "artifact_0.py":
            return False
        return original(spec, path)

    monkeypatch.setattr(subject.downloads, "verify", verify)
    plan = subject.plan({"base": "auto", "client": "manual"})
    result = subject.start("staged-corrupt", plan, consents=plan["required_consents"])
    assert result["error"]["code"] == "INTEGRITY_FAILED" and not bundle["calls"]
    assert result["download_progress"]["state"] == "failed" and result["download_progress"]["percent"] is None


def test_retry_uses_real_partial_offset_and_new_process_read_does_not_resume(bundle):
    raw = bigger_base(bundle)
    initial = Transport(Response(DATA, headers={"ETag": '"same"'}, fail_after=1024))
    bundle["downloads"].transport = initial
    subject = coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "manual"})
    failed = subject.start("resume-download", plan, consents=plan["required_consents"])
    assert failed["download_progress"]["state"] == "failed" and failed["download_progress"]["completed"] == 1024
    old_transfer = failed["download_progress"]["transfer_id"]
    second = Transport(Response(DATA[1024:], status=206, headers={"ETag": '"same"',
        "Content-Length": str(len(DATA) - 1024), "Content-Range": f"bytes 1024-{len(DATA)-1}/{len(DATA)}"}))
    bundle["downloads"].transport = second
    restored = coordinator(bundle)
    assert restored.status("resume-download")["download_progress"] == failed["download_progress"]
    assert not second.calls and not bundle["calls"]
    result = restored.resume("resume-download")
    assert result["operation_id"] == "resume-download" and bundle["calls"] == ["base"]
    assert second.calls[0][1]["Range"] == "bytes=1024-"
    assert result["download_progress"]["transfer_id"] != old_transfer and result["download_progress"]["resumed"]
    assert result["download_progress"]["completed"] == raw["size"]
    assert any(event.get("download_progress", {}).get("transfer_id") == old_transfer for event in restored.events("resume-download"))


@pytest.mark.parametrize("response_kind", ["resume", "reset", "bad_range_then_reset"])
def test_network_retries_keep_one_transfer_and_never_sum_attempt_bytes(bundle, response_kind):
    bigger_base(bundle)
    responses = [Response(DATA, headers={"ETag": '"same"'}, fail_after=1024)]
    if response_kind in {"resume", "bad_range_then_reset"}:
        responses.append(Response(DATA[1024:], status=206, headers={
            "ETag": '"same"' if response_kind == "resume" else '"different"',
            "Content-Range": f"bytes 1024-{len(DATA)-1}/{len(DATA)}"}))
    if response_kind in {"reset", "bad_range_then_reset"}:
        responses.append(Response(DATA, headers={"ETag": '"new"'}))
    transport = Transport(*responses)
    bundle["downloads"].transport, bundle["downloads"].retries = transport, 1
    captured = []
    subject = coordinator(bundle, event_callback=captured.append)
    plan = subject.plan({"base": "auto", "client": "manual"})
    result = subject.start("retrying-transfer", plan, consents=plan["required_consents"])
    projections = [event["download_progress"] for event in captured if "download_progress" in event]
    assert len({item["transfer_id"] for item in projections}) == 1
    assert max(item["completed"] for item in projections) == len(DATA)
    assert all(item["percent"] is None for item in projections if item["state"] == "retrying")
    assert result["download_progress"]["attempt"] == (3 if response_kind == "bad_range_then_reset" else 2)
    assert result["download_progress"]["resumed"] is (response_kind == "resume")
    if response_kind != "resume":
        assert any(item["attempt"] >= 2 and item["state"] == "transferring" and item["completed"] == 0 for item in projections)
    assert result["download_progress"]["state"] == "verified" and bundle["calls"] == ["base"]


def test_multiple_component_artifacts_have_separate_transfers_and_retained_events(bundle):
    captured = []
    subject = coordinator(bundle, event_callback=captured.append)
    plan = subject.plan({"base": "auto", "client": "auto"})
    result = subject.start("multiple-artifacts", plan, consents=plan["required_consents"])
    verified = [event["download_progress"] for event in captured
                if event.get("download_progress", {}).get("state") == "verified"]
    assert [item["artifact_id"] for item in verified] == ["base-package", "client-package"]
    assert len({item["transfer_id"] for item in verified}) == 2
    assert result["download_progress"] == verified[-1] and result["status"] == "READY"
    assert all(item["completed"] == item["total"] for item in verified)


def _killed_download_client(config_path):
    """Independent controller; signal only after the byte observation commits."""
    from test_prerequisites_execution import Transport as FixtureTransport
    value = json.loads(Path(config_path).read_text())
    value["state_dir"], value["tmp_path"] = Path(value["state_dir"]), Path(value["tmp_path"])
    value["transport"] = FixtureTransport({key: bytes.fromhex(content) for key, content in value.pop("files").items()})
    value["verifier"] = SignedPayloadVerifier({"synthetic-test": bytes.fromhex(value.pop("public_key"))}, allow_test_keys=True)
    value.update(calls=[], effects={}, results={})
    value["downloads"] = DownloadManager(value["tmp_path"] / "cache", trusted_hosts=["downloads.example.org"],
                                        transport=value["transport"], retries=0, chunk_size=1024)

    def observer(event):
        if event.get("download_progress", {}).get("completed") == 1024:
            Path(value["signal"]).write_text("committed")
            threading.Event().wait(30)
            raise RuntimeError("parent did not terminate its fixture controller")

    subject = coordinator(value, event_callback=observer)
    subject.start("killed-progress", value["plan"], consents=value["plan"]["required_consents"])
    raise RuntimeError("test controller must be killed before installer")


@pytest.mark.skipif(os.name != "posix", reason="Owned POSIX process-kill fixture; Windows acceptance remains separate")
def test_real_controller_kill_does_not_treat_observed_bytes_as_fsynced_resume_offset(bundle):
    raw = bigger_base(bundle)
    subject = coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "manual"})
    signal, config = bundle["tmp_path"] / "download-committed", bundle["tmp_path"] / "download-client.json"
    config.write_text(json.dumps({key: bundle[key] for key in ("manifest", "target", "options", "inventory", "markers")} | {
        "state_dir": str(bundle["state_dir"]), "tmp_path": str(bundle["tmp_path"]), "signal": str(signal), "plan": plan,
        "files": {key: value.hex() for key, value in bundle["transport"].files.items()},
        "public_key": bundle["private"].public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()}))
    code = ("import sys; sys.path[:0]=sys.argv[1:3]; "
            "from test_prerequisites_progress import _killed_download_client; _killed_download_client(sys.argv[3])")
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", code,
                               str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent), str(config)],
                              cwd=bundle["tmp_path"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not signal.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert signal.exists(), process.communicate(timeout=1)
        before = coordinator(bundle).status("killed-progress")
        assert before["download_progress"]["completed"] == 1024
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.communicate(timeout=10)
    assert process.returncode != 0
    partial = bundle["downloads"].cache_dir / (raw["sha256"] + ".part")
    actual_offset = partial.stat().st_size
    assert actual_offset == 0  # Buffered output had not flushed at the gated callback.
    assert coordinator(bundle).status("killed-progress")["download_progress"] == before["download_progress"]
    retry_transport = Transport(Response(DATA, headers={"ETag": '"synthetic-v1"'}))
    bundle["downloads"].transport = retry_transport
    after = coordinator(bundle).resume("killed-progress")
    assert "Range" not in retry_transport.calls[0][1]
    assert after["download_progress"]["transfer_id"] != before["download_progress"]["transfer_id"]
    assert after["download_progress"]["state"] == "verified" and after["download_progress"]["percent"] == 100
    assert bundle["calls"] == ["base"] and after["steps"]["base"]["attempt"] == 1


@pytest.mark.parametrize("kind", ["download.started", "download.bytes", "download.retry"])
def test_download_measurement_requires_a_network_attempt(bundle, kind):
    subject, operation = downloading(bundle)
    begin(subject, operation)
    with pytest.raises(InstallerError) as error:
        subject.store.record_progress(operation["operation_id"], **binding(operation), event=progress_event(kind))
    assert error.value.code == "INVALID_REQUEST"


def test_staged_verified_cannot_skip_the_measured_verification_boundary(bundle):
    subject, operation = downloading(bundle)
    begin(subject, operation)
    with pytest.raises(InstallerError) as error:
        subject.store.record_progress(operation["operation_id"], **binding(operation),
            event=progress_event("download.staged_verified", completed=len(DATA), source="local"))
    assert error.value.code == "INTEGRITY_FAILED"


@pytest.mark.parametrize("same_plan", [False, True])
def test_replan_clears_projection_and_keeps_history_without_changing_plan_v1(bundle, same_plan):
    subject, operation = downloading(bundle)
    original_plan = deepcopy(operation["plan"])
    begin(subject, operation)
    assert subject._get(operation["operation_id"])["plan"] == original_plan
    plan = original_plan if same_plan else subject.plan({"base": "manual", "client": "manual"})
    result = subject.replace_plan(operation["operation_id"], plan, consents=plan["required_consents"])
    assert "download_progress" not in result
    assert any("download_progress" in event for event in subject.events(operation["operation_id"]))
    assert subject._validate_plan(original_plan, original_plan["required_consents"])[0] == original_plan


def test_byte_checkpoints_follow_size_buckets_without_early_budget_exhaustion(bundle, monkeypatch):
    subject, operation = downloading(bundle)
    ticks = iter(range(0, 20000, 2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    artifact = ArtifactSpec.from_dict(bundle["descriptor"]["entries"][0]["artifacts"][0])
    writer = ProgressRecorder(subject, operation, "base", artifact)
    writer(progress_event("download.started", attempt=1, source="network"))
    for completed in range(1, 1001):
        writer(progress_event("download.bytes", attempt=1, source="network", completed=completed))
    writer(progress_event("download.bytes", attempt=1, source="network", completed=3072))
    assert subject._get(operation["operation_id"])["download_progress"]["completed"] == 3072
    writer(progress_event("download.bytes", attempt=1, source="network", completed=len(DATA)))
    events = subject.events(operation["operation_id"], limit=1000)
    measured = [event for event in events if event.get("download_progress", {}).get("state") == "transferring"]
    assert len(measured) <= MAX_BYTE_CHECKPOINTS + 1  # Includes started(0).
    assert events[-1]["download_progress"]["state"] == "verifying"
    assert events[-1]["download_progress"]["percent"] is None


@pytest.mark.parametrize("cleanup_failure", ["response_close", "partial_fsync"])
def test_mandatory_progress_failure_cannot_be_masked_by_cleanup(tmp_path, monkeypatch, cleanup_failure):
    flags = {"writer_failed": False, "cleanup_failed": False}

    class BrokenClose(Response):
        def close(self):
            super().close()
            if cleanup_failure == "response_close" and flags["writer_failed"] and not flags["cleanup_failed"]:
                flags["cleanup_failed"] = True
                raise OSError("synthetic response cleanup failure")

    transport = Transport(BrokenClose(DATA, headers={"ETag": '"first"'}), Response(DATA))
    downloader = DownloadManager(tmp_path / "cache", trusted_hosts=["downloads.example.org"],
                                 transport=transport, retries=1, chunk_size=1024)
    observed = []

    def writer(event):
        if event["type"] == "download.bytes" and not flags["writer_failed"]:
            flags["writer_failed"] = True
            raise OSError("synthetic mandatory journal failure")

    original_fsync = os.fsync

    def fail_cleanup_fsync(descriptor):
        if cleanup_failure == "partial_fsync" and flags["writer_failed"] and not flags["cleanup_failed"]:
            flags["cleanup_failed"] = True
            raise OSError("synthetic partial fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_cleanup_fsync)
    with pytest.raises(InstallerError) as caught:
        downloader.fetch(spec(), persist_progress=writer, progress=observed.append)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert flags == {"writer_failed": True, "cleanup_failed": True}
    assert len(transport.calls) == 1
    assert not any(event["type"] in {"download.retry", "download.verified"} for event in observed)


@pytest.mark.parametrize("cleanup_failure", ["response_close", "partial_fsync"])
def test_failed_progress_and_cleanup_cannot_reach_coordinator_installer(bundle, monkeypatch, cleanup_failure):
    bigger_base(bundle)
    flags = {"writer_failed": False, "cleanup_failed": False}

    class BrokenClose(Response):
        def close(self):
            super().close()
            if cleanup_failure == "response_close" and flags["writer_failed"] and not flags["cleanup_failed"]:
                flags["cleanup_failed"] = True
                raise OSError("synthetic response cleanup failure")

    transport = Transport(BrokenClose(DATA, headers={"ETag": '"first"'}), Response(DATA))
    bundle["downloads"].transport, bundle["downloads"].retries = transport, 1
    subject = coordinator(bundle)
    original_writer, original_fsync = subject.store.record_progress, os.fsync

    def broken_writer(*args, **kwargs):
        if kwargs["event"]["type"] == "download.bytes" and not flags["writer_failed"]:
            flags["writer_failed"] = True
            raise OSError("synthetic mandatory journal failure")
        return original_writer(*args, **kwargs)

    def broken_fsync(descriptor):
        if cleanup_failure == "partial_fsync" and flags["writer_failed"] and not flags["cleanup_failed"]:
            flags["cleanup_failed"] = True
            raise OSError("synthetic partial fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(subject.store, "record_progress", broken_writer)
    monkeypatch.setattr(os, "fsync", broken_fsync)
    plan = subject.plan({"base": "auto", "client": "manual"})
    result = subject.start("double-failure", plan, consents=plan["required_consents"])
    assert result["error"]["code"] == "STATE_STORAGE_UNAVAILABLE"
    assert flags == {"writer_failed": True, "cleanup_failed": True}
    assert len(transport.calls) == 1 and bundle["calls"] == []
    assert result["steps"]["base"]["attempt"] == 0 and result["download_progress"]["verified"] is False
