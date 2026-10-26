"""SQLite schema/result atomicity и консервативный UNKNOWN reference adapter."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "reference/src/homensai_reference"
SPEC = importlib.util.spec_from_file_location("apply_reference", SOURCE / "__init__.py",
                                            submodule_search_locations=[str(SOURCE)])
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
adapter = importlib.import_module(SPEC.name + ".adapter")
migrations = importlib.import_module(SPEC.name + ".migrate")
profiles = importlib.import_module(SPEC.name + ".profile")


class SimulatedCrash(BaseException):
    pass


def tree(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def reference(tmp_path, monkeypatch):
    release, data, config = (tmp_path / name for name in ("release", "data", "config"))
    release.mkdir()
    (release / "adapter-config.json").write_text(json.dumps({"product_id": "homensai-reference", "port": 12345}))
    (release / "reference-profile.json").write_text(json.dumps(profiles.DEFAULT_PROFILE))
    monkeypatch.chdir(release)
    monkeypatch.setenv("HOMENSAI_DATA_HOME", str(data))
    monkeypatch.setenv("HOMENSAI_CONFIG_HOME", str(config))
    return tmp_path, data, config, release


def profile(release, version, **flags):
    (release / "reference-profile.json").write_text(json.dumps({**profiles.DEFAULT_PROFILE,
        "product_version": str(version) + ".0.0", "database_schema": version, **flags}))


def request(data, key="migration-1"):
    return {"protocol_version": "1.0", "product_id": "homensai-reference", "hook": "migration_apply",
            "idempotency_key": key, "request_digest": hashlib.sha256(adapter.canonical(data).encode()).hexdigest(), "data": data}


def prepare(*, current=None, target=1, key="migration-1"):
    product = adapter.execute({"protocol_version": "1.0", "product_id": "homensai-reference", "hook": "migration_plan",
        "data": {"installation_id": "installation-1", "from_release": "v" + str(current) if current else None,
                 "to_release": "v" + str(target), "mode": "upgrade" if current else "fresh_empty"}})
    data = {"plan_digest": product["plan_digest"], "backup_receipt_id" if current else "fresh_receipt_id": "a" * 64,
        "migration_binding": {"migration_review_id": "review-" + str(target), "binding_sha256": "b" * 64,
            "installation_id": "installation-1", "from_release": "v" + str(current) if current else None,
            "to_release": "v" + str(target), "from_version": str(current) if current else "empty", "to_version": str(target)}}
    return request(data, key)


def status(original):
    return adapter.execute({**original, "hook": "operation_status", "data": {"hook": "migration_apply", "request_digest": original["request_digest"]}})


def schema(data):
    with sqlite3.connect((data / "reference.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_then_two_updates_commit_business_schema_and_effect_together(reference):
    _, data, config, release = reference
    receipts = []
    for version in (1, 2, 3):
        profile(release, version)
        call = prepare(current=version - 1 or None, target=version, key="migration-" + str(version))
        result = adapter.execute(call)
        assert result["applied"] and result["state_version"] == str(version) and schema(data) == version
        assert status(call) == {"outcome": "applied", "request_digest": call["request_digest"], "result": result}
        receipts.append(result["receipt_id"])
        if version == 1:
            (data / "user-document.txt").write_text("preserved user document")
            settings = config / "settings.json"
            value = json.loads(settings.read_text())
            value["site_title"] = "Custom title"
            settings.write_text(json.dumps(value))
            with sqlite3.connect(data / "reference.sqlite") as db:
                db.execute("INSERT INTO notes(id,text) VALUES(2,'  Custom  NOTE ')")
                db.commit()
    assert len(set(receipts)) == 3 and (data / "user-document.txt").read_text() == "preserved user document"
    assert json.loads((config / "settings.json").read_text())["site_title"] == "Custom title"
    with sqlite3.connect(data / "reference.sqlite") as db:
        assert db.execute("SELECT count(*) FROM product_adapter_effects").fetchone() == (3,)
        assert db.execute("SELECT normalized_text,content_sha256 FROM notes WHERE id=2").fetchone() == (
            "custom note", hashlib.sha256(b"  Custom  NOTE ").hexdigest())


def test_lost_ack_after_business_commit_reconciles_without_second_migration(reference, monkeypatch):
    root, data, _, _ = reference
    call = prepare()
    original = adapter.migrate

    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(adapter, "migrate", lost_ack)
        with pytest.raises(SimulatedCrash):
            adapter.execute(call)
    before = tree(root)
    recovered = status(call)
    assert recovered["outcome"] == "applied" and schema(data) == 1
    monkeypatch.setattr(adapter, "migrate", lambda *a, **kw: pytest.fail("Повтор бизнес-миграции"))
    assert adapter.execute(call) == recovered["result"]
    assert tree(root) == before


def test_crash_before_business_commit_rolls_back_schema_and_keeps_unknown(reference, monkeypatch):
    root, data, config, release = reference
    migrations.migrate(data, config, profile=profiles.DEFAULT_PROFILE)
    profile(release, 2)
    call = prepare(current=1, target=2)
    original = migrations._apply_schema

    def crash(db, selected):
        original(db, selected)
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_apply_schema", crash)
        with pytest.raises(SimulatedCrash):
            adapter.execute(call)
    assert schema(data) == 1 and status(call)["outcome"] == "unknown"
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_MIGRATION_OUTCOME_UNKNOWN"):
        adapter.execute(call)
    assert tree(root) == before


def test_commit_before_default_files_is_unknown_and_never_reinitialized(reference, monkeypatch):
    root, data, config, _ = reference
    call = prepare()

    def crash(*args, **kwargs):
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "initialize_files", crash)
        with pytest.raises(SimulatedCrash):
            adapter.execute(call)
    assert schema(data) == 1 and not config.exists()
    before = tree(root)
    assert status(call)["outcome"] == "unknown"
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_MIGRATION_OUTCOME_UNKNOWN"):
        adapter.execute(call)
    assert tree(root) == before


@pytest.mark.parametrize("mutation", ["digest", "to_version", "receipt_format", "both_receipts", "missing_receipt", "fresh_backup_mismatch"])
def test_invalid_plan_or_receipt_shape_is_rejected_before_any_product_write(reference, mutation):
    root, data, _, _ = reference
    call = prepare()
    value = call["data"]
    if mutation == "digest":
        value["plan_digest"] = "f" * 64
    elif mutation == "to_version":
        value["migration_binding"]["to_version"] = "2"
    elif mutation == "receipt_format":
        value["fresh_receipt_id"] = "not-a-proof"
    elif mutation == "both_receipts":
        value["backup_receipt_id"] = "d" * 64
    elif mutation == "missing_receipt":
        value.pop("fresh_receipt_id")
    else:
        value["backup_receipt_id"] = value.pop("fresh_receipt_id")
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError):
        adapter.execute(request(value))
    assert not data.exists() and tree(root) == before


def test_changed_schema_after_review_is_rejected_without_new_intent(reference):
    root, data, config, release = reference
    migrations.migrate(data, config, profile=profiles.DEFAULT_PROFILE)
    profile(release, 3)
    call = prepare(current=1, target=3)
    migrations.migrate(data, config, profile={**profiles.DEFAULT_PROFILE, "database_schema": 2})
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_MIGRATION_PLAN_STALE"):
        adapter.execute(call)
    assert schema(data) == 2 and not (data / adapter.LEDGER).exists() and tree(root) == before


@pytest.mark.parametrize("changed", ["receipt", "installation", "release"])
def test_completed_effect_cannot_be_rebound_to_other_identity_or_backup(reference, changed):
    root, _, _, _ = reference
    call = prepare()
    adapter.execute(call)
    if changed == "receipt":
        call["data"]["fresh_receipt_id"] = "c" * 64
    else:
        call["data"]["migration_binding"]["installation_id" if changed == "installation" else "to_release"] = "foreign"
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_ADAPTER_EFFECT_CONFLICT"):
        adapter.execute(request(call["data"]))
    assert tree(root) == before


def test_missing_business_effect_never_reports_not_applied_or_creates_ledger(reference):
    root, data, config, _ = reference
    migrations.migrate(data, config, profile=profiles.DEFAULT_PROFILE)
    call = request({}, "unknown-effect")
    before = tree(root)
    assert status(call) == {"outcome": "unknown", "request_digest": call["request_digest"]}
    assert not (data / adapter.LEDGER).exists() and tree(root) == before


def test_reconciliation_of_wal_database_is_unknown_and_creates_no_sidecars(reference):
    root, data, _, _ = reference
    call = prepare()
    adapter.execute(call)
    db = sqlite3.connect(data / "reference.sqlite")
    assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    db.close()
    before = tree(root)
    assert not (data / "reference.sqlite-wal").exists() and not (data / "reference.sqlite-shm").exists()
    observed = status(call)
    assert tree(root) == before
    assert observed["outcome"] == "unknown"


def test_malformed_business_effect_result_is_unknown_without_traceback_or_write(reference):
    root, data, _, _ = reference
    call = prepare()
    adapter.execute(call)
    with sqlite3.connect(data / "reference.sqlite") as db:
        db.execute("UPDATE product_adapter_effects SET result='[]'")
        db.commit()
    before = tree(root)
    assert status(call)["outcome"] == "unknown"
    assert tree(root) == before
