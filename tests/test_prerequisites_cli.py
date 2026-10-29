"""CLI transport → реальный signed synthetic coordinator и private journal.

Платформенные installers — fixture. Ни OS packages, ни Docker/GUI, ни сеть
этими source-тестами не принимаются. CLI всегда завершает synchronous drive.
"""
from copy import deepcopy
import json
import threading

import pytest

from homensai_install.api.model import MAX_JSON_BYTES
from homensai_install.api.v1 import InstallerAPI
from homensai_install.cli import main
from homensai_install.prerequisite_runner import CommandResult
from test_prerequisites_execution import ControllerDied, bundle, coordinator, present


def api_for(bundle, *, locale="ru", state_name="product-state", **ports):
    return InstallerAPI.from_bootstrap(bundle["tmp_path"] / state_name, locale=locale,
                                      prerequisites=coordinator(bundle, **ports))


def invoke(capsys, *argv, api=None):
    code = main(["prerequisites", *argv, "--json"], api=api)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    envelope = json.loads(captured.out)
    assert envelope["api_version"] == "1.0" and envelope["request_id"].startswith("req_")
    assert set(envelope) in ({"api_version", "request_id", "result"}, {"api_version", "request_id", "error"})
    return code, envelope


def save_plan(bundle, plan, name="plan.json"):
    path = bundle["tmp_path"] / name
    path.write_text(json.dumps(plan), encoding="utf-8")
    return str(path)


def consents(plan):
    return [argument for reference in plan["required_consents"] for argument in ("--consent-ref", reference)]


def test_stock_cli_reports_unconfigured_and_refuses_before_reading_inputs(capsys, tmp_path):
    state = str(tmp_path / "not-created")
    code, response = invoke(capsys, "capabilities", "--state-dir", state)
    assert code == 0 and response["result"]["configured"] is False
    for command in (("identity",), ("plan",), ("start", "unconfigured", "--plan", str(tmp_path / "missing")),
                    ("operations",), ("resume", "unconfigured")):
        code, response = invoke(capsys, *command, "--state-dir", state)
        assert code == 2 and response["error"]["code"] == "CAPABILITY_UNSUPPORTED"
    assert not (tmp_path / "not-created").exists()


def test_real_metadata_plan_envelope_roundtrip_and_repeated_start(bundle, capsys):
    api = api_for(bundle)
    assert invoke(capsys, "identity", api=api)[1]["result"] == api.prerequisites_identity()
    code, response = invoke(capsys, "license", "base", api=api)
    assert code == 0 and response["result"]["text"].startswith("Synthetic terms")
    choices = save_plan(bundle, {"base": "auto", "client": "auto"}, "choices.json")
    code, envelope = invoke(capsys, "plan", "--choices", choices, "--locale", "en", api=api)
    plan = envelope["result"]
    assert code == 0 and plan["locale"] == "en"
    assert bundle["calls"] == [] and bundle["transport"].calls == []
    path = save_plan(bundle, envelope)
    code, response = invoke(capsys, "start", "cli_one", "--plan", path, *consents(plan), api=api)
    assert code == 0 and response["result"]["status"] == "READY" and response["result"]["ready"]
    assert response["result"]["execution"]["mode"] == "synchronous"
    assert "_dispatch" not in api._prerequisite_instance.store.get("cli_one")
    assert bundle["calls"] == ["base", "client"]
    restored = api_for(bundle, state_name="another-product-state")
    assert invoke(capsys, "start", "cli_one", "--plan", path, *consents(plan), api=restored)[0] == 0
    assert bundle["calls"] == ["base", "client"]
    assert invoke(capsys, "status", "cli_one", api=restored)[0] == 0
    code, response = invoke(capsys, "events", "cli_one", "--limit", "1", api=restored)
    assert code == 0 and len(response["result"]["events"]) == 1
    code, response = invoke(capsys, "attempts", "cli_one", "base", "--after", "0", "--limit", "1", api=restored)
    assert code == 0 and response["result"]["attempts"][0]["attempt"] == 1
    assert invoke(capsys, "attempts", "cli_one", "base", "--after", "1", api=restored)[1]["result"]["attempts"] == []
    assert invoke(capsys, "operations", api=restored)[1]["result"]["operations"] == []
    code, response = invoke(capsys, "operations", "--all", "--limit", "1", api=restored)
    assert code == 0 and response["result"]["operations"][0]["operation_id"] == "cli_one"
    assert not api.state_dir.exists() and not restored.state_dir.exists()


def test_missing_consents_refuse_before_any_operation_or_effect(bundle, capsys):
    api = api_for(bundle)
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    code, response = invoke(capsys, "start", "no_consent", "--plan", save_plan(bundle, plan), api=api)
    assert code == 2 and response["error"]["code"] == "CONSENT_REQUIRED"
    assert api._prerequisite_instance.store.get("no_consent") is None
    assert bundle["calls"] == [] and bundle["transport"].calls == []


def test_manual_pause_recheck_replace_ack_and_synchronous_resume(bundle, capsys):
    api = api_for(bundle)
    plan = api.prerequisites_plan()
    assert invoke(capsys, "start", "manual", "--plan", save_plan(bundle, plan), *consents(plan), api=api)[0] == 3
    assert invoke(capsys, "recheck", "manual", api=api)[0] == 3
    assert invoke(capsys, "events", "manual", "--after-sequence", "0", api=api)[0] == 3
    automatic = api.prerequisites_plan({"base": "auto", "client": "auto"})
    path = save_plan(bundle, automatic, "automatic.json")
    code, refusal = invoke(capsys, "replace-plan", "manual", "--plan", path, *consents(plan), api=api)
    assert code == 2 and refusal["error"]["code"] == "CONSENT_REQUIRED"
    code, accepted = invoke(capsys, "replace-plan", "manual", "--plan", path, *consents(automatic), api=api)
    assert code == 0 and accepted["result"]["status"] == "AWAITING_USER" and not accepted["result"]["ready"]
    assert bundle["calls"] == [] and bundle["transport"].calls == []
    code, result = invoke(capsys, "resume", "manual", api=api)
    assert code == 0 and result["result"]["ready"]
    assert result["result"]["execution"]["mode"] == "synchronous"
    assert bundle["calls"] == ["base", "client"]


def test_cancel_ack_is_distinct_from_readiness(bundle, capsys):
    api = api_for(bundle)
    plan = api.prerequisites_plan()
    assert invoke(capsys, "start", "cancel_me", "--plan", save_plan(bundle, plan), *consents(plan), api=api)[0] == 3
    code, canceled = invoke(capsys, "cancel", "cancel_me", api=api)
    assert code == 0 and canceled["result"]["status"] == "CANCELED"
    for command in ("status", "events", "resume", "recheck"):
        assert invoke(capsys, command, "cancel_me", api=api)[0] == 4
    assert bundle["calls"] == [] and bundle["transport"].calls == []


@pytest.mark.parametrize("returncode,status,marker", [(3010, "REBOOT_REQUIRED", "boot_id"),
                                                     (10, "RELOGIN_REQUIRED", "session_id")])
def test_boundary_ack_does_not_claim_restart_or_readiness(bundle, capsys, returncode, status, marker):
    api = api_for(bundle)
    bundle["results"]["base"] = CommandResult(returncode, True)
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    code, response = invoke(capsys, "start", "boundary", "--plan", save_plan(bundle, plan), *consents(plan), api=api)
    assert code == 3 and response["result"]["status"] == status
    code, response = invoke(capsys, "choose-boundary", "boundary", "later", api=api)
    assert code == 0 and response["result"]["status"] == status
    assert response["result"]["host_action"]["performed"] is False
    assert invoke(capsys, "resume", "boundary", api=api)[0] == 3
    bundle["markers"][marker] = "new-synthetic-boundary"
    assert invoke(capsys, "resume", "boundary", api=api)[0] == 0
    assert bundle["calls"] == ["base", "client"]


@pytest.mark.parametrize("returncode", [5, None])
def test_execution_error_is_failure_even_when_status_awaits_user(bundle, capsys, returncode):
    api = api_for(bundle)
    bundle["results"]["base"] = CommandResult(returncode, True, timed_out=returncode is None)
    bundle["effects"]["base"] = "not_ready"
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    path = save_plan(bundle, plan)
    code, response = invoke(capsys, "start", "failed", "--plan", path, *consents(plan), api=api)
    assert code == 1 and response["result"]["error"]
    assert response["result"]["status"] == ("RECOVERY_REQUIRED" if returncode is None else "AWAITING_USER")
    for command in ("status", "events"):
        assert invoke(capsys, command, "failed", api=api)[0] == 1
    assert invoke(capsys, "start", "failed", "--plan", path, *consents(plan), api=api)[0] == 1
    assert bundle["calls"] == ["base"]


def test_interrupted_synchronous_cli_keeps_intent_and_never_implicitly_replays(bundle, capsys):
    api = api_for(bundle)
    bundle["results"]["base"] = ControllerDied()
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    path = save_plan(bundle, plan)
    with pytest.raises(ControllerDied):
        main(["prerequisites", "start", "interrupted", "--plan", path, *consents(plan)], api=api)
    assert capsys.readouterr().out == ""
    restored = api_for(bundle, state_name="restored-state")
    code, status = invoke(capsys, "status", "interrupted", api=restored)
    assert code == 1 and status["result"]["status"] == "RECOVERY_REQUIRED"
    assert status["result"]["steps"]["base"]["state"] == "INSTALLING"
    code, listing = invoke(capsys, "operations", api=restored)
    assert code == 0 and listing["result"]["operations"][0]["operation_id"] == "interrupted"
    # Idempotent synchronous start is a read-only ACK lookup. It no longer
    # holds its own controller lock and mistakes that read for a live worker.
    code, duplicate = invoke(capsys, "start", "interrupted", "--plan", path, *consents(plan), api=restored)
    assert code == 1 and duplicate["result"]["status"] == "RECOVERY_REQUIRED"
    assert duplicate["result"]["execution"]["state"] == "recovery_required"
    assert duplicate["result"]["execution"]["active"] is False
    assert duplicate["result"]["operation_id"] == "interrupted"
    assert duplicate["result"]["steps"]["base"]["state"] == "INSTALLING"
    assert invoke(capsys, "resume", "interrupted", api=restored)[0] == 1
    assert bundle["calls"] == ["base"]


def test_cli_waits_for_installer_before_returning(bundle, capsys):
    api = api_for(bundle)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    result, failures = [], []

    def effect():
        entered.set()
        assert release.wait(5)
        present(bundle, "base")

    bundle["effects"]["base"] = effect
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    path = save_plan(bundle, plan)

    def caller():
        try:
            result.append(main(["prerequisites", "start", "waiter", "--plan", path, *consents(plan)], api=api))
        except BaseException as error:
            failures.append(error)
        finally:
            returned.set()

    thread = threading.Thread(target=caller)
    thread.start()
    try:
        assert entered.wait(2)
        assert not returned.is_set(), "CLI returned while the installer was still blocked"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures and result == [0]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["result"]["ready"] and envelope["result"]["execution"]["mode"] == "synchronous"
    assert bundle["calls"] == ["base", "client"]


def test_listing_is_identity_scoped_and_omits_private_plan(bundle, capsys):
    api = api_for(bundle)
    plan = api.prerequisites_plan()
    invoke(capsys, "start", "private_op", "--plan", save_plan(bundle, plan), *consents(plan), api=api)
    code, response = invoke(capsys, "operations", "--limit", "1", api=api)
    assert code == 0 and [item["operation_id"] for item in response["result"]["operations"]] == ["private_op"]
    public = json.dumps(response)
    assert "token=never-public" not in public and '"plan"' not in public and '"consents"' not in public
    assert "_dispatch" not in public and str(bundle["payload"]) not in public
    assert invoke(capsys, "operations", "--after-id", "private_op", api=api)[1]["result"]["operations"] == []
    foreign = api_for(bundle, owner_id="another-owner")
    assert invoke(capsys, "operations", api=foreign)[1]["result"]["operations"] == []
    code, refusal = invoke(capsys, "status", "private_op", api=foreign)
    assert code == 2 and refusal["error"]["code"] == "PERMISSION_REQUIRED"


@pytest.mark.parametrize("flag", ["--state-dir", "--trust-config", "--development-runtime", "--docker-cli-config", "--allow-development"])
def test_injected_api_rejects_bootstrap_overrides_without_disclosing_values(bundle, capsys, flag):
    api = api_for(bundle)
    flags = [flag] if flag == "--allow-development" else [flag, "/private-bootstrap-secret"]
    code, response = invoke(capsys, "capabilities", *flags, api=api)
    assert code == 2 and response["error"]["code"] == "INVALID_REQUEST"
    assert "private-bootstrap-secret" not in json.dumps(response)
    assert bundle["calls"] == []


def test_injected_locale_is_inherited_or_explicit_without_mutating_host(bundle, capsys):
    api = api_for(bundle, locale="en")
    assert invoke(capsys, "plan", api=api)[1]["result"]["locale"] == "en"
    assert invoke(capsys, "plan", "--locale", "de", api=api)[1]["result"]["locale"] == "de"
    assert invoke(capsys, "status", "missing", api=api)[1]["error"]["locale"] == "en"
    assert invoke(capsys, "status", "missing", "--locale", "de", api=api)[1]["error"]["locale"] == "de"
    assert api.locale == "en"


@pytest.mark.parametrize("content", ['{"base":"auto","base":"manual"}', '{"base":NaN}', '["auto"]', 'x' * (MAX_JSON_BYTES + 1)])
def test_json_inputs_are_bounded_and_unambiguous(bundle, capsys, content):
    api = api_for(bundle)
    path = bundle["tmp_path"] / "invalid.json"
    path.write_text(content)
    code, response = invoke(capsys, "plan", "--choices", str(path), api=api)
    assert code == 2 and response["error"]["code"] == "INVALID_REQUEST"
    assert bundle["calls"] == [] and bundle["transport"].calls == []


def test_plan_envelope_does_not_accept_another_version_or_extra_fields(bundle, capsys):
    api = api_for(bundle)
    _, valid = invoke(capsys, "plan", api=api)
    for invalid in ({**valid, "api_version": "999"}, {**valid, "bootstrap": {"argv": ["shell"]}}):
        code, response = invoke(capsys, "start", "bad_plan", "--plan", save_plan(bundle, invalid), api=api)
        assert code == 2 and response["error"]["code"] == "INVALID_REQUEST"
        assert api._prerequisite_instance.store.get("bad_plan") is None


@pytest.mark.parametrize("flag", ["--background", "--bootstrap", "--adapter-registry", "--module"])
def test_cli_never_accepts_background_or_dynamic_bootstrap_args(bundle, capsys, flag):
    api = api_for(bundle)
    code, response = invoke(capsys, "plan", flag, "private-argument-secret", api=api)
    assert code == 2 and response["error"]["code"] == "INVALID_REQUEST"
    assert "private-argument-secret" not in json.dumps(response)


def test_injected_api_cannot_be_json_or_disappear_into_gui_or_scaffold(bundle, capsys):
    code, response = invoke(capsys, "capabilities", api={"registry": "shell"})
    assert code == 2 and response["error"]["code"] == "INVALID_REQUEST"
    api = api_for(bundle)
    for argv in (["gui"], ["init-adapter", "--output-dir", str(bundle["tmp_path"] / "unused")]):
        assert main(argv, api=api) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_REQUEST"
    assert not (bundle["tmp_path"] / "unused").exists()


def test_unexpected_exception_is_redacted(bundle, capsys, monkeypatch):
    api = api_for(bundle)

    def broken(*args, **kwargs):
        raise RuntimeError("private-exception-secret")

    monkeypatch.setattr(api, "prerequisites_plan", broken)
    code, response = invoke(capsys, "plan", api=api)
    assert code == 1 and response["error"]["code"] == "UNEXPECTED_ERROR"
    assert "private-exception-secret" not in json.dumps(response)


@pytest.mark.parametrize("future", [{"status": "FUTURE_STATE", "ready": False}, {"status": "READY", "ready": False}])
def test_unknown_or_nonready_response_cannot_be_success(bundle, capsys, monkeypatch, future):
    api = api_for(bundle)
    monkeypatch.setattr(api, "prerequisites_status", lambda operation_id: deepcopy(future))
    assert invoke(capsys, "status", "future", api=api)[0] == 1
