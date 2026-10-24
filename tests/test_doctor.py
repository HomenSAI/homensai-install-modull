"""Диагностика только вымышленных внедрённых фактов, без проб ОС/БД."""

from copy import deepcopy
import json

import pytest

from shared import ctl
from shared.doctor import DoctorError, crash_loop, evaluate_health, rate_limit_alert


@pytest.fixture
def facts():
    return {"now": 10000, "db_opens": True, "schema_current": True,
            "data_dir_writable": True, "free_disk_bytes": 101, "min_free_disk_bytes": 100,
            "components": {"worker": {"heartbeat_age_seconds": 180, "failure_timestamps": []}},
            "last_archive_age_seconds": 172800, "archive_schedule_seconds": 86400}


def codes(facts):
    report = evaluate_health(facts)
    assert report["exit_code"] == int(not report["ok"])
    return {item["code"] for item in report["checks"] if not item["ok"]}


def test_healthy_deterministic_and_pure(facts):
    before = deepcopy(facts)
    report = evaluate_health(facts)
    assert report == evaluate_health(facts) and facts == before
    assert report["exit_code"] == 0 and report["ok"] is True
    assert [item["code"] for item in report["checks"]] == [
        "DB_OK", "SCHEMA_OK", "DATA_DIR_OK", "DISK_OK", "HEARTBEAT_OK", "CRASH_OK", "ARCHIVE_OK"]
    assert all(any("а" <= c <= "я" for c in item["message"]) for item in report["checks"])
    assert json.loads(json.dumps(report)) == report


@pytest.mark.parametrize("key,code", [("db_opens", "DB_FAILED"),
    ("schema_current", "SCHEMA_FAILED"), ("data_dir_writable", "DATA_DIR_FAILED")])
def test_boolean_failures(facts, key, code):
    facts[key] = False
    assert codes(facts) == {code}


@pytest.mark.parametrize("free,expected", [(0, {"DISK_LOW"}), (99, {"DISK_LOW"}),
    (100, {"DISK_LOW"}), (101, set())])
def test_disk_strict_threshold(facts, free, expected):
    facts["free_disk_bytes"] = free
    assert codes(facts) == expected


@pytest.mark.parametrize("age,expected", [(None, {"HEARTBEAT_MISSING"}), (0, set()),
    (179.9, set()), (180, set()), (180.001, {"HEARTBEAT_STALE"})])
def test_heartbeat_boundaries(facts, age, expected):
    facts["components"]["worker"]["heartbeat_age_seconds"] = age
    assert codes(facts) == expected


@pytest.mark.parametrize("age,expected", [(None, {"ARCHIVE_MISSING"}), (0, set()),
    (172799, set()), (172800, set()), (172800.01, {"ARCHIVE_STALE"})])
def test_archive_boundaries(facts, age, expected):
    facts["last_archive_age_seconds"] = age
    assert codes(facts) == expected


@pytest.mark.parametrize("stamps,expected", [([], False), ([10000], False),
    ([9400, 9999], False), ([9400, 10000, 9999], True),
    ([9399.999, 10000, 9999], False), ([0, 9399, 9400, 9500, 9600], True),
    ([9999, 9999, 9999], True)])
def test_crash_window(facts, stamps, expected):
    before = stamps[:]
    assert crash_loop(stamps, now=10000) is expected and stamps == before
    facts["components"]["worker"]["failure_timestamps"] = stamps
    assert codes(facts) == ({"CRASH_LOOP"} if expected else set())


def test_all_faults_and_sorted_components(facts):
    facts.update(db_opens=False, schema_current=False, data_dir_writable=False,
                 free_disk_bytes=0, last_archive_age_seconds=None)
    facts["components"] = {"z": {"heartbeat_age_seconds": 181, "failure_timestamps": [9999] * 3},
                           "a": {"heartbeat_age_seconds": None, "failure_timestamps": []}}
    report = evaluate_health(facts)
    assert report["exit_code"] == 1 and len(report["checks"]) == 9
    assert [c["component"] for c in report["checks"] if "component" in c] == ["a", "a", "z", "z"]
    assert codes(facts) == {"DB_FAILED", "SCHEMA_FAILED", "DATA_DIR_FAILED", "DISK_LOW",
                            "HEARTBEAT_MISSING", "HEARTBEAT_STALE", "CRASH_LOOP", "ARCHIVE_MISSING"}


@pytest.mark.parametrize("key,value", [("now", -1), ("now", float("nan")),
    ("now", float("inf")), ("now", 10**400), ("now", True), ("db_opens", "secret-sentinel"),
    ("schema_current", 1), ("data_dir_writable", None), ("free_disk_bytes", 1.5),
    ("min_free_disk_bytes", True), ("min_free_disk_bytes", -1), ("archive_schedule_seconds", 0),
    ("archive_schedule_seconds", "secret-sentinel"), ("last_archive_age_seconds", -1),
    ("components", {}), ("components", []), ("components", {"secret\nvalue": {}})])
def test_invalid_facts_no_echo(facts, key, value):
    facts[key] = value
    assert codes(facts) == {"INVALID_FACTS"}
    assert "secret" not in json.dumps(evaluate_health(facts))


@pytest.mark.parametrize("value", [None, [], "secret-sentinel", {}, {"password": "secret-sentinel"}])
def test_incomplete_contract(value):
    assert codes(value) == {"INVALID_FACTS"}


@pytest.mark.parametrize("item", [{}, {"heartbeat_age_seconds": 0, "failure_timestamps": [], "secret": "x"},
    {"heartbeat_age_seconds": -1, "failure_timestamps": []},
    {"heartbeat_age_seconds": float("inf"), "failure_timestamps": []},
    {"heartbeat_age_seconds": 0, "failure_timestamps": [10001]},
    {"heartbeat_age_seconds": 0, "failure_timestamps": [True]},
    {"heartbeat_age_seconds": 0, "failure_timestamps": "secret"}])
def test_invalid_component_facts(facts, item):
    facts["components"]["worker"] = item
    assert codes(facts) == {"INVALID_FACTS"}


def test_unknown_top_level_field(facts):
    facts["password"] = "secret-sentinel"
    assert codes(facts) == {"INVALID_FACTS"}


def test_rate_limit_per_component_and_clock_rollback():
    state = {}
    allowed, first = rate_limit_alert("worker", state, now=0)
    assert allowed and first == {"worker": 0} and state == {}
    for now in (0, 3599.999):
        allowed, blocked = rate_limit_alert("worker", first, now=now)
        assert not allowed and blocked == first and blocked is not first
    allowed, second = rate_limit_alert("web", first, now=1)
    assert allowed and second == {"worker": 0, "web": 1}
    allowed, third = rate_limit_alert("worker", second, now=3600)
    assert allowed and third == {"worker": 3600, "web": 1}
    assert not rate_limit_alert("worker", third, now=3500)[0]
    assert not rate_limit_alert("worker", third, now=7199)[0]
    assert rate_limit_alert("worker", third, now=7200)[0]


@pytest.mark.parametrize("component,state,now", [("root\nx", {}, 0), ("worker", [], 0),
    ("worker", {"a": "secret-sentinel"}, 0), ("worker", {"a": -1}, 0),
    ("worker", {"bad name": 0}, 0), ("worker", {}, float("nan")), ("worker", {}, True)])
def test_invalid_rate_limit(component, state, now):
    with pytest.raises(DoctorError) as error:
        rate_limit_alert(component, state, now=now)
    assert "secret-sentinel" not in str(error.value)


@pytest.mark.parametrize("stamps,now", [((), 1), ([2], 1), ([-1], 1), ([float("inf")], 1),
    (["secret-sentinel"], 1), ([], -1)])
def test_invalid_crash_loop(stamps, now):
    with pytest.raises(DoctorError):
        crash_loop(stamps, now=now)


@pytest.mark.parametrize("healthy", [True, False])
def test_cli_doctor_json(tmp_path, capsys, facts, healthy):
    facts["db_opens"] = healthy
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    before = path.read_bytes()
    code = ctl.main(["doctor-eval", "--facts", str(path), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == int(not healthy) == result["code"] == result["data"]["report"]["exit_code"]
    assert result["data"]["report"] == evaluate_health(facts)
    assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]


def test_cli_human_doctor(tmp_path, capsys, facts):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    assert ctl.main(["doctor-eval", "--facts", str(path)]) == 0
    assert "HEARTBEAT_OK [worker]: Пульс компонента актуален" in capsys.readouterr().out


@pytest.mark.parametrize("body", ['{"now":1,"now":2}', '{"now":NaN}', 'secret-sentinel',
    '{"password":"secret-sentinel"}', '[]'])
def test_cli_invalid_facts_no_leak(tmp_path, capsys, body):
    path = tmp_path / "invalid.json"
    path.write_text(body, encoding="utf-8")
    assert ctl.main(["doctor-eval", "--facts", str(path), "--json"]) == 1
    result = capsys.readouterr()
    assert "secret-sentinel" not in result.out + result.err
    assert json.loads(result.out)["ok"] is False


def test_cli_manifest_still_required(capsys):
    assert ctl.main(["validate", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == 2
