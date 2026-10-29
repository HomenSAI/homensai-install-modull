"""Public API / actual filesystem uninstall boundaries on Linux DEV fixtures.

The existing native fixture supplies synthetic trust and an explicit host
interpreter. These are source integration checks, not installed/OS acceptance.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.api.model import digest
from homensai_install.errors import InstallerError


pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native integration")


def release_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted((root / "releases").rglob("*")) if p.is_file()}


@pytest.fixture
def installed():
    spec = importlib.util.spec_from_file_location(
        "uninstall_boundary_native_fixture", Path(__file__).with_name("test_native_engine.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.NativeLifecycleTests(methodName="runTest")
    fixture.setUp()
    try:
        api = InstallerAPI(fixture.base / "boundary-api", engine=fixture.engine)
        first = fixture.bundle("v1", runtime_tag="old")
        latest = fixture.bundle("v2", runtime_tag="new")

        def plan(action, bundle):
            return api.plan(action, bundle["manifest"], fixture.target, options=bundle["options"])

        def submit(review, key):
            return api.submit(review["plan_id"], review["plan_digest"],
                [review["review_consent_ref"], *review["required_consents"]],
                idempotency_key=key, background=False)

        assert submit(plan("install", first), "install")["status"] == "SUCCEEDED"
        fixture.current = first
        assert submit(plan("update", latest), "update")["status"] == "SUCCEEDED"
        fixture.current = latest
        user = fixture.root / "data" / "synthetic-private-user-file.txt"
        user.write_bytes(b"Synthetic user data must survive uninstall refusal.\n")
        yield fixture, api, latest, plan, submit, user
    finally:
        fixture.tearDown()


def attempted_code(submit, review, key):
    try:
        operation = submit(review, key)
    except InstallerError as error:
        return error.code
    if operation["status"] != "RECOVERY_REQUIRED":
        return "PUBLIC_STATUS_" + operation["status"]
    return operation["error"]["code"]


def assert_running(api, fixture, bundle):
    discovered = api.discover(bundle["manifest"], fixture.target, options=bundle["options"])
    assert discovered["components"]["web"]["running"] is True


def test_inactive_owned_byte_changed_after_review_refuses_before_stop_or_unlink(installed):
    fixture, api, latest, plan, submit, user = installed
    review = plan("uninstall", latest)
    inactive = fixture.root / "releases" / "app-v1" / "server.py"
    inactive.write_bytes(inactive.read_bytes() + b"\n# unexpected user modification\n")
    before, user_before = release_bytes(fixture.root), user.read_bytes()

    code = attempted_code(submit, review, "uninstall-inactive-drift")
    assert release_bytes(fixture.root) == before
    assert user.read_bytes() == user_before
    assert code == "INTEGRITY_FAILED"
    assert_running(api, fixture, latest)


def test_new_release_metadata_after_review_cannot_expand_removal_set(installed):
    fixture, api, latest, plan, submit, user = installed
    review = plan("uninstall", latest)
    surprise = fixture.bundle("v3", runtime_tag="unreviewed")
    destination = fixture.root / "releases" / "app-v3"
    shutil.copytree(surprise["options"]["payload_dir"], destination)
    control = fixture.root / ".hinstall" / "releases"
    metadata = json.loads((control / "v2.json").read_text())
    metadata = {**copy.deepcopy(metadata), "release": "v3", "manifest": surprise["manifest"],
                "inventory": fixture.trusted["v3"]["files"]}
    (control / "v3.json").write_text(json.dumps(metadata))
    shutil.copy2(surprise["options"]["inventory_file"], control / "v3.inventory.json")
    shutil.copy2(surprise["options"]["signature_file"], control / "v3.signature.json")
    before, user_before = release_bytes(fixture.root), user.read_bytes()

    code = attempted_code(submit, review, "uninstall-added-release")
    assert release_bytes(fixture.root) == before
    assert user.read_bytes() == user_before
    assert code == "PLAN_STALE"
    assert_running(api, fixture, latest)


@pytest.mark.parametrize("tamper", ["owned_code", "retained_payload", "retained_receipt",
                                  "retained_installation_marker"])
def test_mutation_after_recovery_checkpoint_refuses_before_first_unlink(installed, monkeypatch, tamper):
    fixture, api, latest, plan, submit, user = installed
    review = plan("uninstall", latest)
    original_execute = fixture.engine.execute
    observed = []
    user_before = user.read_bytes()

    def execute(execution_plan, emit, canceled):
        def boundary(event):
            emit(event)
            if event.get("phase") == "preserve_recovery" and event.get("status") == "SUCCEEDED":
                assert not observed
                if tamper == "retained_receipt":
                    token = digest([review["plan_id"], review["plan_digest"]])[:32]
                    receipt = fixture.root / ".hinstall" / "uninstall-receipts" / (token + ".json")
                    record = json.loads(receipt.read_text())
                    record["receipt_id"] = "0" * 64
                    receipt.write_text(json.dumps(record))
                elif tamper == "retained_installation_marker":
                    marker = fixture.root / "runtime" / "recovery" / "v2" / "installation.json"
                    original = json.loads(marker.read_text())
                    altered = {**original, "product_id": "another-synthetic-product"}
                    assert altered["installation_id"] == original["installation_id"]
                    assert altered["owner"] == original["owner"]
                    marker.write_text(json.dumps(altered))
                else:
                    last = (fixture.root / "releases" / "app-v2" / "server.py" if tamper == "owned_code" else
                            fixture.root / "runtime" / "recovery" / "v2" / "payload" / "server.py")
                    assert last.is_file()
                    last.write_bytes(last.read_bytes() + b"\n# changed after retained recovery\n")
                observed.append(release_bytes(fixture.root))
        return original_execute(execution_plan, boundary, canceled)

    monkeypatch.setattr(fixture.engine, "execute", execute)
    code = attempted_code(submit, review, "uninstall-last-boundary-drift")
    assert len(observed) == 1
    assert release_bytes(fixture.root) == observed[0]
    assert user.read_bytes() == user_before
    assert code == "INTEGRITY_FAILED"
    discovered = api.discover(latest["manifest"], fixture.target, options=latest["options"])
    assert discovered["components"] == {}


def test_power_cut_after_unlink_fences_new_operation_and_requires_original_resume(installed):
    fixture, api, latest, plan, submit, user = installed
    review = plan("uninstall", latest)
    victim = fixture.root / "releases" / "app-v1" / "server.py"
    assert victim.is_file()
    before, user_before = release_bytes(fixture.root), user.read_bytes()
    pid = os.fork()
    if pid == 0:
        original_unlink = os.unlink

        def cut_after_unlink(path, *, dir_fd=None):
            candidate = Path(os.fsdecode(path))
            if dir_fd is not None:
                candidate = Path(os.readlink("/proc/self/fd/" + str(dir_fd))) / candidate
            result = original_unlink(path, dir_fd=dir_fd)
            if candidate == victim:
                # The actual unlink happened; neither the engine nor public API
                # gets a Python exception/finally opportunity to record success.
                os._exit(89)
            return result

        os.unlink = cut_after_unlink
        try:
            submit(review, "uninstall-power-cut")
        except BaseException:
            os._exit(86)
        os._exit(87)
    deadline = time.monotonic() + 20
    while True:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("Synthetic uninstall child did not reach the power-cut boundary")
        time.sleep(0.02)
    assert os.waitstatus_to_exitcode(status) == 89
    assert not victim.exists()
    remaining = release_bytes(fixture.root)
    assert remaining and len(remaining) < len(before)
    assert user.read_bytes() == user_before

    # A fresh facade observes interruption; construction/status performs no
    # recovery and never guesses that absent files were removed by this token.
    reopened = InstallerAPI(api.state_dir, engine=fixture.engine)
    operations = reopened.operations_list()["operations"]
    matches = [item for item in operations if item["plan_id"] == review["plan_id"]]
    assert len(matches) == 1
    operation_id = matches[0]["operation_id"]
    assert reopened.status(operation_id)["status"] == "RECOVERY_REQUIRED"
    assert release_bytes(fixture.root) == remaining

    try:
        new_review = reopened.plan("uninstall", latest["manifest"], fixture.target, options=latest["options"])
    except InstallerError as error:
        code = error.code
    else:
        def new_submit(other, key):
            return reopened.submit(other["plan_id"], other["plan_digest"],
                [other["review_consent_ref"], *other["required_consents"]],
                idempotency_key=key, background=False)
        code = attempted_code(new_submit, new_review, "uninstall-unrelated-new-operation")
    assert release_bytes(fixture.root) == remaining
    assert user.read_bytes() == user_before
    # The public facade may reject its still-owned resource before the native
    # uninstall fence is consulted; both refusals preserve the same boundary.
    assert code in {"OPERATION_CONFLICT", "RECOVERY_REQUIRED"}

    resumed = reopened.resume(operation_id, background=False)
    assert resumed["operation_id"] == operation_id and resumed["status"] == "SUCCEEDED"
    assert release_bytes(fixture.root) == {}
    assert user.read_bytes() == user_before
    assert reopened.status(operation_id)["result"]["data_preserved"] is True


def test_successful_uninstall_returns_bound_retained_recovery_receipt(installed):
    fixture, api, latest, plan, submit, user = installed
    review = plan("uninstall", latest)
    installation = api.discover(latest["manifest"], fixture.target,
        options=latest["options"])["installation"]["installation_id"]
    user_before = user.read_bytes()
    operation = submit(review, "uninstall-retained-receipt")
    assert operation["status"] == "SUCCEEDED"
    receipt = operation["result"]["retained_recovery"]
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    expected = {"schema_version": 1, "kind": "retained_uninstall_recovery",
        "installation_id": installation, "operation_id": "op_" + token,
        "review_sha256": digest(review["engine_plan"]["uninstall_removal"]),
        "active_release": "v2", "retained_payload_sha256": digest(fixture.trusted["v2"]["files"]),
        "verification_scope": "signed_active_payload_and_original_removal_set"}
    assert receipt == {"receipt_id": digest(expected), **expected}
    assert release_bytes(fixture.root) == {}
    assert user.read_bytes() == user_before
    reopened = InstallerAPI(api.state_dir, engine=fixture.engine)
    assert reopened.status(operation["operation_id"])["result"]["retained_recovery"] == receipt
    duplicate = reopened.submit(review["plan_id"], review["plan_digest"],
        [review["review_consent_ref"], *review["required_consents"]],
        idempotency_key="uninstall-retained-receipt", background=False)
    assert duplicate["operation_id"] == operation["operation_id"]
    assert duplicate["result"]["retained_recovery"] == receipt
    assert user.read_bytes() == user_before
