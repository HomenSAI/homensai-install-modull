"""Настоящий reference SQLite ledger и HTTP, отдельно от installed приёмки."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "reference/src/homensai_reference"
SPEC = importlib.util.spec_from_file_location("acceptance_reference", SOURCE / "__init__.py",
                                            submodule_search_locations=[str(SOURCE)])
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
adapter = importlib.import_module(SPEC.name + ".adapter")
migrate = importlib.import_module(SPEC.name + ".migrate")


def request(hook, data=None, key="effect-1", digest="a" * 64):
    return {"protocol_version": "1.0", "product_id": "homensai-reference", "request_id": "request-1",
            "request_digest": digest, "operation_id": "op_1", "idempotency_key": key,
            "hook": hook, "phase": "diagnostic" if hook in {"readiness", "operation_status"} else "execute",
            "data": data or {}}


@pytest.fixture
def reference(tmp_path, monkeypatch):
    home, config, release = (tmp_path / name for name in ("data", "config", "release"))
    release.mkdir()
    migrate.migrate(home, config)
    monkeypatch.chdir(release)
    monkeypatch.setenv("HOMENSAI_DATA_HOME", str(home))
    (release / "adapter-config.json").write_text(json.dumps({"product_id": "homensai-reference", "port": 12345}))
    return home, config, release


def test_product_quiesce_has_durable_application_result_and_exact_replay(reference):
    home, _, _ = reference
    original = (home / "reference.sqlite").read_bytes()
    first = adapter.execute(request("quiesce"))
    assert first == {"quiesced": True, "active_writers": 0}
    assert adapter.execute(request("quiesce")) == first
    with sqlite3.connect(home / adapter.LEDGER) as db:
        assert db.execute("SELECT count(*) FROM effects").fetchone() == (1,)
    assert (home / "reference.sqlite").read_bytes() == original
    recovered = adapter.execute(request("operation_status", {"hook": "quiesce"}))
    assert recovered == {"outcome": "applied", "request_digest": "a" * 64, "result": first}


@pytest.mark.parametrize("kind", ["absent", "empty", "missing_effect", "foreign_digest", "foreign_hook"])
def test_unknown_application_result_never_proves_not_applied(reference, kind):
    home, _, _ = reference
    if kind == "empty":
        (home / adapter.LEDGER).touch(mode=0o600)
    elif kind != "absent":
        adapter.execute(request("quiesce"))
    kwargs = {"key": "missing"} if kind == "missing_effect" else {"digest": "b" * 64} if kind == "foreign_digest" else {}
    value = adapter.execute(request("operation_status", {"hook": "migration_apply" if kind == "foreign_hook" else "quiesce"}, **kwargs))
    assert value == {"outcome": "unknown", "request_digest": kwargs.get("digest", "a" * 64)}


def test_idempotency_key_cannot_change_product_effect_request(reference):
    adapter.execute(request("quiesce"))
    with pytest.raises(adapter.ReferenceProductError, match="REFERENCE_ADAPTER_EFFECT_CONFLICT"):
        adapter.execute(request("quiesce", digest="b" * 64))


def test_readiness_uses_real_loopback_observation_and_keeps_wrong_identity(reference):
    home, _, release = reference
    expected = {"product_id": "homensai-reference", "release": "reference-v1", "installation_id": "instance-1"}
    actual = dict(expected)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({**actual, "ready": True}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (release / "adapter-config.json").write_text(json.dumps({"product_id": "homensai-reference", "port": server.server_port}))
    original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir() if p.is_file()}
    try:
        data = {"expected_identity": expected, "probe_id": "fresh-probe-1"}
        observed = adapter.execute(request("readiness", data))
        assert observed["ready"] and observed["observed_identity"] == {**expected, "probe_id": "fresh-probe-1"}
        actual["release"] = "other-release"
        observed = adapter.execute(request("readiness", data))
        assert not observed["ready"] and observed["observed_identity"]["release"] == "other-release"
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir() if p.is_file()} == original
        assert not (home / adapter.LEDGER).exists()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_quiesce_rejects_corrupt_business_database_before_application_receipt(reference):
    home, _, _ = reference
    (home / "reference.sqlite").write_bytes(b"invalid SQLite")
    with pytest.raises(sqlite3.Error):
        adapter.execute(request("quiesce"))
    assert not (home / adapter.LEDGER).exists()
