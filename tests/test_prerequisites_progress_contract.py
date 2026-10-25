"""Published progress schemas against real SDK and transport capability results.

Signed fixtures and SQLite are real; artifact transport and OS installers are
synthetic. The actual loopback HTTP case lives in integration_tests, preserving
this directory's unconditional network prohibition.
"""
from copy import deepcopy
import json

import pytest

from homensai_install.api.v1 import InstallerAPI
from integration_tests.prerequisites_progress_contract import (
    MAX_BYTES, ROOT, SCHEMAS, assert_binding, assert_schema, collect_pages)
from test_prerequisites_execution import bundle, coordinator, present
from test_wizard_prerequisites import wizard


def test_sdk_auto_progress_satisfies_schema_before_effect_and_after_recovery(bundle):
    api = InstallerAPI(bundle["tmp_path"] / "api", prerequisites=coordinator(bundle))
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    before_effect = []

    def install(component):
        snapshot = api.prerequisites_status("progress-contract-sdk")
        assert_schema(snapshot, SCHEMAS["PrerequisiteSnapshot"])
        assert_binding(snapshot["download_progress"], plan, snapshot, bundle)
        assert snapshot["status"] == "RUNNING" and snapshot["ready"] is False
        assert snapshot["steps"][component]["state"] == "INSTALLING"
        assert snapshot["download_progress"]["component_id"] == component
        assert snapshot["download_progress"]["state"] == "verified"
        before_effect.append(component)
        present(bundle, component)

    for component in ("base", "client"):
        bundle["effects"][component] = lambda name=component: install(name)
    final = api.prerequisites_start("progress-contract-sdk", plan, plan["required_consents"])
    assert final["status"] == "READY" and before_effect == ["base", "client"]
    restored = InstallerAPI(bundle["tmp_path"] / "new-api", prerequisites=coordinator(bundle))
    assert restored.prerequisites_status("progress-contract-sdk")["download_progress"] == final["download_progress"]
    events = collect_pages(lambda cursor: restored.prerequisites_events(
        "progress-contract-sdk", after_sequence=cursor, limit=3), plan, bundle)
    assert {item["download_progress"]["state"] for item in events} >= {
        "preparing", "transferring", "verifying", "verified"}
    assert len({item["download_progress"]["transfer_id"] for item in events}) == 2


def test_published_schema_rejects_corrupt_real_progress(bundle):
    api = InstallerAPI(bundle["tmp_path"] / "api", prerequisites=coordinator(bundle))
    plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    actual = api.prerequisites_start("progress-schema-input", plan, plan["required_consents"])["download_progress"]
    assert_schema(actual, SCHEMAS["PrerequisiteDownloadProgress"])
    changes = [
        {"schema_version": True}, {"schema_version": 2}, {"completed": True},
        {"completed": -1}, {"completed": 1.5}, {"completed": MAX_BYTES + 1},
        {"total": 0}, {"total": MAX_BYTES + 1}, {"total": True},
        {"attempt": -1}, {"attempt": 8}, {"attempt": False},
        {"sequence": 0}, {"sequence": 2**53}, {"sequence": True},
        {"component_id": "base\n"}, {"artifact_id": "a" * 129},
        {"artifact_sha256": "F" * 64}, {"plan_digest": "0" * 63},
        {"transfer_id": "f" * 32 + "\n"}, {"unit": "items"},
        {"source": "other"}, {"source": "unknown"}, {"resumed": 1},
        {"verified": False}, {"percent": None}, {"percent": 99},
        {"state": "READY"}, {"state": "verifying", "verified": False, "percent": 100},
        {"state": "transferring", "verified": False, "percent": None},
        {"state": "transferring", "verified": False, "percent": 100},
        {"state": "failed", "verified": False, "percent": 0},
        {"updated_at": "2026-10-25T00:00:00.000Z\n"},
        {"url": "https://downloads.example.org/?token=never-public"},
        {"path": str(bundle["tmp_path"])},
    ]
    for change in changes:
        with pytest.raises(AssertionError):
            assert_schema({**actual, **change}, SCHEMAS["PrerequisiteDownloadProgress"])
    missing = deepcopy(actual)
    del missing["artifact_sha256"]
    with pytest.raises(AssertionError):
        assert_schema(missing, SCHEMAS["PrerequisiteDownloadProgress"])


def test_capability_and_absent_progress_are_backward_compatible(bundle):
    session, _ = wizard(bundle)
    caps = session.route("GET", "/prerequisites/capabilities", {}, {})
    assert_schema(caps, SCHEMAS["PrerequisiteCapabilitiesResult"])
    assert caps["durable_byte_progress"] is True
    assert session.api.prerequisites_capabilities()["durable_byte_progress"] is True
    published = json.loads((ROOT / "api/discovery.json").read_text())["prerequisites"]
    assert published == session.api.describe()["prerequisites"]
    assert published["durable_byte_progress"] is True
    legacy_caps = {key: value for key, value in caps.items() if key != "durable_byte_progress"}
    assert_schema(legacy_caps, SCHEMAS["PrerequisiteCapabilitiesResult"])

    plan = session.api.prerequisites_plan({"base": "manual", "client": "manual"})
    manual = session.api.prerequisites_start("progress-manual", plan, plan["required_consents"])
    canceled = session.api.prerequisites_cancel("progress-manual")
    for snapshot in (manual, canceled):
        assert_schema(snapshot, SCHEMAS["PrerequisiteSnapshot"])
        assert "download_progress" not in snapshot
    events = session.api.prerequisites_events("progress-manual")
    assert_schema(events, SCHEMAS["PrerequisiteEventsResult"])
    assert all("download_progress" not in item for item in events["events"])
    assert bundle["calls"] == [] and bundle["transport"].calls == []
    unconfigured = InstallerAPI(bundle["tmp_path"] / "unconfigured").prerequisites_capabilities()
    assert_schema(unconfigured, SCHEMAS["PrerequisiteCapabilitiesResult"])
    assert unconfigured.get("durable_byte_progress", False) is False
    for source, packaged in (("api/openapi.yaml", "openapi.yaml"), ("api/discovery.json", "discovery.json"),
                             ("INSTALLER_API.md", "INSTALLER_API.md")):
        assert (ROOT / source).read_bytes() == (ROOT / "homensai_install/api/resources" / packaged).read_bytes()
