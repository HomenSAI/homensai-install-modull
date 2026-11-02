# HomenS.AI · © 2026 Serhii Khomenko
"""Локальные словари сообщений; файлы данных никогда не исполняются как код."""
from __future__ import annotations

import json
from pathlib import Path
import re
from string import Formatter
from typing import Any

_DIRECTORY = Path(__file__).with_name('locales')
_LANGUAGE = re.compile(r'[a-z]{2,3}\Z')
_FIELD = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_KEY = re.compile(r'[a-z][a-z0-9_.]{0,127}\Z')


class LanguageError(ValueError):
    """Словарь или параметры не соответствуют контракту."""


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise LanguageError('Повтор ключа в словаре')
        result[key] = value
    return result


def _fields(text: str) -> set[str]:
    fields = set()
    try:
        for _, field, spec, conversion in Formatter().parse(text):
            if field is not None:
                if _FIELD.fullmatch(field) is None or spec or conversion:
                    raise LanguageError('Недопустимый шаблон сообщения')
                fields.add(field)
    except ValueError:
        raise LanguageError('Недопустимый шаблон сообщения') from None
    return fields


def _catalog(language: str) -> dict[str, str]:
    if type(language) is not str or _LANGUAGE.fullmatch(language) is None:
        raise LanguageError('Недопустимый код языка')
    try:
        with (_DIRECTORY / (language + '.json')).open('rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise LanguageError('Словарь слишком велик')
        catalog = json.loads(data.decode('utf-8'), object_pairs_hook=_unique)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise LanguageError('Словарь недоступен или повреждён') from None
    if type(catalog) is not dict or not catalog or len(catalog) > 1000:
        raise LanguageError('Недопустимый словарь')
    for key, value in catalog.items():
        if (type(key) is not str or _KEY.fullmatch(key) is None or type(value) is not str
                or not value or len(value) > 4096):
            raise LanguageError('Недопустимая запись словаря')
        _fields(value)
    return catalog


def translate(language: str, key: str, **variables: str) -> str:
    """Вернуть перевод по ключу; неизвестные ключи и языки вызывают ошибку."""
    if type(key) is not str or _KEY.fullmatch(key) is None:
        raise LanguageError('Недопустимый ключ сообщения')
    reference = _catalog('en')
    if key not in reference:
        raise LanguageError('Неизвестный ключ сообщения')
    catalog = reference if language == 'en' else _catalog(language)
    if set(catalog) != set(reference):
        raise LanguageError('Неполный словарь')
    for item in reference:
        if _fields(reference[item]) != _fields(catalog[item]):
            raise LanguageError('Несовместимые переменные словаря')
    required = _fields(reference[key])
    if set(variables) != required or any(type(value) is not str or len(value) > 8192 for value in variables.values()):
        raise LanguageError('Недопустимые переменные сообщения')
    return catalog[key].format(**variables)
