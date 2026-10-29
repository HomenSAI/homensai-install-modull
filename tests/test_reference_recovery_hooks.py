"""Настоящие reference recovery validation, SQLite effects, hold и reconciliation."""
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "reference/src/homensai_reference"
SPEC = importlib.util.spec_from_file_location("reviewed_reference_recovery", SOURCE / "__init__.py",
                                            submodule_search_locations=[str(SOURCE)])
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
adapter = importlib.import_module(SPEC.name + ".adapter")
migrate = importlib.import_module(SPEC.name + ".migrate")
profile = importlib.import_module(SPEC.name + ".profile")


def request(hook="restore", *, action="rollback", digest="a" * 64, key=None):
    return {"protocol_version": "1.0", "product_id": "homensai-reference", "request_id": "request_test",
            "request_digest": digest, "operation_id": "op_review", "idempotency_key": key or hook + "_test",
            "hook": hook, "phase": "execute", "data": {"installation_id": "1" * 32,
            "release": "reference-v1", "recovery_action": action,
            "source_archive_sha256": "b" * 64, "before_recovery_receipt_id": "c" * 64}}


def status(original):
    return {**original, "hook": "operation_status", "phase": "diagnostic",
            "data": {"hook": original["hook"], "request_digest": original["request_digest"]}}


@pytest.fixture
def reference(tmp_path, monkeypatch):
    home, config, release = (tmp_path / name for name in ("data", "config", "release"))
    release.mkdir()
    migrate.migrate(home, config)
    monkeypatch.chdir(release)
    monkeypatch.setenv("HOMENSAI_DATA_HOME", str(home))
    monkeypatch.setenv("HOMENSAI_CONFIG_HOME", str(config))
    (release / "adapter-config.json").write_text(json.dumps({"product_id": "homensai-reference", "port": 12345}))
    (release / "reference-profile.json").write_text(json.dumps(profile.DEFAULT_PROFILE))
    return home, config, release


@pytest.mark.parametrize("action", ["restore", "rollback"])
def test_recovery_hooks_validate_snapshot_record_once_and_keep_hold(reference, action):
    home, _, _ = reference
    database_before = (home / "reference.sqlite").read_bytes()
    for hook in ("restore", "post_restore"):
        original = request(hook, action=action)
        result = adapter.execute(original)
        assert result["applied"] and result["state_version"] == "1" and len(result["receipt_id"]) == 64
        assert adapter.execute(original) == result
        assert adapter.execute(status(original)) == {"outcome": "applied", "request_digest": original["request_digest"], "result": result}
    with sqlite3.connect(home / adapter.LEDGER) as db:
        rows = db.execute("SELECT hook,operation_id,binding,external_actions_on_hold FROM recovery_effects ORDER BY hook").fetchall()
    assert len(rows) == 2 and {row[0] for row in rows} == {"restore", "post_restore"}
    assert all(row[1] == "op_review" and json.loads(row[2])["recovery_action"] == action and row[3] == 1 for row in rows)
    assert (home / "reference.sqlite").read_bytes() == database_before


def test_post_restore_requires_same_operation_binding_restore(reference):
    home, _, _ = reference
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_RECOVERY_RESTORE_NOT_CONFIRMED"):
        adapter.execute(request("post_restore"))
    adapter.execute(request())
    changed = request("post_restore", action="restore")
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_RECOVERY_RESTORE_NOT_CONFIRMED"):
        adapter.execute(changed)
    with sqlite3.connect(home / adapter.LEDGER) as db:
        assert db.execute("SELECT hook FROM recovery_effects").fetchall() == [("restore",)]


def test_changed_recovery_idempotency_binding_is_rejected(reference):
    home, _, _ = reference
    adapter.execute(request())
    for changed in (request(digest="d" * 64), request(action="restore")):
        with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_ADAPTER_EFFECT_CONFLICT"):
            adapter.execute(changed)
    with sqlite3.connect(home / adapter.LEDGER) as db:
        assert db.execute("SELECT count(*) FROM recovery_effects").fetchone() == (1,)


@pytest.mark.parametrize("fault", ["schema", "settings", "required_file"])
def test_invalid_restored_state_never_returns_success_or_creates_effect(reference, fault):
    home, config, release = reference
    if fault == "schema":
        selected = {**profile.DEFAULT_PROFILE, "database_schema": 2, "product_version": "2.0.0"}
        (release / "reference-profile.json").write_text(json.dumps(selected))
    elif fault == "settings":
        (config / "settings.json").write_text('{"invalid": true}')
    else:
        (home / adapter.WELCOME_FILE).unlink()
    with pytest.raises(adapter.ReferenceProductError):
        adapter.execute(request())
    assert not (home / adapter.LEDGER).exists()


@pytest.mark.parametrize("fault", ["missing_ledger", "foreign_digest", "foreign_operation", "schema", "hold"])
def test_reconciliation_needs_durable_matching_effect_and_valid_held_state(reference, fault):
    home, _, release = reference
    original = request()
    adapter.execute(original)
    query = status(original)
    if fault == "missing_ledger":
        (home / adapter.LEDGER).unlink()
    elif fault == "foreign_digest":
        query["request_digest"] = "d" * 64
    elif fault == "foreign_operation":
        query["operation_id"] = "op_foreign"
    elif fault == "schema":
        (release / "reference-profile.json").write_text(json.dumps({**profile.DEFAULT_PROFILE, "database_schema": 2}))
    else:
        with sqlite3.connect(home / adapter.LEDGER) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE recovery_effects SET external_actions_on_hold=0")
    assert adapter.execute(query) == {"outcome": "unknown", "request_digest": query["request_digest"]}
    if fault == "missing_ledger":
        assert not (home / adapter.LEDGER).exists()
