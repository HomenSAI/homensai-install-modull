"""CI summary подтверждает текущие installed receipts и не выводит их raw values."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installed_check_summary", ROOT / "tools/summarize_installed_checks.py")
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)
REVISION = "a" * 40


@pytest.fixture
def receipts(tmp_path):
    for index, (filename, _, source_field) in enumerate(summary.CHECKS):
        value = {"schema_version": 1, "status": "PASS", source_field: REVISION,
                 "wheel": {"sha256": str(index + 1) * 64}}
        if filename == "reference-update.json":
            value["profile"] = "update"
        (tmp_path / filename).write_text(json.dumps(value), encoding="utf-8")
    return tmp_path


def run(receipts, *extra):
    return summary.main(["--receipts", str(receipts), "--source-revision", REVISION, *extra])


def change(receipts, filename, patch):
    path = receipts / filename
    value = json.loads(path.read_text())
    value.update(patch)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_three_current_producer_shapes_pass_and_append_summary(receipts, capsys):
    destination = receipts / "step-summary.md"
    destination.write_text("Earlier step\n", encoding="utf-8")
    # Extra artifacts are ignored; exactly the three known receipts are read.
    (receipts / "other.json").write_text("not valid json", encoding="utf-8")
    assert run(receipts, "--summary", str(destination)) == 0
    output = capsys.readouterr()
    assert not output.err and output.out.count("| PASS |") == 3
    assert REVISION in output.out and all(str(i) * 64 in output.out for i in (1, 2, 3))
    assert destination.read_text() == "Earlier step\n\n" + output.out
    assert "production" in output.out


@pytest.mark.parametrize("filename", [entry[0] for entry in summary.CHECKS])
def test_missing_receipt_fails_even_when_other_profiles_pass(receipts, capsys, filename):
    (receipts / filename).unlink()
    assert run(receipts) == 1
    output = capsys.readouterr().out
    assert "Квитанция отсутствует." in output and output.count("| PASS |") == 2


@pytest.mark.parametrize("status", ["FAIL", "BLOCKED", "NOT RUN", None, "PASS\n| injected |"])
def test_nonpass_receipt_fails_without_echoing_status_or_error(receipts, capsys, status):
    change(receipts, "sdk.json", {"status": status, "error": "SECRET_RAW_ERROR<script>"})
    assert run(receipts) == 1
    output = capsys.readouterr().out
    assert "Статус проверки не PASS." in output
    assert "SECRET_RAW_ERROR" not in output and "injected" not in output


@pytest.mark.parametrize("filename,source_field", [(entry[0], entry[2]) for entry in summary.CHECKS])
def test_stale_or_wrong_source_field_does_not_pass(receipts, capsys, filename, source_field):
    change(receipts, filename, {source_field: "b" * 40, "source_revision": REVISION})
    assert run(receipts) == 1
    assert "Ревизия исходников не совпадает." in capsys.readouterr().out


@pytest.mark.parametrize("payload", ["{", "[]", 'NaN', '{"schema_version":1,"status":"PASS","status":"FAIL"}',
                                      '{"schema_version":true}', b"\xff"])
def test_malformed_receipt_fails_without_traceback(receipts, capsys, payload):
    (receipts / "sdk.json").write_bytes(payload if isinstance(payload, bytes) else payload.encode())
    assert run(receipts) == 1
    output = capsys.readouterr()
    assert "FAIL:" in output.out and not output.err


@pytest.mark.parametrize("wheel", [None, {}, {"sha256": "a" * 63}, {"sha256": "<script>alert(1)</script>"},
                                    {"sha256": 123}, {"sha256": "A" * 64}])
def test_invalid_wheel_digest_is_not_rendered(receipts, capsys, wheel):
    change(receipts, "sdk.json", {"wheel": wheel})
    assert run(receipts) == 1
    output = capsys.readouterr().out
    assert "Некорректный SHA-256 wheel." in output and "script" not in output


def test_preparation_receipt_cannot_substitute_for_update_acceptance(receipts, capsys):
    change(receipts, "reference-update.json", {"profile": "preparation"})
    assert run(receipts) == 1
    assert "Не подтверждён профиль последовательных updates." in capsys.readouterr().out


def test_invalid_expected_revision_is_not_printed(receipts, capsys):
    assert summary.main(["--receipts", str(receipts), "--source-revision", "<script>RAW_REVISION</script>"]) == 1
    output = capsys.readouterr().out
    assert "RAW_REVISION" not in output and output.count("FAIL:") == 3


def test_summary_write_failure_has_fixed_error(receipts, capsys):
    assert run(receipts, "--summary", str(receipts / "missing" / "summary.md")) == 1
    assert capsys.readouterr().err == "Не удалось записать сводку проверок.\n"


def test_receipt_size_is_bounded(receipts, capsys, monkeypatch):
    monkeypatch.setattr(summary, "MAX_RECEIPT_BYTES", 4)
    assert run(receipts) == 1
    assert capsys.readouterr().out.count("Превышен размер квитанции.") == 3
