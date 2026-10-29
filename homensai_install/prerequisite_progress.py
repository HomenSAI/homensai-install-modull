"""Bounded measured observations; a progress record is not a resume offset.

Only a trusted coordinator writes these records. The signed plan supplies the
artifact identity and total; a transfer token fences callbacks from a previous
download pass. Byte observations precede file fsync, so recovery must inspect
the real partial file and HTTP validator again.
"""
from __future__ import annotations

import re
import uuid

from .errors import InstallerError
from .prerequisite_catalog import fail, identifier

_TRANSFER = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EVENTS = {"download.preparing", "download.attempt", "download.started", "download.bytes",
           "download.retry", "download.cache_hit", "download.verified", "download.local_verified",
           "download.staged_verified", "download.failed", "download.canceled"}
_SOURCES = {"unknown", "network", "cache", "partial", "local"}
MAX_BYTE_CHECKPOINTS = 256


def project_progress(operation, *, plan_digest, component_id, artifact_id, transfer_id,
                     event, start=False, previous_transfer_id=None):
    """Return a safe projection, or None for an obsolete callback.

    The caller reads the latest operation and commits this result in the same
    transaction as its corresponding event. Unknown event fields never enter
    the projection; identifiers and counters are checked before serialization.
    """
    if (type(plan_digest) is not str or not _HASH.fullmatch(plan_digest)
            or not identifier(component_id) or not identifier(artifact_id)
            or type(transfer_id) is not str or not _TRANSFER.fullmatch(transfer_id)
            or type(event) is not dict or type(event.get("type")) is not str or event["type"] not in _EVENTS
            or type(start) is not bool):
        fail("INVALID_REQUEST", "prerequisite_progress_format")
    if operation["plan_digest"] != plan_digest:
        return None
    item = next((item for item in operation["plan"]["components"]
                 if item["component_id"] == component_id), None)
    artifact = next((raw for raw in item["artifacts"] if raw["artifact_id"] == artifact_id), None) if item else None
    step = operation["steps"].get(component_id)
    if not artifact or not step or item["choice"] != "auto":
        fail("INTEGRITY_FAILED", "prerequisite_progress_artifact_binding")
    if operation["status"] != "RUNNING" or step["state"] != "DOWNLOADING":
        return None
    previous = operation.get("download_progress")
    if start:
        if (event["type"] != "download.preparing" or
                (previous.get("transfer_id") if previous else None) != previous_transfer_id):
            return None
        if previous and previous["transfer_id"] == transfer_id:
            fail("INVALID_REQUEST", "prerequisite_progress_transfer_reused")
    elif (not previous or previous.get("transfer_id") != transfer_id
          or previous.get("component_id") != component_id or previous.get("artifact_id") != artifact_id
          or previous.get("artifact_sha256") != artifact["sha256"]):
        return None
    if not start and previous["state"] in {"verified", "failed", "canceled"}:
        return None
    completed, total = event.get("completed"), event.get("total")
    attempt, resumed, source = event.get("attempt"), event.get("resumed"), event.get("source")
    if (type(completed) is not int or type(total) is not int or total != artifact["size"]
            or not 0 <= completed <= total <= 64 * 1024**3
            or type(attempt) is not int or not 0 <= attempt <= 7
            or type(resumed) is not bool or type(source) is not str or source not in _SOURCES
            or event.get("unit") != "bytes"):
        fail("INVALID_REQUEST", "prerequisite_progress_counters")
    kind = event["type"]
    if kind == "download.preparing" and not start:
        fail("INVALID_REQUEST", "prerequisite_progress_initial_state")
    if kind in {"download.attempt", "download.started", "download.bytes", "download.retry"} and (attempt == 0 or source != "network"):
        fail("INVALID_REQUEST", "prerequisite_progress_network_attempt")
    if not start:
        if attempt < previous["attempt"]:
            return None
        if kind == "download.bytes" and (attempt != previous["attempt"]
                or completed < previous["completed"] or previous["state"] == "retrying"):
            return None
    elif completed != 0 or attempt != 0 or resumed or source != "unknown":
        fail("INVALID_REQUEST", "prerequisite_progress_initial_state")
    if kind in {"download.cache_hit", "download.verified", "download.local_verified", "download.staged_verified"} and completed != total:
        fail("INTEGRITY_FAILED", "prerequisite_progress_unverified_size")
    verified, percent = False, None
    if kind in {"download.preparing", "download.attempt"}:
        state = "preparing"
    elif kind in {"download.started", "download.bytes"}:
        state = "verifying" if completed == total else "transferring"
        percent = None if completed == total else completed * 100 // total
    elif kind == "download.retry":
        state = "retrying"
    elif kind == "download.failed":
        state = "failed"
    elif kind == "download.canceled":
        state = "canceled"
    elif kind == "download.staged_verified":
        if (source == "unknown" or previous is None or previous["state"] != "verifying" or previous["completed"] != total
                or source != previous["source"] or attempt != previous["attempt"] or resumed != previous["resumed"]):
            fail("INTEGRITY_FAILED", "prerequisite_progress_unverified_source")
        state, verified, percent = "verified", True, 100
    else:
        state = "verifying"
    return {"schema_version": 1, "plan_digest": plan_digest, "component_id": component_id,
            "artifact_id": artifact_id, "artifact_sha256": artifact["sha256"], "transfer_id": transfer_id,
            "attempt": attempt, "state": state, "completed": completed, "total": total, "unit": "bytes",
            "source": source, "resumed": resumed, "verified": verified, "percent": percent}


class ProgressRecorder:
    """One sequential artifact transfer with mandatory, throttled persistence."""
    def __init__(self, coordinator, operation, component_id, spec):
        self.coordinator, self.operation_id = coordinator, operation["operation_id"]
        self.binding = {"plan_digest": operation["plan_digest"], "component_id": component_id,
                        "artifact_id": spec.artifact_id, "transfer_id": uuid.uuid4().hex}
        self.total = spec.size
        self.observed = {"completed": 0, "total": spec.size, "unit": "bytes", "attempt": 0,
                         "source": "unknown", "resumed": False}
        self.last_bucket = -1
        previous = coordinator._get(self.operation_id).get("download_progress")
        self._record({"type": "download.preparing", **self.observed}, start=True,
                     previous_transfer_id=previous["transfer_id"] if previous else None)

    def _record(self, event, **options):
        try:
            snapshot, durable_event = self.coordinator.store.record_progress(
                self.operation_id, **self.binding, event=event, **options)
        except Exception:
            raise InstallerError("STATE_STORAGE_UNAVAILABLE", phase="prerequisites",
                                 details={"reason": "prerequisite_progress_unavailable"}) from None
        if options.get("start") and snapshot is None:
            fail("STATE_STORAGE_UNAVAILABLE", "prerequisite_progress_start_changed")
        if durable_event is not None:
            self.coordinator._emit(durable_event)
        return snapshot

    def __call__(self, event):
        kind = event["type"]
        self.observed.update({key: event[key] for key in self.observed if key in event})
        if kind in {"download.started", "download.attempt"}:
            self.last_bucket = -1
        if kind == "download.bytes" and self.observed["completed"] != self.total:
            # Distribute the finite event budget over the whole signed size.
            # A slow first kilobyte cannot spend all checkpoints on timer ticks.
            # The first chunk is immediate; later granularity is ceil(size/256).
            bucket = self.observed["completed"] * MAX_BYTE_CHECKPOINTS // self.total
            if bucket <= self.last_bucket:
                return
        snapshot = self._record({"type": kind, **self.observed})
        if snapshot is not None and kind == "download.bytes":
            self.last_bucket = self.observed["completed"] * MAX_BYTE_CHECKPOINTS // self.total
