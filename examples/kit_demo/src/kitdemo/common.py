# HomenS.AI · © 2026 Serhii Khomenko
"""Общие помощники вымышленного продукта: аргументы, версия выпуска, SQLite."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import signal
import sqlite3


def release() -> tuple[str, str]:
    """Продукт и версия, которые сообщает загрузчик homensai_entry.py."""
    return os.environ.get('HOMENSAI_PRODUCT_ID', 'homensai-kit-demo'), os.environ.get('HOMENSAI_RELEASE', 'dev')


def arguments(description: str, *, port: bool = False, migration: bool = False):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--data-dir', type=Path, required=True)
    if port:
        parser.add_argument('--port', type=int, required=True)
    if migration:
        parser.add_argument('--migration', required=True)
    return parser.parse_args()


def graceful_stop():
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)


def database(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(data_dir / 'kitdemo.sqlite3')
    connection.execute('CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)')
    connection.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL)')
    connection.commit()
    return connection


def token_fingerprint() -> str | None:
    """Первые 12 символов SHA-256 токена: значение секрета не раскрывается."""
    token = os.environ.get('KITDEMO_TOKEN')
    return hashlib.sha256(token.encode('utf-8')).hexdigest()[:12] if token else None
