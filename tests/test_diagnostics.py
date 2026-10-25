"""Исходниковые проверки диагностической устойчивости, без сети/реальных данных."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from homensai_install.diagnostics import DiagnosticStore
from homensai_install.errors import InstallerError, error_envelope


def test_operations_attempts_and_rollbacks_use_honest_denominators(tmp_path):
    store = DiagnosticStore(tmp_path / "diagnostics.sqlite")
    ok = store.start_operation("install", operation_id="install-1", product_id="reference", target="linux-native")
    store.start_operation("install", operation_id="install-1", product_id="reference", target="linux-native")
    attempt = store.start_attempt(ok, attempt_id="attempt-1")
    store.finish_attempt(attempt, "FAILED", duration_ms=12)
    attempt2 = store.start_attempt(ok, attempt_id="attempt-2")
    store.finish_attempt(attempt2, "SUCCEEDED", duration_ms=8)
    store.finish_operation(ok, "SUCCEEDED", duration_ms=20)
    failed = store.start_operation("update", operation_id="update-1", product_id="reference", target="linux-native")
    store.finish_operation(failed, "FAILED_ROLLED_BACK", duration_ms=35)
    cancelled = store.start_operation("install", operation_id="cancel-1")
    store.finish_operation(cancelled, "CANCELLED")
    waiting = store.start_operation("install", operation_id="waiting-1")
    store.finish_operation(waiting, "REBOOT_REQUIRED")
    stats = store.query_statistics()
    assert stats["total_operations"] == 4
    assert stats["total_attempts"] == 2 and stats["execution_retries"] == 1
    assert stats["successful_operations"] == 1 and stats["failed_operations"] == 1
    assert stats["cancelled_operations"] == 1
    assert stats["success_rate_all_operations"] == {"numerator": 1, "denominator": 4, "ratio": .25}
    assert stats["success_rate_technical_outcomes"]["denominator"] == 2
    assert stats["success_rate_technical_outcomes"]["ratio"] == .5
    assert stats["active_duration_ms"]["measured_operations"] == 2
    assert stats["active_duration_ms"]["sum"] == 55
    assert stats["data_complete"]
    filtered = store.query_statistics(product_id="reference", target="linux-native")
    assert filtered["total_operations"] == 2
    with pytest.raises(ValueError):
        store.query_statistics(since="2027-01-01T00:00:00Z", until="2026-01-01T00:00:00Z")


def test_deduplicated_events_do_not_inflate_download_or_phase_statistics(tmp_path):
    store = DiagnosticStore(tmp_path / "diag.db")
    operation = store.start_operation("install")
    event = {"event_id": "event-1", "sequence": 1, "type": "download.bytes", "bytes_delta": 42, "unknown_stdout": "PRIVATE"}
    store.record_event(operation, event)
    store.record_event(operation, event)
    store.record_event(operation, {"event_id": "event-2", "sequence": 2, "type": "download.retry"})
    store.record_event(operation, {"event_id": "event-3", "sequence": 3, "type": "download.cache_hit"})
    store.record_event(operation, {"event_id": "event-4", "sequence": 4, "type": "phase.completed", "phase": "verify", "duration_ms": 5})
    stats = store.query_statistics()
    assert stats["download_bytes"] == 42 and stats["download_retries"] == 1 and stats["cache_hits"] == 1
    assert stats["phase_duration_ms"] == {"verify": 5}
    assert len(store.get(operation)["events"]) == 4
    assert "PRIVATE" not in json.dumps(store.get(operation))
    with pytest.raises(ValueError, match="sequence"):
        store.record_event(operation, {"event_id": "other", "sequence": 1, "type": "event"})


def test_primary_error_survives_a_separate_rollback_error(tmp_path):
    store = DiagnosticStore(tmp_path / "diag.db")
    operation = store.start_operation("update")
    primary = error_envelope("MIGRATION_FAILED", error_id="primary", cause_status="suspected", evidence=[{"check": "migration_exit", "observed": 3}])
    store.record_error(operation, primary)
    rollback = error_envelope("ROLLBACK_FAILED", error_id="rollback", parent_error_id="primary")
    store.record_error(operation, rollback)
    store.record_error(operation, rollback)
    values = {item["error_id"]: item for item in store.get(operation)["errors"]}
    assert len(values) == 2 and values["rollback"]["parent_error_id"] == "primary"
    assert values["primary"]["cause_status"] == "suspected"
    assert store.query_statistics()["errors_by_code"]["ROLLBACK_FAILED"] == {"occurrences": 1, "affected_operations": 1}


def test_corrupt_database_is_preserved_and_fallback_is_readable(tmp_path):
    path = tmp_path / "diag.db"
    corruption = b"synthetic-corrupted-SQLite-evidence"
    path.write_bytes(corruption)
    store = DiagnosticStore(path)
    operation = store.start_operation("install", operation_id="fallback-op")
    result = store.record_event(operation, {"event_id": "fallback-event", "sequence": 1, "type": "phase.completed", "phase": "detect"})
    assert result["stored"] == "fallback"
    assert path.read_bytes() == corruption
    report = store.get(operation)
    assert report["operation"]["operation_id"] == operation
    assert report["diagnostic_state"]["source"] == "fallback"
    assert not store.query_statistics()["data_complete"]
    preserved = path.with_suffix(".corrupt-evidence")
    path.rename(preserved)
    imported = store.import_fallback()
    assert imported["available"] and imported["imported"] == 2
    assert store.get(operation)["events"][0]["event_id"] == "fallback-event"
    assert preserved.read_bytes() == corruption
    assert store.import_fallback()["imported"] == 0


def test_replay_after_commit_before_cleanup_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "diag.db"
    path.write_bytes(b"corrupt")
    store = DiagnosticStore(path)
    operation = store.start_operation("install")
    store.record_event(operation, {"event_id": "stable", "sequence": 1, "type": "download.bytes", "bytes_delta": 7})
    path.rename(path.with_suffix(".evidence"))
    original = store._replace_fallback

    def fail_cleanup(content):
        if content == b"":
            raise OSError("synthetic crash after DB commit")
        original(content)

    monkeypatch.setattr(store, "_replace_fallback", fail_cleanup)
    assert not store.import_fallback()["available"]
    monkeypatch.setattr(store, "_replace_fallback", original)
    result = store.import_fallback()
    assert result["duplicates"] == 2 and result["imported"] == 0
    assert store.query_statistics()["download_bytes"] == 7


def test_bounded_fallback_reports_data_loss_and_cumulative_episodes(tmp_path, monkeypatch):
    path = tmp_path / "diag.db"
    store = DiagnosticStore(path, max_fallback_bytes=4096, max_record_bytes=1024)
    operation = store.start_operation("install", operation_id="overflow-op")
    original_connection = store._connection

    @contextmanager
    def unavailable():
        raise OSError("synthetic SQLite outage")
        yield

    monkeypatch.setattr(store, "_connection", unavailable)
    for sequence in range(30):
        store.record_event(operation, {"sequence": sequence, "type": "download.bytes", "bytes_delta": 1})
    assert store.fallback_path.stat().st_size <= 4096
    first_count = store.query_statistics()["diagnostic_record_loss_count"]
    assert first_count > 0
    monkeypatch.setattr(store, "_connection", original_connection)
    assert store.import_fallback()["available"]
    assert store.query_statistics()["diagnostic_record_loss_count"] == first_count
    monkeypatch.setattr(store, "_connection", unavailable)
    for sequence in range(40, 70):
        store.record_event(operation, {"sequence": sequence, "type": "download.bytes", "bytes_delta": 1})
    second_count = store.query_statistics()["diagnostic_record_loss_count"]
    monkeypatch.setattr(store, "_connection", original_connection)
    assert store.import_fallback()["available"]
    assert store.query_statistics()["diagnostic_record_loss_count"] == first_count + second_count
    assert not store.query_statistics()["data_complete"]


def test_both_stores_unavailable_do_not_recurse(tmp_path, monkeypatch):
    store = DiagnosticStore(tmp_path / "diag.db")

    @contextmanager
    def unavailable():
        raise OSError("synthetic unavailable storage")
        yield

    monkeypatch.setattr(store, "_connection", unavailable)
    monkeypatch.setattr(store, "_fallback_lock", unavailable)
    result = store.record_event("operation", {"event_id": "event", "type": "event"})
    assert result["stored"] == "unavailable"
    assert store._unpersisted == 1


def test_secret_redaction_and_export_allowlist(tmp_path):
    store = DiagnosticStore(tmp_path / "diag.db", known_secrets=("synthetic-known-secret",))
    operation = store.start_operation("install", metadata={"token": "token-to-hide", "arbitrary_personal_text": "private biography"})
    store.record_event(operation, {"event_id": "one", "type": "event", "stdout": "private stdout", "error": {"password": "pass-to-hide"}})
    store.record_error(operation, error_envelope("PORT_IN_USE", details={"api_key": "key-to-hide", "note": "synthetic-known-secret", "arbitrary_text": "personal text"}))
    data = json.dumps(store.get(operation), ensure_ascii=False)
    for secret in ("token-to-hide", "pass-to-hide", "key-to-hide", "synthetic-known-secret", "private stdout"):
        assert secret not in data
    preview = store.export_preview(operation)
    exported = json.dumps(preview)
    for private in ("private biography", "personal text", "private stdout"):
        assert private not in exported
    assert preview["requires_confirmation"]
    with pytest.raises(InstallerError) as exc:
        store.export(tmp_path / "support.json", operation_id=operation)
    assert exc.value.code == "CONSENT_REQUIRED"
    result = store.export(tmp_path / "support.json", operation_id=operation, confirmed=True, expected_digest=preview["sha256"])
    assert result["uploaded"] is False
    assert json.loads((tmp_path / "support.json").read_text())["operations"]
    with pytest.raises(FileExistsError):
        store.export(tmp_path / "support.json", operation_id=operation, confirmed=True)


def test_export_preview_change_requires_new_confirmation(tmp_path):
    store = DiagnosticStore(tmp_path / "diag.db")
    operation = store.start_operation("install")
    preview = store.export_preview(operation)
    store.record_event(operation, {"type": "event"})
    with pytest.raises(InstallerError) as exc:
        store.export(tmp_path / "new.json", operation_id=operation, confirmed=True, expected_digest=preview["sha256"])
    assert exc.value.code == "PLAN_STALE"
    assert not (tmp_path / "new.json").exists()


def test_foreign_database_is_never_replaced(tmp_path):
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE user_data (value TEXT)")
        connection.execute("INSERT INTO user_data VALUES ('synthetic-user-value')")
    store = DiagnosticStore(path)
    store.start_operation("install")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM user_data").fetchone()[0] == "synthetic-user-value"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='operations'").fetchone()
    assert store.last_warning == "DIAGNOSTIC_STORE_UNAVAILABLE"


def test_retention_preserves_active_operations_and_marks_incomplete_events(tmp_path):
    store = DiagnosticStore(tmp_path / "diag.db")
    finished = store.start_operation("install", operation_id="finished")
    active = store.start_operation("install", operation_id="active")
    for sequence in range(10):
        store.record_event(finished, {"sequence": sequence, "type": "event"})
    store.record_event(active, {"sequence": 1, "type": "event"})
    store.finish_operation(finished, "SUCCEEDED")
    result = store.prune(max_events=2)
    assert result["pruned"] and result["events"] == 9
    assert len(store.get(active)["events"]) == 1
    assert store.get(active)["operation"]["status"] == "RUNNING"
    assert not store.query_statistics()["data_complete"]


def test_unmeasured_active_time_is_unknown_after_restart(tmp_path):
    path = tmp_path / "diag.db"
    store = DiagnosticStore(path)
    operation = store.start_operation("install")
    store.finish_operation(operation, "REBOOT_REQUIRED")
    resumed = DiagnosticStore(path)
    resumed.start_operation("install", operation_id=operation)
    assert resumed.get(operation)["operation"]["status"] == "RUNNING"
    resumed.finish_operation(operation, "SUCCEEDED")
    assert resumed.get(operation)["operation"]["duration_ms"] is None
    assert resumed.query_statistics()["active_duration_ms"]["measured_operations"] == 0


def test_public_api_target_shape_and_cancel_spelling(tmp_path):
    store = DiagnosticStore(tmp_path / "diag.db")
    target = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
    operation = store.start_operation("install", target=json.dumps(target), product_version="1.2.0")
    store.finish_operation(operation, "CANCELED")
    selected = store.query_statistics(target=target, product_version="1.2.0", action="install")
    assert selected["total_operations"] == 1 and selected["cancelled_operations"] == 1
    assert selected["success_rate_technical_outcomes"]["denominator"] == 0
    with pytest.raises(ValueError):
        store.start_operation("install", target={"environment": {"secret": "no"}})


def test_symlink_database_does_not_write_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"private unrelated bytes")
    path = tmp_path / "diag.db"
    path.symlink_to(target)
    store = DiagnosticStore(path)
    store.start_operation("install")
    assert target.read_bytes() == b"private unrelated bytes"
    assert store.last_warning == "DIAGNOSTIC_STORE_UNAVAILABLE"


def test_two_fallback_writers_keep_each_others_records(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "diag.db"
    path.write_bytes(b"synthetic corruption")
    stores = [DiagnosticStore(path), DiagnosticStore(path)]

    def write(index):
        operation = stores[index].start_operation("install", operation_id="parallel-" + str(index))
        for sequence in range(10):
            result = stores[index].record_event(operation, {"sequence": sequence, "type": "download.bytes", "bytes_delta": 1})
            assert result["stored"] == "fallback"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, (0, 1)))
    path.rename(path.with_suffix(".preserved"))
    assert stores[0].import_fallback()["available"]
    statistics = stores[0].query_statistics()
    assert statistics["total_operations"] == 2 and statistics["download_bytes"] == 20
