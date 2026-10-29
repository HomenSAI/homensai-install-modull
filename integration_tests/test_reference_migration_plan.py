"""Read-only ProductAdapter migration plan наблюдает SQLite, не создаёт данные."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "reference/src/homensai_reference"
SPEC = importlib.util.spec_from_file_location("migration_reference", SOURCE / "__init__.py",
                                            submodule_search_locations=[str(SOURCE)])
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
adapter = importlib.import_module(SPEC.name + ".adapter")
migrations = importlib.import_module(SPEC.name + ".migrate")
profiles = importlib.import_module(SPEC.name + ".profile")


def tree(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


@pytest.fixture
def reference(tmp_path, monkeypatch):
    release, data = tmp_path / "release", tmp_path / "data"
    release.mkdir()
    (release / "adapter-config.json").write_text(json.dumps({"product_id": "homensai-reference", "port": 12345}))
    (release / "reference-profile.json").write_text(json.dumps({**profiles.DEFAULT_PROFILE, "database_schema": 3}))
    monkeypatch.chdir(release)
    monkeypatch.setenv("HOMENSAI_DATA_HOME", str(data))
    return tmp_path, data, release


def plan(mode="upgrade", **data):
    return adapter.execute({"protocol_version": "1.0", "product_id": "homensai-reference", "hook": "migration_plan",
        "data": {"installation_id": "test-installation", "from_release": None if mode == "fresh_empty" else "reference-v1",
                 "to_release": "reference-v3", "mode": mode, **data}})


def test_fresh_missing_directory_stays_absent_and_has_explicit_empty_version(reference):
    root, data, _ = reference
    before = tree(root)
    result = plan("fresh_empty")
    assert result["from_version"] == "empty" and result["to_version"] == "3"
    assert [step["id"] for step in result["steps"]] == ["create_notes", "normalize_notes", "hash_and_search_terms"]
    assert all(not step["backup_required"] for step in result["steps"])
    assert result["plan_digest"] == hashlib.sha256(adapter.canonical({key: value for key, value in result.items() if key != "plan_digest"}).encode()).hexdigest()
    assert not data.exists() and tree(root) == before


def test_existing_empty_directory_is_observed_without_marker_or_ledger(reference):
    root, data, _ = reference
    data.mkdir(mode=0o700)
    before = tree(root)
    assert plan("fresh_empty")["from_version"] == "empty"
    assert not list(data.iterdir()) and tree(root) == before


def test_upgrade_observes_real_schema_and_signed_target_without_writes(reference):
    root, data, _ = reference
    migrations.migrate(data, profile=profiles.DEFAULT_PROFILE)
    (data / "document.txt").write_text("user document")
    before = tree(root)
    result = plan()
    assert result["from_version"] == "1" and result["to_version"] == "3"
    assert [step["id"] for step in result["steps"]] == ["normalize_notes", "hash_and_search_terms"]
    assert all(step["backup_required"] for step in result["steps"])
    assert tree(root) == before


@pytest.mark.parametrize("kind", ["user_document", "known_database", "unknown_database", "empty_database_file"])
def test_fresh_never_treats_existing_unknown_or_known_data_as_empty(reference, kind):
    root, data, _ = reference
    data.mkdir(mode=0o700)
    if kind == "known_database":
        migrations.migrate(data, profile=profiles.DEFAULT_PROFILE)
    elif kind == "unknown_database":
        (data / "reference.sqlite").write_bytes(b"unknown data")
    elif kind == "empty_database_file":
        (data / "reference.sqlite").touch()
    else:
        (data / "only-file.txt").write_text("keep")
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_FRESH_DATA_NOT_EMPTY"):
        plan("fresh_empty")
    assert tree(root) == before


@pytest.mark.parametrize("extra", [{"from_version": "empty"}, {"to_version": "3"}, {"mode": "unknown"},
                                  {"from_release": None}, {"installation_id": ""}])
def test_plan_rejects_caller_schema_or_unknown_identity_before_observations(reference, extra):
    root, _, _ = reference
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_MIGRATION_REQUEST_INVALID"):
        plan(**extra)
    assert tree(root) == before


def test_upgrade_missing_database_does_not_create_empty_database(reference):
    root, data, _ = reference
    before = tree(root)
    with pytest.raises(FileNotFoundError):
        plan()
    assert not data.exists() and tree(root) == before


def test_downgrade_requires_snapshot_restore_and_changes_nothing(reference):
    root, data, release = reference
    migrations.migrate(data, profile={**profiles.DEFAULT_PROFILE, "database_schema": 3})
    (release / "reference-profile.json").write_text(json.dumps(profiles.DEFAULT_PROFILE))
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_DOWNGRADE_REQUIRES_SNAPSHOT_RESTORE"):
        plan()
    assert tree(root) == before


def test_same_schema_plan_has_no_steps(reference):
    root, data, release = reference
    migrations.migrate(data, profile=profiles.DEFAULT_PROFILE)
    (release / "reference-profile.json").write_text(json.dumps(profiles.DEFAULT_PROFILE))
    before = tree(root)
    assert plan()["steps"] == []
    assert tree(root) == before


@pytest.mark.parametrize("kind", ["corrupt", "unrecognized_schema", "hardlink", "symlink"])
def test_upgrade_rejects_unsupported_or_shared_database_without_mutation(reference, kind):
    root, data, _ = reference
    migrations.migrate(data, profile=profiles.DEFAULT_PROFILE)
    path = data / "reference.sqlite"
    if kind == "corrupt":
        path.write_bytes(b"not sqlite")
    elif kind == "unrecognized_schema":
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=0")
    elif kind == "hardlink":
        os.link(path, data / "other.sqlite")
    else:
        path.rename(data / "original.sqlite")
        path.symlink_to(data / "original.sqlite")
    before = tree(root)
    with pytest.raises(adapter.ReferenceProductError):
        plan()
    assert tree(root) == before


def test_wal_with_uncheckpointed_data_is_refused_without_sidecar_mutation(reference):
    root, data, _ = reference
    migrations.migrate(data, profile=profiles.DEFAULT_PROFILE)
    db = sqlite3.connect(data / "reference.sqlite")
    try:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("INSERT INTO notes(id,text) VALUES(2,'WAL note')")
        db.commit()
        before = tree(root)
        with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_MIGRATION_DATABASE_BUSY"):
            plan()
        assert tree(root) == before
    finally:
        db.close()


def test_describe_reflects_signed_subset_not_all_implemented_hooks(reference):
    _, _, release = reference
    (release / "installer").mkdir()
    (release / "installer/adapter.json").write_text(json.dumps({"adapter_version": adapter.VERSION,
        "capabilities": {name: {} for name in ("describe", "readiness", "quiesce", "operation_status")}}))
    result = adapter.execute({"protocol_version": "1.0", "product_id": "homensai-reference", "hook": "describe", "data": {}})
    assert result["capabilities"] == ["describe", "operation_status", "quiesce", "readiness"]
