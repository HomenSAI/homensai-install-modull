"""The prerequisite HTTP gate must not reject a product's own durable claim."""
import time

import pytest

from test_prerequisites_execution import bundle
from test_wizard_prerequisites import wizard


def settled(api, operation_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = api.status(operation_id)
        if result["status"] not in {"RUNNING", "QUEUED"}:
            return result
        time.sleep(0.01)
    pytest.fail("synthetic product did not reach its durable boundary")


@pytest.mark.parametrize("scenario", ["duplicate", "resume"])
def test_http_product_ack_and_resume_are_not_blocked_by_own_domain(bundle, scenario):
    session, engine = wizard(bundle)
    calls = []

    def interrupted(plan, emit, canceled):
        calls.append("execute")
        return {"status": "RECOVERY_REQUIRED"}

    def resumed(plan, previous, emit, canceled):
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
    assert settled(session.api, operation_id)["status"] == "RECOVERY_REQUIRED"
    assert session.api.execution_domain.busy() is True
    if scenario == "duplicate":
        result = session.route("POST", "/operations", {}, body)
        assert result["operation_id"] == operation_id and result["status"] == "RECOVERY_REQUIRED"
        assert calls == ["execute"]
    else:
        result = session.route("POST", "/operations/" + operation_id + "/resume", {}, {})
        assert result["operation_id"] == operation_id
        assert settled(session.api, operation_id)["status"] == "SUCCEEDED"
        assert calls == ["execute", "resume"]
