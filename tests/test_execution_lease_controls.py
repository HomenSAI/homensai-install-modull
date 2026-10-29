"""Независимые control-path regressions; synthetic engine, без product effects."""

import json
from pathlib import Path
import sqlite3
import threading

import pytest

from homensai_install.api.v1 import InstallerAPI
from test_public_api import Engine, submit
from test_prerequisites_execution import bundle, coordinator


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("entrypoint", ["submit", "resume"])
def test_synchronous_product_status_and_cancel_do_not_wait_for_engine(tmp_path, entrypoint):
    """The worker is gated; control calls must finish before its gate opens."""
    engine = Engine(tmp_path)
    api = InstallerAPI(tmp_path / "api-state", engine=engine)
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text())
    plan = api.plan("install", manifest, options={"install_root": str(tmp_path / "install")})
    if entrypoint == "resume":
        engine.execute = lambda *_: {"status": "RECOVERY_REQUIRED"}
        operation_id = submit(api, plan, background=False)["operation_id"]
    entered, release = threading.Event(), threading.Event()
    status_done, cancel_done = threading.Event(), threading.Event()
    outcomes, failures = {}, []

    def gated_engine(plan, emit, canceled):
        entered.set()
        if not release.wait(8):
            raise AssertionError("The test did not release its own synthetic engine")
        return {"status": "CANCELED" if canceled() else "SUCCEEDED"}

    engine.execute = gated_engine
    engine.resume = lambda plan, previous, emit, canceled: gated_engine(plan, emit, canceled)

    def call(name, function, done=None):
        try:
            outcomes[name] = function()
        except BaseException as error:
            failures.append((name, repr(error)))
        finally:
            if done is not None:
                done.set()

    worker = threading.Thread(target=call, args=("worker", (
        (lambda: submit(api, plan, background=False)) if entrypoint == "submit"
        else (lambda: api.resume(operation_id, background=False)))))
    controls = []
    worker.start()
    try:
        assert entered.wait(3), "Synthetic engine did not enter its bounded gate"
        operation_id = api._journal().operations_list(None, unfinished_only=True, limit=10)[0]["operation_id"]
        for name, function, done in (
            ("status", lambda: api.status(operation_id), status_done),
            ("cancel", lambda: api.cancel(operation_id), cancel_done),
        ):
            thread = threading.Thread(target=call, args=(name, function, done))
            controls.append(thread)
            thread.start()
        responsive = status_done.wait(1.5) and cancel_done.wait(1.5)
        gate_was_closed = not release.is_set()
    finally:
        release.set()
        worker.join(5)
        for thread in controls:
            thread.join(5)
    assert not worker.is_alive() and all(not thread.is_alive() for thread in controls)
    assert not failures
    assert responsive and gate_was_closed, "status/cancel waited for synchronous installer completion"
    assert outcomes["cancel"]["cancel_requested"] is True
    assert outcomes["worker"]["status"] == "CANCELED"
    assert not (tmp_path / "executed").exists()


def test_product_revalidates_inventory_under_domain_before_publication(bundle, monkeypatch):
    """A dependency finishes in the gap after product discovery, before reserve."""
    from homensai_install.execution_lease import ExecutionDomain
    from homensai_install.errors import InstallerError

    root = bundle["tmp_path"]
    domain = ExecutionDomain(root / "shared-domain", owner_id="original-user-1000")
    port = coordinator(bundle, execution_domain=domain)
    engine = Engine(root)
    api = InstallerAPI(root / "product-state", engine=engine, prerequisites=port)
    plan = api.plan("install", bundle["manifest"], bundle["target"],
                    options={"install_root": str(root / "install")})
    dependency_plan = port.plan({"base": "auto", "client": "auto"})
    before_reserve, release_reserve = threading.Event(), threading.Event()
    original_reserve = domain.reserve
    failures, outputs = [], []

    def reserve(kind, *args, **kwargs):
        if kind == "product" and not before_reserve.is_set():
            before_reserve.set()
            assert release_reserve.wait(5)
        return original_reserve(kind, *args, **kwargs)

    monkeypatch.setattr(domain, "reserve", reserve)

    def caller():
        try:
            outputs.append(submit(api, plan, background=False))
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=caller)
    worker.start()
    try:
        assert before_reserve.wait(3)
        state = port.start("dependency-changed-environment", dependency_plan,
                           consents=dependency_plan["required_consents"])
        assert state["status"] == "READY"
        engine.fact = "dependency-changed-environment"
    finally:
        release_reserve.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not outputs, "Product executed a plan validated before the intervening dependency effect"
    assert len(failures) == 1 and isinstance(failures[0], InstallerError)
    assert failures[0].code == "PLAN_STALE"
    assert engine.calls == 0 and not (root / "executed").exists()
    assert api.operations_list(unfinished_only=False)["operations"] == []
    assert domain.busy() is False
    fresh = api.plan("install", bundle["manifest"], bundle["target"],
                     options={"install_root": str(root / "install")})
    assert submit(api, fresh, key="new-review", background=False)["status"] == "SUCCEEDED"
    assert engine.calls == 1 and domain.busy() is False


def test_failure_to_commit_queued_lease_prevents_effect_and_resumes_same_operation(tmp_path):
    from homensai_install.execution_lease import ExecutionDomain
    from homensai_install.errors import InstallerError

    domain = ExecutionDomain(tmp_path / "domain", owner_id="synthetic-owner")
    primer = domain.reserve("product", "schema-only", "a" * 64)
    primer.close(release=True)
    with sqlite3.connect(domain.path) as db:
        db.execute("CREATE TRIGGER fail_queue BEFORE UPDATE ON claim "
                   "WHEN json_extract(NEW.body,'$.phase')='QUEUED' "
                   "BEGIN SELECT RAISE(ABORT,'synthetic queue failure'); END")
    engine = Engine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=engine, execution_domain=domain)
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text())
    plan = api.plan("install", manifest, options={"install_root": str(tmp_path / "install")})
    with pytest.raises(InstallerError) as caught:
        submit(api, plan, background=False)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert engine.calls == 0 and not (tmp_path / "executed").exists()
    assert domain.busy() is True
    operation = api.operations_list(unfinished_only=False)["operations"][0]
    duplicate = submit(api, plan, background=False)
    assert duplicate["operation_id"] == operation["operation_id"] and engine.calls == 0
    with sqlite3.connect(domain.path) as db:
        db.execute("DROP TRIGGER fail_queue")
    restored = api.resume(operation["operation_id"], background=False)
    assert restored["status"] == "SUCCEEDED" and engine.calls == 1
    assert restored["operation_id"] == operation["operation_id"] and domain.busy() is False


def test_directory_fsync_failure_keeps_reserved_identity_before_any_effect(tmp_path, monkeypatch):
    from homensai_install import execution_lease
    from homensai_install.errors import InstallerError

    domain = execution_lease.ExecutionDomain(tmp_path / "domain", owner_id="synthetic-owner")
    engine = Engine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=engine, execution_domain=domain)
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text())
    plan = api.plan("install", manifest, options={"install_root": str(tmp_path / "install")})
    original_sync = execution_lease._sync_directory

    def sync(path):
        if path == domain.directory:
            raise OSError("synthetic directory fsync failure after reservation")
        return original_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(execution_lease, "_sync_directory", sync)
        with pytest.raises(InstallerError) as caught:
            submit(api, plan, background=False)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert engine.calls == 0 and not (tmp_path / "executed").exists()
    assert api.operations_list(unfinished_only=False)["operations"] == []
    assert domain.busy() is True
    with sqlite3.connect(domain.path) as db:
        claim = json.loads(db.execute("SELECT body FROM claim WHERE id=1").fetchone()[0])
    assert claim["phase"] == "RESERVED"
    restored = submit(api, plan, background=False)
    assert restored["operation_id"] == claim["operation_id"]
    assert restored["status"] == "SUCCEEDED" and engine.calls == 1 and domain.busy() is False
