# HomenS.AI · © 2026 Serhii Khomenko
"""Вымышленный тестовый продукт: синтетические данные, SQLite и процессы.

Не использовать как адаптер настоящего продукта или готовый установщик.
"""

from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sqlite3
import time


def database(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (data_dir / "settings.json").open("x", encoding="utf-8") as settings:
            json.dump({"display_name": "Synthetic User", "locale": "ru"}, settings)
            settings.write("\n")
    except FileExistsError:
        pass
    connection = sqlite3.connect(data_dir / "demo.sqlite3")
    connection.execute("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
    connection.commit()
    return connection


def migrate(data_dir: Path, migration: str, failure: str) -> None:
    with closing(database(data_dir)) as connection, connection:
        if connection.execute("SELECT 1 FROM migrations WHERE name = ?", (migration,)).fetchone():
            return
        if failure == "before":
            raise RuntimeError("Синтетический отказ миграции")
        connection.execute("INSERT INTO entries(text) VALUES (?)", ("synthetic-" + migration,))
        connection.execute("INSERT INTO migrations(name) VALUES (?)", (migration,))
        if failure == "during":
            raise RuntimeError("Синтетический отказ миграции")
    if failure == "after":
        raise RuntimeError("Синтетический отказ после фиксации")


def serve(data_dir: Path, release: str, port: int, failure: str, *, listen: str = "127.0.0.1") -> None:
    if failure == "start":
        raise RuntimeError("Синтетический отказ запуска")
    if failure == "stop":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with closing(database(data_dir)):
        pass

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/health" and failure != "health"
            body = json.dumps({"product": "homensai-test-demo", "release": release,
                               "healthy": ok}).encode("utf-8")
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    with ThreadingHTTPServer((listen, port), Handler) as server:
        print(json.dumps({"port": server.server_port, "release": release}), flush=True)
        server.serve_forever(poll_interval=0.1)


def worker(data_dir: Path, release: str, failure: str) -> None:
    if failure == "start":
        raise RuntimeError("Синтетический отказ запуска")
    if failure == "stop":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    data_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = data_dir / "worker-heartbeat.json"
    while True:
        temporary = heartbeat.with_suffix(".tmp")
        temporary.write_text(json.dumps({"release": release, "pid": os.getpid(),
                                         "time": time.time()}), encoding="utf-8")
        temporary.replace(heartbeat)
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Синтетический тестовый продукт")
    parser.add_argument("action", choices=("migrate", "serve", "worker"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--release", choices=("1", "2"))
    parser.add_argument("--migration", choices=("schema-1", "schema-2"), default="schema-1")
    parser.add_argument("--fail", choices=("none", "before", "during", "after", "start", "health", "stop"), default="none")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--listen", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
                        help="0.0.0.0 только для изолированной контейнерной сети")
    parser.add_argument("--writer-lock", type=Path, help="абсолютный путь общего замка писателей Linux")
    args = parser.parse_args()
    release_file = Path(__file__).with_name("release.json")
    if release_file.exists():
        try:
            meta = json.loads(release_file.read_text(encoding="utf-8"))
            if type(meta) is not dict or set(meta) != {"release"} or meta["release"] not in ("1", "2"):
                raise ValueError("некорректные метаданные")
        except (OSError, ValueError, TypeError):
            parser.error("Некорректный файл версии тестового продукта")
        if args.release is not None and args.release != meta["release"]:
            parser.error("Запрошенная версия отличается от версии выпуска")
        args.release = meta["release"]
    else:
        args.release = args.release or "1"
    if os.name == "posix" and args.action in ("serve", "worker") and args.fail != "stop":
        # PID 1 in a container needs an explicit handler for graceful SIGTERM.
        def stop_process(signum, frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop_process)
    lease = nullcontext()
    if args.writer_lock is not None:
        from homensai_installer_client import CooperativeWriterLock
        lease = CooperativeWriterLock(str(args.writer_lock)).hold(exclusive=False)
    with lease:
        if args.action == "migrate":
            if args.release == "1" and args.migration == "schema-2":
                parser.error("Миграция schema-2 недоступна в версии 1")
            migrate(args.data_dir, args.migration, args.fail)
        elif args.action == "serve":
            serve(args.data_dir, args.release, args.port, args.fail, listen=args.listen)
        else:
            worker(args.data_dir, args.release, args.fail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
