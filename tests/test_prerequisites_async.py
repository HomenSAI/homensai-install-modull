"""Real threads/process exits and private SQLite; synthetic signed installers.

No OS package, live network, service, Docker daemon or user installation is
modified. These tests are source concurrency/recovery contracts on Linux.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import threading
import time

import pytest

from homensai_install import prerequisite_async as background
from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from test_prerequisites_execution import ControllerDied, bundle, coordinator, present


def api_for(bundle, *, state_name="product-state", **ports):
    return InstallerAPI.from_bootstrap(bundle["tmp_path"] / state_name, prerequisites=coordinator(bundle, **ports))


def auto_plan(api):
    return api.prerequisites_plan({"base": "auto", "client": "auto"})


def settled(api, operation_id="async_one"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = api.prerequisites_status(operation_id)
        with background._WORKERS_LOCK:
            reserved = background._key(api._prerequisite_instance) in background._WORKERS
        if not reserved and state["execution"]["active"] is False:
            # The first read may predate the worker's last cancellation write.
            # Observe the journal again after quiescence, then ensure another
            # dispatch did not begin while that final snapshot was being read.
            state = api.prerequisites_status(operation_id)
            with background._WORKERS_LOCK:
                reserved = background._key(api._prerequisite_instance) in background._WORKERS
            if not reserved and state["execution"]["active"] is False:
                return state
        threading.Event().wait(0.01)
    pytest.fail("Background controller did not reach a bounded observable result")


def test_ack_precedes_slow_discovery_and_cancellation_remains_responsive(bundle):
    entered, release = threading.Event(), threading.Event()
    api = api_for(bundle)
    plan = auto_plan(api)

    def slow_discovery():
        entered.set()
        assert release.wait(5)
        return deepcopy(bundle["inventory"])

    api._prerequisite_instance.discover = slow_discovery
    try:
        ack = api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
        assert entered.wait(2) and not release.is_set()
        assert ack["operation_id"] == "async_one" and not ack["ready"]
        assert ack["execution"]["state"] in {"queued", "running"}
        assert api._prerequisite_instance.store.get("async_one")["_initial_validation_pending"]
        cancel = api.prerequisites_cancel("async_one")
        assert cancel["cancel_requested"] and not release.is_set()
        assert bundle["calls"] == [] and bundle["transport"].calls == []
    finally:
        release.set()
    assert settled(api)["status"] == "CANCELED"
    assert bundle["calls"] == []


def test_blocked_installer_does_not_block_status_list_duplicate_or_cancel(bundle):
    entered, release, controls_done = threading.Event(), threading.Event(), threading.Event()
    api = api_for(bundle)
    restored = api_for(bundle, state_name="different-product-state")
    plan = auto_plan(api)
    other_plan = api.prerequisites_plan()
    errors, observations = [], {}

    def effect():
        entered.set()
        assert release.wait(5)
        present(bundle, "base")

    def controls():
        try:
            observations["status"] = restored.prerequisites_status("async_one")
            observations["list"] = restored.prerequisites_operations_list()
            observations["duplicate"] = restored.prerequisites_start(
                "async_one", plan, plan["required_consents"], background=True)
            for action in (
                lambda: restored.prerequisites_start("another", plan, plan["required_consents"], background=True),
                lambda: restored.prerequisites_start("async_one", other_plan, other_plan["required_consents"], background=True),
                lambda: restored.prerequisites_resume("async_one", background=True),
            ):
                with pytest.raises(InstallerError) as caught:
                    action()
                assert caught.value.code == "OPERATION_CONFLICT"
            observations["cancel"] = restored.prerequisites_cancel("async_one")
        except BaseException as error:
            errors.append(error)
        finally:
            controls_done.set()

    bundle["effects"]["base"] = effect
    try:
        api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
        assert entered.wait(2)
        controller = threading.Thread(target=controls)
        controller.start()
        assert controls_done.wait(2), "Polling/cancel waited for the installer to finish"
        assert not errors and not release.is_set()
        assert observations["status"]["steps"]["base"]["state"] == "INSTALLING"
        assert observations["duplicate"]["execution"]["active"] is True
        assert [item["operation_id"] for item in observations["list"]["operations"]] == ["async_one"]
        assert observations["cancel"]["required_action"] == "wait_for_safe_boundary"
    finally:
        release.set()
        if "controller" in locals():
            controller.join(5)
    result = settled(api)
    assert result["status"] == "CANCELED" and result["cancel_requested"]
    assert bundle["calls"] == ["base"], "Cancel must prevent the next dependency"
    assert not api.state_dir.exists() and not restored.state_dir.exists()


@pytest.mark.parametrize("phase", ["prerequisite_background_queued", "prerequisite_pass_complete",
                                    "prerequisite_background_finished"])
def test_cancel_at_dispatch_boundaries_is_consumed_without_another_resume(bundle, phase):
    observations = []
    controller = coordinator(bundle)

    def observe(event):
        if event["message_code"] == phase:
            observations.append(controller.cancel("async_one"))

    api = api_for(bundle, event_callback=observe)
    plan = api.prerequisites_plan()
    api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
    result = settled(api)
    assert observations and result["status"] == "CANCELED"
    assert result["cancel_requested"] and result["required_action"] is None
    assert bundle["calls"] == [] and bundle["transport"].calls == []
    # Terminal cancellation released the authoritative claim atomically.
    assert controller.start("next", plan, consents=plan["required_consents"])["status"] == "AWAITING_USER"


def test_external_cancel_after_finish_commit_before_unlock_is_settled(bundle):
    entered, release = threading.Event(), threading.Event()

    def observe(event):
        if event["message_code"] == "prerequisite_background_finished":
            entered.set()
            assert release.wait(5)

    api = api_for(bundle, event_callback=observe)
    plan = api.prerequisites_plan()
    try:
        api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
        assert entered.wait(2)
        canceled = api.prerequisites_cancel("async_one")
        assert canceled["status"] == "CANCELED" and not release.is_set()
    finally:
        release.set()
    assert settled(api)["status"] == "CANCELED"


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("outside_process", [False, True])
def test_finished_dispatch_is_not_idle_while_final_observer_owns_controller(
        bundle, monkeypatch, ready, outside_process):
    entered, release = threading.Event(), threading.Event()

    def observe(event):
        if event["message_code"] == "prerequisite_background_finished":
            entered.set()
            assert release.wait(5)

    if ready:
        present(bundle, "base")
        present(bundle, "client")
    api = api_for(bundle, event_callback=observe)
    plan = api.prerequisites_plan()
    try:
        api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
        assert entered.wait(2)
        subject = api._prerequisite_instance
        assert subject.store.get("async_one")["_dispatch"]["state"] == "FINISHED"
        assert subject.store.controller_busy() is True
        with monkeypatch.context() as patch:
            if outside_process:
                # Another process cannot see this process's thread reservation.
                # The actual file lock is still held by the gated worker.
                patch.setattr(background, "_reserved", lambda *_: False)
            state = api.prerequisites_status("async_one")
            listed = api.prerequisites_operations_list(unfinished_only=False)["operations"][0]
            for snapshot in (state, listed):
                assert snapshot["execution"]["active"] is (None if outside_process else True)
                assert snapshot["status"] == ("READY" if ready else "AWAITING_USER")
        canceled = api.prerequisites_cancel("async_one")
        assert canceled["status"] == ("READY" if ready else "CANCELED")
        assert not release.is_set(), "Status/list/cancel must not wait for the observer"
    finally:
        release.set()
    final = settled(api)
    assert final["status"] == ("READY" if ready else "CANCELED")
    assert final["execution"]["active"] is False
    assert bundle["calls"] == [] and bundle["transport"].calls == []


def test_settled_reads_current_journal_after_worker_exit_not_earlier_snapshot(bundle, monkeypatch):
    entered, allow_cancel, released = threading.Event(), threading.Event(), threading.Event()
    cancel_results = []
    other = coordinator(bundle)

    def observe(event):
        if event["message_code"] == "prerequisite_background_finished":
            entered.set()
            assert allow_cancel.wait(5)
            cancel_results.append(other.cancel("async_one"))

    api = api_for(bundle, event_callback=observe)
    subject = api._prerequisite_instance
    original_release = background._Worker.release

    def signal_release(worker):
        original_release(worker)
        if worker.coordinator is subject:
            released.set()

    monkeypatch.setattr(background._Worker, "release", signal_release)
    plan = api.prerequisites_plan()
    delayed = []
    try:
        api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
        assert entered.wait(2)
        original_get = subject._get

        def delayed_read(operation_id):
            snapshot = original_get(operation_id)
            if not delayed:
                delayed.append(snapshot)
                assert snapshot["status"] == "AWAITING_USER"
                allow_cancel.set()
                assert released.wait(5)
            return snapshot

        # Force the exact failing ordering: read old journal -> worker commits
        # cancel and exits -> return the old read -> observe no reservation.
        monkeypatch.setattr(subject, "_get", delayed_read)
        result = settled(api)
    finally:
        allow_cancel.set()
        assert released.wait(5)
    durable = subject.store.get("async_one")
    assert cancel_results[0]["status"] == durable["status"] == "CANCELED"
    assert result["status"] == "CANCELED" and result["revision"] == durable["revision"]
    assert not subject.store.has_claim()
    assert bundle["calls"] == [] and bundle["transport"].calls == []


@pytest.mark.parametrize("error", [RuntimeError("private-start-secret"), ControllerDied("private-start-secret")])
def test_thread_start_failure_is_durable_and_does_not_leak_reservation(bundle, monkeypatch, error):
    api = api_for(bundle)
    plan = auto_plan(api)

    def fail_start(self):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_start)
        if isinstance(error, Exception):
            api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
        else:
            with pytest.raises(ControllerDied):
                api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
    failed = settled(api)
    assert failed["execution"]["state"] == "failed"
    assert failed["error"]["reason"] == "prerequisite_background_start_failed"
    assert failed["required_action"] == "resume"
    assert "private-start-secret" not in json.dumps(failed)
    api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
    assert bundle["calls"] == [], "Duplicate start must never turn failure into implicit resume"
    api.prerequisites_resume("async_one", background=True)
    assert settled(api)["ready"] and bundle["calls"] == ["base", "client"]


def test_worker_baseexception_preserves_exact_intent_and_requires_reconciliation(bundle):
    api = api_for(bundle)
    bundle["results"]["base"] = ControllerDied("private-worker-secret")
    plan = auto_plan(api)
    api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
    failed = settled(api)
    assert failed["status"] == "RECOVERY_REQUIRED"
    assert failed["execution"]["state"] == "failed"
    assert failed["steps"]["base"]["state"] == "INSTALLING"
    receipt = failed["steps"]["base"]["receipt"]
    assert receipt["outcome"] == "unknown" and receipt["artifact_pins"]
    assert "private-worker-secret" not in json.dumps(failed)
    assert api.prerequisites_resume("async_one")["status"] == "RECOVERY_REQUIRED"
    assert bundle["calls"] == ["base"], "A present probe cannot settle an unknown same-boot installer"
    recovered = api_for(bundle, reconcile=lambda entry, receipt, inventory: "applied")
    recovered.prerequisites_resume("async_one", background=True)
    assert settled(recovered)["ready"] and bundle["calls"] == ["base", "client"]


def test_deferred_exact_plan_verification_rejects_tampered_review_before_any_effect(bundle):
    api = api_for(bundle)
    plan = auto_plan(api)
    plan["components"][0]["ownership"] = "managed_private"
    body = {key: value for key, value in plan.items() if key not in {"plan_digest", "required_consents"}}
    plan["plan_digest"] = digest(body)
    plan["required_consents"] = api._prerequisite_instance._consents(body)
    api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
    result = settled(api)
    assert result["error"]["code"] == "PLAN_STALE" and result["required_action"] == "replan"
    assert bundle["calls"] == [] and bundle["transport"].calls == []


@pytest.mark.parametrize("crash_point", ["before_thread", "after_effect"])
def test_real_process_exit_recovers_by_listing_without_replay(bundle, crash_point):
    api = api_for(bundle)
    plan = auto_plan(api)
    effect_file = bundle["tmp_path"] / "synthetic-effect.txt"
    if crash_point == "after_effect":
        def effect():
            effect_file.write_text("one synthetic effect")
            os._exit(24)
        bundle["effects"]["base"] = effect
    pid = os.fork()
    if pid == 0:
        if crash_point == "before_thread":
            threading.Thread.start = lambda self: os._exit(23)
        api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
        threading.Event().wait(5)
        os._exit(99)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == (23 if crash_point == "before_thread" else 24)
    restored = api_for(bundle, state_name="new-product-state")
    raw_before = restored._prerequisite_instance.store.path.read_bytes()
    history = restored.prerequisites_operations_list()
    assert [item["operation_id"] for item in history["operations"]] == ["async_one"]
    result = restored.prerequisites_status("async_one")
    assert result["status"] == "RECOVERY_REQUIRED" and result["execution"]["active"] is False
    assert restored._prerequisite_instance.store.path.read_bytes() == raw_before
    restored.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
    assert bundle["calls"] == []
    if crash_point == "before_thread":
        assert not effect_file.exists() and result["required_action"] == "resume"
        assert restored.prerequisites_resume("async_one")["ready"]
        assert bundle["calls"] == ["base", "client"]
    else:
        present(bundle, "base")
        assert effect_file.read_text() == "one synthetic effect"
        assert result["steps"]["base"]["state"] == "INSTALLING"
        assert restored.prerequisites_resume("async_one")["status"] == "RECOVERY_REQUIRED"
        assert bundle["calls"] == []


def test_listing_filters_full_identity_before_limit_and_omits_private_payload(bundle):
    api = api_for(bundle)
    subject = api._prerequisite_instance
    plan = api.prerequisites_plan()
    subject.start("seed", plan, consents=plan["required_consents"])
    subject.cancel("seed")
    template = subject.store.get("seed")
    variants = [
        ("aa_owner", {"owner_id": "another-owner"}),
        ("ab_product", {"product_id": "another-product"}),
        ("ac_target", {"target": {**template["target"], "arch": "arm64"}}),
        ("ad_catalog", {"catalog_digest": "0" * 64}),
        ("match_a", {}), ("match_b", {}),
    ]
    with subject.store.lock():
        for operation_id, changes in variants:
            operation = deepcopy(template)
            operation.update(operation_id=operation_id, **changes)
            operation["plan"]["local_artifacts"] = {"fixture": "/never-expose/local-artifact"}
            operation["consents"] = ["never-expose-consent"]
            subject.store.save(operation, "synthetic_history", create=True)
    first = api.prerequisites_operations_list(unfinished_only=False, limit=1)
    second = api.prerequisites_operations_list(unfinished_only=False, limit=1, after_id=first["next_cursor"])
    assert first["operations"][0]["operation_id"] == "match_a" and first["next_cursor"] == "match_a"
    assert second["operations"][0]["operation_id"] == "match_b"
    assert api.prerequisites_operations_list()["operations"] == []
    public = json.dumps(first)
    assert "never-expose" not in public and "_dispatch" not in public and '"steps"' not in public
    with pytest.raises(InstallerError):
        api.prerequisites_operations_list(limit=True)
    foreign = api_for(bundle, owner_id="another-original-owner")
    assert foreign.prerequisites_operations_list(unfinished_only=False)["operations"] == []
    with pytest.raises(InstallerError):
        foreign.prerequisites_status("seed")


def test_status_does_not_create_missing_lock_or_invent_interruption_proof(bundle):
    api = api_for(bundle)
    subject = api._prerequisite_instance
    plan = auto_plan(api)
    with subject.store.lock():
        operation = subject._prepare_start("async_one", plan, plan["required_consents"], defer_validation=True)
        operation["_dispatch"] = {"token": "lost-process", "state": "QUEUED"}
        subject.store.save(operation, "synthetic_interruption", create=True)
    lock_path = subject.store.directory / "controller.lock"
    lock_path.unlink()
    before = subject.store.path.read_bytes()
    status = api.prerequisites_status("async_one")
    assert status["execution"] == {"mode": "background", "state": "unknown", "active": None}
    api.prerequisites_operations_list()
    assert not lock_path.exists() and subject.store.path.read_bytes() == before
    assert bundle["calls"] == []


def test_worker_retries_only_lock_acquisition_after_transient_observer_contention(bundle, monkeypatch):
    api = api_for(bundle)
    subject = api._prerequisite_instance
    plan = auto_plan(api)
    original_lock, original_launch = subject.store.lock, background._Worker.launch
    contention = threading.Event()

    @contextmanager
    def observed_lock():
        try:
            with original_lock():
                yield
        except InstallerError as error:
            if error.context.get("details", {}).get("reason") == "prerequisite_controller_busy":
                contention.set()
            raise

    def launch_under_observer(worker):
        with original_lock():
            assert api.prerequisites_status("async_one")["execution"] == {
                "mode": "background", "state": "queued", "active": True}
            original_launch(worker)
            assert contention.wait(2)

    monkeypatch.setattr(subject.store, "lock", observed_lock)
    monkeypatch.setattr(background._Worker, "launch", launch_under_observer)
    api.prerequisites_start("async_one", plan, plan["required_consents"], background=True)
    assert settled(api)["ready"] and bundle["calls"] == ["base", "client"]


def test_sync_resume_after_background_does_not_reuse_finished_dispatch_for_cancel(bundle):
    observed = []
    api = api_for(bundle)
    manual = api.prerequisites_plan()
    api.prerequisites_start("async_one", manual, manual["required_consents"], background=True)
    assert settled(api)["status"] == "AWAITING_USER"
    automatic = auto_plan(api)
    api.prerequisites_replace_plan("async_one", automatic, automatic["required_consents"])

    def effect():
        observed.append(api.prerequisites_cancel("async_one"))
        present(bundle, "base")

    bundle["effects"]["base"] = effect
    result = api.prerequisites_resume("async_one")
    assert observed[0]["status"] == "RUNNING"
    assert observed[0]["required_action"] == "wait_for_safe_boundary"
    assert result["status"] == "CANCELED" and bundle["calls"] == ["base"]


def test_background_opt_in_is_strict_and_factory_keeps_trusted_registration(bundle):
    api = api_for(bundle)
    plan = api.prerequisites_plan()
    for flag in (1, "true", None):
        with pytest.raises(InstallerError):
            api.prerequisites_start("async_one", plan, plan["required_consents"], background=flag)
        with pytest.raises(InstallerError):
            api.prerequisites_resume("async_one", background=flag)
    with pytest.raises(InstallerError):
        InstallerAPI.from_bootstrap(bundle["tmp_path"] / "untrusted", prerequisites={"command": "shell"})
    assert api.prerequisites_start("sync_one", plan, plan["required_consents"])["execution"]["mode"] == "synchronous"
