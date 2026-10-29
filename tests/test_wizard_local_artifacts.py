"""Явный local file transport → прежний SDK plan/consent/coordinator/lease."""
import io
import json
from pathlib import Path

import pytest

from homensai_install.errors import InstallerError
from test_prerequisites_execution import bundle, present
from test_wizard_prerequisites import wizard, plan, review, settled


def allocate(session, value, *, component="base", artifact_id="base-package"):
    return session.route("POST", "/prerequisites/uploads", {}, {
        "plan_id": value["plan_id"], "plan_digest": value["plan_digest"],
        "component_id": component, "artifact_id": artifact_id})


def receive(session, allocation, content):
    return session.receive_local_artifact("/prerequisites/uploads/" + allocation["upload_id"] + "/content",
                                          {}, io.BytesIO(content), len(content))


def fixture_content(bundle, component="base"):
    return bundle["transport"].files["https://downloads.example.org/" + component + "?token=never-public"]


def test_selected_file_is_staged_only_then_new_review_binds_existing_sdk(bundle):
    session, _ = wizard(bundle)
    assert session.route("GET", "/prerequisites/capabilities", {}, {})["local_artifact_upload"] is True
    previous = plan(session, automatic=True)
    allocation = allocate(session, previous)
    uploaded = receive(session, allocation, fixture_content(bundle))
    assert uploaded["verified"] and not bundle["calls"] and not bundle["transport"].calls
    local = session.route("POST", "/prerequisites/plans", {}, {
        "mode": "native", "locale": "en", "choices": {"base": "auto", "client": "auto"},
        "local_uploads": {"base-package": allocation["upload_id"]}})
    assert local["plan_digest"] != previous["plan_digest"]
    assert local["plan"]["local_artifacts"] == {"base-package": allocation["upload_id"]}
    assert "wizard-uploads" not in json.dumps(local)
    assert str(bundle["tmp_path"]) not in json.dumps(local)
    with pytest.raises(InstallerError):
        session.route("POST", "/prerequisites/operations", {}, {**review(local), "consent_refs": previous["plan"]["required_consents"]})
    assert not bundle["calls"]
    session.route("POST", "/prerequisites/operations", {}, review(local))
    result = settled(session, local["operation_id"])
    assert result["status"] == "READY" and bundle["calls"] == ["base", "client"]
    assert bundle["transport"].calls == ["https://downloads.example.org/client?token=never-public"]
    assert "wizard-uploads" not in json.dumps(result)


def test_two_uploads_use_one_signed_plan_and_offline_coordinator(bundle):
    session, _ = wizard(bundle)
    value = plan(session, automatic=True)
    uploads = {}
    for component in ("base", "client"):
        allocation = allocate(session, value, component=component, artifact_id=component + "-package")
        receive(session, allocation, fixture_content(bundle, component))
        uploads[component + "-package"] = allocation["upload_id"]
    local = session.route("POST", "/prerequisites/plans", {}, {
        "mode": "native", "locale": "de", "offline": True,
        "choices": {"base": "auto", "client": "auto"}, "local_uploads": uploads})
    assert not bundle["calls"]
    session.route("POST", "/prerequisites/operations", {}, review(local))
    assert settled(session, local["operation_id"])["ready"] is True
    assert not bundle["transport"].calls
    assert bundle["calls"] == ["base", "client"]


@pytest.mark.parametrize("change", [{"plan_digest": "forged"}, {"component_id": "client"},
    {"artifact_id": "../../other"}, {"owner_id": "attacker"}, {"path": "/tmp/other"},
    {"argv": ["sh"]}])
def test_allocation_is_bound_to_exact_server_plan_and_fields(bundle, change):
    session, _ = wizard(bundle)
    value = plan(session, automatic=True)
    body = {"plan_id": value["plan_id"], "plan_digest": value["plan_digest"],
            "component_id": "base", "artifact_id": "base-package"}
    with pytest.raises(InstallerError):
        session.route("POST", "/prerequisites/uploads", {}, {**body, **change})
    assert not bundle["calls"] and not bundle["transport"].calls


def test_manual_choice_cannot_allocate_an_automatic_installer_upload(bundle):
    session, _ = wizard(bundle)
    with pytest.raises(InstallerError):
        allocate(session, plan(session))


def test_reopen_reads_original_operation_without_accepting_foreign_upload_handles(bundle):
    session, _ = wizard(bundle)
    value = plan(session, automatic=True)
    allocation = allocate(session, value)
    receive(session, allocation, fixture_content(bundle))
    another, _ = wizard(bundle)
    with pytest.raises(InstallerError):
        another.route("POST", "/prerequisites/plans", {}, {"choices": {"base": "auto", "client": "auto"},
            "local_uploads": {"base-package": allocation["upload_id"]}})
    assert not bundle["calls"]


def test_canceled_upload_cannot_be_attached_to_review(bundle):
    session, _ = wizard(bundle)
    value = plan(session, automatic=True)
    allocation = allocate(session, value)
    receive(session, allocation, fixture_content(bundle))
    session.route("POST", "/prerequisites/uploads/" + allocation["upload_id"] + "/cancel", {}, {})
    with pytest.raises(InstallerError):
        session.route("POST", "/prerequisites/plans", {}, {"choices": {"base": "auto", "client": "auto"},
            "local_uploads": {"base-package": allocation["upload_id"]}})


@pytest.mark.parametrize("failure", ["invalid_choice", "sdk_failure", "stale_generation"])
def test_failed_new_plan_does_not_permanently_retain_an_upload(bundle, monkeypatch, failure):
    session, _ = wizard(bundle)
    value = plan(session, automatic=True)
    allocation = allocate(session, value)
    receive(session, allocation, fixture_content(bundle))
    body = {"choices": {"base": "auto", "client": "auto"},
            "local_uploads": {"base-package": allocation["upload_id"]}}
    if failure == "invalid_choice":
        body["choices"]["base"] = "forged"
    elif failure == "sdk_failure":
        def broken(*args, **kwargs):
            raise InstallerError("STATE_STORAGE_UNAVAILABLE")
        monkeypatch.setattr(session.api, "prerequisites_plan", broken)
    else:
        original = session.api.prerequisites_plan
        def stale(*args, **kwargs):
            result = original(*args, **kwargs)
            session._review_generation += 1
            return result
        monkeypatch.setattr(session.api, "prerequisites_plan", stale)
    with pytest.raises(InstallerError):
        session.route("POST", "/prerequisites/plans", {}, body)
    canceled = session.route("POST", "/prerequisites/uploads/" + allocation["upload_id"] + "/cancel", {}, {})
    assert canceled["state"] == "canceled" and not bundle["calls"]
