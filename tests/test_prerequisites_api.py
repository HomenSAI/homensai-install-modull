"""Общий SDK facade → настоящий signed catalogue + durable coordinator.

Platform installer инъецирован fixture; это не installed OS acceptance.
"""
import json

import pytest

from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from test_prerequisites_execution import bundle, coordinator, present


def api_for(bundle, **kwargs):
    return InstallerAPI(bundle["tmp_path"] / "product-state", prerequisites=coordinator(bundle, **kwargs))


def test_unconfigured_prerequisites_never_start_an_implicit_installer(tmp_path):
    api = InstallerAPI(tmp_path / "not-created")
    caps = api.prerequisites_capabilities()
    assert caps["configured"] is False and not caps["automatic_component_ids"]
    with pytest.raises(InstallerError) as caught:
        api.prerequisites_plan()
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    with pytest.raises(InstallerError):
        InstallerAPI(tmp_path / "other", prerequisites={"argv": ["sh"]})
    assert not api.state_dir.exists()


def test_signed_auto_execution_and_restart_keep_one_prerequisite_operation(bundle):
    api = api_for(bundle)
    assert api.prerequisites_identity()["manifest_digest"] == digest(bundle["manifest"])
    assert set(api.prerequisites_capabilities()["automatic_component_ids"]) == {"base", "client", "metrics"}
    terms = api.prerequisites_license("base")
    assert terms["text"].startswith("Synthetic terms")
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    with pytest.raises(InstallerError) as caught:
        api.prerequisites_start("prep_one", plan)
    assert caught.value.code == "CONSENT_REQUIRED" and bundle["calls"] == []
    result = api.prerequisites_start("prep_one", plan, plan["required_consents"])
    assert result["status"] == "READY" and result["ready"] is True
    assert bundle["calls"] == ["base", "client"]
    again = api_for(bundle)
    assert again.prerequisites_start("prep_one", plan, plan["required_consents"])["operation_id"] == "prep_one"
    assert bundle["calls"] == ["base", "client"]
    events = again.prerequisites_events("prep_one")
    assert events["snapshot"]["status"] == "READY" and events["events"]
    assert again.prerequisites_attempts("prep_one", "base")["attempts"][0]["attempt"] == 1
    assert "token=never-public" not in json.dumps(events)
    assert not api.state_dir.exists(), "Prerequisite READY does not invent a product install operation"


def test_manual_recheck_stays_read_only_through_public_sdk(bundle):
    api = api_for(bundle)
    plan = api.prerequisites_plan({"base": "manual", "client": "manual"})
    result = api.prerequisites_start("prep_manual", plan, plan["required_consents"])
    assert result["status"] == "AWAITING_USER"
    for name in ("base", "client"):
        present(bundle, name)
    assert api.prerequisites_recheck("prep_manual")["status"] == "READY"
    assert bundle["calls"] == [] and bundle["transport"].calls == []


def test_unexpected_registered_port_error_is_sanitized(bundle):
    api = api_for(bundle)
    def broken():
        raise RuntimeError("private-fixture-secret")
    api._prerequisite_instance.capabilities = broken
    with pytest.raises(InstallerError) as caught:
        api.prerequisites_capabilities()
    assert caught.value.code == "UNEXPECTED_ERROR"
    assert "private-fixture-secret" not in json.dumps(caught.value.to_dict())


def test_public_replan_and_cancel_preserve_identity_and_require_new_consents(bundle):
    api = api_for(bundle)
    manual = api.prerequisites_plan()
    api.prerequisites_start("prep_replan", manual, manual["required_consents"])
    changed = api.prerequisites_plan({"base": "auto", "client": "manual"})
    with pytest.raises(InstallerError) as caught:
        api.prerequisites_replace_plan("prep_replan", changed, manual["required_consents"])
    assert caught.value.code == "CONSENT_REQUIRED"
    updated = api.prerequisites_replace_plan("prep_replan", changed, changed["required_consents"])
    assert updated["operation_id"] == "prep_replan"
    assert bundle["calls"] == [], "Replacing a plan cannot start its installer"
    assert api.prerequisites_cancel("prep_replan")["status"] == "CANCELED"
    assert api.prerequisites_resume("prep_replan")["status"] == "CANCELED"
