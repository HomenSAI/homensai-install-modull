# HomenS.AI · © 2026 Serhii Khomenko
"""Идемпотентная транзакционная миграция SQLite по идентификатору."""
from __future__ import annotations

from contextlib import closing

from kitdemo.common import arguments, database, token_fingerprint


def main() -> int:
    args = arguments('kitdemo migrate', migration=True)
    if token_fingerprint() is None:
        # Миграции тоже получают секреты через homensai_entry.py.
        print('kitdemo migrate: KITDEMO_TOKEN is not set')
        return 3
    with closing(database(args.data_dir)) as connection, connection:
        if connection.execute('SELECT 1 FROM migrations WHERE name = ?', (args.migration,)).fetchone():
            return 0
        connection.execute('INSERT INTO notes(text) VALUES (?)', ('migrated ' + args.migration,))
        connection.execute('INSERT INTO migrations(name) VALUES (?)', (args.migration,))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
