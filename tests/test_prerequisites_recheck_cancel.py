"""Cancel at synchronous probe boundaries: real threads/store, fake installer.

No OS package or user installation is changed. The final transaction, not an
observer or the absence of a process, decides whether a claim can be released.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time

import pytest

from homensai_install.errors import InstallerError
from test_prerequisites_execution import ControllerDied, bundle, coordinator, present, resign, start_auto


def manual_operation(value):
    subject = coordinator(value)
    plan = subject.plan()
    subject.start("checking", plan, consents=plan["required_consents"])
    return subject


def assert_released(value):
    restored = coordinator(value)
    fresh = restored.plan()
    result = restored.start("next_operation", fresh, consents=fresh["required_consents"])
    assert result["status"] in {"AWAITING_USER", "READY"}


@pytest.mark.parametrize("becomes_ready", [False, True])
def test_cancel_during_blocked_recheck_wins_over_stale_probe_result(bundle, becomes_ready):
    subject = manual_operation(bundle)
    other = coordinator(bundle)
    entered, release = threading.Event(), threading.Event()

    def discover():
        entered.set()
        assert release.wait(5)
        if becomes_ready:
            present(bundle, "base")
            present(bundle, "client")
        return deepcopy(bundle["inventory"])

    subject.discover = discover
    with ThreadPoolExecutor(max_workers=3) as pool:
        checking = pool.submit(subject.recheck, "checking")
        assert entered.wait(2)
        try:
            state = pool.submit(other.status, "checking").result(timeout=2)
            canceled = pool.submit(other.cancel, "checking").result(timeout=2)
            assert not state["ready"] and canceled["cancel_requested"]
            assert canceled["status"] == "CANCELED" and not release.is_set()
        finally:
            release.set()
        result = checking.result(timeout=2)
    assert result["status"] == "CANCELED" and not result["ready"]
    assert result["cancel_requested"] and result["required_action"] is None
    assert other.status("checking")["status"] == "CANCELED"
    assert bundle["calls"] == [] and bundle["transport"].calls == []
    assert_released(bundle)


@pytest.mark.parametrize("becomes_ready", [False, True])
def test_cancel_in_final_recheck_observer_respects_committed_ready(bundle, becomes_ready):
    subject = manual_operation(bundle)
    other, observations = coordinator(bundle), []
    if becomes_ready:
        present(bundle, "base")
        present(bundle, "client")

    def observe(event):
        if event["message_code"] == "prerequisite_rechecked":
            observations.append(other.cancel("checking"))

    subject.event_callback = observe
    result = subject.recheck("checking")
    assert len(observations) == 1
    expected = "READY" if becomes_ready else "CANCELED"
    assert result["status"] == expected == other.status("checking")["status"]
    assert result["ready"] is becomes_ready
    assert result["cancel_requested"] is not becomes_ready
    assert observations[0]["status"] == expected
    assert bundle["calls"] == [] and bundle["transport"].calls == []
    assert_released(bundle)


def test_external_cancel_after_probe_commit_before_unlock_releases_claim(bundle):
    subject = manual_operation(bundle)
    other = coordinator(bundle)
    committed, release = threading.Event(), threading.Event()

    def observe(event):
        if event["message_code"] == "prerequisite_rechecked":
            committed.set()
            assert release.wait(5)

    subject.event_callback = observe
    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(subject.recheck, "checking")
        assert committed.wait(2)
        try:
            canceled = pool.submit(other.cancel, "checking").result(timeout=2)
            assert canceled["status"] == "CANCELED" and not release.is_set()
            assert other.operations_list()["operations"] == []
        finally:
            release.set()
        assert checking.result(timeout=2)["status"] == "CANCELED"
    events = other.events("checking", limit=1000)
    assert [item["sequence"] for item in events] == list(range(1, events[-1]["sequence"] + 1))
    assert events[-1]["status"] == "CANCELED"
    assert_released(bundle)


def test_cancel_during_recheck_of_unknown_installer_keeps_receipt_and_claim(bundle):
    bundle["results"]["base"] = ControllerDied()
    with pytest.raises(ControllerDied):
        start_auto(coordinator(bundle), operation_id="checking")
    subject, other = coordinator(bundle), coordinator(bundle)
    original_receipt = other.status("checking")["steps"]["base"]["receipt"]
    entered, release = threading.Event(), threading.Event()

    def discover():
        entered.set()
        assert release.wait(5)
        return deepcopy(bundle["inventory"])

    subject.discover = discover
    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(subject.recheck, "checking")
        assert entered.wait(2)
        try:
            canceled = pool.submit(other.cancel, "checking").result(timeout=2)
            assert canceled["status"] == "RUNNING" and canceled["cancel_requested"]
            assert canceled["required_action"] == "wait_for_safe_boundary"
            assert not release.is_set()
        finally:
            release.set()
        result = checking.result(timeout=2)
    assert result["status"] == "RECOVERY_REQUIRED" and not result["ready"]
    assert result["required_action"] == "reconcile_installer"
    assert result["steps"]["base"]["state"] == "UNKNOWN"
    receipt = result["steps"]["base"]["receipt"]
    assert receipt["artifact_pins"] == original_receipt["artifact_pins"]
    assert receipt["outcome"] == "unknown" and receipt["reconciled"] == "unknown"
    fresh = other.plan()
    with pytest.raises(InstallerError) as caught:
        other.start("next_operation", fresh, consents=fresh["required_consents"])
    assert caught.value.code == "OPERATION_CONFLICT"
    assert bundle["calls"] == ["base"], "Unknown installer must not be replayed"


@pytest.mark.parametrize("phase", ["queued", "discover"])
def test_active_async_validation_retains_pending_cancel_until_safe_finish(bundle, phase):
    subject, other = coordinator(bundle), coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "auto"})
    entered, release = threading.Event(), threading.Event()

    def discover():
        entered.set()
        assert release.wait(5)
        return deepcopy(bundle["inventory"])

    if phase == "discover":
        subject.discover = discover
    else:
        def observe(event):
            if event["message_code"] == "prerequisite_background_queued":
                entered.set()
                assert release.wait(5)
        subject.event_callback = observe
    try:
        subject.start_background("checking", plan, consents=plan["required_consents"])
        assert entered.wait(2)
        canceled = other.cancel("checking")
        assert canceled["status"] == "AWAITING_USER" and canceled["cancel_requested"]
        assert canceled["execution"]["active"] is True
        assert other.store.has_claim() and other.busy()
        assert not release.is_set()
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = other.status("checking")
        if result["status"] == "CANCELED" and result["execution"]["active"] is False and not other.busy():
            break
        threading.Event().wait(.01)
    else:
        pytest.fail("Async validation did not settle its cancellation")
    assert not other.store.has_claim()
    assert bundle["calls"] == [] and bundle["transport"].calls == []


@pytest.mark.parametrize("boundary", ["after_initial_probe", "before_intent_commit"])
def test_cancel_before_effect_stops_stale_synchronous_drive(bundle, boundary):
    subject, other = coordinator(bundle), coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "auto"})
    entered, release = threading.Event(), threading.Event()
    if boundary == "after_initial_probe":
        def observe(event):
            if event["message_code"] == "prerequisite_rechecked":
                entered.set()
                assert release.wait(5)
        subject.event_callback = observe
    else:
        original_save = subject._save
        def before_intent(operation, message, **kwargs):
            if message == "prerequisite_execution_intent":
                entered.set()
                assert release.wait(5)
            return original_save(operation, message, **kwargs)
        subject._save = before_intent
    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(subject.start, "checking", plan, consents=plan["required_consents"])
        assert entered.wait(2)
        try:
            canceled = pool.submit(other.cancel, "checking").result(timeout=2)
            durable = other.store.get("checking")
            assert canceled["cancel_requested"] and bundle["calls"] == []
            assert durable["steps"]["base"]["state"] != "INSTALLING", "Cancel must precede installer intent"
            if boundary == "after_initial_probe":
                assert canceled["status"] == "CANCELED" and not other.store.has_claim()
            else:
                assert canceled["status"] == "RUNNING" and other.store.has_claim()
        finally:
            release.set()
        result = starting.result(timeout=2)
    assert result["status"] == "CANCELED" and bundle["calls"] == []
    assert not other.store.has_claim()
    if boundary == "after_initial_probe":
        assert bundle["transport"].calls == [], "Stale RUNNING transition must not download"
    else:
        receipt = result["steps"]["base"]["receipt"]
        assert receipt["started"] is False and receipt["outcome"] == "NOT_STARTED"


def test_busy_observes_claim_hidden_by_catalog_or_owner_without_revealing_identity(bundle):
    original = manual_operation(bundle)
    foreign_owner = coordinator(bundle, owner_id="other-original-owner")
    assert foreign_owner.operations_list()["operations"] == []
    assert foreign_owner.busy() is True
    bundle["descriptor"]["catalog_version"] = "1.0.1"
    resign(bundle)
    changed_catalog = coordinator(bundle)
    assert changed_catalog.operations_list()["operations"] == []
    before = changed_catalog.store.path.read_bytes()
    assert changed_catalog.busy() is True
    assert changed_catalog.store.path.read_bytes() == before
    original.cancel("checking")
    assert changed_catalog.busy() is False and not changed_catalog.store.has_claim()


def test_busy_is_conservative_for_missing_lock_and_journal_error(bundle):
    subject = coordinator(bundle)
    assert subject.busy() is False
    lock = subject.store.directory / "controller.lock"
    lock.unlink()
    before = subject.store.path.read_bytes()
    assert subject.busy() is True
    assert not lock.exists() and subject.store.path.read_bytes() == before
    subject.store.path.write_bytes(b"synthetic invalid SQLite journal")
    with pytest.raises(InstallerError) as caught:
        subject.busy()
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
