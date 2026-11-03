# HomenS.AI · © 2026 Serhii Khomenko
"""Локальные словари сообщений; файлы данных никогда не исполняются как код."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
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


def _validate_entries(catalog: Any) -> None:
    if type(catalog) is not dict or not catalog or len(catalog) > 1000:
        raise LanguageError('Недопустимый словарь')
    for key, value in catalog.items():
        if (type(key) is not str or _KEY.fullmatch(key) is None or type(value) is not str
                or not value or len(value) > 4096):
            raise LanguageError('Недопустимая запись словаря')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise LanguageError('Недопустимый текст словаря') from None
        _fields(value)


def _compatible(reference: dict, catalog: dict) -> None:
    if set(catalog) != set(reference):
        raise LanguageError('Неполный словарь')
    for item in reference:
        if _fields(reference[item]) != _fields(catalog[item]):
            raise LanguageError('Несовместимые переменные словаря')


def validate_catalog(catalog: Any) -> dict:
    """Проверить данные нового словаря без записи и выполнения кода."""
    _validate_entries(catalog)
    try:
        data = json.dumps(catalog, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise LanguageError('Словарь не может быть записан в UTF-8') from None
    if len(data) > 65536:
        raise LanguageError('Словарь слишком велик')
    _compatible(_catalog('en'), catalog)
    return {'valid': True, 'message_count': len(catalog), 'compact_utf8_bytes': len(data)}


def _catalog(language: str) -> dict[str, str]:
    if type(language) is not str or _LANGUAGE.fullmatch(language) is None:
        raise LanguageError('Недопустимый код языка')
    try:
        path = _DIRECTORY / (language + '.json')
        def signature(info):
            if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise LanguageError('Требуется обычный файл словаря')
            # Windows exposes creation time separately and st_ctime may differ
            # between path and handle; on POSIX retain metadata-change detection.
            timestamp = info.st_birthtime_ns if os.name == 'nt' else info.st_ctime_ns
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, timestamp)
        before = signature(path.lstat())
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                 | getattr(os, 'O_BINARY', 0))
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            if signature(os.fstat(stream.fileno())) != before or signature(path.lstat()) != before:
                raise LanguageError('Файл словаря изменился при открытии')
            data = stream.read(65537)
            if signature(os.fstat(stream.fileno())) != before or signature(path.lstat()) != before:
                raise LanguageError('Файл словаря изменился при чтении')
        if len(data) > 65536:
            raise LanguageError('Словарь слишком велик')
        catalog = json.loads(data.decode('utf-8'), object_pairs_hook=_unique)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise LanguageError('Словарь недоступен или повреждён') from None
    _validate_entries(catalog)
    return catalog


def translate(language: str, key: str, **variables: str) -> str:
    """Вернуть перевод по ключу; неизвестные ключи и языки вызывают ошибку."""
    if type(key) is not str or _KEY.fullmatch(key) is None:
        raise LanguageError('Недопустимый ключ сообщения')
    reference = _catalog('en')
    if key not in reference:
        raise LanguageError('Неизвестный ключ сообщения')
    catalog = reference if language == 'en' else _catalog(language)
    _compatible(reference, catalog)
    required = _fields(reference[key])
    if set(variables) != required or any(type(value) is not str or len(value) > 8192 for value in variables.values()):
        raise LanguageError('Недопустимые переменные сообщения')
    return catalog[key].format(**variables)


def available_languages() -> list[str]:
    """Перечислить только полные совместимые словари установленного пакета."""
    try:
        candidates = sorted(path.stem for path in _DIRECTORY.glob('*.json')
                            if _LANGUAGE.fullmatch(path.stem) is not None)
    except OSError:
        return []
    result = []
    for language in candidates:
        try:
            translate(language, 'api.invalid_request')
        except LanguageError:
            continue
        result.append(language)
    return result
