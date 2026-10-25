"""HTTP session contracts: signed fixture, real threads/SQLite, fake installers.

No browser, OS packages, product lifecycle or live network is accepted here.
"""
from concurrent.futures import ThreadPoolExecutor
import base64
from copy import deepcopy
import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.trust import canonical_bytes
from homensai_install.errors import InstallerError
from homensai_install.gui import MAX_PREREQUISITE_PLANS, WizardSession
from test_prerequisites_execution import bundle, coordinator, present, resign


class ProductEngine:
    """Only transport review/submit; never a claim of installed acceptance."""
    def __init__(self):
        self.executed = []

    def capabilities(self, target):
        return {"actions": {"install": {"supported": True}}}

    def discover(self, manifest, target, options):
        return {"fingerprint_data": {"fixture": True}}

    def plan(self, action, manifest, target, inventory, options):
        return {"steps": [], "required_consents": ["action:install", "terms:" + options["eula_digest"]]}

    def execute(self, plan, emit, cancel_requested):
        self.executed.append(plan["plan_id"])
        return {"status": "SUCCEEDED", "production_ready": False}


def prepare_bundle(value):
    (value["payload"] / "legal").mkdir(exist_ok=True)
    for locale in ("ru", "en", "de"):
        (value["payload"] / "legal" / (locale + ".txt")).write_text("Synthetic product terms " + locale)
    resign(value)


def wizard(value, **ports):
    prepare_bundle(value)
    engine = ProductEngine()
    api = InstallerAPI(value["tmp_path"] / "product-state", engine=engine, prerequisites=coordinator(value, **ports))
    options = {**value["options"], "install_root": str(value["tmp_path"] / "installed")}
    return WizardSession(api, value["manifest"], options, verifier=value["verifier"], development=True), engine


def plan(session, *, automatic=False):
    return session.route("POST", "/prerequisites/plans", {}, {
        "mode": "native", "locale": "en", "choices": {
            "base": "auto" if automatic else "manual", "client": "auto" if automatic else "manual"}})


def review(value):
    return {"plan_id": value["plan_id"], "plan_digest": value["plan_digest"],
            "terms_digest": value["terms_digest"], "accepted_plan": True, "accepted_terms": True,
            "consent_refs": value["plan"]["required_consents"]}


def start(session, value):
    return session.route("POST", "/prerequisites/operations", {}, review(value))


def status(session, operation_id):
    return session.route("GET", "/prerequisites/operations/" + operation_id, {}, {})


def settled(session, operation_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = status(session, operation_id)
        if result["execution"]["active"] is False:
            return result
        threading.Event().wait(.01)
    pytest.fail("Prerequisite worker did not settle")


def test_transport_requires_explicit_trusted_coordinator(tmp_path):
    api = InstallerAPI(tmp_path / "state")
    session = WizardSession(api, {"product_id": "no-prerequisites"}, {})
    value = session.route("GET", "/prerequisites/capabilities", {}, {})
    assert value["configured"] is False and value["gui_auto_flow"] is False
    with pytest.raises(InstallerError) as caught:
        session.route("POST", "/prerequisites/plans", {}, {})
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    assert not api.state_dir.exists()


def test_exact_payload_binding_and_identical_relocation(bundle):
    session, _ = wizard(bundle)
    original = session.api.prerequisites_identity()["payload_binding"]
    original["release"] = "caller-mutation"
    assert session.api.prerequisites_identity()["payload_binding"]["release"] == "1.0.0"
    destination = bundle["tmp_path"] / "relocated"
    shutil.copytree(bundle["payload"], destination)
    options = {**session.options, "payload_dir": str(destination)}
    relocated = WizardSession(session.api, bundle["manifest"], options, verifier=bundle["verifier"])
    assert relocated._prerequisite_binding == session._prerequisite_binding
    (bundle["payload"] / "runtime").write_bytes(b"different but legitimately re-signed runtime")
    resign(bundle)
    with pytest.raises(InstallerError) as caught:
        WizardSession(session.api, bundle["manifest"], session.options, verifier=bundle["verifier"])
    assert caught.value.code == "INTEGRITY_FAILED"


@pytest.mark.parametrize("field,value", [("release", "2.0.0"), ("runtime_executable", "runtime-alternative")])
def test_same_files_with_different_signed_release_or_runtime_do_not_bind(bundle, field, value):
    # Both executables exist before registration: only the signed choice changes.
    (bundle["payload"] / "runtime-alternative").write_bytes(b"alternative signed placeholder")
    session, _ = wizard(bundle)
    inventory_path = Path(bundle["options"]["inventory_file"])
    inventory = json.loads(inventory_path.read_text())
    inventory[field] = value
    inventory_path.write_bytes(canonical_bytes(inventory))
    signature = {"algorithm": "ed25519", "key_id": "synthetic-test",
                 "signature": base64.b64encode(bundle["private"].sign(canonical_bytes(inventory))).decode()}
    Path(bundle["options"]["signature_file"]).write_bytes(canonical_bytes(signature))
    with pytest.raises(InstallerError) as caught:
        WizardSession(session.api, bundle["manifest"], {**session.options, field: value}, verifier=bundle["verifier"])
    assert caught.value.code == "INTEGRITY_FAILED"


def test_public_binding_snapshot_does_not_alias_catalog_target(bundle):
    session, _ = wizard(bundle)
    catalog = session.api._prerequisite_instance.catalog
    expected = catalog.payload_binding()
    catalog.target["mode"] = "docker"
    assert catalog.payload_binding() == expected


@pytest.mark.parametrize("field", ["product_id", "owner_id", "manifest_digest", "target", "catalog_digest"])
def test_changed_registration_identity_never_controls_an_existing_operation(bundle, monkeypatch, field):
    session, _ = wizard(bundle)
    value = plan(session)
    start(session, value)
    settled(session, value["operation_id"])
    identity = session.api.prerequisites_identity()
    identity[field] = {**identity[field], "mode": "docker"} if field == "target" else "different"
    monkeypatch.setattr(session.api, "prerequisites_identity", lambda: deepcopy(identity))
    with pytest.raises(InstallerError) as caught:
        session.route("POST", "/prerequisites/operations/" + value["operation_id"] + "/cancel", {}, {})
    assert caught.value.code == "PLAN_STALE"
    assert not bundle["calls"]


@pytest.mark.parametrize("extra", [
    {"plan": {}}, {"argv": ["sh"]}, {"trust_config": "/tmp/untrusted"},
    {"catalog_digest": "forged"}, {"owner_id": "other"}, {"target": {"os": "windows"}},
    {"local_artifacts": {"base-package": "/tmp/file"}}, {"url": "https://attacker.invalid/"},
    {"payload_dir": "/tmp/other"},
])
def test_browser_cannot_supply_plan_trust_commands_or_paths(bundle, extra):
    session, _ = wizard(bundle)
    with pytest.raises(InstallerError) as caught:
        session.route("POST", "/prerequisites/plans", {}, {"mode": "native", **extra})
    assert caught.value.code == "INVALID_REQUEST"
    assert not bundle["calls"] and not bundle["transport"].calls


def test_target_unknown_queries_and_explicit_consents(bundle):
    session, _ = wizard(bundle)
    assert session.route("GET", "/prerequisites/capabilities", {"mode": ["docker"]}, {})["configured"] is False
    for query in ({"mode": ["native", "docker"]}, {"owner_id": ["other"]}):
        with pytest.raises(InstallerError):
            session.route("GET", "/prerequisites/capabilities", query, {})
    with pytest.raises(InstallerError):
        session.route("POST", "/prerequisites/plans", {}, {"mode": "docker"})
    value = plan(session)
    for change in ({"accepted_terms": False}, {"accepted_plan": False}, {"consent_refs": []},
                   {"terms_digest": "unshown"}, {"plan": value["plan"]}):
        with pytest.raises(InstallerError):
            session.route("POST", "/prerequisites/operations", {}, {**review(value), **change})
    assert not bundle["calls"]


def test_truncated_product_operation_scan_never_allows_prerequisite_start(bundle, monkeypatch):
    session, _ = wizard(bundle)
    value = plan(session)
    monkeypatch.setattr(session.api, "operations_list", lambda **kwargs: {"operations": [], "has_more": True})
    with pytest.raises(InstallerError) as caught:
        start(session, value)
    assert caught.value.code == "OPERATION_CONFLICT" and not bundle["calls"]


def test_session_plan_storage_is_bounded_and_returns_opaque_ids(bundle):
    session, _ = wizard(bundle)
    first = plan(session)
    for _ in range(MAX_PREREQUISITE_PLANS):
        current = plan(session)
    assert len(session._prerequisite_plans) == MAX_PREREQUISITE_PLANS
    assert first["plan_id"] != current["plan_id"]
    with pytest.raises(InstallerError) as caught:
        start(session, first)
    assert caught.value.code == "PLAN_NOT_FOUND"
    assert not bundle["calls"]


def test_ack_status_and_cancel_precede_slow_discovery(bundle):
    entered, release = threading.Event(), threading.Event()
    session, _ = wizard(bundle)
    value = plan(session, automatic=True)
    def slow():
        entered.set()
        assert release.wait(5)
        return deepcopy(bundle["inventory"])
    session.api._prerequisite_instance.discover = slow
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ack = pool.submit(start, session, value).result(timeout=2)
            assert entered.wait(2)
            assert ack["operation_id"] == value["operation_id"]
            assert ack["execution"]["state"] in {"queued", "running"}
            assert status(session, value["operation_id"])["execution"]["active"] is True
            canceled = pool.submit(session.route, "POST", "/prerequisites/operations/" + value["operation_id"] + "/cancel", {}, {}).result(timeout=2)
            assert canceled["cancel_requested"] is True and not release.is_set()
    finally:
        release.set()
    assert settled(session, value["operation_id"])["status"] == "CANCELED"
    assert not bundle["calls"]


def test_cancel_and_events_precede_slow_installer(bundle):
    entered, release = threading.Event(), threading.Event()
    def install():
        entered.set()
        assert release.wait(5)
        present(bundle, "base")
    bundle["effects"]["base"] = install
    session, _ = wizard(bundle)
    value = plan(session, automatic=True)
    try:
        start(session, value)
        assert entered.wait(2)
        stream = session.route("GET", "/prerequisites/operations/" + value["operation_id"] + "/events", {}, {})
        assert stream["events"] and stream["snapshot"]["execution"]["active"] is True
        assert "token=never-public" not in json.dumps(stream)
        with ThreadPoolExecutor(max_workers=1) as pool:
            canceled = pool.submit(session.route, "POST", "/prerequisites/operations/" + value["operation_id"] + "/cancel", {}, {}).result(timeout=2)
        assert canceled["cancel_requested"] and not release.is_set()
    finally:
        release.set()
    assert settled(session, value["operation_id"])["status"] == "CANCELED"
    assert bundle["calls"] == ["base"]


def test_slow_new_plan_does_not_block_cancel(bundle):
    entered, release = threading.Event(), threading.Event()
    session, _ = wizard(bundle)
    value = plan(session)
    start(session, value)
    settled(session, value["operation_id"])
    def slow():
        entered.set()
        assert release.wait(5)
        return deepcopy(bundle["inventory"])
    session.api._prerequisite_instance.discover = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(plan, session)
        assert entered.wait(2)
        try:
            canceled = pool.submit(session.route, "POST", "/prerequisites/operations/" + value["operation_id"] + "/cancel", {}, {}).result(timeout=2)
            assert canceled["status"] == "CANCELED" and not release.is_set()
        finally:
            release.set()
        future.result(timeout=2)


def test_slow_recheck_does_not_block_status_or_cancel(bundle):
    entered, release = threading.Event(), threading.Event()
    session, _ = wizard(bundle)
    value = plan(session)
    start(session, value)
    settled(session, value["operation_id"])
    def slow():
        entered.set()
        assert release.wait(5)
        return deepcopy(bundle["inventory"])
    session.api._prerequisite_instance.discover = slow
    path = "/prerequisites/operations/" + value["operation_id"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        checking = pool.submit(session.route, "POST", path + "/recheck", {}, {})
        assert entered.wait(2)
        try:
            observed = pool.submit(status, session, value["operation_id"]).result(timeout=2)
            canceled = pool.submit(session.route, "POST", path + "/cancel", {}, {}).result(timeout=2)
            assert observed["ready"] is False and canceled["cancel_requested"] and not release.is_set()
        finally:
            release.set()
        checking.result(timeout=2)
    assert status(session, value["operation_id"])["status"] == "CANCELED"
    assert not bundle["calls"]


def test_old_concurrent_snapshot_cannot_rewind_review_observation(bundle):
    session, _ = wizard(bundle)
    value = plan(session)
    start(session, value)
    old = settled(session, value["operation_id"])
    for name in ("base", "client"):
        present(bundle, name)
    session.route("POST", "/prerequisites/operations/" + value["operation_id"] + "/recheck", {}, {})
    generation = session._review_generation
    session._prerequisite_snapshot(old)
    assert session._review_generation == generation


def test_lost_ack_and_new_controller_list_require_explicit_resume(bundle):
    session, _ = wizard(bundle)
    value = plan(session)
    start(session, value)  # Pretend the HTTP response was lost.
    settled(session, value["operation_id"])
    again = start(session, value)
    assert again["operation_id"] == value["operation_id"] and not bundle["calls"]
    second, _ = wizard(bundle)
    page = second.route("GET", "/prerequisites/operations", {}, {})
    assert [item["operation_id"] for item in page["operations"]] == [value["operation_id"]]
    assert not bundle["calls"]
    for name in ("base", "client"):
        present(bundle, name)
    result = second.route("POST", "/prerequisites/operations/" + value["operation_id"] + "/recheck", {}, {})
    assert result["status"] == "READY" and not bundle["calls"]


def test_source_corruption_keeps_status_and_cancel_available(bundle):
    session, _ = wizard(bundle)
    value = plan(session)
    start(session, value)
    settled(session, value["operation_id"])
    (bundle["payload"] / "runtime").write_bytes(b"corrupt after trusted registration")
    assert status(session, value["operation_id"])["operation_id"] == value["operation_id"]
    session.route("POST", "/prerequisites/operations/" + value["operation_id"] + "/resume", {}, {})
    failed = settled(session, value["operation_id"])
    assert failed["ready"] is False and failed["error"]["code"] == "INTEGRITY_FAILED"
    canceled = session.route("POST", "/prerequisites/operations/" + value["operation_id"] + "/cancel", {}, {})
    assert canceled["status"] == "CANCELED" and not bundle["calls"]


def product_plan(session):
    return session.route("POST", "/plans", {}, {"locale": "en", "mode": "native", "action": "install"})


def product_review(session, value):
    return {"plan_id": value["plan_id"], "plan_digest": value["plan_digest"],
            "consent_refs": [value["review_consent_ref"], *value["required_consents"]],
            "idempotency_key": "reviewed-product", "accepted_terms": True, "accepted_plan": True,
            "terms_digest": session.terms("en")["digest"]}


def test_ready_invalidates_review_even_without_browser_poll_and_never_submits(bundle):
    session, engine = wizard(bundle)
    original = product_plan(session)
    original_body = product_review(session, original)
    value = plan(session)
    start(session, value)
    settled(session, value["operation_id"])
    with pytest.raises(InstallerError) as caught:
        session.route("POST", "/operations", {}, original_body)
    assert caught.value.code == "OPERATION_CONFLICT"
    for name in ("base", "client"):
        present(bundle, name)
    # Simulate a transition completed outside this browser's polling loop.
    session.api.prerequisites_recheck(value["operation_id"])
    with pytest.raises(InstallerError) as caught:
        session.route("POST", "/operations", {}, original_body)
    assert caught.value.code == "PLAN_STALE" and not engine.executed
    fresh = product_plan(session)
    assert not engine.executed
    response = session.route("POST", "/operations", {}, product_review(session, fresh))
    assert response["operation_id"].startswith("op_")


def test_unknown_or_still_active_ready_controller_cannot_submit_product(bundle, monkeypatch):
    session, _ = wizard(bundle)
    value = plan(session)
    start(session, value)
    settled(session, value["operation_id"])
    for name in ("base", "client"):
        present(bundle, name)
    session.api.prerequisites_recheck(value["operation_id"])
    observed = status(session, value["operation_id"])
    real_status = session.api.prerequisites_status
    for active in (True, None):
        snapshot = {**observed, "execution": {"mode": "background", "state": "unknown", "active": active}}
        monkeypatch.setattr(session.api, "prerequisites_status", lambda _: deepcopy(snapshot))
        with pytest.raises(InstallerError) as caught:
            product_plan(session)
        assert caught.value.code == "OPERATION_CONFLICT"
    monkeypatch.setattr(session.api, "prerequisites_status", real_status)


def test_hidden_prior_catalog_claim_blocks_product_plan_and_submit(bundle):
    first, _ = wizard(bundle)
    reviewed = product_plan(first)
    body = product_review(first, reviewed)
    value = plan(first)
    start(first, value)
    settled(first, value["operation_id"])
    bundle["descriptor"]["catalog_version"] = "2.0.0"
    second, engine = wizard(bundle)
    assert second.api.prerequisites_operations_list()["operations"] == []
    assert second.api.prerequisites_busy() is True
    for path, request in (("/plans", {"mode": "native", "locale": "en"}), ("/operations", body)):
        with pytest.raises(InstallerError) as caught:
            second.route("POST", path, {}, request)
        assert caught.value.code == "OPERATION_CONFLICT"
        assert value["operation_id"] not in json.dumps(caught.value.to_dict())
    assert not engine.executed
    canceled = first.route("POST", "/prerequisites/operations/" + value["operation_id"] + "/cancel", {}, {})
    assert canceled["status"] == "CANCELED"


@pytest.mark.parametrize("busy", [True, None])
def test_unknown_or_busy_registered_controller_never_allows_product_plan(bundle, monkeypatch, busy):
    session, _ = wizard(bundle)
    monkeypatch.setattr(session.api._prerequisite_instance.store, "controller_busy", lambda: busy)
    assert session.api.prerequisites_busy() is True
    with pytest.raises(InstallerError) as caught:
        product_plan(session)
    assert caught.value.code == "OPERATION_CONFLICT"


@pytest.mark.parametrize("busy", [None, 0])
def test_malformed_custom_busy_probe_cannot_allow_product_plan(bundle, monkeypatch, busy):
    session, _ = wizard(bundle)
    monkeypatch.setattr(session.api, "prerequisites_busy", lambda: busy)
    with pytest.raises(InstallerError) as caught:
        product_plan(session)
    assert caught.value.code == "OPERATION_CONFLICT"


def test_prerequisite_change_invalidates_a_product_plan_built_concurrently(bundle):
    entered, release = threading.Event(), threading.Event()
    session, engine = wizard(bundle)
    value = plan(session)
    original_plan = engine.plan
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return original_plan(*args)
    engine.plan = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(product_plan, session)
        assert entered.wait(2)
        try:
            start(session, value)
        finally:
            release.set()
        with pytest.raises(InstallerError) as caught:
            future.result(timeout=2)
        assert caught.value.code == "PLAN_STALE"
    settled(session, value["operation_id"])
    assert not engine.executed


def test_published_http_contract_and_paired_resources_match_transport(tmp_path):
    root = Path(__file__).resolve().parents[1]
    contract = json.loads((root / "api/openapi.yaml").read_text())
    base = "/api/v1/prerequisites"
    routes = {
        "/capabilities": {"get"}, "/licenses/{component_id}": {"get"}, "/plans": {"post"},
        "/operations": {"get", "post"}, "/operations/{operation_id}": {"get"},
        "/operations/{operation_id}/events": {"get"},
        "/operations/{operation_id}/attempts/{component_id}": {"get"},
        **{"/operations/{operation_id}/" + tail: {"post"}
           for tail in ("resume", "cancel", "recheck", "boundary", "replace-plan")},
    }
    for path, methods in routes.items():
        assert set(contract["paths"][base + path]) == methods
        for method in methods:
            assert set(contract["paths"][base + path][method]["responses"]) == {"200", "400", "403", "500"}
    schemas = contract["components"]["schemas"]
    assert schemas["PrerequisiteReviewRequest"]["additionalProperties"] is False
    assert "plan" not in schemas["PrerequisiteReviewRequest"]["properties"]
    assert set(schemas["PrerequisitePlanRequest"]["properties"]) == {"mode", "locale", "choices", "offline"}
    for path, packaged in (("api/openapi.yaml", "openapi.yaml"), ("api/discovery.json", "discovery.json"),
                           ("INSTALLER_API.md", "INSTALLER_API.md")):
        assert (root / path).read_bytes() == (root / "homensai_install/api/resources" / packaged).read_bytes()
    published = json.loads((root / "api/discovery.json").read_text())["prerequisites"]
    assert published == InstallerAPI(tmp_path / "none").describe()["prerequisites"]
    assert published["http_plan_input"] == "server_plan_id" and published["gui_auto_flow"] is False
    assert published["durable_byte_progress"] is False
