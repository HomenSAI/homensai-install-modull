"""Локальные сообщения ru/en/de без сети и необязательных зависимостей.

Ключи являются частью интерфейса. GUI может хранить собственные переводы
отдельно: этот модуль владеет только core/error/status сообщениями.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from string import Formatter

supported_locales = ("ru", "en", "de")
SUPPORTED_LOCALES = supported_locales
DEFAULT_LOCALE = "ru"
_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_locale(locale: str | None = None) -> str:
    """ru-RU/ru_RU → ru; неизвестный язык → en; отсутствие → ru."""
    if locale is None or locale == "":
        return DEFAULT_LOCALE
    if not isinstance(locale, str):
        raise ValueError("locale must be a string")
    selected = locale.strip().lower().replace("_", "-").split("-", 1)[0]
    return selected if selected in supported_locales else "en"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate localization key")
        result[key] = value
    return result


def _fields(text: str) -> set[str]:
    fields = set()
    for _, field, spec, conversion in Formatter().parse(text):
        if field is None:
            continue
        if not _FIELD.fullmatch(field) or spec or conversion:
            raise ValueError("unsafe localization placeholder")
        fields.add(field)
    return fields


@lru_cache(maxsize=3)
def _messages(locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    """Файлы включаются в wheel/frozen; наружу выдаётся копия."""
    chosen = normalize_locale(locale)
    path = Path(__file__).with_name("locales") / (chosen + ".json")
    content = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    if not isinstance(content, dict) or len(content) > 5000:
        raise ValueError("invalid localization catalog")
    for key, value in content.items():
        if not _KEY.fullmatch(key) or not isinstance(value, str) or len(value) > 8192:
            raise ValueError("invalid localization entry")
        _fields(value)
    return content


def messages(locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    return dict(_messages(normalize_locale(locale)))


def translate(key: str, locale: str = DEFAULT_LOCALE, **params) -> str:
    """Отсутствующий безопасный ключ возвращается как ключ, без исполнения кода.

    Результат — обычный текст. HTML-оболочка обязана экранировать его как текст.
    """
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError("invalid localization key")
    template = messages(normalize_locale(locale)).get(key)
    if template is None:
        template = messages("en").get(key, key)
    required = _fields(template)
    if required != params.keys():
        if required or params:
            raise ValueError("localization placeholder mismatch")
    values = {}
    for name, value in params.items():
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError("localization values must be scalar text/numbers")
        values[name] = str(value)[:4096]
    return template.format_map(values)


def validate_translations() -> dict:
    """Проверка покрытия языков и одинаковых placeholders, без сети."""
    catalogs = {locale: messages(locale) for locale in supported_locales}
    keys = set(catalogs["en"])
    for locale, catalog in catalogs.items():
        if set(catalog) != keys:
            raise ValueError("localization key coverage differs: " + locale)
        for key in keys:
            if _fields(catalog[key]) != _fields(catalogs["en"][key]):
                raise ValueError("localization placeholders differ: " + key)
    return {"locales": list(supported_locales), "keys": len(keys), "complete": True}


class Translator:
    def __init__(self, locale: str = DEFAULT_LOCALE):
        self.locale = normalize_locale(locale)

    def __call__(self, key: str, **params) -> str:
        return translate(key, self.locale, **params)

    def translate(self, key: str, **params) -> str:
        return self(key, **params)
