"""Продуктовая проверка целостности SQLite для filesystem snapshot adapter."""
from pathlib import Path
import json
import os
import sqlite3

from .migrate import validate_schema
from .profile import ReferenceProductError, safe_path


def verify_database(path):
    path = safe_path(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        result = db.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise ReferenceProductError("REFERENCE_DATABASE_INTEGRITY_FAILED")
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        validate_schema(db, schema)
    return {"consistent": True, "strategy": "quiesced-filesystem", "database": "reference.sqlite", "database_schema": schema}


def main():
    try:
        path = Path(os.environ["HOMENSAI_DATA_HOME"]) / "reference.sqlite"
        print(json.dumps(verify_database(path)))
        return 0
    except ReferenceProductError as error:
        print(json.dumps({"error": error.code}))
    except (KeyError, OSError, sqlite3.Error):
        print(json.dumps({"error": "REFERENCE_STORAGE_UNAVAILABLE"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
