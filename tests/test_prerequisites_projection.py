"""Signed requirements для review без изменения durable plan v1.

Настоящие подписи/SQLite; installer и inventory синтетические. Проверки не
являются browser, installed-wheel или OS acceptance.
"""
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from homensai_install import API_VERSION, prerequisite_async as background
from homensai_install.api.model import canonical
from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from homensai_install.trust import canonical_bytes
from test_prerequisites_execution import bundle, coordinator, resign
from test_wizard_prerequisites import wizard


REQUIREMENT_FIELDS = {
    "component_id", "kind", "required", "minimum_version", "maximum_version", "scope"}


def test_capabilities_project_signed_requirements_without_probe_or_effect(bundle):
    base, _, metrics = bundle["descriptor"]["entries"]
    base["requirement"].update(minimum_version="v1.2", maximum_version="2.0.0+build")
    metrics["requirement"].pop("minimum_version")
    resign(bundle)

    def forbidden(*args):
        pytest.fail("Чтение требований вызвало discovery, markers или событие операции")

    subject = coordinator(bundle, discover=forbidden, markers=forbidden, event_callback=forbidden)
    api = InstallerAPI(bundle["tmp_path"] / "product-state", prerequisites=subject)
    # Store создаётся trusted constructor; capabilities не изменяет его байты.
    before = {path.name: path.read_bytes() for path in subject.store.directory.iterdir()}
    caps = api.prerequisites_capabilities()
    expected = [
        {"component_id": "base", "kind": "base", "required": True,
         "minimum_version": "v1.2", "maximum_version": "2.0.0+build", "scope": "host"},
        {"component_id": "metrics", "kind": "metrics", "required": False,
         "minimum_version": None, "maximum_version": None, "scope": "host"},
        {"component_id": "client", "kind": "client", "required": True,
         "minimum_version": "1.0.0", "maximum_version": None, "scope": "host"}]
    assert caps["requirements"] == expected
    assert all(set(item) == REQUIREMENT_FIELDS for item in caps["requirements"])
    for key in ("product_id", "owner_id", "target", "catalog_digest"):
        assert caps[key] == subject.identity()[key]
    public = json.dumps(caps)
    assert "never-public" not in public and str(bundle["tmp_path"]) not in public
    assert "text_path" not in public and "artifacts" not in public

    caps["requirements"][0]["minimum_version"] = "999.0.0"
    caps["requirements"][1]["required"] = True
    caps["requirements"].pop()
    assert api.prerequisites_capabilities()["requirements"] == expected
    assert {path.name: path.read_bytes() for path in subject.store.directory.iterdir()} == before
    assert not api.state_dir.exists()
    assert bundle["calls"] == [] and bundle["transport"].calls == []


@pytest.mark.parametrize("resigned,code", [(False, "INTEGRITY_FAILED"), (True, "PLAN_STALE")])
def test_capabilities_reverify_metadata_before_exposing_changed_requirement(bundle, resigned, code):
    subject = coordinator(bundle)
    before = {path.name: path.read_bytes() for path in subject.store.directory.iterdir()}
    assert subject.capabilities()["requirements"][0]["minimum_version"] == "1.0.0"
    bundle["descriptor"]["entries"][0]["requirement"]["minimum_version"] = "2.0.0"
    if resigned:
        resign(bundle)
    else:
        (bundle["payload"] / "installer/prerequisites.json").write_bytes(canonical_bytes(bundle["descriptor"]))
    with pytest.raises(InstallerError) as caught:
        subject.capabilities()
    assert caught.value.code == code
    assert {path.name: path.read_bytes() for path in subject.store.directory.iterdir()} == before
    assert bundle["calls"] == [] and bundle["transport"].calls == []


@pytest.mark.parametrize("detected,status", [
    ("v1.2", "present"), ("1.2.0+runtime", "present"), ("2.0", "present"),
    ("1.1.9", "incompatible"), ("2.0.1", "incompatible"),
    ("1.2.0-rc1", "manual"), (None, "manual")])
def test_projected_range_keeps_original_spelling_and_server_version_semantics(bundle, detected, status):
    bundle["descriptor"]["entries"][0]["requirement"].update(
        minimum_version="v1.2", maximum_version="2.0.0+build")
    resign(bundle)
    bundle["inventory"]["components"]["base"].update(status="present", version=detected)
    subject = coordinator(bundle)
    requirement = subject.capabilities()["requirements"][0]
    assert requirement["minimum_version"] == "v1.2" and requirement["maximum_version"] == "2.0.0+build"
    component = next(item for item in subject.plan()["components"] if item["component_id"] == "base")
    assert component["initial_status"] == status
    assert component["initial_ready"] is (status == "present")
    assert component["detected_version"] == detected
    assert "minimum_version" not in component and "maximum_version" not in component
    assert bundle["calls"] == [] and bundle["transport"].calls == []


def test_pending_v1_plan_resumes_unchanged_after_requirements_projection(bundle, monkeypatch):
    subject = coordinator(bundle)
    api = InstallerAPI(bundle["tmp_path"] / "product-state", prerequisites=subject)
    legacy = json.loads(canonical(api.prerequisites_plan({"base": "auto", "client": "auto"})))
    # Фиксируем прежнюю сохранённую форму CP012: metadata проекции в ней нет.
    component_fields = {"component_id", "kind", "required", "depends_on", "ownership", "choice",
        "initial_status", "initial_ready", "detected_version", "scope", "adapter_id",
        "adapter_policy_digest", "elevated", "artifacts", "license", "manual"}
    assert legacy["schema_version"] == 1 and "requirements" not in legacy
    assert all(set(item) == component_fields for item in legacy["components"])
    accepted = deepcopy(legacy["required_consents"])
    # Моделируется потеря controller между durable queue и запуском worker.
    # Настоящий process-kill этим source тестом не заявляется.
    def abandoned_controller(worker):
        # Process death closes its OS handle but keeps the durable claim.
        # Dropping only the Python reservation no longer models that boundary.
        worker.lease.close(release=False)
        worker.release()
    with monkeypatch.context() as patch:
        patch.setattr(background._Worker, "launch", abandoned_controller)
        ack = api.prerequisites_start("pending_projection", legacy, accepted, background=True)
    assert ack["execution"]["state"] == "recovery_required"
    assert subject.execution_domain.busy() is True
    assert subject.store.get("pending_projection")["_initial_validation_pending"] is True

    restarted = coordinator(bundle)
    restored = InstallerAPI(bundle["tmp_path"] / "new-product-state", prerequisites=restarted)
    before = restarted.store.path.read_bytes()
    projection = restored.prerequisites_capabilities()
    projection["requirements"][0]["minimum_version"] = "999.0.0"
    assert restarted.store.path.read_bytes() == before
    assert restored.prerequisites_plan(legacy["choices"]) == legacy
    result = restored.prerequisites_resume("pending_projection")
    saved = restarted.store.get("pending_projection")
    assert result["status"] == "READY" and result["plan_digest"] == legacy["plan_digest"]
    assert saved["plan"] == legacy and saved["consents"] == accepted
    assert saved["_initial_validation_pending"] is False
    assert bundle["calls"] == ["base", "client"]
    assert not api.state_dir.exists() and not restored.state_dir.exists()


def test_http_projection_binding_and_published_resource_contract(bundle):
    session, _ = wizard(bundle)
    caps = session.route("GET", "/prerequisites/capabilities", {}, {})
    result = session.route("POST", "/prerequisites/plans", {}, {"locale": "en"})
    assert caps["requirements"] == session.api.prerequisites_capabilities()["requirements"]
    for key in ("product_id", "owner_id", "target", "catalog_digest"):
        assert caps[key] == caps["binding"][key] == result["plan"][key]
    assert {item["component_id"] for item in caps["requirements"]} == {
        item["component_id"] for item in result["plan"]["components"]}

    root = Path(__file__).resolve().parents[1]
    contract = json.loads((root / "api/openapi.yaml").read_text())
    schemas = contract["components"]["schemas"]
    requirement = schemas["PrerequisiteRequirement"]
    assert set(requirement["required"]) == set(requirement["properties"]) == REQUIREMENT_FIELDS
    assert requirement["additionalProperties"] is False
    for field in ("minimum_version", "maximum_version"):
        assert set(requirement["properties"][field]["type"]) == {"string", "null"}
        pattern = requirement["properties"][field]["pattern"]
        assert re.search(pattern, "v1.2.0+build") and re.search(pattern, "1.2")
        assert not re.search(pattern, "1.2.0-rc1") and not re.search(pattern, "1.2\n")
    capability = schemas["PrerequisiteCapabilitiesResult"]
    # HTTP envelope version and inner public SDK version are separate contracts.
    # Compare the actual configured route result, not only the two schema copies.
    assert capability["properties"]["api_version"]["const"] == caps["api_version"] == API_VERSION
    assert capability["properties"]["requirements"] == {
        "type": "array", "minItems": 1, "maxItems": 128,
        "items": {"$ref": "#/components/schemas/PrerequisiteRequirement"}}
    assert set(capability["allOf"][0]["then"]["required"]) == {
        "product_id", "owner_id", "target", "catalog_digest", "requirements"}
    response = contract["paths"]["/api/v1/prerequisites/capabilities"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"]["properties"]["api_version"]["const"] == "1"
    assert response["content"]["application/json"]["schema"]["properties"]["result"] == {
        "$ref": "#/components/schemas/PrerequisiteCapabilitiesResult"}
    assert set(schemas["PrerequisitePlanRequest"]["properties"]) == {"mode", "locale", "choices", "offline", "local_uploads"}
    for source, packaged in (("api/openapi.yaml", "openapi.yaml"), ("INSTALLER_API.md", "INSTALLER_API.md")):
        assert (root / source).read_bytes() == (root / "homensai_install/api/resources" / packaged).read_bytes()
    assert bundle["calls"] == [] and bundle["transport"].calls == []
