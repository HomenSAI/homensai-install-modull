"""Реальная связь SDK→SQLite diagnostics; адаптер синтетический, не installer QA."""
import json
from pathlib import Path

from homensai_install.api.v1 import InstallerAPI


class DiagnosticEngine:
    def __init__(self, final="SUCCEEDED"):
        self.final = final
        self.calls = 0

    def capabilities(self, target):
        return {"actions": {"install": {"supported": True}}, "readiness": "test-adapter"}

    def discover(self, manifest, target, options):
        return {"fingerprint_data": {"synthetic": True}}

    def plan(self, action, manifest, target, inventory, options):
        return {"steps": [{"step_id": "synthetic-verify"}], "required_consents": []}

    def execute(self, plan, emit, canceled):
        self.calls += 1
        emit({"type": "phase.completed", "phase": "verify", "duration_ms": 2,
              "progress": {"percent": 100}, "stdout": "synthetic-private-text"})
        return {"status": self.final}


def run_operation(tmp_path, final="SUCCEEDED"):
    engine = DiagnosticEngine(final)
    api = InstallerAPI(tmp_path / "state", engine=engine, locale="de")
    manifest = json.loads((Path(__file__).resolve().parents[1] / "examples/rent.manifest.json").read_text())
    plan = api.plan("install", manifest, options={"install_root": str(tmp_path / "synthetic-install")})
    consents = [plan["review_consent_ref"], *plan["required_consents"]]
    operation = api.submit(plan["plan_id"], plan["plan_digest"], consents, idempotency_key="synthetic-key", background=False)
    return api, engine, plan, consents, operation


def test_sdk_projection_has_one_operation_and_private_output_is_omitted(tmp_path):
    api, engine, plan, consents, operation = run_operation(tmp_path)
    assert operation["status"] == "SUCCEEDED"
    history = api.diagnostics_get(operation["operation_id"])
    assert history["operation"]["operation_id"] == operation["operation_id"]
    assert history["attempts"][0]["status"] == "SUCCEEDED"
    assert history["events"]
    assert "synthetic-private-text" not in json.dumps(history)
    repeated = api.submit(plan["plan_id"], plan["plan_digest"], consents, idempotency_key="synthetic-key", background=False)
    assert repeated["operation_id"] == operation["operation_id"] and engine.calls == 1
    stats = api.statistics_query()
    assert stats["total_operations"] == 1 and stats["total_attempts"] == 1
    assert stats["successful_operations"] == 1 and stats["data_complete"]
    assert api.errors_describe("PORT_IN_USE")["locale"] == "de"


def test_sdk_cancelled_operation_is_not_counted_as_success(tmp_path):
    api, _, _, _, operation = run_operation(tmp_path, final="CANCELED")
    assert operation["status"] == "CANCELED"
    stats = api.statistics_query()
    assert stats["cancelled_operations"] == 1 and stats["successful_operations"] == 0
    assert stats["success_rate_technical_outcomes"]["denominator"] == 0
