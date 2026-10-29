"""Исполняемый ProductAdapter reference: реальные HTTP/SQLite, без shell.

Reference server обслуживает только чтение. Поэтому quiesce подтверждает ноль
продуктовых писателей; остановкой процесса и полным архивом управляет SDK.
Application ledger хранится отдельно от SQLite бизнес-данных и не разрешает
повтор неизвестного эффекта при отсутствии доказательства результата.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .archive import verify_database
from .migrate import _effect_result, migrate, validate_schema
from .profile import ReferenceProductError, load_profile, read_json, safe_path
from .storage import WELCOME_FILE, load_settings


VERSION = "0.1.0"
HOOKS = ("describe", "migration_apply", "migration_plan", "operation_status", "post_restore", "quiesce", "readiness", "restore")
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
    if request["data"].get("hook") == "migration_apply":
        return _migration_operation_status(request, home)
    if request["data"].get("hook") in {"restore", "post_restore"}:
        return _recovery_operation_status(request, home)
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


@contextmanager
def _business_connection(home):
    database = safe_path(home / "reference.sqlite")
    info = database.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name != "nt" and info.st_uid != os.getuid())):
        raise ReferenceProductError("REFERENCE_DATABASE_FILE_INVALID")
    # mode=ro тоже создаёт -wal/-shm для SQLite WAL. Проверяем режим до open.
    if any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ReferenceProductError("REFERENCE_MIGRATION_DATABASE_BUSY")
    with database.open("rb") as stream:
        header = stream.read(100)
    if (len(header) < 100 or header[:16] != b"SQLite format 3\0"
            or header[18:20] != b"\x01\x01"):
        raise ReferenceProductError("REFERENCE_MIGRATION_DATABASE_STATE_UNSUPPORTED")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        yield connection
    finally:
        connection.close()


def _migration_plan(request, home):
    """Наблюдение схемы без инициализации БД, sidecars или application ledger."""
    data = request["data"]
    if (set(data) != {"installation_id", "from_release", "to_release", "mode"}
            or type(data["installation_id"]) is not str or not data["installation_id"]
            or type(data["to_release"]) is not str or not data["to_release"]
            or data["mode"] not in {"fresh_empty", "upgrade"}):
        raise ReferenceProductError("REFERENCE_MIGRATION_REQUEST_INVALID")
    if data["mode"] == "fresh_empty":
        if data["from_release"] is not None or home.exists() and (not home.is_dir() or any(home.iterdir())):
            raise ReferenceProductError("REFERENCE_FRESH_DATA_NOT_EMPTY")
        current = 0
    else:
        if type(data["from_release"]) is not str or not data["from_release"]:
            raise ReferenceProductError("REFERENCE_MIGRATION_REQUEST_INVALID")
        with _business_connection(home) as connection:
            connection.execute("BEGIN")
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            validate_schema(connection, current)
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ReferenceProductError("REFERENCE_DATABASE_INTEGRITY_FAILED")
    target = load_profile()["database_schema"]
    if current > target:
        raise ReferenceProductError("REFERENCE_DOWNGRADE_REQUIRES_SNAPSHOT_RESTORE")
    names = {1: "create_notes", 2: "normalize_notes", 3: "hash_and_search_terms"}
    result = {"from_version": str(current) if current else "empty", "to_version": str(target),
              "steps": [{"id": names[version], "backup_required": bool(current), "reversible": False}
                        for version in range(current + 1, target + 1)]}
    result["plan_digest"] = hashlib.sha256(canonical(result).encode("utf-8")).hexdigest()
    return result


def _migration_identity(request):
    data = request["data"]
    binding_keys = {"migration_review_id", "binding_sha256", "installation_id", "from_release",
                    "to_release", "from_version", "to_version"}
    receipt_keys = set(data) & {"backup_receipt_id", "fresh_receipt_id"}
    if (len(receipt_keys) != 1 or set(data) != {"plan_digest", "migration_binding", *receipt_keys}
            or type(data.get("migration_binding")) is not dict
            or set(data["migration_binding"]) != binding_keys):
        raise ReferenceProductError("REFERENCE_MIGRATION_REQUEST_INVALID")
    binding = data["migration_binding"]
    hashes = (data["plan_digest"], data[next(iter(receipt_keys))], binding["binding_sha256"])
    if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
        raise ReferenceProductError("REFERENCE_MIGRATION_REQUEST_INVALID")
    if (any(type(binding[key]) is not str or not binding[key] for key in
            ("migration_review_id", "installation_id", "to_release", "from_version", "to_version"))
            or binding["to_version"] not in {"1", "2", "3"}):
        raise ReferenceProductError("REFERENCE_MIGRATION_REQUEST_INVALID")
    fresh = "fresh_receipt_id" in data
    if ((fresh and (binding["from_release"] is not None or binding["from_version"] != "empty"))
            or (not fresh and (type(binding["from_release"]) is not str or not binding["from_release"]
                              or binding["from_version"] not in {"1", "2", "3"}))):
        raise ReferenceProductError("REFERENCE_MIGRATION_REQUEST_INVALID")
    identity = hashlib.sha256(canonical({"product_id": request["product_id"], "data": data}).encode()).hexdigest()
    receipt = hashlib.sha256(canonical({"effect_key": request["idempotency_key"], "identity": identity,
                                        "request_digest": request["request_digest"]}).encode()).hexdigest()
    result = {"applied": True, "receipt_id": receipt, "state_version": binding["to_version"]}
    return {"effect_key": request["idempotency_key"], "request_digest": request["request_digest"],
            "identity_sha256": identity, "result": result}


def _config_home():
    return safe_path(Path(os.environ["HOMENSAI_CONFIG_HOME"]))


def _required_files_ready(home, config):
    try:
        for path in (safe_path(home / WELCOME_FILE), safe_path(config / "settings.json")):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
        load_settings(config)
        return True
    except (OSError, ReferenceProductError):
        return False


def _read_business_effect(home, effect):
    path = safe_path(home / "reference.sqlite")
    if not path.exists():
        return None
    with _business_connection(home) as connection:
        result = _effect_result(connection, effect)
        if result is None:
            return None
        schema = connection.execute("PRAGMA user_version").fetchone()[0]
        validate_schema(connection, schema)
        if (result != effect["result"] or str(schema) != result["state_version"]
                or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]):
            raise ReferenceProductError("REFERENCE_MIGRATION_OUTCOME_UNKNOWN")
        return result


def _migration_intent(home, effect, *, create=False, complete=False):
    if not create and not (home / LEDGER).exists():
        return None
    with _connection(home, create=create) as db:
        if create:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS migration_intents(effect_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL, identity_sha256 TEXT NOT NULL, state TEXT NOT NULL)")
        elif not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='migration_intents'").fetchone():
            return None
        row = db.execute("SELECT request_digest,identity_sha256,state FROM migration_intents WHERE effect_key=?",
                         (effect["effect_key"],)).fetchone()
        if row is not None and row[:2] != (effect["request_digest"], effect["identity_sha256"]):
            raise ReferenceProductError("REFERENCE_ADAPTER_EFFECT_CONFLICT")
        if create and row is None:
            db.execute("INSERT INTO migration_intents VALUES (?,?,?,'STARTED')",
                       (effect["effect_key"], effect["request_digest"], effect["identity_sha256"]))
        if complete:
            db.execute("UPDATE migration_intents SET state='COMPLETE' WHERE effect_key=?", (effect["effect_key"],))
        if create:
            db.execute("COMMIT")
        return row


def _migration_apply(request, home):
    # Receipt ID не является proof сам по себе. Engine trusted authorizer
    # проверяет реальный архив/fresh evidence до запуска этого executable.
    effect = _migration_identity(request)
    config = _config_home()
    binding = request["data"]["migration_binding"]
    previous = _read_business_effect(home, effect)
    if previous is not None:
        if not _required_files_ready(home, config):
            raise ReferenceProductError("REFERENCE_MIGRATION_OUTCOME_UNKNOWN")
        return previous
    if _migration_intent(home, effect) is not None:
        raise ReferenceProductError("REFERENCE_MIGRATION_OUTCOME_UNKNOWN")
    observed = _migration_plan({"data": {key: binding[key] for key in
                                ("installation_id", "from_release", "to_release")}
                               | {"mode": "fresh_empty" if binding["from_version"] == "empty" else "upgrade"}}, home)
    if (observed["plan_digest"] != request["data"]["plan_digest"]
            or observed["from_version"] != binding["from_version"] or observed["to_version"] != binding["to_version"]):
        raise ReferenceProductError("REFERENCE_MIGRATION_PLAN_STALE")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _migration_intent(home, effect, create=True)
    migrate(home, config, _effect=effect)
    if _read_business_effect(home, effect) != effect["result"] or not _required_files_ready(home, config):
        raise ReferenceProductError("REFERENCE_MIGRATION_OUTCOME_UNKNOWN")
    _migration_intent(home, effect, create=True, complete=True)
    return effect["result"]


def _migration_operation_status(request, home):
    unknown = {"outcome": "unknown", "request_digest": request["request_digest"]}
    # Reconciliation envelope содержит только исходный digest и hook. Identity
    # и результат берём из атомарно зафиксированного business effect, а не из
    # отсутствующей строки отдельного application ledger.
    path = safe_path(home / "reference.sqlite")
    if not path.exists():
        return unknown
    try:
        with _business_connection(home) as connection:
            if not connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_adapter_effects'").fetchone():
                return unknown
            row = connection.execute("SELECT request_digest,identity_sha256,result FROM product_adapter_effects WHERE effect_key=?",
                                     (request["idempotency_key"],)).fetchone()
            if row is None or row[0] != request["request_digest"]:
                return unknown
            result = json.loads(row[2])
            receipt = hashlib.sha256(canonical({"effect_key": request["idempotency_key"], "identity": row[1],
                                                "request_digest": request["request_digest"]}).encode()).hexdigest()
            if (type(result) is not dict or set(result) != {"applied", "receipt_id", "state_version"}
                    or result["applied"] is not True or result["receipt_id"] != receipt):
                return unknown
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            validate_schema(connection, schema)
            if (result["state_version"] != str(schema)
                    or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                    or not _required_files_ready(home, _config_home())):
                return unknown
            return {"outcome": "applied", "request_digest": row[0], "result": result}
    except (OSError, sqlite3.Error, ReferenceProductError, ValueError, KeyError, TypeError):
        return unknown


def _recovery_state(home):
    selected = load_profile()
    with _business_connection(home) as connection:
        schema = connection.execute("PRAGMA user_version").fetchone()[0]
        validate_schema(connection, schema)
        if (schema != selected["database_schema"]
                or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                or not _required_files_ready(home, _config_home())):
            raise ReferenceProductError("REFERENCE_RECOVERY_STATE_INVALID")
    return str(schema)


def _recovery_result(request, state_version):
    identity = {key: request[key] for key in ("operation_id", "idempotency_key", "request_digest", "hook", "data")}
    receipt_id = hashlib.sha256(canonical(identity).encode()).hexdigest()
    return {"applied": True, "receipt_id": receipt_id, "state_version": state_version}


def _recover(request, home):
    # SDK уже восстановил bytes и проверил настоящий before-recovery receipt.
    # Product hook проверяет восстановленную схему/файлы, затем атомарно сохраняет
    # receipt и hold. ID receipt не подменяет trusted authorizer SDK.
    data = request["data"]
    if (set(data) != {"installation_id", "release", "recovery_action", "source_archive_sha256", "before_recovery_receipt_id"}
            or data["recovery_action"] not in {"restore", "rollback"}
            or any(type(data[key]) is not str or not data[key] for key in ("installation_id", "release"))
            or any(type(data[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", data[key]) is None
                   for key in ("source_archive_sha256", "before_recovery_receipt_id"))):
        raise ReferenceProductError("REFERENCE_RECOVERY_REQUEST_INVALID")
    schema = _recovery_state(home)
    result = _recovery_result(request, schema)
    with _connection(home, create=True) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS recovery_effects (effect_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL, hook TEXT NOT NULL, operation_id TEXT NOT NULL, binding TEXT NOT NULL, result TEXT NOT NULL, external_actions_on_hold INTEGER NOT NULL CHECK(external_actions_on_hold=1))")
        old = db.execute("SELECT request_digest,hook,operation_id,binding,result,external_actions_on_hold FROM recovery_effects WHERE effect_key=?",
                         (request["idempotency_key"],)).fetchone()
        if old is not None:
            if old != (request["request_digest"], request["hook"], request["operation_id"], canonical(data), canonical(result), 1):
                raise ReferenceProductError("REFERENCE_ADAPTER_EFFECT_CONFLICT")
            return json.loads(old[4])
        if request["hook"] == "post_restore":
            restored = db.execute("SELECT 1 FROM recovery_effects WHERE operation_id=? AND hook='restore' AND binding=? AND external_actions_on_hold=1",
                                  (request["operation_id"], canonical(data))).fetchone()
            if restored is None:
                raise ReferenceProductError("REFERENCE_RECOVERY_RESTORE_NOT_CONFIRMED")
        db.execute("INSERT INTO recovery_effects VALUES (?,?,?,?,?,?,1)", (request["idempotency_key"], request["request_digest"],
                   request["hook"], request["operation_id"], canonical(data), canonical(result)))
        db.execute("COMMIT")
    # Reference не имеет внешних scheduler/email/payment действий. Durable hold
    # намеренно не снимается ни restore, ни post_restore, ни operation_status.
    return result


def _recovery_operation_status(request, home):
    unknown = {"outcome": "unknown", "request_digest": request["request_digest"]}
    try:
        schema = _recovery_state(home)
        with _connection(home) as db:
            row = db.execute("SELECT request_digest,hook,operation_id,binding,result,external_actions_on_hold FROM recovery_effects WHERE effect_key=?",
                             (request["idempotency_key"],)).fetchone()
        if (row is None or row[:3] != (request["request_digest"], request["data"]["hook"], request["operation_id"])
                or row[5] != 1):
            return unknown
        result = json.loads(row[4])
        expected = _recovery_result({**request, "hook": row[1], "data": json.loads(row[3])}, schema)
        if result != expected:
            return unknown
        return {"outcome": "applied", "request_digest": row[0], "result": result}
    except (OSError, sqlite3.Error, ReferenceProductError, ValueError, KeyError, TypeError):
        return unknown


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
        descriptor = read_json(Path.cwd() / "installer/adapter.json")
        capabilities = descriptor["capabilities"]
        if type(capabilities) is not dict or set(capabilities) - set(HOOKS) or descriptor["adapter_version"] != VERSION:
            raise ReferenceProductError("REFERENCE_ADAPTER_CONFIG_INVALID")
        return {"protocol_version": "1.0", "product_id": config["product_id"],
                "adapter_version": VERSION, "capabilities": sorted(capabilities)}
    if hook == "readiness":
        return _readiness(request, config)
    home = safe_path(Path(os.environ["HOMENSAI_DATA_HOME"]))
    if hook == "migration_plan":
        return _migration_plan(request, home)
    if hook == "migration_apply":
        return _migration_apply(request, home)
    if hook == "operation_status":
        return _operation_status(request, home)
    if hook in {"restore", "post_restore"}:
        return _recover(request, home)
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
