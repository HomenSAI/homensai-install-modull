# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальный локальный API v1: только capabilities и validate.

Это не установщик и не сетевой сервис. Новые операции нельзя объявлять до их
реализации и проверки; значения входных данных не возвращаются в ошибках.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from shared.manifest import ManifestError, validate_manifest

API_MAJOR = 1
API_MINOR = 0
MAX_BYTES = 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 50000
MAX_STRING = 8192
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class InvalidRequest(ValueError):
    pass


def _check_tree(value: Any) -> None:
    # Явный обход не зависит от рекурсивной глубины Python и обнаруживает
    # циклические структуры, переданные напрямую через Python API.
    pending = [(value, 0)]
    seen = set()
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise InvalidRequest()
        if type(item) is dict:
            if id(item) in seen:
                raise InvalidRequest()
            seen.add(id(item))
            for key, child in item.items():
                if type(key) is not str or len(key) > MAX_STRING:
                    raise InvalidRequest()
                pending.append((child, depth + 1))
        elif type(item) is list:
            if id(item) in seen:
                raise InvalidRequest()
            seen.add(id(item))
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > MAX_STRING:
                raise InvalidRequest()
        elif item is not None and type(item) not in (bool, int):
            raise InvalidRequest()


def _error(request_id: str | None, code: str, message: str) -> dict:
    return {"api_major": API_MAJOR, "request_id": request_id, "ok": False,
            "error": {"code": code, "message": message, "retryable": False}}


def handle(request: Any) -> dict:
    """Обработать один объект без процессов, сети или изменения файлов."""
    request_id = None
    try:
        _check_tree(request)
        if type(request) is not dict or set(request) != {"api_major", "operation", "request_id", "params"}:
            raise InvalidRequest()
        candidate = request["request_id"]
        if type(candidate) is not str or _ID.fullmatch(candidate) is None:
            raise InvalidRequest()
        request_id = candidate
        if type(request["api_major"]) is not int:
            raise InvalidRequest()
        if request["api_major"] != API_MAJOR:
            return _error(request_id, "unsupported_version", "Версия API не поддерживается")
        operation, params = request["operation"], request["params"]
        if type(operation) is not str or type(params) is not dict:
            raise InvalidRequest()
        if operation == "capabilities":
            if params:
                raise InvalidRequest()
            result = {"api_major": API_MAJOR, "api_minor": API_MINOR,
                      "operations": ["capabilities", "validate"],
                      "manifest_versions": [1, 2], "execution_modes": [],
                      "limits": {"request_bytes": MAX_BYTES, "depth": MAX_DEPTH,
                                 "nodes": MAX_NODES, "string_chars": MAX_STRING}}
        elif operation == "validate":
            if set(params) != {"manifest"}:
                raise InvalidRequest()
            try:
                manifest = validate_manifest(params["manifest"])
            except (ManifestError, RecursionError):
                return _error(request_id, "invalid_request", "Манифест не соответствует контракту")
            result = {"product_id": manifest["product_id"], "schema_version": manifest["schema_version"]}
        else:
            return _error(request_id, "unsupported_operation", "Операция API не поддерживается")
    except (InvalidRequest, RecursionError):
        return _error(request_id, "invalid_request", "Неверный запрос API")
    return {"api_major": API_MAJOR, "request_id": request_id, "ok": True, "result": result}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest()
        result[key] = value
    return result


def _constant(_):
    raise InvalidRequest()


def main() -> int:
    """Прочитать один ограниченный JSON из stdin и записать один ответ."""
    try:
        if len(sys.argv) != 1:
            raise InvalidRequest()
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise InvalidRequest()
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_constant)
        response = handle(request)
    except (InvalidRequest, ValueError, UnicodeError, RecursionError):
        response = _error(None, "invalid_request", "Неверный запрос API")
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n")
    if response["ok"]:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
