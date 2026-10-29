"""Independent public maintenance contracts with real DEV-signed source fixtures.

Linux source integration only. The a7d baseline deliberately advertises neither
repair nor reinstall; its CAPABILITY_UNSUPPORTED is an unimplemented contract,
not evidence of a destructive baseline defect. No production gate is closed.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from homensai_install.errors import InstallerError
from shared import archive_crypto
from test_native_adapter_uninstall_boundary import case, child_effect, effect_rows, interrupted_operation, tree, uninstall
from test_native_migration import migration


def business_state(case):
    with sqlite3.connect(case.database.as_uri() + "?mode=ro", uri=True) as db:
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        rows = db.execute("SELECT value FROM user_state ORDER BY value").fetchall()
    return {"schema": schema, "rows": rows,
        "document": case.document.read_bytes(), "credential": case.credential.read_bytes(),
        "settings": (case.fixture.root / "config/settings.json").read_bytes(),
        "migration_effects": effect_rows(case.database, "migration_apply"),
        "restore_effects": effect_rows(case.database, "restore"),
        "post_restore_effects": effect_rows(case.database, "post_restore")}


def maintenance_review(case, action, monkeypatch):
    before = tree(case.fixture.root)
    with monkeypatch.context() as readonly:
        readonly.setattr(ExecutableProductAdapter, "invoke",
            lambda *a, **k: pytest.fail("maintenance plan must not execute product hooks"))
        review = case.plan(action, case.second)
    assert tree(case.fixture.root) == before
    return review


def assert_refused(call, codes):
    try:
        operation = call()
    except InstallerError as error:
        assert error.code in codes
    else:
        assert operation["status"] in {"FAILED", "RECOVERY_REQUIRED"}, operation
        assert operation["error"]["code"] in codes


def assert_safety_oracle(case, review, operation, before):
    result = operation["result"]
    proof = result["before_maintenance"]
    locator = result["before_maintenance_archive"]
    assert proof["receipt_id"] == digest({k: v for k, v in proof.items() if k != "receipt_id"})
    assert proof["kind"] == "before_code_maintenance_backup"
    assert proof["maintenance_action"] == review["action"]
    assert proof["review_sha256"] == review["engine_plan"]["code_maintenance"]["review_sha256"]
    assert proof["candidate_binding_sha256"] == review["engine_plan"]["code_maintenance"]["candidate_binding_sha256"]
    assert proof["migration_eligible"] is False
    assert proof["encrypted"] is True and proof["includes_recovery_payload"] is True
    assert proof["verification_scope"] == "full_snapshot_and_signed_recovery_payload_restored"
    assert proof["schema_plan"]["from_version"] == proof["schema_plan"]["to_version"] == "schema_2"
    assert proof["schema_plan"]["steps"] == []
    assert proof["archive_sha256"] == locator["sha256"] == hashlib.sha256(Path(locator["path"]).read_bytes()).hexdigest()
    assert archive_crypto.is_encrypted(locator["path"])
    oracle = case.fixture.base / ("before-" + review["action"] + "-independent-oracle")
    oracle.mkdir(mode=0o700)
    archive_crypto.restore_archive(locator["path"], oracle,
        password_provider=lambda: "synthetic-archive-passphrase", expected_product_id=case.second["manifest"]["product_id"])
    assert (oracle / "roots/data/document.txt").read_bytes() == before["document"]
    assert (oracle / "roots/data/synthetic-credential.txt").read_bytes() == before["credential"]
    assert (oracle / "roots/config/settings.json").read_bytes() == before["settings"]
    with sqlite3.connect((oracle / "roots/data/schema.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == before["schema"]
        assert db.execute("SELECT value FROM user_state ORDER BY value").fetchall() == before["rows"]


@pytest.mark.parametrize("action", ["repair", "reinstall"])
def test_damaged_installed_adapter_is_replaced_from_signed_candidate_without_data_migration(case, action, monkeypatch):
    root = case.fixture.root
    installed = root / "releases/app-release-2"
    damaged = installed / "adapter.py"
    assert damaged.is_file()
    damaged.write_text("raise RuntimeError('synthetic damaged installed adapter must never run')\n")
    before = business_state(case)
    invoked = []
    actual = ExecutableProductAdapter._execute

    def observed(adapter, hook, request, request_digest):
        payload = Path(adapter.options["payload_dir"])
        if payload == installed:
            assert hook == "readiness", "only readiness may run from the published installed target"
            assert tree(installed) == tree(Path(case.second["options"]["payload_dir"])), "published target must equal the signed candidate before readiness"
        invoked.append((hook, payload))
        return actual(adapter, hook, request, request_digest)

    monkeypatch.setattr(ExecutableProductAdapter, "_execute", observed)
    review = maintenance_review(case, action, monkeypatch)
    result = case.submit(review, "independent-" + action + "-damaged")
    assert result["status"] == "SUCCEEDED", result.get("error")
    assert business_state(case) == before
    assert_safety_oracle(case, review, result, before)
    assert tree(installed) == tree(Path(case.second["options"]["payload_dir"]))
    assert any(hook == "quiesce" for hook, _ in invoked)
    assert any(hook == "readiness" for hook, _ in invoked)
    assert not any(hook in {"migration_apply", "restore", "post_restore"} for hook, _ in invoked)


def test_same_release_reinstall_after_registered_uninstall_preserves_latest_schema_and_business_data(case, monkeypatch):
    root = case.fixture.root
    before = business_state(case)
    uninstall(case, "independent-before-reinstall")
    assert not any((root / "releases").iterdir())
    assert business_state(case) == before
    review = maintenance_review(case, "reinstall", monkeypatch)
    result = case.submit(review, "independent-reinstall-retained")
    assert result["status"] == "SUCCEEDED", result.get("error")
    assert business_state(case) == before
    assert_safety_oracle(case, review, result, before)
    assert tree(root / "releases/app-release-2") == tree(Path(case.second["options"]["payload_dir"]))
    owner = json.loads((root / ".hinstall/installation.json").read_text())
    assert owner["installed"] is True and "retained_uninstall" not in owner


@pytest.mark.parametrize("action", ["repair", "reinstall"])
def test_candidate_signed_byte_drift_after_review_refuses_before_any_product_hook(case, action, monkeypatch):
    root = case.fixture.root
    review = maintenance_review(case, action, monkeypatch)
    candidate = Path(case.second["options"]["payload_dir"])
    (candidate / "adapter.py").write_text("raise RuntimeError('synthetic changed candidate')\n")
    before_data = tree(root / "data")
    before_releases = tree(root / "releases")
    with monkeypatch.context() as no_hooks:
        no_hooks.setattr(ExecutableProductAdapter, "_execute",
            lambda *a, **k: pytest.fail("changed signed candidate must fail before hooks"))
        assert_refused(lambda: case.submit(review, "candidate-drift-" + action),
            {"INTEGRITY_FAILED", "PLAN_STALE"})
    assert tree(root / "data") == before_data
    assert tree(root / "releases") == before_releases


def test_fresh_candidate_quiesce_never_recreates_ledger_lost_between_proof_and_invoke(case, monkeypatch):
    root = case.fixture.root
    review = maintenance_review(case, "repair", monkeypatch)
    ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    assert ledger.is_file()
    before_data = tree(root / "data")
    before_releases = tree(root / "releases")
    before_archives = set(case.fixture.base.rglob("*.hsac"))
    original = ExecutableProductAdapter.invoke
    actual = ExecutableProductAdapter._execute
    cut = []

    def disappear(adapter, hook, request, **kwargs):
        if hook == "quiesce":
            assert not cut and adapter.state_dir == ledger.parent
            ledger.unlink()
            cut.append(True)
        return original(adapter, hook, request, **kwargs)

    def no_quiesce(adapter, hook, *args, **kwargs):
        if hook == "quiesce":
            pytest.fail("missing authoritative ledger must not run a product hook")
        return actual(adapter, hook, *args, **kwargs)

    monkeypatch.setattr(ExecutableProductAdapter, "invoke", disappear)
    monkeypatch.setattr(ExecutableProductAdapter, "_execute", no_quiesce)
    assert_refused(lambda: case.submit(review, "maintenance-missing-ledger-race"),
        {"RECOVERY_REQUIRED", "INTEGRITY_FAILED"})
    assert cut == [True] and not ledger.exists()
    assert tree(root / "data") == before_data
    assert tree(root / "releases") == before_releases
    assert set(case.fixture.base.rglob("*.hsac")) == before_archives


@pytest.mark.parametrize("fault", ["lost_ack", "missing_ledger", "unknown_effect"])
def test_candidate_quiesce_lost_ack_original_operation_resume_or_fail_closed(case, monkeypatch, fault):
    review = maintenance_review(case, "repair", monkeypatch)
    before = business_state(case)
    original = ExecutableProductAdapter._execute
    fired = []

    def lost_ack(adapter, hook, request, request_digest):
        result = original(adapter, hook, request, request_digest)
        if hook == "quiesce" and not fired:
            fired.append(request["idempotency_key"])
            raise InstallerError("ADAPTER_TIMEOUT")
        return result

    monkeypatch.setattr(ExecutableProductAdapter, "_execute", lost_ack)
    operation = case.submit(review, "maintenance-lost-ack")
    assert operation["status"] == "RECOVERY_REQUIRED" and len(fired) == 1
    assert business_state(case) == before
    root = case.fixture.root
    ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    if fault == "missing_ledger":
        ledger.unlink()
    elif fault == "unknown_effect":
        with sqlite3.connect(case.database) as db:
            db.execute("DELETE FROM adapter_effects WHERE call_id=? AND hook='quiesce'", (fired[0],))
    after_fault_data = tree(root / "data")
    archives = set(case.fixture.base.rglob("*.hsac"))
    reopened = case.new_api()
    if fault == "lost_ack":
        resumed = reopened.resume(operation["operation_id"], background=False)
        assert resumed["operation_id"] == operation["operation_id"] and resumed["status"] == "SUCCEEDED"
        assert business_state(case) == before
        assert [r[0] for r in effect_rows(case.database, "quiesce")].count(fired[0]) == 1
        assert len(fired) == 1
        assert_safety_oracle(case, review, resumed, before)
    else:
        def status_only(adapter, hook, *args, **kwargs):
            if fault == "unknown_effect" and hook == "operation_status":
                return original(adapter, hook, *args, **kwargs)
            pytest.fail("ambiguous or missing authoritative ledger must not repeat a mutating hook")

        monkeypatch.setattr(ExecutableProductAdapter, "_execute", status_only)
        assert_refused(lambda: reopened.resume(operation["operation_id"], background=False),
            {"RECOVERY_REQUIRED", "INTEGRITY_FAILED"})
        assert tree(root / "data") == after_fault_data
        assert set(case.fixture.base.rglob("*.hsac")) == archives
        if fault == "missing_ledger":
            assert not ledger.exists()


@pytest.mark.parametrize("fault", ["resume", "missing_closure", "corrupt_closure"])
def test_powercut_after_actual_candidate_publish_uses_original_closure_or_refuses(case, fault, monkeypatch):
    root = case.fixture.root
    installed = root / "releases/app-release-2"
    (installed / "adapter.py").write_text("raise RuntimeError('synthetic old corrupt code')\n")
    review = maintenance_review(case, "repair", monkeypatch)
    before = business_state(case)
    actual = os.rename

    def resolved(path, directory_fd):
        value = Path(path)
        return value if value.is_absolute() or directory_fd is None else Path(os.readlink('/proc/self/fd/' + str(directory_fd))) / value

    def crash_after_publish(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        target = resolved(destination, dst_dir_fd)
        actual(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if target == installed:
            os._exit(89)

    def child():
        os.rename = crash_after_publish
        case.submit(review, "maintenance-publish-crash")

    child_effect(child)
    assert business_state(case) == before
    assert tree(installed) == tree(Path(case.second["options"]["payload_dir"]))
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    closure = root / ".hinstall/code-maintenance" / token / "candidate"
    assert (closure / "payload/adapter.py").is_file()
    if fault == "missing_closure":
        shutil.rmtree(closure)
    elif fault == "corrupt_closure":
        (closure / "payload/adapter.py").write_text("raise RuntimeError('synthetic changed closure')\n")
    data_after_fault = tree(root / "data")
    archives = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in case.fixture.base.rglob("*.hsac")}
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    if fault == "resume":
        resumed = reopened.resume(operation["operation_id"], background=False)
        assert resumed["operation_id"] == operation["operation_id"] and resumed["status"] == "SUCCEEDED"
        assert business_state(case) == before
        assert_safety_oracle(case, review, resumed, before)
    else:
        monkeypatch.setattr(ExecutableProductAdapter, "_execute",
            lambda *a, **k: pytest.fail("missing/corrupt original closure must not execute alternate payload"))
        assert_refused(lambda: reopened.resume(operation["operation_id"], background=False),
            {"RECOVERY_REQUIRED", "INTEGRITY_FAILED"})
        assert tree(root / "data") == data_after_fault
        if fault == "missing_closure":
            assert not closure.exists()
    assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in case.fixture.base.rglob("*.hsac")} == archives
