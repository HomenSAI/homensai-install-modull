"""Исполняемый ProductAdapter reference: реальные HTTP/SQLite, без shell.

Reference server обслуживает только чтение. Поэтому quiesce подтверждает ноль
продуктовых писателей; остановкой процесса и полным архивом управляет SDK.
Application ledger хранится отдельно от SQLite бизнес-данных и не разрешает
повтор неизвестного эффекта при отсутствии доказательства результата.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .archive import verify_database
from .profile import ReferenceProductError, read_json, safe_path


VERSION = "0.1.0"
HOOKS = ("describe", "operation_status", "quiesce", "readiness")
MAX_BYTES = 256 * 1024
LEDGER = "reference-adapter.sqlite3"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _file(path, *, create=False):
    path = safe_path(path)
    if create:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
        raise ReferenceProductError("REFERENCE_ADAPTER_LEDGER_INVALID")
    return path


@contextmanager
def _connection(home, *, create=False):
    path = _file(home / LEDGER, create=create)
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if create else "?mode=ro"),
                                 uri=True, timeout=5, isolation_level=None)
    try:
        if create:
            connection.execute("PRAGMA synchronous=FULL")
        with connection:
            yield connection
    finally:
        connection.close()


def _quiesce(request, home):
    # У продукта нет изменяющего HTTP API, scheduler, worker или внешних effects.
    verify_database(home / "reference.sqlite")
    result = {"quiesced": True, "active_writers": 0}
    with _connection(home, create=True) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS effects (effect_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL, hook TEXT NOT NULL, result TEXT NOT NULL)")
        old = db.execute("SELECT request_digest,hook,result FROM effects WHERE effect_key=?",
                         (request["idempotency_key"],)).fetchone()
        if old is not None:
            if old[:2] != (request["request_digest"], request["hook"]):
                raise ReferenceProductError("REFERENCE_ADAPTER_EFFECT_CONFLICT")
            return json.loads(old[2])
        db.execute("INSERT INTO effects VALUES (?,?,?,?)", (request["idempotency_key"],
                   request["request_digest"], request["hook"], canonical(result)))
        db.execute("COMMIT")
    return result


def _operation_status(request, home):
    unknown = {"outcome": "unknown", "request_digest": request["request_digest"]}
    try:
        with _connection(home) as db:
            row = db.execute("SELECT request_digest,hook,result FROM effects WHERE effect_key=?",
                             (request["idempotency_key"],)).fetchone()
    except (FileNotFoundError, sqlite3.Error):
        return unknown
    if (row is None or row[0] != request["request_digest"]
            or row[1] != request["data"].get("hook")):
        return unknown
    return {"outcome": "applied", "request_digest": row[0], "result": json.loads(row[2])}


def _readiness(request, config):
    expected = request["data"]["expected_identity"]
    probe_id = request["data"]["probe_id"]
    url = "http://127.0.0.1:" + str(config["port"]) + "/healthz"
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(url, headers={"Cache-Control": "no-cache"}), timeout=2) as reply:
            observed = json.loads(reply.read(MAX_BYTES + 1))
    except HTTPError as error:
        if error.code != 503:
            raise
        with error:
            observed = json.loads(error.read(MAX_BYTES + 1))
    identity = {key: observed[key] for key in ("product_id", "release", "installation_id")}
    valid = identity == expected and identity["product_id"] == config["product_id"]
    ready = valid and observed.get("ready") is True
    return {"ready": ready, "checks": [{"id": "live-reference-http", "status": "ok" if ready else "failed"}],
            "observed_identity": {**identity, "probe_id": probe_id}}


def execute(request):
    if (type(request) is not dict or request.get("protocol_version") != "1.0"
            or request.get("hook") not in HOOKS or type(request.get("data")) is not dict):
        raise ReferenceProductError("REFERENCE_ADAPTER_REQUEST_INVALID")
    config = read_json(Path.cwd() / "adapter-config.json")
    if (set(config) != {"product_id", "port"} or config["product_id"] != request["product_id"]
            or type(config["port"]) is not int or not 1024 <= config["port"] <= 65535):
        raise ReferenceProductError("REFERENCE_ADAPTER_CONFIG_INVALID")
    hook = request["hook"]
    if hook == "describe":
        return {"protocol_version": "1.0", "product_id": config["product_id"],
                "adapter_version": VERSION, "capabilities": sorted(HOOKS)}
    if hook == "readiness":
        return _readiness(request, config)
    home = safe_path(Path(os.environ["HOMENSAI_DATA_HOME"]))
    if hook == "operation_status":
        return _operation_status(request, home)
    return _quiesce(request, home)


def main():
    request = None
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ReferenceProductError("REFERENCE_ADAPTER_REQUEST_INVALID")
        request = json.loads(raw)
        result = execute(request)
        response = {key: request[key] for key in ("protocol_version", "product_id", "request_id", "request_digest", "hook")}
        response.update(status="ok", result=result)
        print(canonical(response))
        return 0
    except (ReferenceProductError, OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        # Никаких значений данных, путей, argv, SQL и traceback в протоколе.
        print(canonical({"error": "REFERENCE_ADAPTER_FAILED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
