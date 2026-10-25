"""Настоящие additive SQLite migrations v1→v2→v3, с атомарным отказом до commit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import sqlite3
import stat

from .profile import ReferenceProductError, load_profile, safe_path, validate_profile
from .storage import initialize_files


SEED_TEXT = "HomenSAI reference data — сохраняется после удаления"
BASE_COLUMNS = {"id", "text"}
V2_COLUMNS = {"normalized_text", "text_length", "category"}
V3_COLUMNS = {"content_sha256", "is_archived"}


def validate_schema(db: sqlite3.Connection, version: int) -> None:
    if type(version) is not int or version not in {1, 2, 3}:
        raise ReferenceProductError("REFERENCE_DATABASE_SCHEMA_UNSUPPORTED")
    expected = BASE_COLUMNS | (V2_COLUMNS if version >= 2 else set()) | (V3_COLUMNS if version >= 3 else set())
    columns = {row[1] for row in db.execute("PRAGMA table_info(notes)")}
    if not expected <= columns:
        raise ReferenceProductError("REFERENCE_DATABASE_SCHEMA_INVALID")
    if version >= 3:
        terms = {row[1] for row in db.execute("PRAGMA table_info(search_terms)")}
        if not {"note_id", "term"} <= terms:
            raise ReferenceProductError("REFERENCE_DATABASE_SCHEMA_INVALID")


def _v1(db):
    db.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
    db.execute("INSERT INTO notes(id,text) VALUES(1,?)", (SEED_TEXT,))


def _v2(db):
    db.execute("ALTER TABLE notes ADD COLUMN normalized_text TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE notes ADD COLUMN text_length INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE notes ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
    cursor = db.execute("SELECT id,text FROM notes ORDER BY id")
    while rows := cursor.fetchmany(256):
        updates = []
        for note_id, text in rows:
            if type(text) is not str:
                raise ReferenceProductError("REFERENCE_NOTE_DATA_INVALID")
            normalized = " ".join(text.split()).casefold()
            updates.append((normalized, len(normalized), note_id))
        db.executemany("UPDATE notes SET normalized_text=?,text_length=? WHERE id=?", updates)


def _v3(db):
    db.execute("ALTER TABLE notes ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE notes ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE search_terms(
        note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        term TEXT NOT NULL, PRIMARY KEY(note_id,term))""")
    cursor = db.execute("SELECT id,text FROM notes ORDER BY id")
    while rows := cursor.fetchmany(256):
        for note_id, text in rows:
            if type(text) is not str:
                raise ReferenceProductError("REFERENCE_NOTE_DATA_INVALID")
            normalized = " ".join(text.split()).casefold()
            db.execute("UPDATE notes SET normalized_text=?,text_length=?,content_sha256=? WHERE id=?",
                       (normalized, len(normalized), hashlib.sha256(text.encode("utf-8")).hexdigest(), note_id))
            db.executemany("INSERT INTO search_terms(note_id,term) VALUES(?,?)",
                           ((note_id, term) for term in sorted(set(normalized.split()))))


def migrate(data_home: str | Path, config_home: str | Path | None = None, *, profile: dict | None = None) -> dict:
    selected = load_profile() if profile is None else validate_profile(profile)
    home = safe_path(data_home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = safe_path(home / "reference.sqlite")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReferenceProductError("REFERENCE_DATABASE_FILE_INVALID")
    else:
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(descriptor)
        except FileExistsError:
            raise ReferenceProductError("REFERENCE_DATABASE_CONCURRENT_CREATION") from None
    target = selected["database_schema"]
    with sqlite3.connect(path, timeout=5, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if current > target:
                raise ReferenceProductError("REFERENCE_DOWNGRADE_REQUIRES_SNAPSHOT_RESTORE")
            if current not in {0, 1, 2, 3}:
                raise ReferenceProductError("REFERENCE_DATABASE_SCHEMA_UNSUPPORTED")
            if current:
                validate_schema(db, current)
            elif db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                raise ReferenceProductError("REFERENCE_DATABASE_STATE_UNRECOGNIZED")
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            # У legacy v1 могло не быть таблицы markers. Это marker схемы, не выдуманная audit timestamp.
            if current:
                db.execute("INSERT OR IGNORE INTO schema_migrations(version,name) VALUES(?,?)", (current, "observed_existing_schema"))
            steps = []
            for version in range(current + 1, target + 1):
                {1: _v1, 2: _v2, 3: _v3}[version](db)
                db.execute("INSERT INTO schema_migrations(version,name) VALUES(?,?)",
                           (version, {1: "create_notes", 2: "normalize_notes", 3: "hash_and_search_terms"}[version]))
                db.execute("PRAGMA user_version=" + str(version))
                steps.append(version)
            validate_schema(db, target)
            if selected["migration_failure"]:
                raise ReferenceProductError("REFERENCE_MIGRATION_FAILURE_REQUESTED")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    files = initialize_files(home, config_home)
    return {"product_version": selected["product_version"], "database_schema": target,
            "migrations_applied": steps, **files}


def main():
    try:
        migrate(Path(os.environ["HOMENSAI_DATA_HOME"]),
                Path(os.environ["HOMENSAI_CONFIG_HOME"]) if os.environ.get("HOMENSAI_CONFIG_HOME") else None)
        return 0
    except ReferenceProductError as error:
        print(json.dumps({"error": error.code}))
    except (OSError, sqlite3.Error, KeyError):
        print(json.dumps({"error": "REFERENCE_STORAGE_UNAVAILABLE"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
