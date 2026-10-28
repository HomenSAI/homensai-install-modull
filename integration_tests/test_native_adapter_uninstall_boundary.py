"""Registered uninstall/recovery public contracts using real DEV signatures.

Linux source integration, synthetic data and an explicit host runtime; not
installed/frozen production acceptance. Baseline unsupported capability is an
unimplemented contract, not evidence of a destructive baseline defect.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from shared import archive_crypto
from test_native_adapter_recovery import RECOVERY_HOOK
from test_native_migration import migration


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree(root):
    return {p.relative_to(root).as_posix(): sha(p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


def effect_rows(database, hook):
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT call_id,request_digest,result FROM adapter_effects WHERE hook=? ORDER BY call_id",
                          (hook,)).fetchall()


@pytest.fixture
def case(migration, monkeypatch, tmp_path):
    import test_native_migration as fixture_module
    script = fixture_module.SCRIPT.replace("elif hook=='quiesce':", RECOVERY_HOOK + "\nelif hook=='quiesce':")
    script = script.replace(" result={'quiesced':True,'active_writers':0}", r'''
 result={'quiesced':True,'active_writers':0}
 with sqlite3.connect(database) as db:
  old=db.execute('SELECT request_digest,result FROM adapter_effects WHERE call_id=?',(request['idempotency_key'],)).fetchone()
  if old:
   assert old[0]==request['request_digest'];result=json.loads(old[1])
  else:
   db.execute('INSERT INTO adapter_effects VALUES(?,?,?,?)',(request['idempotency_key'],request['request_digest'],hook,json.dumps(result)))
''')
    monkeypatch.setattr(fixture_module, "SCRIPT", script)
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"schema_version": 1, "keys": {
        "synthetic": base64.b64encode(migration.public_key).decode()}}))

    def new_api():
        return InstallerAPI.from_bootstrap(tmp_path / "public-state", trust_config=trust,
            allow_development=True, development_runtime=sys.executable,
            product_adapter_descriptor="installer/adapter.json",
            secret_provider=migration.engine.secret_provider)

    api = new_api()

    def plan(action, bundle, **extra):
        return api.plan(action, bundle["manifest"], migration.target,
                        options={**bundle["options"], **extra})

    def submit(review, key):
        return api.submit(review["plan_id"], review["plan_digest"],
            [review["review_consent_ref"], *review["required_consents"]],
            idempotency_key=key, background=False)

    def run(action, bundle, key, **extra):
        result = submit(plan(action, bundle, **extra), key)
        assert result["status"] == "SUCCEEDED", result.get("error")
        return result

    first = migration.release("release-1", "schema_1", config=True)
    review = api.prepare_migration("install", first["manifest"], migration.target, options=first["options"])
    run("install", first, "setup-install", migration_review_id=review["migration_review_id"])
    migration.current = first
    document = migration.root / "data/document.txt"
    credential = migration.root / "data/synthetic-credential.txt"
    credential.write_text("synthetic snapshot credential"); credential.chmod(0o600)
    document.write_text("snapshot document")
    database = migration.root / "data/schema.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE user_state(value TEXT)")
        db.execute("INSERT INTO user_state VALUES (?)", ("snapshot user state",))
    archive = run("backup", first, "setup-snapshot")["result"]["archive"]
    second = migration.release("release-2", "schema_2", config=True)
    review = api.prepare_migration("update", second["manifest"], migration.target, options=second["options"])
    run("update", second, "setup-update", migration_review_id=review["migration_review_id"])
    migration.current = second
    document.write_text("latest document after snapshot")
    credential.write_text("synthetic latest credential")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE user_state SET value=?", ("latest user state",))
    yield SimpleNamespace(fixture=migration, api=api, new_api=new_api, plan=plan, submit=submit, run=run,
        first=first, second=second, archive=archive, document=document, credential=credential, database=database)


def uninstall(case, key="uninstall"):
    review = case.plan("uninstall", case.second)
    operation = case.submit(review, key)
    assert operation["status"] == "SUCCEEDED", operation.get("error")
    return review, operation


def restore_plan(case):
    return case.plan("restore", case.second, archive_path=case.archive["path"])


def restore_source_closure(case, review):
    root = case.fixture.root
    work = Path(case.second["options"].get("work_dir", str(root.parent / ("." + root.name + "-recovery"))))
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    return work / (token + "-retained-adapter-source")


def assert_refused(fn, codes):
    try:
        result = fn()
    except InstallerError as error:
        assert error.code in codes
    else:
        assert result["status"] == "RECOVERY_REQUIRED", result
        assert result["error"]["code"] in codes


def test_registered_uninstall_then_restore_uses_retained_source_without_incoming_payload(case, monkeypatch):
    root = case.fixture.root
    invoked = []
    actual = ExecutableProductAdapter._execute

    def observed(adapter, hook, request, request_digest):
        invoked.append((hook, adapter.options["release"], Path(adapter.options["payload_dir"])))
        return actual(adapter, hook, request, request_digest)

    monkeypatch.setattr(ExecutableProductAdapter, "_execute", observed)
    with monkeypatch.context() as no_hooks:
        no_hooks.setattr(ExecutableProductAdapter, "invoke", lambda *a, **k: pytest.fail("plan must not run hooks"))
        review = case.plan("uninstall", case.second)
    removed = case.submit(review, "uninstall")
    assert removed["status"] == "SUCCEEDED", removed.get("error")
    receipt = removed["result"]["retained_recovery"]
    assert receipt["source_adapter_binding"] == review["engine_plan"]["product_adapter_binding"]
    assert receipt["source_adapter_binding"]["release"] == "release-2"
    witness = receipt["quiesce_witness"]
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    key = "quiesce_" + token
    descriptor = json.loads((root / "runtime/recovery/release-2/payload/installer/adapter.json").read_text())
    expected_digest = digest({"hook": "quiesce", "operation_id": "op_" + token,
        "data": {"installation_id": receipt["installation_id"], "release": "release-2", "data_home": str(root / "data")},
        "descriptor_digest": digest(descriptor), "release": "release-2"})
    assert witness["request_digest"] == expected_digest
    assert witness["idempotency_key"] == key and witness["operation_id"] == "op_" + token
    assert witness["call_id"] == digest({"product_id": case.second["manifest"]["product_id"],
        "adapter_id": descriptor["adapter_id"], "key": key})
    assert witness["result"] == {"quiesced": True, "active_writers": 0}
    assert receipt["receipt_id"] == digest({k: v for k, v in receipt.items() if k != "receipt_id"})
    ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    info = ledger.stat()
    assert witness["ledger_identity"] == [info.st_dev, info.st_ino, info.st_uid]
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT request_digest,operation_id,hook,state,result FROM calls WHERE call_id=?",
                         (witness["call_id"],)).fetchone()
    assert row[:4] == (expected_digest, "op_" + token, "quiesce", "SUCCEEDED")
    assert json.loads(row[4]) == witness["result"]
    assert not any((root / "releases").iterdir())
    assert case.document.read_text() == "latest document after snapshot"
    assert len(effect_rows(case.database, "migration_apply")) == 2
    for bundle in (case.first, case.second):
        shutil.rmtree(bundle["options"]["payload_dir"])
    invoked.clear()
    with monkeypatch.context() as no_hooks:
        no_hooks.setattr(ExecutableProductAdapter, "invoke", lambda *a, **k: pytest.fail("plan must not run hooks"))
        planned = restore_plan(case)
    operation = case.submit(planned, "restore-after-uninstall")
    assert operation["status"] == "SUCCEEDED", operation.get("error")
    case.fixture.current = case.first
    result = operation["result"]
    assert result["health"]["release"] == "release-1" and result["health"]["product_adapter"]["ready"]
    assert result["external_actions_on_hold"] is True
    assert case.document.read_text() == "snapshot document"
    assert case.credential.read_text() == "synthetic snapshot credential"
    assert stat.S_IMODE(case.credential.stat().st_mode) == 0o600
    with sqlite3.connect(case.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT value FROM user_state").fetchall() == [("snapshot user state",)]
    assert len(effect_rows(case.database, "migration_apply")) == 1
    assert len(effect_rows(case.database, "restore")) == len(effect_rows(case.database, "post_restore")) == 1
    source_calls = [(hook, payload) for hook, release, payload in invoked if release == "release-2"]
    closure = restore_source_closure(case, planned)
    assert source_calls and all(payload == closure / "payload" for _, payload in source_calls)
    assert any(hook == "quiesce" for hook, _ in source_calls)
    assert not (root / "runtime/recovery/release-2").exists()
    safety = result["before_recovery_archive"]
    oracle = case.fixture.base / "before-restore-oracle"; oracle.mkdir(mode=0o700)
    archive_crypto.restore_archive(safety["path"], oracle,
        password_provider=lambda: "synthetic-archive-passphrase", expected_product_id=case.second["manifest"]["product_id"])
    assert (oracle / "roots/data/document.txt").read_text() == "latest document after snapshot"
    assert (oracle / "roots/data/synthetic-credential.txt").read_text() == "synthetic latest credential"
    assert safety["sha256"] == sha(Path(safety["path"]).read_bytes())
    assert result["before_recovery"]["archive_sha256"] == safety["sha256"]
    assert result["before_recovery"]["from_release"] == "release-2"
    assert case.new_api().status(operation["operation_id"])["result"] == result
    capabilities = case.api.capabilities(case.fixture.target)
    for action in ("repair", "reinstall"):
        assert capabilities["actions"][action]["supported"] is False


@pytest.mark.parametrize("fault", ["receipt", "retained_runtime", "retained_marker"])
def test_uninstalled_retained_source_tamper_refuses_without_hooks_data_or_archive_effects(case, monkeypatch, fault):
    review, _ = uninstall(case)
    root = case.fixture.root
    if fault == "receipt":
        token = digest([review["plan_id"], review["plan_digest"]])[:32]
        path = root / ".hinstall/uninstall-receipts" / (token + ".json")
        record = json.loads(path.read_text()); record["receipt_id"] = "0" * 64
        path.write_text(json.dumps(record))
    elif fault == "retained_runtime":
        path = root / "runtime/recovery/release-2/payload/runtime/python"
        path.write_bytes(path.read_bytes() + b"\nsynthetic retained runtime corruption\n")
    else:
        path = root / "runtime/recovery/release-2/installation.json"
        record = json.loads(path.read_text()); record["product_id"] = "other-synthetic-product"
        path.write_text(json.dumps(record))
    before = {name: tree(root / name) for name in ("data", "config", "backup")}
    archives = set(case.fixture.base.rglob("*.hsac"))
    monkeypatch.setattr(ExecutableProductAdapter, "_execute", lambda *a, **k: pytest.fail("unverified retained code executed"))
    assert_refused(lambda: case.submit(restore_plan(case), "tampered-retained-restore"),
                   {"INTEGRITY_FAILED", "RECOVERY_REQUIRED", "OPERATION_CONFLICT"})
    assert {name: tree(root / name) for name in before} == before
    assert set(case.fixture.base.rglob("*.hsac")) == archives
    assert not any((root / "releases").iterdir())


@pytest.mark.parametrize("fault", [None, "missing_ledger", "missing_invocation", "unknown_product_effect"])
def test_uninstall_quiesce_lost_ack_only_original_authoritative_reconciliation_can_continue(case, monkeypatch, fault):
    review = case.plan("uninstall", case.second)
    before_code = tree(case.fixture.root / "releases")
    actual = ExecutableProductAdapter._execute
    calls = []

    def lose_ack(adapter, hook, request, request_digest):
        result = actual(adapter, hook, request, request_digest)
        if hook == "quiesce" and adapter.options["release"] == "release-2":
            calls.append(request_digest)
            if len(calls) == 1:
                raise InstallerError("ADAPTER_TIMEOUT")
        return result

    monkeypatch.setattr(ExecutableProductAdapter, "_execute", lose_ack)
    operation = case.submit(review, "uninstall-lost-quiesce-ack")
    assert operation["status"] == "RECOVERY_REQUIRED"
    assert len(calls) == 1 and tree(case.fixture.root / "releases") == before_code
    assert len([row for row in effect_rows(case.database, "quiesce") if row[1] == calls[0]]) == 1
    ledger = case.fixture.root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    if fault == "missing_ledger":
        ledger.unlink()
    elif fault == "missing_invocation":
        with sqlite3.connect(ledger) as db:
            db.execute("DELETE FROM calls WHERE hook='quiesce' AND request_digest=?", (calls[0],))
    elif fault == "unknown_product_effect":
        with sqlite3.connect(case.database) as db:
            db.execute("DELETE FROM adapter_effects WHERE hook='quiesce' AND request_digest=?", (calls[0],))
    before_data = tree(case.fixture.root / "data")
    reopened = case.new_api()
    if fault:
        assert_refused(lambda: reopened.resume(operation["operation_id"], background=False), {"RECOVERY_REQUIRED"})
        assert tree(case.fixture.root / "releases") == before_code
        assert tree(case.fixture.root / "data") == before_data
        assert len(calls) == 1
        if fault == "missing_ledger":
            assert not ledger.exists()
        elif fault == "missing_invocation":
            with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
                assert db.execute("SELECT count(*) FROM calls WHERE request_digest=?", (calls[0],)).fetchone() == (0,)
    else:
        resumed = reopened.resume(operation["operation_id"], background=False)
        assert resumed["operation_id"] == operation["operation_id"] and resumed["status"] == "SUCCEEDED"
        assert not tree(case.fixture.root / "releases")
        assert case.document.read_text() == "latest document after snapshot"
        assert len(calls) == 1
        assert resumed["result"]["retained_recovery"]["quiesce_witness"]["request_digest"] == calls[0]


def test_corrupt_installed_adapter_never_executes_a_hook(case, monkeypatch):
    review = case.plan("uninstall", case.second)
    adapter_file = case.fixture.root / "releases/app-release-2/adapter.py"
    adapter_file.write_bytes(adapter_file.read_bytes() + b"\nraise AssertionError('corrupt payload executed')\n")
    before_code = tree(case.fixture.root / "releases")
    before_data = tree(case.fixture.root / "data")
    monkeypatch.setattr(ExecutableProductAdapter, "_execute", lambda *a, **k: pytest.fail("corrupt adapter executed"))
    assert_refused(lambda: case.submit(review, "uninstall-corrupt-installed"), {"PLAN_STALE", "INTEGRITY_FAILED"})
    assert tree(case.fixture.root / "releases") == before_code
    assert tree(case.fixture.root / "data") == before_data


def test_new_restore_cannot_recreate_authoritative_ledger_lost_between_proof_and_fresh_quiesce(case, monkeypatch):
    _, removed = uninstall(case)
    old_operation = removed["result"]["retained_recovery"]["quiesce_witness"]["operation_id"]
    review = restore_plan(case)
    root = case.fixture.root
    ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    before_data = tree(root / "data")
    archives = set(case.fixture.base.rglob("*.hsac"))
    original_invoke = ExecutableProductAdapter.invoke
    original_execute = ExecutableProductAdapter._execute
    injected = []

    def lose_ledger(adapter, hook, request, **kwargs):
        if hook == "quiesce" and adapter.options["release"] == "release-2" and request["operation_id"] != old_operation:
            assert not injected and adapter.state_dir == ledger.parent
            ledger.unlink()
            injected.append(True)
        return original_invoke(adapter, hook, request, **kwargs)

    def observe(adapter, hook, *args, **kwargs):
        if hook == "quiesce" and adapter.options["release"] == "release-2":
            pytest.fail("Fresh quiesce executed after its authoritative ledger disappeared")
        return original_execute(adapter, hook, *args, **kwargs)

    monkeypatch.setattr(ExecutableProductAdapter, "invoke", lose_ledger)
    monkeypatch.setattr(ExecutableProductAdapter, "_execute", observe)
    operation = case.submit(review, "restore-missing-ledger-before-fresh-invoke")
    assert injected and operation["status"] == "RECOVERY_REQUIRED"
    assert operation["error"]["code"] == "RECOVERY_REQUIRED"
    assert not ledger.exists()
    assert tree(root / "data") == before_data
    assert set(case.fixture.base.rglob("*.hsac")) == archives
    assert not tree(root / "releases")


def child_effect(callback):
    pid = os.fork()
    if pid == 0:
        try:
            callback()
        except BaseException:
            os._exit(86)
        os._exit(87)
    deadline = time.monotonic() + 30
    while True:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
            pytest.fail("Synthetic child did not reach the selected actual effect")
        time.sleep(0.02)
    assert os.waitstatus_to_exitcode(status) == 89


def interrupted_operation(api, review):
    operations = api.operations_list()["operations"]
    matches = [item for item in operations if item["plan_id"] == review["plan_id"]]
    assert len(matches) == 1
    operation = api.status(matches[0]["operation_id"])
    assert operation["status"] == "RECOVERY_REQUIRED"
    return operation


def test_partial_unlink_original_resume_uses_retained_adapter_and_same_ledger(case):
    root = case.fixture.root
    review = case.plan("uninstall", case.second)
    victim = root / "releases/app-release-2/adapter.py"
    ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    ledger_identity = ledger.stat().st_dev, ledger.stat().st_ino
    before = tree(root / "releases")

    def cut():
        actual = os.unlink
        def unlink(path, *, dir_fd=None):
            candidate = Path(os.fsdecode(path))
            if dir_fd is not None:
                candidate = Path(os.readlink("/proc/self/fd/" + str(dir_fd))) / candidate
            actual(path, dir_fd=dir_fd)
            if candidate == victim:
                os._exit(89)
        os.unlink = unlink
        case.submit(review, "partial-unlink")

    child_effect(cut)
    assert not victim.exists() and tree(root / "releases") != before
    quiesces = effect_rows(case.database, "quiesce")
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    remaining = tree(root / "releases")
    assert_refused(lambda: reopened.plan("uninstall", case.second["manifest"], case.fixture.target,
        options=case.second["options"]), {"RECOVERY_REQUIRED"})
    assert tree(root / "releases") == remaining
    resumed = reopened.resume(operation["operation_id"], background=False)
    assert resumed["operation_id"] == operation["operation_id"] and resumed["status"] == "SUCCEEDED"
    assert not tree(root / "releases") and case.document.read_text() == "latest document after snapshot"
    assert effect_rows(case.database, "quiesce") == quiesces
    assert (ledger.stat().st_dev, ledger.stat().st_ino) == ledger_identity


def cut_restore(case, phase):
    from homensai_install.engine import NativeEngine
    review = restore_plan(case)
    def cut():
        original = NativeEngine.execute
        def execute(engine, plan, emit=lambda event: None, canceled=lambda: False, **kwargs):
            def event(value):
                emit(value)
                if plan["action"] == "restore" and value.get("phase") == phase and value.get("status") == "SUCCEEDED":
                    os._exit(89)
            return original(engine, plan, event, canceled, **kwargs)
        NativeEngine.execute = execute
        case.submit(review, "restore-power-cut")
    child_effect(cut)
    return review


@pytest.mark.parametrize("phase", ["restore", "activate"])
def test_original_restore_resume_retains_v2_source_after_v1_publish_and_activation(case, phase):
    uninstall(case)
    review = cut_restore(case, phase)
    root = case.fixture.root
    assert not (root / "runtime/recovery/release-2").exists()
    assert restore_source_closure(case, review).is_dir()
    before_document = case.document.read_bytes()
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    resumed = reopened.resume(operation["operation_id"], background=False)
    assert resumed["operation_id"] == operation["operation_id"] and resumed["status"] == "SUCCEEDED"
    case.fixture.current = case.first
    assert case.document.read_bytes() == before_document == b"snapshot document"
    assert len(effect_rows(case.database, "migration_apply")) == 1
    assert len(effect_rows(case.database, "restore")) == len(effect_rows(case.database, "post_restore")) == 1


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_original_restore_missing_or_corrupt_closure_is_not_rebuilt_from_other_source(case, fault, monkeypatch):
    uninstall(case)
    review, operation = lost_target_ack(case, monkeypatch, "restore")
    root = case.fixture.root
    closure = restore_source_closure(case, review)
    assert closure.is_dir() and Path(case.second["options"]["payload_dir"]).is_dir()
    if fault == "missing":
        shutil.rmtree(closure)
    else:
        executable = closure / "payload/runtime/python"
        executable.write_bytes(executable.read_bytes() + b"synthetic closure corruption")
    before = {name: tree(root / name) for name in ("data", "config", "backup", "runtime")}
    archives = set(case.fixture.base.rglob("*.hsac"))
    monkeypatch.setattr(ExecutableProductAdapter, "_execute", lambda *a, **k: pytest.fail("hook after lost closure"))
    reopened = case.new_api()
    assert_refused(lambda: reopened.resume(operation["operation_id"], background=False),
                   {"RECOVERY_REQUIRED", "INTEGRITY_FAILED"})
    assert {name: tree(root / name) for name in before} == before
    assert set(case.fixture.base.rglob("*.hsac")) == archives
    if fault == "missing":
        assert not closure.exists()


def lost_target_ack(case, monkeypatch, selected):
    review = restore_plan(case)
    actual = ExecutableProductAdapter._execute
    fired = []
    def execute(adapter, hook, request, request_digest):
        result = actual(adapter, hook, request, request_digest)
        if hook == selected and adapter.options["release"] == "release-1" and not fired:
            fired.append(request_digest)
            raise InstallerError("ADAPTER_TIMEOUT")
        return result
    monkeypatch.setattr(ExecutableProductAdapter, "_execute", execute)
    operation = case.submit(review, "restore-target-lost-ack")
    assert fired and operation["status"] == "RECOVERY_REQUIRED"
    assert not (case.fixture.root / "runtime/recovery/release-2").exists()
    assert restore_source_closure(case, review).is_dir()
    assert len(effect_rows(case.database, selected)) == 1
    return review, operation


@pytest.mark.parametrize("hook", ["restore", "post_restore"])
def test_target_lost_ack_after_uninstall_uses_original_closure_and_never_replaces_data_again(case, monkeypatch, hook):
    uninstall(case)
    ledger = case.fixture.root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    identity = ledger.stat().st_dev, ledger.stat().st_ino
    review, operation = lost_target_ack(case, monkeypatch, hook)
    closure = tree(restore_source_closure(case, review))
    case.document.write_text("new user data after committed target hook")
    archives = {p: sha(p.read_bytes()) for p in case.fixture.base.rglob("*.hsac")}
    reopened = case.new_api()
    resumed = reopened.resume(operation["operation_id"], background=False)
    assert resumed["operation_id"] == operation["operation_id"] and resumed["status"] == "SUCCEEDED"
    case.fixture.current = case.first
    assert case.document.read_text() == "new user data after committed target hook"
    assert len(effect_rows(case.database, "restore")) == len(effect_rows(case.database, "post_restore")) == 1
    assert len(effect_rows(case.database, "migration_apply")) == 1
    assert tree(restore_source_closure(case, review)) == closure
    assert (ledger.stat().st_dev, ledger.stat().st_ino) == identity
    assert {p: sha(p.read_bytes()) for p in case.fixture.base.rglob("*.hsac")} == archives
