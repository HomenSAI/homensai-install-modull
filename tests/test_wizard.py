"""Защитные и языковые границы мастера без сети и установки продукта."""
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from homensai_install.errors import InstallerError
from homensai_install.gui import ASSETS, WizardSession, _json


def make_session(tmp_path):
    payload = tmp_path / "payload"
    (payload / "legal").mkdir(parents=True)
    files = {}
    for lang in ("ru", "en", "de"):
        text = ("Test legal text " + lang).encode()
        (payload / "legal" / (lang + ".txt")).write_bytes(text)
        files["legal/" + lang + ".txt"] = hashlib.sha256(text).hexdigest()
    api = SimpleNamespace(state_dir=tmp_path / "state")
    return WizardSession(api, {"product_id": "test"}, {"payload_dir": str(payload)},
                         verifier=lambda *args: files)


def test_duplicate_and_nonfinite_json_rejected():
    for value in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]'):
        with pytest.raises(InstallerError):
            _json(value)


def test_secret_never_becomes_public_session_option(tmp_path):
    session = make_session(tmp_path)
    secret = "test-only-long-password"
    result = session.route("POST", "/secrets", {}, {"archive_password": secret})
    assert result == {"reference": "wizard_archive", "stored": "session_memory"}
    assert session.secret("wizard_archive") == secret
    assert secret not in json.dumps(session.route("GET", "/session", {}, {}))
    assert not (tmp_path / "state").exists()
    with pytest.raises(InstallerError):
        session.secret("unknown")


def test_terms_are_bound_to_selected_signed_file(tmp_path):
    session = make_session(tmp_path)
    assert session.terms("de")["text"].endswith("de")
    (tmp_path / "payload/legal/de.txt").write_text("altered agreement")
    with pytest.raises(InstallerError) as caught:
        session.terms("de")
    assert caught.value.code == "INTEGRITY_FAILED"


def test_locale_persisted_and_strict_inputs(tmp_path):
    session = make_session(tmp_path)
    assert session.preferred_locale is None
    session.route("POST", "/preferences", {}, {"locale": "de"})
    again = WizardSession(session.api, session.manifest, session.options)
    assert again.preferred_locale == "de"
    with pytest.raises(InstallerError):
        session.route("POST", "/preferences", {}, {"locale": "xx"})
    with pytest.raises(InstallerError):
        session.route("POST", "/secrets", {}, {"archive_password": "test-secret-long", "argv": ["sh"]})
    with pytest.raises(InstallerError):
        session.route("POST", "/plans", {}, {"options": {"trust_keys": {}}})


def test_no_submit_without_explicit_terms_and_review(tmp_path):
    session = make_session(tmp_path)
    for body in ({}, {"accepted_terms": True}, {"accepted_terms": True, "accepted_plan": True}):
        with pytest.raises(InstallerError) as caught:
            session.route("POST", "/operations", {}, body)
        assert caught.value.code == "CONSENT_REQUIRED"


def test_operation_routes_cannot_resume_another_product(tmp_path):
    session = make_session(tmp_path)
    session.api.status = lambda op: {"plan_id": "plan_test", "status": "RECOVERY_REQUIRED"}
    session.api.get_plan = lambda plan: {"source_manifest": {"product_id": "other-product"}}
    with pytest.raises(InstallerError) as caught:
        session.route("POST", "/operations/op_test/resume", {}, {})
    assert caught.value.code == "OPERATION_NOT_FOUND"


def test_declared_product_secrets_only_and_no_mid_operation_change(tmp_path):
    session = make_session(tmp_path)
    session.manifest["secrets"] = [{"name": "APP_KEY", "required": True}]
    session.route("POST", "/secrets", {}, {"product_secrets": {"APP_KEY": "test-only-app-key"}})
    assert session.secret("wizard_product:APP_KEY") == "test-only-app-key"
    assert session.options["secret_refs"] == {"APP_KEY": "wizard_product:APP_KEY"}
    assert "test-only-app-key" not in json.dumps(session.route("GET", "/session", {}, {}))
    with pytest.raises(InstallerError):
        session.route("POST", "/secrets", {}, {"product_secrets": {"UNDECLARED": "test"}})
    session._operations.add("op_busy")
    session.api.status = lambda _: {"status": "QUEUED"}
    with pytest.raises(InstallerError) as caught:
        session.route("POST", "/secrets", {}, {"archive_password": "another-test-password"})
    assert caught.value.code == "OPERATION_CONFLICT"


def test_unfinished_operations_are_filtered_by_bound_product(tmp_path):
    session = make_session(tmp_path)
    seen = []
    session.api.operations_list = lambda **kwargs: seen.append(kwargs) or {"operations": []}
    assert session.route("GET", "/operations", {}, {}) == {"operations": []}
    assert seen == [{"product_id": "test", "unfinished_only": True, "limit": 100}]
    with pytest.raises(InstallerError):
        session.route("GET", "/operations", {"product_id": ["foreign"]}, {})


def test_wizard_translation_coverage():
    catalogs = json.loads((ASSETS / "locales.json").read_text())
    assert set(catalogs) == {"ru", "en", "de"}
    keys = set(catalogs["en"])
    for lang, catalog in catalogs.items():
        assert set(catalog) == keys, lang
        assert all(isinstance(text, str) and text.strip() for text in catalog.values())
    html = (ASSETS / "index.html").read_text()
    assert set(re.findall(r'data-i18n="([^"]+)"', html)) <= keys
    script = (ASSETS / "app.js").read_text()
    assert set(re.findall(r'tr\("([a-z_]+)"\)', script)) - {"status_"} <= keys
    assert "innerHTML" not in script


def test_openapi_internal_references_and_security_match_handlers():
    root = Path(__file__).parents[1]
    contract = json.loads((root / "api/openapi.yaml").read_text())
    schemes = contract["components"]["securitySchemes"]
    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value and value["$ref"].startswith("#/"):
                selected = contract
                for part in value["$ref"][2:].split("/"):
                    selected = selected[part.replace("~1", "/").replace("~0", "~")]
            for item in value.get("security", []):
                assert set(item) <= set(schemes)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(contract)
    assert (root / "api/openapi.yaml").read_bytes() == (root / "homensai_install/api/resources/openapi.yaml").read_bytes()
    for path in ("/operations", "/preferences", "/secrets", "/diagnostics/export/preview", "/diagnostics/export"):
        assert "/api/v1" + path in contract["paths"]
