"""Идемпотентная реальная SQLite-миграция reference product."""
from pathlib import Path
import os
import sqlite3


def main():
    home = Path(os.environ["HOMENSAI_DATA_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "reference.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO notes(id,text) VALUES (1,?)", ("HomenSAI reference data — сохраняется после удаления",))
        db.execute("PRAGMA user_version=1")


if __name__ == "__main__":
    main()
