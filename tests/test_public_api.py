"""Реальные journal/API contracts; fake engine не является installed-приёмкой."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from homensai_install.api.model import parse_json
from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError


ROOT = Path(__file__).resolve().parents[1]


class Engine:
    def __init__(self, root):
        self.root = root
        self.fact = "before"
        self.calls = 0
        self.block = threading.Event()
        self.block.set()
        self.started = threading.Event()
        self.final = "SUCCEEDED"

    def capabilities(self, target):
        return {"actions": {"install": {"supported": True}, "update": {"supported": True}},
                "readiness": "test-adapter"}

    def discover(self, manifest, target, options):
        return {"fingerprint_data": {"fact": self.fact}, "timestamp": str(time.monotonic())}

    def plan(self, action, manifest, target, inventory, options):
        return {"steps": [{"step_id": "write-marker"}], "required_consents": ["terms:test"]}

    def execute(self, plan, emit, canceled):
        self.calls += 1
        self.started.set()
        while not self.block.wait(0.01):
            if canceled():
                return {"status": "CANCELED"}
        emit({"type": "step.completed", "phase": "apply", "percent": 80,
              "stdout": "private-secret", "environment": {"API_TOKEN": "private-secret"}})
        self.root.joinpath("executed").write_text(plan["action"])
        self.fact = "after"
        return {"status": self.final}

    def resume(self, plan, previous, emit, canceled):
        return self.execute(plan, emit, canceled)


@pytest.fixture
def manifest():
    return json.loads((ROOT / "examples/rent.manifest.json").read_text())


@pytest.fixture
def subject(tmp_path, manifest):
    engine = Engine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=engine)
    options = {"install_root": str(tmp_path / "install")}
    return api, engine, manifest, options


def make_plan(subject, action="install"):
    api, _, manifest, options = subject
    return api.plan(action, manifest, options=options)


def submit(api, plan, key="one", **kwargs):
    return api.submit(plan["plan_id"], plan["plan_digest"],
                      [plan["review_consent_ref"], *plan["required_consents"]],
                      idempotency_key=key, **kwargs)


def wait_terminal(api, operation_id):
    for _ in range(200):
        result = api.status(operation_id)
        if result["status"] not in {"QUEUED", "RUNNING"}:
            return result
        time.sleep(0.01)
    pytest.fail("worker did not complete")


def test_validate_normalizes_both_sources_without_inventing_native(tmp_path):
    api = InstallerAPI(tmp_path / "never-created")
    for filename, complete in (("jobpilot.manifest.json", False), ("rent.manifest.json", True)):
        result = api.validate(ROOT / "examples" / filename)
        assert result["normalized_manifest"]["native_complete"] is complete
        assert result["normalized_manifest"]["source_schema_version"] == result["source_manifest"]["schema_version"]
    assert not api.state_dir.exists()


def test_plan_does_not_execute_or_write_product_and_is_immutable(subject):
    api, engine, _, options = subject
    plan = make_plan(subject)
    assert not Path(options["install_root"]).exists()
    assert engine.calls == 0
    original = api.get_plan(plan["plan_id"])
    plan["options"]["install_root"] = "/arbitrary"
    assert api.get_plan(plan["plan_id"])["options"] == original["options"]
    assert api.schema("plan")["required"]


def test_stale_plan_rejected_before_execution(subject):
    api, engine, _, _ = subject
    plan = make_plan(subject)
    engine.fact = "other-installation"
    with pytest.raises(InstallerError) as caught:
        submit(api, plan, background=False)
    assert caught.value.code == "PLAN_STALE"
    assert engine.calls == 0


def test_review_and_terms_both_required(subject):
    api, engine, _, _ = subject
    plan = make_plan(subject)
    for consents in ([], [plan["review_consent_ref"]], ["terms:test"]):
        with pytest.raises(InstallerError) as caught:
            api.submit(plan["plan_id"], plan["plan_digest"], consents, idempotency_key="one", background=False)
        assert caught.value.code == "CONSENT_REQUIRED"
    assert engine.calls == 0


def test_idempotency_survives_new_sdk_instance_and_changed_inventory(subject):
    api, engine, _, _ = subject
    plan = make_plan(subject)
    first = submit(api, plan, background=False)
    second = submit(InstallerAPI(api.state_dir, engine=engine), plan, background=False)
    assert first["status"] == "SUCCEEDED"
    assert first["operation_id"] == second["operation_id"]
    assert engine.calls == 1


def test_same_key_different_plan_conflicts(subject):
    api, _, _, _ = subject
    first = make_plan(subject)
    submit(api, first, background=False)
    second = make_plan(subject, "update")
    with pytest.raises(InstallerError) as caught:
        submit(api, second, background=False)
    assert caught.value.code == "OPERATION_CONFLICT"


def test_same_resource_serialized_across_sdk_instances(subject):
    api, engine, _, _ = subject
    engine.block.clear()
    first, second = make_plan(subject), make_plan(subject)
    operation = submit(api, first)
    assert engine.started.wait(1)
    other = InstallerAPI(api.state_dir, engine=engine)
    with pytest.raises(InstallerError) as caught:
        submit(other, second, "second")
    assert caught.value.code == "OPERATION_CONFLICT"
    engine.block.set()
    assert wait_terminal(api, operation["operation_id"])["status"] == "SUCCEEDED"


def test_concurrent_same_key_has_one_execution(subject):
    api, engine, _, _ = subject
    engine.block.clear()
    plan = make_plan(subject)
    barrier = threading.Barrier(2)
    outputs, failures = [], []

    def caller():
        try:
            other = InstallerAPI(api.state_dir, engine=engine)
            barrier.wait()
            outputs.append(submit(other, plan))
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=caller) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    engine.block.set()
    assert not failures
    assert len(outputs) == 2
    assert outputs[0]["operation_id"] == outputs[1]["operation_id"]
    wait_terminal(api, outputs[0]["operation_id"])
    assert engine.calls == 1


def test_events_cursor_sequences_and_no_arbitrary_stdout(subject):
    api, _, _, _ = subject
    operation = submit(api, make_plan(subject), background=False)
    first = api.events(operation["operation_id"])
    sequence = [event["sequence"] for event in first["events"]]
    assert sequence == list(range(1, len(sequence) + 1))
    assert first["snapshot"]["last_sequence"] == sequence[-1]
    assert api.events(operation["operation_id"], sequence[-1])["events"] == []
    assert "private-secret" not in json.dumps(first)


def test_cancel_is_cooperative_and_does_not_report_success(subject):
    api, engine, _, _ = subject
    engine.block.clear()
    operation = submit(api, make_plan(subject))
    assert engine.started.wait(1)
    api.cancel(operation["operation_id"])
    result = wait_terminal(api, operation["operation_id"])
    engine.block.set()
    assert result["status"] == "CANCELED"
    assert not engine.root.joinpath("executed").exists()


def test_rollback_success_is_failed_update(subject):
    api, engine, _, _ = subject
    engine.final = "FAILED_ROLLED_BACK"
    result = submit(api, make_plan(subject), background=False)
    assert result["status"] == "FAILED_ROLLED_BACK"


def test_changed_plan_content_cannot_execute(subject):
    api, engine, _, _ = subject
    plan = make_plan(subject)
    with sqlite3.connect(api.state_dir / "operations.sqlite3") as db:
        changed = deepcopy(plan)
        changed["action"] = "update"
        db.execute("UPDATE plans SET body=?", (json.dumps(changed),))
    with pytest.raises(InstallerError) as caught:
        submit(api, plan, background=False)
    assert caught.value.code == "INTEGRITY_FAILED"
    assert engine.calls == 0


def test_unknown_capability_and_options_fail_closed(subject):
    api, _, manifest, options = subject
    with pytest.raises(InstallerError) as caught:
        api.plan("build", manifest, options=options)
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    with pytest.raises(InstallerError):
        api.plan("install", manifest, options={**options, "verified": True})


def test_json_duplicates_nonfinite_depth_and_secrets_not_echoed():
    for body in ('{"x":1,"x":2}', '{"x":NaN}', '["private-secret",' + '[' * 60 + '0' + ']' * 60 + ']'):
        with pytest.raises(InstallerError) as caught:
            parse_json(body)
        assert "private-secret" not in json.dumps(caught.value.to_dict())


def test_world_readable_or_symlink_state_is_rejected(tmp_path, manifest):
    engine = Engine(tmp_path)
    exposed = tmp_path / "exposed"
    exposed.mkdir(mode=0o755)
    for path in (exposed, tmp_path / "link"):
        if path.name == "link":
            path.symlink_to(exposed, target_is_directory=True)
        with pytest.raises(InstallerError):
            InstallerAPI(path, engine=engine).plan("install", manifest, options={"install_root": str(tmp_path / "install")})


def test_missing_operation_does_not_create_state(tmp_path):
    api = InstallerAPI(tmp_path / "not-created")
    with pytest.raises(InstallerError) as caught:
        api.status("op_missing")
    assert caught.value.code == "OPERATION_NOT_FOUND"
    assert not api.state_dir.exists()


def test_offline_schemas_and_repository_mirrors():
    api = InstallerAPI("/tmp/not-created-public-api-test")
    for name in ("request", "plan", "operation", "event", "normalized", "manifest"):
        assert api.schema(name) == json.loads((ROOT / "api/schemas" / (name + ".schema.json")).read_text())


def test_cli_machine_output_and_errors_hide_arguments(capsys, tmp_path):
    from homensai_install.cli import main
    assert main(["--state-dir", str(tmp_path / "unused"), "api", "describe", "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["result"]["api_version"] == "1.0"
    assert main(["validate", "--private-secret", "do-not-echo"]) == 2
    output = capsys.readouterr()
    assert "do-not-echo" not in output.out + output.err
    assert json.loads(output.out)["error"]["code"] == "INVALID_REQUEST"


def test_operations_list_missing_state_is_empty_without_writes(tmp_path):
    api = InstallerAPI(tmp_path / "not-created")
    assert api.operations_list() == {"operations": [], "has_more": False, "limit": 100}
    assert not api.state_dir.exists()


def test_operations_list_recovers_lost_ui_session_without_resuming_execution(subject):
    api, engine, manifest, _ = subject
    operation = submit(api, make_plan(subject), background=False)
    operation_id = operation["operation_id"]
    # Моделируем persisted RUNNING от завершившегося процесса. Это source contract,
    # а не installed/GUI acceptance: fake engine не запускает сервер.
    api._journal().update(operation_id, {
        "status": "RUNNING", "owner": {"pid": 0}, "result": None,
        "consent_refs": ["never-list-this-private-reference"],
        "error": {"private": "never-list-this-private-value"}})
    restarted = InstallerAPI(api.state_dir, engine=engine)
    found = restarted.operations_list(product_id=manifest["product_id"])
    assert len(found["operations"]) == 1
    row = found["operations"][0]
    assert row["operation_id"] == operation_id and row["status"] == "RECOVERY_REQUIRED"
    assert set(row) == {"operation_id", "plan_id", "action", "status", "product_id", "updated_at"}
    assert "never-list-this" not in json.dumps(found)
    assert engine.calls == 1
    events = restarted.events(operation_id)["events"]
    assert events[-1]["type"] == "operation.interrupted"
    assert restarted.operations_list()["operations"][0]["operation_id"] == operation_id
    assert engine.calls == 1


def test_operations_list_filters_product_finished_and_recoverable_cancel(subject):
    api, engine, manifest, options = subject
    first = submit(api, make_plan(subject), background=False)
    other_manifest = deepcopy(manifest)
    other_manifest["product_id"] = "other_product"
    other_plan = api.plan("install", other_manifest, options=options)
    second = submit(api, other_plan, key="other", background=False)
    api._journal().update(second["operation_id"], {
        "status": "CANCELED", "result": {"resume_required": True}})
    assert api.operations_list(manifest["product_id"])["operations"] == []
    all_finished = api.operations_list(manifest["product_id"], unfinished_only=False)
    assert [row["operation_id"] for row in all_finished["operations"]] == [first["operation_id"]]
    recoverable = api.operations_list("other_product")
    assert [row["operation_id"] for row in recoverable["operations"]] == [second["operation_id"]]
    assert recoverable["operations"][0]["status"] == "CANCELED"
    assert engine.calls == 2


def test_operations_list_bounded_and_invalid_inputs_fail_closed(subject):
    api, _, _, _ = subject
    for index in range(3):
        submit(api, make_plan(subject), key=f"op-{index}", background=False)
    page = api.operations_list(unfinished_only=False, limit=2)
    assert len(page["operations"]) == 2 and page["has_more"] and page["limit"] == 2
    for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"unfinished_only": "false"},
                   {"product_id": "invalid/private/product"}):
        with pytest.raises(InstallerError) as caught:
            api.operations_list(**kwargs)
        assert caught.value.code == "INVALID_REQUEST"


def test_cli_operations_list_after_ui_state_loss(capsys, tmp_path):
    from homensai_install.cli import main
    state = tmp_path / "empty"
    assert main(["operations", "--state-dir", str(state), "--product-id", "rent", "--json"]) == 0
    value = json.loads(capsys.readouterr().out)["result"]
    assert value == {"operations": [], "has_more": False, "limit": 100}
    assert not state.exists()


def test_operations_list_accepts_digit_prefixed_product_id_from_shared_schema(subject):
    api, _, manifest, options = subject
    manifest = deepcopy(manifest)
    manifest['product_id'] = '7-product'
    plan = api.plan('install', manifest, options=options)
    operation = submit(api, plan, background=False)
    listed = api.operations_list('7-product', unfinished_only=False)
    assert listed['operations'][0]['operation_id'] == operation['operation_id']
