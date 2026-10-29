"""Настоящие SQLite/data transformations reference; helper checks не открывают сокеты."""

from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "reference/src/homensai_reference"
SPEC = importlib.util.spec_from_file_location("source_reference_product", SOURCE / "__init__.py",
                                            submodule_search_locations=[str(SOURCE)])
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
profile_module = importlib.import_module(SPEC.name + ".profile")
migrations = importlib.import_module(SPEC.name + ".migrate")
storage = importlib.import_module(SPEC.name + ".storage")
server = importlib.import_module(SPEC.name + ".server")
archive = importlib.import_module(SPEC.name + ".archive")
ReferenceProductError = profile_module.ReferenceProductError


def profile(version=1, **flags):
    return {**profile_module.DEFAULT_PROFILE, "product_version": str(version) + ".0.0",
            "database_schema": version, **flags}


def dump(database):
    with sqlite3.connect(database) as db:
        return db.execute("PRAGMA user_version").fetchone()[0], list(db.iterdump())


def health(database, selected, **kwargs):
    return server.readiness(database, profile=selected, product_id="homensai-reference",
                            installation_id="0123456789abcdef0123456789abcdef", release="reference-test-release",
                            environ=kwargs.pop("environ", {}), **kwargs)


def test_default_absent_profile_is_v1_and_does_not_write_release(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    monkeypatch.chdir(release)
    assert profile_module.load_profile() == profile(1)
    assert list(release.iterdir()) == []
    selected = profile_module.load_profile()
    selected["database_schema"] = 3
    assert profile_module.load_profile()["database_schema"] == 1


def test_explicit_profile_is_loaded_from_release_cwd_not_environment(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    (release / "reference-profile.json").write_text(json.dumps(profile(2)))
    monkeypatch.chdir(release)
    monkeypatch.setenv("REFERENCE_HEALTH_FAILURE", "true")
    monkeypatch.setenv("REFERENCE_MIGRATION_FAILURE", "true")
    monkeypatch.setenv("HOMENSAI_RELEASE", "unrelated-v3")
    assert profile_module.load_profile() == profile(2)
    home = tmp_path / "data"
    config = tmp_path / "config"
    migrations.migrate(home, config)
    assert health(home / "reference.sqlite", profile_module.load_profile(), config_home=config)["ready"]


@pytest.mark.parametrize("mutation", [
    {"database_schema": True}, {"database_schema": 4}, {"schema_version": 2},
    {"health_failure": "true"}, {"migration_failure": 1}, {"require_api_key": None},
    {"product_version": "1; arbitrary command"}, {"command": ["not", "allowed"]},
    {"api_key": "never-accept-secret-value-in-profile"},
])
def test_profile_rejects_types_unknown_fields_and_embedded_secrets(mutation):
    value = {**profile(1), **mutation}
    with pytest.raises(ReferenceProductError) as caught:
        profile_module.validate_profile(value)
    assert str(caught.value) == "REFERENCE_PROFILE_INVALID"
    assert "never-accept-secret" not in str(caught.value)


@pytest.mark.parametrize("content", ['{"schema_version":1,"schema_version":1}', '{"value":NaN}', '[]', '{bad'])
def test_invalid_profile_json_never_falls_back_to_default(tmp_path, content):
    (tmp_path / "reference-profile.json").write_text(content)
    with pytest.raises(ReferenceProductError, match="REFERENCE_PROFILE_INVALID"):
        profile_module.load_profile(tmp_path)


def test_v1_initializes_real_db_config_and_user_file_once(tmp_path):
    home, config = tmp_path / "data", tmp_path / "config"
    first = migrations.migrate(home, config, profile=profile(1))
    assert first["migrations_applied"] == [1]
    assert first["user_file_created"] and first["settings_created"]
    with sqlite3.connect(home / "reference.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT text FROM notes WHERE id=1").fetchone()[0] == migrations.SEED_TEXT
    assert (home / storage.WELCOME_FILE).read_text() == storage.WELCOME_TEXT
    assert json.loads((config / "settings.json").read_text()) == storage.DEFAULT_SETTINGS
    before = dump(home / "reference.sqlite")
    repeated = migrations.migrate(home, config, profile=profile(1))
    assert repeated["migrations_applied"] == [] and not repeated["user_file_created"] and not repeated["settings_created"]
    assert dump(home / "reference.sqlite") == before


def test_real_v1_v2_v3_migrations_transform_data_twice_idempotently_and_preserve_user_files(tmp_path):
    home, config = tmp_path / "data", tmp_path / "config"
    migrations.migrate(home, config, profile=profile(1))
    user_file = home / storage.WELCOME_FILE
    user_file.write_bytes(b"user-owned content after initial installation\n")
    settings = {**storage.DEFAULT_SETTINGS, "site_title": "My custom title", "language": "de", "user_extension": "keep exactly"}
    config_file = config / "settings.json"
    config_bytes = (json.dumps(settings, ensure_ascii=False, indent=3) + "\n").encode()
    config_file.write_bytes(config_bytes)
    original = "  Grüß   DICH\nПривет  Мир  "
    with sqlite3.connect(home / "reference.sqlite") as db:
        db.execute("INSERT INTO notes(id,text) VALUES(2,?)", (original,))
    v2 = migrations.migrate(home, config, profile=profile(2))
    assert v2["migrations_applied"] == [2]
    with sqlite3.connect(home / "reference.sqlite") as db:
        row = db.execute("SELECT text,normalized_text,text_length,category FROM notes WHERE id=2").fetchone()
        assert row == (original, "grüss dich привет мир", len("grüss dich привет мир"), "general")
        db.execute("UPDATE notes SET category='user-chosen' WHERE id=2")
        db.execute("INSERT INTO notes(id,text) VALUES(3,?)", ("Created AFTER v2",))
    before = dump(home / "reference.sqlite")
    assert migrations.migrate(home, config, profile=profile(2))["migrations_applied"] == []
    assert dump(home / "reference.sqlite") == before
    v3 = migrations.migrate(home, config, profile=profile(3))
    assert v3["migrations_applied"] == [3]
    with sqlite3.connect(home / "reference.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        row = db.execute("SELECT text,category,content_sha256,is_archived FROM notes WHERE id=2").fetchone()
        assert row == (original, "user-chosen", hashlib.sha256(original.encode()).hexdigest(), 0)
        terms = {row[0] for row in db.execute("SELECT term FROM search_terms WHERE note_id=3")}
        assert terms == {"created", "after", "v2"}
        assert db.execute("SELECT normalized_text FROM notes WHERE id=3").fetchone()[0] == "created after v2"
        db.execute("UPDATE notes SET is_archived=1 WHERE id=2")
    before = dump(home / "reference.sqlite")
    assert migrations.migrate(home, config, profile=profile(3))["migrations_applied"] == []
    assert dump(home / "reference.sqlite") == before
    assert user_file.read_bytes() == b"user-owned content after initial installation\n"
    assert config_file.read_bytes() == config_bytes
    assert storage.load_settings(config)["site_title"] == "My custom title"
    assert "user_extension" not in storage.load_settings(config)


@pytest.mark.parametrize("version", [2, 3])
def test_fresh_install_can_reach_selected_schema_without_fake_version_marker(tmp_path, version):
    home = tmp_path / "data"
    result = migrations.migrate(home, profile=profile(version))
    assert result["migrations_applied"] == list(range(1, version + 1))
    assert archive.verify_database(home / "reference.sqlite")["database_schema"] == version
    assert health(home / "reference.sqlite", profile(version))["ready"]


@pytest.mark.parametrize("target", [2, 3])
def test_failure_flag_rolls_back_real_ddl_data_and_user_version(tmp_path, target):
    home, config = tmp_path / "data", tmp_path / "config"
    migrations.migrate(home, config, profile=profile(1))
    with sqlite3.connect(home / "reference.sqlite") as db:
        db.execute("INSERT INTO notes(id,text) VALUES(2,'Keep this exact user value')")
    before = dump(home / "reference.sqlite")
    settings_before = (config / "settings.json").read_bytes()
    user_before = (home / storage.WELCOME_FILE).read_bytes()
    with pytest.raises(ReferenceProductError, match="REFERENCE_MIGRATION_FAILURE_REQUESTED"):
        migrations.migrate(home, config, profile=profile(target, migration_failure=True))
    assert dump(home / "reference.sqlite") == before
    assert (config / "settings.json").read_bytes() == settings_before
    assert (home / storage.WELCOME_FILE).read_bytes() == user_before
    assert health(home / "reference.sqlite", profile(1), config_home=config)["ready"]
    assert not health(home / "reference.sqlite", profile(target), config_home=config)["ready"]
    assert migrations.migrate(home, config, profile=profile(target))["database_schema"] == target


def test_failing_first_migration_keeps_schema_zero_and_does_not_create_user_defaults(tmp_path):
    home, config = tmp_path / "data", tmp_path / "config"
    with pytest.raises(ReferenceProductError):
        migrations.migrate(home, config, profile=profile(3, migration_failure=True))
    with sqlite3.connect(home / "reference.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    assert not (home / storage.WELCOME_FILE).exists()
    assert not (config / "settings.json").exists()


@pytest.mark.parametrize("target", [1, 2])
def test_downgrade_requires_snapshot_restore_and_never_rewrites_data(tmp_path, target):
    home = tmp_path / "data"
    migrations.migrate(home, profile=profile(3))
    before = dump(home / "reference.sqlite")
    with pytest.raises(ReferenceProductError, match="REFERENCE_DOWNGRADE_REQUIRES_SNAPSHOT_RESTORE"):
        migrations.migrate(home, profile=profile(target))
    assert dump(home / "reference.sqlite") == before
    assert health(home / "reference.sqlite", profile(3))["ready"]
    assert not health(home / "reference.sqlite", profile(target))["ready"]


def test_legacy_v1_db_is_upgraded_without_overwriting_existing_notes(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    with sqlite3.connect(home / "reference.sqlite") as db:
        db.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY,text TEXT NOT NULL)")
        db.execute("INSERT INTO notes VALUES(11,'Existing legacy value')")
        db.execute("PRAGMA user_version=1")
    migrations.migrate(home, profile=profile(3))
    with sqlite3.connect(home / "reference.sqlite") as db:
        assert db.execute("SELECT id,text FROM notes").fetchall() == [(11, "Existing legacy value")]
        assert db.execute("SELECT normalized_text FROM notes").fetchone()[0] == "existing legacy value"


def test_health_identity_schema_failure_flags_and_api_key_presence_never_echo_value(tmp_path):
    home = tmp_path / "data"
    migrations.migrate(home, profile=profile(2))
    base = health(home / "reference.sqlite", profile(2))
    assert base["ready"] and base["product_version"] == "2.0.0" and base["database_schema"] == 2
    assert base["product_id"] == "homensai-reference" and base["release"] == "reference-test-release"
    assert not base["api_key_configured"]
    assert not health(home / "reference.sqlite", profile(2, health_failure=True))["ready"]
    missing = health(home / "reference.sqlite", profile(2, require_api_key=True))
    assert not missing["ready"] and not missing["checks"]["api_key"]
    marker = "synthetic-key-that-must-not-be-printed"
    configured = health(home / "reference.sqlite", profile(2, require_api_key=True), environ={"REFERENCE_API_KEY": marker})
    assert configured["ready"] and configured["api_key_configured"]
    assert marker not in json.dumps(configured)
    assert marker not in server.render_page(configured, server.read_notes(home / "reference.sqlite", 2)).decode()
    assert not health(home / "reference.sqlite", profile(2, require_api_key=True), environ={"REFERENCE_API_KEY": "  "})["ready"]


def test_missing_database_does_not_get_created_by_readiness_or_notes(tmp_path):
    database = tmp_path / "absent.sqlite"
    result = health(database, profile(1))
    assert not result["ready"] and result["database_schema"] is None
    assert server.read_notes(database, 1) == []
    assert not database.exists()


def test_settings_and_notes_are_escaped_and_invalid_settings_are_unready(tmp_path):
    home, config = tmp_path / "data", tmp_path / "config"
    migrations.migrate(home, config, profile=profile(3))
    values = {**storage.DEFAULT_SETTINGS, "site_title": "<script>untrusted title</script>", "language": "en"}
    (config / "settings.json").write_text(json.dumps(values))
    with sqlite3.connect(home / "reference.sqlite") as db:
        db.execute("UPDATE notes SET text='<img src=x onerror=alert(1)>' WHERE id=1")
    result = health(home / "reference.sqlite", profile(3), config_home=config)
    page = server.render_page(result, server.read_notes(home / "reference.sqlite", 3), storage.load_settings(config)).decode()
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page and "&lt;img" in page and "lang='en'" in page
    (config / "settings.json").write_text('{"schema_version":99}')
    assert not health(home / "reference.sqlite", profile(3), config_home=config)["ready"]


def test_cli_migration_reads_cwd_profile_and_reports_only_safe_error_code(tmp_path, monkeypatch, capsys):
    release = tmp_path / "release"
    release.mkdir()
    (release / "reference-profile.json").write_text(json.dumps(profile(2, migration_failure=True)))
    monkeypatch.chdir(release)
    monkeypatch.setenv("HOMENSAI_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOMENSAI_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("REFERENCE_API_KEY", "never-print-this-key")
    assert migrations.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "REFERENCE_MIGRATION_FAILURE_REQUESTED"}
    assert "never-print-this-key" not in captured.out + captured.err
    assert str(tmp_path) not in captured.out + captured.err


def test_linked_profile_or_user_storage_is_rejected_without_following(tmp_path):
    elsewhere = tmp_path / "external.json"
    elsewhere.write_text(json.dumps(profile(1)))
    release = tmp_path / "release"
    release.mkdir()
    (release / "reference-profile.json").symlink_to(elsewhere)
    with pytest.raises(ReferenceProductError): profile_module.load_profile(release)
    home = tmp_path / "data"
    home.mkdir()
    (home / "reference.sqlite").symlink_to(tmp_path / "outside.sqlite")
    with pytest.raises(ReferenceProductError): migrations.migrate(home, profile=profile(1))
    assert not (tmp_path / "outside.sqlite").exists()
