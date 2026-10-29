"""Opt-in background dispatch for the same durable prerequisite coordinator.

The in-process reservation only bounds threads. The private store claim and
nonblocking OS lock remain authoritative across processes. No worker is
restarted by a constructor, status request, listing or duplicate start.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

from .errors import InstallerError, utcnow
from .prerequisite_catalog import fail, identifier

_WORKERS = {}
_WORKERS_LOCK = threading.RLock()
_TERMINAL = {"READY", "CANCELED"}
_ACTIVE = {"QUEUED", "RUNNING"}


def _key(coordinator):
    # A second API may have another product-state directory, while sharing the
    # same prerequisite journal. Forked children must not inherit reservations.
    return os.getpid(), os.path.normcase(str(coordinator.store.directory.resolve()))


def _reserved(coordinator, operation):
    with _WORKERS_LOCK:
        worker = _WORKERS.get(_key(coordinator))
        return (worker is not None and worker.operation_id == operation["operation_id"]
                and worker.token == operation.get("_dispatch", {}).get("token"))


def execution_status(coordinator, operation):
    """Project interruption without writing or claiming an installer outcome."""
    dispatch = operation.get("_dispatch", {})
    state = dispatch.get("state")
    mode = "background" if dispatch else "synchronous"
    # FINISHED records the durable drive result, not release of its controller.
    # Its final observer may still commit an accepted cancellation. Do not tell
    # a poller to stop while that worker still owns its reservation/OS lock.
    if dispatch and _reserved(coordinator, operation):
        return {"mode": mode, "state": state.lower(), "active": True}
    pending = operation["status"] not in _TERMINAL and (state in _ACTIVE or operation["status"] == "RUNNING")
    if pending or dispatch:
        busy = coordinator.store.controller_busy()
        if busy is not False:
            # Another process may still be finalizing a FINISHED dispatch, or
            # the lock is unavailable. Neither proves that polling may stop.
            return {"mode": mode, "state": "unknown", "active": None}
        if pending:
            return {"mode": mode, "state": "recovery_required", "active": False,
                    "reason": "prerequisite_controller_interrupted"}
    return {"mode": mode, "state": state.lower() if state and state not in _ACTIVE else "idle", "active": False}


class _Worker:
    def __init__(self, coordinator, operation_id, lease):
        self.coordinator, self.operation_id = coordinator, operation_id
        self.token, self.revision, self.queued_event = uuid.uuid4().hex, None, None
        self.lease = lease

    def release(self):
        with _WORKERS_LOCK:
            if _WORKERS.get(_key(self.coordinator)) is self:
                del _WORKERS[_key(self.coordinator)]

    def finish(self):
        try:
            self.coordinator._finish_lease(self.lease)
        finally:
            self.release()

    def failed_locked(self, reason):
        """Preserve the latest durable intent, including concurrent cancel."""
        coordinator = self.coordinator
        operation = coordinator._get(self.operation_id)
        if operation.get("_dispatch", {}).get("token") != self.token:
            return
        operation["_dispatch"].update(state="FAILED", finished_at=utcnow())
        if operation["status"] not in _TERMINAL:
            uncertain = any(step["state"] in {"INSTALLING", "UNKNOWN"} for step in operation["steps"].values())
            if operation["cancel_requested"]:
                operation = coordinator._settle(operation)
            else:
                operation["ready"] = False
                operation["status"] = "RECOVERY_REQUIRED" if uncertain else "AWAITING_USER"
                operation["required_action"] = "reconcile_installer" if uncertain else "resume"
            operation["error"] = {"code": "RECOVERY_REQUIRED", "reason": reason}
        # An observer may itself have raised BaseException. The failure record
        # must not call that observer again; it remains visible through events.
        coordinator.store.save(operation, reason)

    def run(self):
        coordinator = self.coordinator
        try:
            for attempt in range(10):
                acquired = False
                try:
                    with coordinator.store.lock():
                        acquired = True
                        try:
                            coordinator._emit(self.queued_event)
                            operation = coordinator._get(self.operation_id)
                            dispatch = operation.get("_dispatch", {})
                            if dispatch.get("token") != self.token or dispatch.get("state") != "QUEUED":
                                return
                            if operation["revision"] != self.revision or operation["status"] in _TERMINAL:
                                dispatch.update(state="SUPERSEDED", finished_at=utcnow())
                                if operation["cancel_requested"]:
                                    operation = coordinator._settle(operation)
                                coordinator._save(operation, "prerequisite_background_superseded")
                                return
                            self.lease.active()
                            dispatch["state"] = "RUNNING"
                            operation = coordinator._save(operation, "prerequisite_background_started")
                            operation = coordinator._drive(operation, execute=True)
                            operation["_dispatch"].update(state="FINISHED", finished_at=utcnow())
                            coordinator._save(operation, "prerequisite_background_finished")
                        except BaseException:
                            self.failed_locked("prerequisite_background_worker_failed")
                    return
                except InstallerError as error:
                    if (acquired or error.context.get("details", {}).get("reason") != "prerequisite_controller_busy"
                            or attempt == 9):
                        return
                    # A cross-process status observer may briefly hold the same
                    # lock while probing. Retry acquisition only, never _drive.
                    time.sleep(0.02)
        except BaseException:
            # Without the authoritative lock (or a writable journal), changing
            # the receipt would be unsafe. The durable unfinished dispatch is
            # exposed as recovery-required once the controller is known idle.
            pass
        finally:
            self.finish()

    def launch(self):
        try:
            threading.Thread(target=self.run, name="hinstall-prerequisites", daemon=True).start()
        except BaseException as error:
            try:
                with self.coordinator.store.lock():
                    self.failed_locked("prerequisite_background_start_failed")
            finally:
                self.finish()
            if not isinstance(error, Exception):
                raise


def _queue_locked(coordinator, operation, *, lease, create=False):
    """Reserve before releasing the prepare lock; persist before Thread.start."""
    worker = _Worker(coordinator, operation["operation_id"], lease)
    with _WORKERS_LOCK:
        if _key(coordinator) in _WORKERS:
            fail("OPERATION_CONFLICT", "prerequisite_background_worker_active")
        _WORKERS[_key(coordinator)] = worker
    try:
        operation["_dispatch"] = {"token": worker.token, "state": "QUEUED", "requested_at": utcnow()}
        # Queue observers run in the worker, so a slow observer cannot delay
        # the durable ACK or hold the registration lock on the calling thread.
        operation, worker.queued_event = coordinator.store.save(operation, "prerequisite_background_queued", create=create)
        lease.queued()
        worker.revision = operation["revision"]
    except BaseException:
        worker.release()
        raise
    return worker


def start(coordinator, operation_id, plan, *, consents):
    if not identifier(operation_id):
        fail("INVALID_REQUEST", "prerequisite_operation_id")
    plan, consents = coordinator._validate_plan(plan, consents)
    # Duplicate ACK recovery remains responsive while another thread owns the
    # controller lock. Check the caller's exact identity/plan before projecting.
    old = coordinator._existing_start(operation_id, plan)
    if old is not None:
        return coordinator._public(old)
    lease = coordinator._reserve_lease(operation_id, plan=plan)
    transferred = False
    try:
        with coordinator.store.lock():
            old = coordinator._existing_start(operation_id, plan)
            if old is not None:
                return coordinator._public(old)
            operation = coordinator._prepare_start(operation_id, plan, consents, defer_validation=True)
            worker = _queue_locked(coordinator, operation, lease=lease, create=True)
        transferred = True
        worker.launch()
    finally:
        if not transferred:
            coordinator._finish_lease(lease)
    return coordinator.status(operation_id)


def resume(coordinator, operation_id, *, consents=None):
    operation = coordinator._get(operation_id)
    if operation["status"] in _TERMINAL:
        # Explicit recovery may finish a terminal journal whose old controller
        # died before releasing its common claim. No duplicate start does so.
        return coordinator.resume(operation_id, consents=consents)
    lease = coordinator._reserve_lease(operation_id, resume=True)
    transferred = False
    try:
        with coordinator.store.lock():
            operation = coordinator._get(operation_id)
            if consents is not None:
                _, operation["consents"] = coordinator._validate_plan(operation["plan"], consents)
            worker = _queue_locked(coordinator, operation, lease=lease)
        transferred = True
        worker.launch()
    finally:
        if not transferred:
            coordinator._finish_lease(lease)
    return coordinator.status(operation_id)
