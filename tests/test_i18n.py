"""Проверки локализации на исходниках Linux; не GUI/Windows acceptance."""
import pytest

from homensai_install.i18n import normalize_locale, translate, validate_translations
from homensai_install.errors import ErrorCatalog, InstallerError, error_envelope, redact


def test_all_locales_have_the_same_safe_placeholders():
    result = validate_translations()
    assert result["complete"] and result["keys"] >= 30
    assert result["locales"] == ["ru", "en", "de"]
    assert translate("statistics.operations", "de-DE", count=3) == "Vorgänge: 3"
    assert translate("statistics.operations", "en", count=3) == "Operations: 3"
    assert translate("statistics.operations", "ru", count=3) == "Операций: 3"


def test_locale_normalization_and_unknown_key_fallback():
    assert normalize_locale("ru_RU") == "ru"
    assert normalize_locale("DE-de") == "de"
    assert normalize_locale("xx") == "en"
    assert normalize_locale(None) == "ru"
    assert translate("future.safe_key", "de") == "future.safe_key"
    with pytest.raises(ValueError):
        translate("__class__", "en")
    with pytest.raises(ValueError):
        translate("statistics.operations", "ru")


def test_error_catalog_complete_for_public_api():
    catalog = ErrorCatalog()
    codes = ("INVALID_MANIFEST", "INVALID_REQUEST", "API_UNSUPPORTED", "CAPABILITY_UNSUPPORTED",
             "PLAN_STALE", "PLAN_NOT_FOUND", "OPERATION_NOT_FOUND", "OPERATION_CONFLICT",
             "CONSENT_REQUIRED", "INTEGRITY_FAILED", "RECOVERY_REQUIRED", "STATE_STORAGE_UNAVAILABLE",
             "OPERATION_FAILED", "UNEXPECTED_ERROR")
    for code in codes:
        for locale in ("ru", "en", "de"):
            entry = catalog.describe(code, locale)
            assert entry["known_code"] and entry["locale"] == locale
            for field in ("title", "description", "impact", "possible_causes", "checks", "remediation"):
                assert entry[field]
    assert catalog.search("port", "en")[0]["code"] == "PORT_IN_USE"
    assert any(item["code"] == "PORT_IN_USE" for item in catalog.search("порт", "ru"))
    with pytest.raises(ValueError):
        catalog.search("error", limit=10000)


def test_unknown_error_is_not_an_invented_diagnosis():
    entry = ErrorCatalog().describe("PRODUCT.NEW_FAILURE", "en")
    assert entry["code"] == "PRODUCT.NEW_FAILURE" and not entry["known_code"]
    assert entry["catalog_code"] == "UNKNOWN_ERROR"
    envelope = error_envelope("PRODUCT.NEW_FAILURE", details={"password": "synthetic-secret"})
    assert envelope["cause_status"] == "unknown"
    assert envelope["details"]["password"] == "[REDACTED]"
    with pytest.raises(ValueError):
        error_envelope("PORT_IN_USE", cause_status="confirmed")
    proven = error_envelope("PORT_IN_USE", cause_status="confirmed", evidence=[{"check": "port_bind", "ok": False}])
    assert proven["cause_status"] == "confirmed"


def test_error_keeps_parent_and_external_code_without_raw_exception():
    error = InstallerError("MIGRATION_FAILED", "synthetic-private-stdout", parent_error_id="primary-1", external_exit_code=22)
    envelope = error.to_dict("de")
    assert envelope["locale"] == "de" and envelope["parent_error_id"] == "primary-1"
    assert envelope["external_exit_code"] == 22
    assert "synthetic-private-stdout" not in str(envelope)
    assert error.to_dict()["error_id"] == envelope["error_id"]


def test_redaction_handles_nested_credentials_and_urls():
    result = redact({"api_key": "synthetic-key", "message": "Bearer AbCd123 password=testpass https://user:pw@example.test/a?token=abc&x=1", "note": "/home/alice/private user@example.test custom-secret", "number": float("inf")}, known_secrets=("custom-secret",))
    output = str(result)
    for secret in ("synthetic-key", "AbCd123", "testpass", "user:pw", "token=abc", "alice", "user@example.test", "custom-secret"):
        assert secret not in output
    assert result["number"] == "[UNSUPPORTED_VALUE]"


def test_catalog_returns_copies_and_rejects_missing_translation():
    catalog = ErrorCatalog()
    entry = catalog.describe("PORT_IN_USE")
    entry["checks"].clear()
    assert catalog.describe("PORT_IN_USE")["checks"]
    with pytest.raises(ValueError):
        ErrorCatalog({"schema_version": 1, "catalog_version": "1", "errors": {"UNKNOWN_ERROR": {}}})
