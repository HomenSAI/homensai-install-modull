"""Independent public recovery boundaries with real signed Linux DEV fixtures.

Whole-root continuation is a new bounded contract. The public 855 baseline
deliberately refuses it; that refusal is not an unsafe overwrite defect. No
fake verifier/authorizer or product success replaces an actual effect here.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import time

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI
from homensai_install.engine import EngineError, NativeEngine
import homensai_install.engine as native_module
from homensai_install.errors import InstallerError
from shared.adapters_fs import FilesystemPorts
from test_native_adapter_uninstall_boundary import case, effect_rows, interrupted_operation
from test_native_migration import migration


ROOT_NAMES = ("data", "backup", "services", "config", "runtime")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def tree_state(endpoint):
    entries = {}
    pending = [endpoint]
    while pending:
        path = pending.pop()
        relative = path.relative_to(endpoint).as_posix()
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries[relative] = None
            continue
        item = {"identity": identity(info)}
        if stat.S_ISLNK(info.st_mode):
            item["link"] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            item["sha256"] = sha(path)
        elif stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        entries[relative] = item
    return entries


def managed_state(root):
    """Read-only oracle includes missing roots, empty dirs and inode/ctime drift."""
    return {"installation_root": identity(root.lstat()),
            **{name: tree_state(root / name) for name in ROOT_NAMES}}


def refuse_original_resume(api, operation):
    try:
        result = api.resume(operation["operation_id"], background=False)
    except InstallerError as error:
        assert error.code == "RECOVERY_REQUIRED"
    else:
        assert result["status"] == "RECOVERY_REQUIRED", result
        assert result["error"]["code"] == "RECOVERY_REQUIRED"


def review_restore(case):
    return case.plan("restore", case.second, archive_path=case.archive["path"])


def killed_child(callback):
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
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("Child did not reach the selected real filesystem effect")
        time.sleep(0.02)
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL


def kill_at_data_boundary(case, review, *, inside_root=False):
    data = case.fixture.root / "data"
    actual_reconcile = FilesystemPorts._reconcile
    actual_replace = os.replace

    def whole_root(source, destination, preserved):
        actual_reconcile(source, destination, preserved)
        if Path(destination) == data and Path(source).parent.name == "roots":
            os.kill(os.getpid(), signal.SIGKILL)

    def mid_root(source, destination, *args, **kwargs):
        actual_replace(source, destination, *args, **kwargs)
        # The real first document replacement precedes the still-old schema
        # database and credential, so this is demonstrably mixed, not no-op.
        if Path(destination) == data / "document.txt":
            os.kill(os.getpid(), signal.SIGKILL)

    def child():
        if inside_root:
            os.replace = mid_root
        else:
            FilesystemPorts._reconcile = staticmethod(whole_root)
        case.submit(review, "root-boundary-crash")

    killed_child(child)
    assert case.document.read_text() == "snapshot document"
    if inside_root:
        assert case.credential.read_text() == "synthetic latest credential"
    else:
        assert case.credential.read_text() == "synthetic snapshot credential"


def kill_at_durable_root_handoff(case, review, cut):
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    journal = case.fixture.root / ".hinstall/operations" / (token + ".json")
    actual_write = native_module._write

    def child():
        def after_durable_journal_write(path, value, *args, **kwargs):
            actual_write(path, value, *args, **kwargs)
            if Path(path) != journal:
                return
            if cut == "all_roots" and value.get("recovery_roots_complete") and not value.get("recovery_target_stage_started"):
                os.kill(os.getpid(), signal.SIGKILL)
            if cut == "stage_complete" and value.get("recovery_target_stage_complete") and "restore" not in value.get("completed", []):
                os.kill(os.getpid(), signal.SIGKILL)

        native_module._write = after_durable_journal_write
        case.submit(review, "root-handoff-crash")

    killed_child(child)
    assert case.document.read_text() == "snapshot document"


@pytest.mark.parametrize("cut", ["data_effect", "all_roots", "stage_complete"])
def test_original_public_resume_accepts_whole_root_or_durable_handoff_without_recopy(case, monkeypatch, cut):
    root = case.fixture.root
    review = review_restore(case)
    trace = case.fixture.base / "root-resume-hook-trace.jsonl"
    actual_execute = ExecutableProductAdapter._execute
    at_target_restore = {}
    resuming = [False]

    def observed(adapter, hook, request, request_digest):
        release = adapter.options["release"]
        if resuming[0] and release == "release-2":
            pytest.fail("Source hooks must not read or quiesce the partially restored DataHome")
        if resuming[0] and release == "release-1" and hook == "restore":
            assert {str(p.relative_to(root / "data")): (p.stat().st_dev, p.stat().st_ino)
                    for p in (root / "data").rglob("*") if p.is_file()} == at_target_restore
        with trace.open("a") as output:
            output.write(json.dumps({"hook": hook, "release": release,
                "idempotency_key": request["idempotency_key"]}) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return actual_execute(adapter, hook, request, request_digest)

    monkeypatch.setattr(ExecutableProductAdapter, "_execute", observed)
    if cut == "data_effect":
        kill_at_data_boundary(case, review)
    else:
        kill_at_durable_root_handoff(case, review, cut)
    at_target_restore.update({str(p.relative_to(root / "data")): (p.stat().st_dev, p.stat().st_ino)
        for p in (root / "data").rglob("*") if p.is_file()})
    source_ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    ledger_identity = source_ledger.stat().st_dev, source_ledger.stat().st_ino
    archives = {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")}
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    resuming[0] = True
    result = reopened.resume(operation["operation_id"], background=False)
    assert result["operation_id"] == operation["operation_id"] and result["status"] == "SUCCEEDED"
    case.fixture.current = case.first
    assert case.document.read_text() == "snapshot document"
    assert case.credential.read_text() == "synthetic snapshot credential"
    assert len(effect_rows(case.database, "restore")) == len(effect_rows(case.database, "post_restore")) == 1
    assert (source_ledger.stat().st_dev, source_ledger.stat().st_ino) == ledger_identity
    assert {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")} == archives
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert sum(item["hook"] == "quiesce" and item["release"] == "release-2" for item in calls) == 1
    assert not any(item["hook"] == "operation_status" and item["release"] == "release-2" for item in calls)


def test_published_stage_with_lost_completion_is_not_adopted_or_used_to_recopy_roots(case, monkeypatch):
    review = review_restore(case)
    actual_stage = FilesystemPorts.stage_release

    def child():
        def after_actual_stage_publish(ports, key, release):
            actual_stage(ports, key, release)
            os.kill(os.getpid(), signal.SIGKILL)

        FilesystemPorts.stage_release = after_actual_stage_publish
        case.submit(review, "stage-effect-crash")

    killed_child(child)
    api = case.new_api()
    operation = interrupted_operation(api, review)
    before = managed_state(case.fixture.root)
    releases = tree_state(case.fixture.root / "releases")
    archives = {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")}
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Unknown stage completion must refuse before product hooks"))
    refuse_original_resume(api, operation)
    assert managed_state(case.fixture.root) == before
    assert tree_state(case.fixture.root / "releases") == releases
    assert {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")} == archives


def test_real_midroot_crash_refuses_original_resume_without_root_writes_or_product_hooks(case, monkeypatch):
    review = review_restore(case)
    kill_at_data_boundary(case, review, inside_root=True)
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    before = managed_state(case.fixture.root)
    archives = {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")}
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Mixed-root refusal must precede all product hooks"))
    refuse_original_resume(reopened, operation)
    assert managed_state(case.fixture.root) == before
    assert {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")} == archives


@pytest.mark.parametrize("fault", ["missing_root", "root_mode"])
def test_pending_resume_refuses_root_drift_before_mkdir_or_chmod_can_hide_it(case, monkeypatch, fault):
    review = review_restore(case)
    kill_at_data_boundary(case, review)
    endpoint = case.fixture.root / "config"
    if fault == "missing_root":
        shutil.rmtree(endpoint)
    else:
        endpoint.chmod(0o755)
    reopened = case.new_api()
    operation = interrupted_operation(reopened, review)
    before = managed_state(case.fixture.root)
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Root identity/mode drift must fail before hooks"))
    refuse_original_resume(reopened, operation)
    assert managed_state(case.fixture.root) == before
    if fault == "missing_root":
        assert not endpoint.exists()
    else:
        assert stat.S_IMODE(endpoint.lstat().st_mode) == 0o755


def refused(call):
    try:
        result = call()
    except InstallerError as error:
        assert error.code == "RECOVERY_REQUIRED"
    else:
        assert result["status"] == "RECOVERY_REQUIRED", result
        assert result["error"]["code"] == "RECOVERY_REQUIRED"


def pending_with_lost_journal(case):
    review = review_restore(case)
    kill_at_data_boundary(case, review)
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    marker = case.fixture.root / ".hinstall/installation.json"
    pending = json.loads(marker.read_text())["pending_recovery"]
    assert pending["operation_id"] == "op_" + token and len(pending["receipt_id"]) == 64
    journal = case.fixture.root / ".hinstall/operations" / (token + ".json")
    assert journal.is_file()
    journal.unlink()
    return review, journal, marker


@pytest.mark.parametrize("caller", ["original_registered", "new_legacy_factory", "new_legacy_engine"])
def test_lost_native_journal_pending_fence_cannot_be_bypassed_by_constructor_or_new_token(case, monkeypatch, tmp_path, caller):
    review, journal, marker = pending_with_lost_journal(case)
    before = managed_state(case.fixture.root)
    marker_bytes = marker.read_bytes()
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Missing original journal must not authorize hooks"))
    options = {**case.second["options"], "archive_path": case.archive["path"]}
    if caller == "original_registered":
        api = case.new_api()
        operation = interrupted_operation(api, review)
        refuse_original_resume(api, operation)
    elif caller == "new_legacy_factory":
        api = InstallerAPI.from_bootstrap(tmp_path / "legacy-factory-state",
            trust_config=tmp_path / "trust.json", allow_development=True,
            development_runtime=sys.executable, secret_provider=case.fixture.engine.secret_provider)

        def new_public_operation():
            plan = api.plan("restore", case.second["manifest"], case.fixture.target, options=options)
            return api.submit(plan["plan_id"], plan["plan_digest"],
                [plan["review_consent_ref"], *plan["required_consents"]],
                idempotency_key="legacy-new-token", background=False)

        refused(new_public_operation)
    else:
        engine = NativeEngine(case.fixture.engine.verifier, case.fixture.engine.secret_provider,
                              development_runtime=sys.executable)

        def new_direct_operation():
            detail = engine.plan("restore", case.second["manifest"], case.fixture.target, {}, options)
            return engine.execute({"action": "restore", "manifest": case.second["manifest"],
                "target": case.fixture.target, "options": options, "engine_plan": detail,
                "plan_id": "independent-legacy-new-token", "plan_digest": digest(["new-token", review["plan_digest"]])})

        with pytest.raises(EngineError) as caught:
            new_direct_operation()
        assert caught.value.code == "RECOVERY_REQUIRED"
    assert not journal.exists(), "Missing original journal must never be reconstructed"
    assert marker.read_bytes() == marker_bytes
    assert managed_state(case.fixture.root) == before


def test_explicit_registered_stop_with_pending_lost_journal_has_no_product_hook_or_root_preparation(case, monkeypatch):
    review, journal, marker = pending_with_lost_journal(case)
    before = managed_state(case.fixture.root)
    marker_bytes = marker.read_bytes()
    proof = case.fixture.root / ".hinstall/recovery-root-receipts" / journal.name
    proof_before = proof.read_bytes(), identity(proof.lstat())
    marker_identity = identity(marker.lstat())
    api = case.new_api()
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Cancel and emergency owned stop must not quiesce mixed product data"))
    original = interrupted_operation(api, review)
    canceled = api.cancel(original["operation_id"])
    assert canceled["status"] == "CANCELED"
    assert canceled["result"]["resume_required"] is True and canceled["result"]["effects_undone"] is False
    assert managed_state(case.fixture.root) == before
    assert marker.read_bytes() == marker_bytes and not journal.exists()
    assert identity(marker.lstat()) == marker_identity
    assert (proof.read_bytes(), identity(proof.lstat())) == proof_before
    plan = api.plan("stop", case.second["manifest"], case.fixture.target, options=case.second["options"])
    result = api.submit(plan["plan_id"], plan["plan_digest"],
        [plan["review_consent_ref"], *plan["required_consents"]], idempotency_key="pending-owned-stop", background=False)
    assert result["status"] == "SUCCEEDED", result.get("error")
    assert managed_state(case.fixture.root) == before
    assert marker.read_bytes() == marker_bytes and not journal.exists()
    assert identity(marker.lstat()) == marker_identity
    assert (proof.read_bytes(), identity(proof.lstat())) == proof_before
    assert json.loads((case.fixture.root / ".hinstall/processes.json").read_text()) == {}
    # Cancellation released only the public claim. The original native
    # recovery still requires its missing original journal and cannot restart.
    refuse_original_resume(api, canceled)
    assert marker.read_bytes() == marker_bytes and not journal.exists()
    assert identity(marker.lstat()) == marker_identity
    assert (proof.read_bytes(), identity(proof.lstat())) == proof_before
    assert managed_state(case.fixture.root) == before


@pytest.mark.parametrize("fault", ["completed_file", "untouched_file", "empty_directory", "source_ledger"])
def test_original_resume_rechecks_all_roots_and_existing_authoritative_ledger_before_any_write(case, monkeypatch, fault):
    review = review_restore(case)
    kill_at_data_boundary(case, review)
    root = case.fixture.root
    source_ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    if fault == "completed_file":
        case.document.write_text("synthetic offline edit of the completed root")
    elif fault == "untouched_file":
        settings = root / "config/settings.json"
        assert settings.is_file()
        settings.write_text('{"synthetic_offline_edit":true}')
    elif fault == "empty_directory":
        (root / "data/synthetic-unreviewed-empty-directory").mkdir(mode=0o700)
    else:
        assert source_ledger.is_file()
        source_ledger.unlink()
    api = case.new_api()
    operation = interrupted_operation(api, review)
    before = managed_state(root)
    archives = {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")}
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Whole-root and ledger authority must be checked before product hooks"))
    refuse_original_resume(api, operation)
    assert managed_state(root) == before
    assert {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")} == archives
    if fault == "source_ledger":
        assert not source_ledger.exists(), "Missing authoritative ledger must not be recreated"


@pytest.mark.parametrize("fault", [None, "terminal_receipt", "root_receipt"])
def test_original_resume_after_real_marker_retirement_still_requires_terminal_authority(case, monkeypatch, fault):
    review = review_restore(case)
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    root = case.fixture.root
    owner_path = root / ".hinstall/installation.json"
    journal_path = root / ".hinstall/operations" / (token + ".json")
    actual_write = native_module._write

    def child():
        marker_seen = [False]

        def after_durable_owner_write(path, value, *args, **kwargs):
            actual_write(path, value, *args, **kwargs)
            if Path(path) == owner_path:
                if value.get("pending_recovery") is not None:
                    marker_seen[0] = True
                elif marker_seen[0]:
                    os.kill(os.getpid(), signal.SIGKILL)

        native_module._write = after_durable_owner_write
        case.submit(review, "terminal-retirement-crash")

    killed_child(child)
    case.fixture.current = case.first
    native = json.loads(journal_path.read_text())
    assert native["status"] == "SUCCEEDED"
    assert "pending_recovery" not in json.loads(owner_path.read_text())
    terminal_id = native["recovery_root_terminal_id"]
    assert len(terminal_id) == 64
    directory = root / ".hinstall/recovery-root-receipts"
    if fault == "terminal_receipt":
        (directory / (terminal_id + ".terminal.json")).unlink()
    elif fault == "root_receipt":
        (directory / (token + ".json")).unlink()
    api = case.new_api()
    operation = interrupted_operation(api, review)
    before = managed_state(root)
    owner_before = (owner_path.read_bytes(), identity(owner_path.lstat()))
    archives = {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")}
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Terminal resume must validate receipts without product hooks"))
    if fault is None:
        result = api.resume(operation["operation_id"], background=False)
        assert result["status"] == "SUCCEEDED" and result["operation_id"] == operation["operation_id"]
        assert len(effect_rows(case.database, "restore")) == len(effect_rows(case.database, "post_restore")) == 1
    else:
        refuse_original_resume(api, operation)
    assert managed_state(root) == before
    assert (owner_path.read_bytes(), identity(owner_path.lstat())) == owner_before
    assert {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")} == archives


@pytest.mark.parametrize("fault", ["missing_receipt", "receipt_body", "receipt_mode", "receipt_hardlink", "missing_candidate"])
def test_pending_root_proof_or_candidate_loss_is_never_recreated_or_accepted(case, monkeypatch, fault):
    review = review_restore(case)
    kill_at_data_boundary(case, review)
    token = digest([review["plan_id"], review["plan_digest"]])[:32]
    root = case.fixture.root
    proof = root / ".hinstall/recovery-root-receipts" / (token + ".json")
    assert proof.is_file()
    work = Path(case.second["options"].get("work_dir", str(root.parent / ("." + root.name + "-recovery"))))
    candidate = work / (token + "-restore-candidate")
    assert candidate.is_dir()
    if fault == "missing_receipt":
        proof.unlink()
    elif fault == "receipt_body":
        value = json.loads(proof.read_text())
        value["body"]["source_archive_sha256"] = "0" * 64
        proof.write_text(json.dumps(value))
    elif fault == "receipt_mode":
        proof.chmod(0o644)
    elif fault == "receipt_hardlink":
        os.link(proof, case.fixture.base / "synthetic-extra-root-proof-link.json")
    else:
        candidate.rename(work / (token + "-synthetic-preserved-candidate"))
    api = case.new_api()
    operation = interrupted_operation(api, review)
    before = managed_state(root)
    marker = root / ".hinstall/installation.json"
    marker_before = marker.read_bytes(), identity(marker.lstat())
    proof_before = tree_state(proof)
    archives = {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")}
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Unverified root proof/candidate must refuse before product hooks"))
    refuse_original_resume(api, operation)
    assert managed_state(root) == before
    assert (marker.read_bytes(), identity(marker.lstat())) == marker_before
    assert tree_state(proof) == proof_before
    assert {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")} == archives
    if fault == "missing_candidate":
        assert not candidate.exists(), "Missing immutable candidate must not be extracted again"


def test_missing_owner_marker_cannot_allow_new_legacy_operation_to_normalize_pending_roots(case, monkeypatch, tmp_path):
    review = review_restore(case)
    kill_at_data_boundary(case, review)
    root = case.fixture.root
    marker = root / ".hinstall/installation.json"
    value = json.loads(marker.read_text())
    assert value.pop("pending_recovery")["operation_id"].startswith("op_")
    marker.write_text(json.dumps(value))
    (root / "config").chmod(0o755)
    before = managed_state(root)
    marker_before = marker.read_bytes(), identity(marker.lstat())
    api = InstallerAPI.from_bootstrap(tmp_path / "marker-loss-legacy-state",
        trust_config=tmp_path / "trust.json", allow_development=True,
        development_runtime=sys.executable, secret_provider=case.fixture.engine.secret_provider)
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Lost marker must not erase pending recovery authority"))

    def new_operation():
        plan = api.plan("restore", case.second["manifest"], case.fixture.target,
            options={**case.second["options"], "archive_path": case.archive["path"]})
        return api.submit(plan["plan_id"], plan["plan_digest"],
            [plan["review_consent_ref"], *plan["required_consents"]],
            idempotency_key="new-legacy-token-after-marker-loss", background=False)

    refused(new_operation)
    assert managed_state(root) == before
    assert (marker.read_bytes(), identity(marker.lstat())) == marker_before
    assert stat.S_IMODE((root / "config").stat().st_mode) == 0o755


def test_orphan_root_receipt_fences_new_legacy_token_when_marker_and_original_journal_are_lost(case, monkeypatch, tmp_path):
    review, journal, marker = pending_with_lost_journal(case)
    root = case.fixture.root
    owner = json.loads(marker.read_text())
    pending = owner.pop("pending_recovery")
    marker.write_text(json.dumps(owner))
    proof = root / ".hinstall/recovery-root-receipts" / journal.name
    assert json.loads(proof.read_text())["receipt_id"] == pending["receipt_id"]
    (root / "config").chmod(0o755)
    ledger = root / ".hinstall/product-adapter/release-2/adapter-invocations.sqlite3"
    protected = {p: (p.read_bytes(), identity(p.lstat())) for p in (marker, proof, ledger)}
    before = managed_state(root)
    archives = {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")}
    api = InstallerAPI.from_bootstrap(tmp_path / "orphan-proof-legacy-state",
        trust_config=tmp_path / "trust.json", allow_development=True,
        development_runtime=sys.executable, secret_provider=case.fixture.engine.secret_provider)
    monkeypatch.setattr(ExecutableProductAdapter, "_execute",
        lambda *a, **k: pytest.fail("Orphan root proof must fence new legacy operations before product hooks"))

    def new_operation():
        plan = api.plan("restore", case.second["manifest"], case.fixture.target,
            options={**case.second["options"], "archive_path": case.archive["path"]})
        return api.submit(plan["plan_id"], plan["plan_digest"],
            [plan["review_consent_ref"], *plan["required_consents"]],
            idempotency_key="new-token-with-orphan-proof", background=False)

    refused(new_operation)
    assert managed_state(root) == before
    assert {p: (p.read_bytes(), identity(p.lstat())) for p in protected} == protected
    assert not journal.exists(), "Orphan proof is a fence, never authority to reconstruct the lost journal"
    assert {str(p): sha(p) for p in case.fixture.base.rglob("*.hsac")} == archives
