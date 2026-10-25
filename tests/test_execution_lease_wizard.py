"""The prerequisite HTTP gate must not reject a product's own durable claim."""
import time
import threading

import pytest

from test_prerequisites_execution import bundle
from test_wizard_prerequisites import wizard


def settled(api, operation_id, workers):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = api.status(operation_id)
        if result["status"] not in {"RUNNING", "QUEUED"}:
            # Durable status precedes final diagnostics and lease.close().
            # This fixture owns the actual worker; wait for its finalizers,
            # then read a fresh snapshot before attempting another dispatch.
            worker = workers[-1]
            worker.join(max(0, deadline - time.monotonic()))
            assert not worker.is_alive(), "synthetic product worker did not finish"
            return api.status(operation_id)
        time.sleep(0.01)
    pytest.fail("synthetic product did not reach its durable boundary")


@pytest.mark.parametrize("scenario", ["duplicate", "resume"])
def test_http_product_ack_and_resume_are_not_blocked_by_own_domain(bundle, scenario):
    session, engine = wizard(bundle)
    calls = []
    workers = []

    def interrupted(plan, emit, canceled):
        workers.append(threading.current_thread())
        calls.append("execute")
        return {"status": "RECOVERY_REQUIRED"}

    def resumed(plan, previous, emit, canceled):
        workers.append(threading.current_thread())
        calls.append("resume")
        return {"status": "SUCCEEDED"}

    engine.execute, engine.resume = interrupted, resumed
    product = session.route("POST", "/plans", {}, {"action": "install", "mode": "native", "locale": "en"})
    body = {"plan_id": product["plan_id"], "plan_digest": product["plan_digest"],
            "consent_refs": [product["review_consent_ref"], *product["required_consents"]],
            "idempotency_key": "same-product", "accepted_terms": True, "accepted_plan": True,
            "terms_digest": product["options"]["eula_digest"]}
    initial = session.route("POST", "/operations", {}, body)
    operation_id = initial["operation_id"]
    assert settled(session.api, operation_id, workers)["status"] == "RECOVERY_REQUIRED"
    assert session.api.execution_domain.busy() is True
    if scenario == "duplicate":
        result = session.route("POST", "/operations", {}, body)
        assert result["operation_id"] == operation_id and result["status"] == "RECOVERY_REQUIRED"
        assert calls == ["execute"]
    else:
        result = session.route("POST", "/operations/" + operation_id + "/resume", {}, {})
        assert result["operation_id"] == operation_id
        assert settled(session.api, operation_id, workers)["status"] == "SUCCEEDED"
        assert calls == ["execute", "resume"]


def test_durable_boundary_keeps_lease_until_worker_finalizer_finishes(bundle, monkeypatch):
    """Terminal journal status is not proof that live ownership was released."""
    from homensai_install.execution_lease import ExecutionLease
    from homensai_install.errors import InstallerError

    session, engine = wizard(bundle)
    entered, release = threading.Event(), threading.Event()
    workers = []
    original_close = ExecutionLease.close

    def close(lease, **kwargs):
        if lease.domain is session.api.execution_domain and not entered.is_set():
            entered.set()
            assert release.wait(8), "test did not release its own finalizer"
        return original_close(lease, **kwargs)

    def interrupted(*_):
        workers.append(threading.current_thread())
        return {"status": "RECOVERY_REQUIRED"}

    monkeypatch.setattr(ExecutionLease, "close", close)
    engine.execute = interrupted
    engine.resume = lambda *_: {"status": "SUCCEEDED"}
    product = session.route("POST", "/plans", {}, {"action": "install", "mode": "native", "locale": "en"})
    body = {"plan_id": product["plan_id"], "plan_digest": product["plan_digest"],
            "consent_refs": [product["review_consent_ref"], *product["required_consents"]],
            "idempotency_key": "held-finalizer", "accepted_terms": True, "accepted_plan": True,
            "terms_digest": product["options"]["eula_digest"]}
    operation = session.route("POST", "/operations", {}, body)
    try:
        assert entered.wait(5)
        assert session.api.status(operation["operation_id"])["status"] == "RECOVERY_REQUIRED"
        with pytest.raises(InstallerError) as error:
            session.route("POST", "/operations/" + operation["operation_id"] + "/resume", {}, {})
        assert error.value.code == "OPERATION_CONFLICT"
        assert session.api.execution_domain.busy() is True
    finally:
        release.set()
        if workers:
            workers[0].join(5)
    assert workers and not workers[0].is_alive()
    result = session.api.resume(operation["operation_id"], background=False)
    assert result["status"] == "SUCCEEDED"
    assert session.api.execution_domain.busy() is False
