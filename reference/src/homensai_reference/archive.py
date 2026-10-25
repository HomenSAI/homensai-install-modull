"""Продуктовая проверка целостности SQLite для filesystem snapshot adapter."""
from pathlib import Path
import json
import os
import sqlite3


def main():
    path = Path(os.environ["HOMENSAI_DATA_HOME"]) / "reference.sqlite"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        result = db.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise RuntimeError("Reference SQLite verification failed")
    print(json.dumps({"consistent": True, "strategy": "quiesced-filesystem", "database": "reference.sqlite"}))


if __name__ == "__main__":
    main()
