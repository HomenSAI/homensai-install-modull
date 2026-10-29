"""Версионированный офлайн-каталог и безопасные структурированные ошибки."""
from __future__ import annotations

import copy
import json
import math
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .i18n import normalize_locale, supported_locales

_CODE = re.compile(r"^[A-Z][A-Z0-9_.]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
_SENSITIVE_KEY = re.compile(r"password|passwd|passphrase|secret|token|authorization|cookie|credential|private.?key|api.?key|access.?key|connection.?string|(^|_)dsn($|_)", re.I)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+")
_ASSIGNMENT = re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization|cookie)\s*[:=]\s*([^\s,;&]+)")
_URL = re.compile(r"https?://[^\s<>\"']+")
_HOME = re.compile(r"(?i)(?:[a-z]:[\\/]Users[\\/]|/(?:home|Users)/)[^\\/\s]+")
REDACTED = "[REDACTED]"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact(value, *, known_secrets=(), max_string: int = 4096, _depth: int = 0):
    """Защита в глубину, а не универсальный классификатор персональных данных.

    Произвольный stdout/environment запрещён на уровне вызывающего кода.
    Экспорт дополнительно использует строгие списки разрешённых полей.
    """
    if _depth > 8:
        return "[DEPTH_LIMIT]"
    secrets = tuple(s for s in known_secrets if isinstance(s, str) and s)
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:128]:
            if not isinstance(key, str):
                continue
            safe_key = key[:128]
            result[safe_key] = REDACTED if _SENSITIVE_KEY.search(key) else redact(item, known_secrets=secrets, max_string=max_string, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(v, known_secrets=secrets, max_string=max_string, _depth=_depth + 1) for v in value[:128]]
    if isinstance(value, str):
        text = value
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub(lambda match: match.group(1) + " " + REDACTED, text)
        text = _ASSIGNMENT.sub(lambda match: match.group(1) + "=" + REDACTED, text)

        def clean_url(match):
            try:
                parts = urlsplit(match.group(0))
                host = parts.hostname or ""
                if ":" in host:
                    host = "[" + host + "]"
                if parts.port is not None:
                    host += ":" + str(parts.port)
                query = urlencode([(k, REDACTED if _SENSITIVE_KEY.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)])
                return urlunsplit((parts.scheme, host, parts.path, query, ""))
            except (ValueError, TypeError):
                return "[REDACTED_URL]"

        text = _URL.sub(clean_url, text)
        text = _EMAIL.sub("[REDACTED_EMAIL]", text)
        text = _HOME.sub("<USER_HOME>", text)
        return text[:max_string]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return "[UNSUPPORTED_VALUE]"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate error catalog key")
        result[key] = value
    return result


@lru_cache(maxsize=1)
def _builtin_catalog() -> dict:
    path = Path(__file__).with_name("catalog") / "errors.v1.json"
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("error catalog is too large")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    _validate_catalog(data)
    return data


def _validate_catalog(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("catalog_version"), str):
        raise ValueError("unsupported error catalog schema")
    entries = data.get("errors")
    if not isinstance(entries, dict) or "UNKNOWN_ERROR" not in entries or len(entries) > 5000:
        raise ValueError("invalid error catalog entries")
    for code, entry in entries.items():
        if not _CODE.fullmatch(code) or not isinstance(entry, dict):
            raise ValueError("invalid error catalog code")
        if entry.get("severity") not in ("info", "warning", "error", "critical") or type(entry.get("retryable")) is not bool:
            raise ValueError("invalid error catalog properties")
        translations = entry.get("translations", {})
        if set(translations) != set(supported_locales):
            raise ValueError("error catalog translation coverage differs")
        for localized in translations.values():
            for field in ("title", "description", "impact"):
                if not isinstance(localized.get(field), str) or len(localized[field]) > 8192:
                    raise ValueError("invalid error catalog text")
            for field in ("possible_causes", "checks", "remediation"):
                if not isinstance(localized.get(field), list) or not all(isinstance(item, str) and len(item) <= 8192 for item in localized[field]):
                    raise ValueError("invalid error catalog instructions")


class ErrorCatalog:
    """Каталог — данные; описанные действия не являются исполняемыми scripts.

    Внешний уже верифицированный catalog_data может передать доверенный
    обновляющий код. Этот класс ничего не скачивает и не устанавливает ключи.
    """
    def __init__(self, catalog_data: dict | None = None):
        if catalog_data is None:
            self._data = _builtin_catalog()
        else:
            _validate_catalog(catalog_data)
            self._data = copy.deepcopy(catalog_data)
        self.version = self._data["catalog_version"]

    def describe(self, code: str, locale: str = "ru") -> dict:
        requested = code if isinstance(code, str) and _CODE.fullmatch(code) else "UNKNOWN_ERROR"
        known = requested in self._data["errors"]
        selected = requested if known else "UNKNOWN_ERROR"
        entry = self._data["errors"][selected]
        chosen_locale = normalize_locale(locale)
        result = copy.deepcopy(entry["translations"][chosen_locale])
        result.update(code=requested, catalog_code=selected, known_code=known, catalog_version=self.version,
                      schema_version=1, locale=chosen_locale, severity=entry["severity"], retryable=entry["retryable"],
                      permitted_actions=list(entry.get("permitted_actions", [])))
        return result

    def search(self, query: str, locale: str = "ru", limit: int = 20) -> list[dict]:
        if not isinstance(query, str) or len(query) > 512 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid catalog search")
        terms = query.casefold().split()
        results = []
        for code in sorted(self._data["errors"]):
            item = self.describe(code, locale)
            searchable = " ".join([code, item["title"], item["description"], *item["possible_causes"], *item["remediation"]]).casefold()
            words = re.findall(r"[^\W_]+", searchable)
            code_words = re.findall(r"[^\W_]+", code.casefold())
            title_words = re.findall(r"[^\W_]+", item["title"].casefold())
            # Prefix search supports Russian/German inflection while avoiding
            # accidental substring hits: 'port' must not match 'unsupported'.
            if all(term == code.casefold() or any(word.startswith(term) for word in words) for term in terms):
                score = 1000 if query.casefold().strip() == code.casefold() else 0
                for term in terms:
                    score += 100 * int(term in code_words) + 50 * int(term in title_words)
                    score += 20 * int(any(word.startswith(term) for word in title_words))
                results.append((score, code, item))
        results.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in results[:limit]]


def error_envelope(code: str = "UNKNOWN_ERROR", *, locale: str = "ru", phase=None, component_id=None,
                   cause_status: str = "unknown", evidence=None, details=None, retryable=None,
                   error_id=None, parent_error_id=None, known_secrets=(), external_exit_code=None) -> dict:
    if cause_status not in ("confirmed", "suspected", "unknown"):
        raise ValueError("invalid cause_status")
    if evidence is not None and (not isinstance(evidence, (list, tuple)) or len(evidence) > 128 or not all(isinstance(item, dict) for item in evidence)):
        raise ValueError("evidence must be a bounded list of check results")
    if cause_status == "confirmed" and not evidence:
        raise ValueError("confirmed cause requires evidence")
    if retryable is not None and type(retryable) is not bool:
        raise ValueError("retryable must be boolean")
    for label, value in (("phase", phase), ("component_id", component_id), ("error_id", error_id), ("parent_error_id", parent_error_id)):
        if value is not None and (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)):
            raise ValueError("invalid error " + label)
    if external_exit_code is not None and not (type(external_exit_code) is int or isinstance(external_exit_code, str) and _IDENTIFIER.fullmatch(external_exit_code)):
        raise ValueError("invalid external exit code")
    item = ErrorCatalog().describe(code, locale)
    result = {"schema_version": 1, "error_id": str(error_id or uuid.uuid4()), "code": item["code"],
              "known_code": item["known_code"], "catalog_version": item["catalog_version"],
              "locale": item["locale"], "title": item["title"], "message": item["description"],
              "severity": item["severity"], "retryable": item["retryable"] if retryable is None else retryable,
              "cause_status": cause_status, "phase": phase, "component_id": component_id,
              "parent_error_id": parent_error_id, "evidence": evidence or [], "details": details or {},
              "external_exit_code": external_exit_code, "timestamp": utcnow()}
    return redact(result, known_secrets=known_secrets)


class InstallerError(Exception):
    """Ожидаемый отказ; текст исключения не включает произвольные входные данные."""
    def __init__(self, code: str, message: str | None = None, **context):
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else "UNKNOWN_ERROR"
        self.context = context
        self.error_id = str(context.pop("error_id", None) or uuid.uuid4())
        # message намеренно не попадает в публичную ошибку: исключения ОС могут
        # содержать полный путь/секрет. Безопасные факты передаются в details.
        super().__init__(self.code)

    def to_dict(self, locale: str = "ru") -> dict:
        context = dict(self.context)
        context.pop("locale", None)
        return error_envelope(self.code, locale=locale, error_id=self.error_id, **context)
