# HomenS.AI · © 2026 Serhii Khomenko
"""Проверка фиксированной схемы манифеста v1, только стандартная библиотека."""

from __future__ import annotations

import argparse
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any


class ManifestError(ValueError):
    """Манифест не соответствует контракту; значения полей не выводятся."""


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("manifest.schema.json").read_text(encoding="utf-8"))


def _check(value: Any, rule: dict[str, Any], root: dict[str, Any], path: str) -> None:
    # Поддерживается только набор ключевых слов нашей фиксированной схемы,
    # не произвольные внешние JSON Schema. Сеть и удалённые $ref не используются.
    if "$ref" in rule:
        rule = root["$defs"][rule["$ref"].removeprefix("#/$defs/")]
    expected = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}[rule["type"]]
    if type(value) is not expected:
        raise ManifestError(f"{path}: ожидается {rule['type']}")
    if "const" in rule and value != rule["const"]:
        raise ManifestError(f"{path}: недопустимое значение константы")
    if expected is dict:
        if not all(type(key) is str for key in value):
            raise ManifestError(f"{path}: ключи должны быть строками")
        if set(rule["required"]) - value.keys():
            raise ManifestError(f"{path}: отсутствуют обязательные поля")
        if value.keys() - rule["properties"].keys():
            raise ManifestError(f"{path}: неизвестные поля")
        for key, item in value.items():
            _check(item, rule["properties"][key], root, f"{path}.{key}")
    elif expected in (list, str):
        lower, upper = ("minItems", "maxItems") if expected is list else ("minLength", "maxLength")
        if len(value) < rule.get(lower, 0) or len(value) > rule.get(upper, float("inf")):
            raise ManifestError(f"{path}: недопустимая длина")
        if expected is list:
            for index, item in enumerate(value):
                _check(item, rule["items"], root, f"{path}[{index}]")
        elif "pattern" in rule and re.search(rule["pattern"], value) is None:
            raise ManifestError(f"{path}: недопустимый формат")
    elif expected is int:
        if value < rule.get("minimum", value) or value > rule.get("maximum", value):
            raise ManifestError(f"{path}: вне допустимого диапазона")


def validate_manifest(value: Any) -> dict[str, Any]:
    """Проверить структуру и связи полей; вернуть независимую копию без исполнения."""
    schema = _schema()
    _check(value, schema, schema, "manifest")
    prefix = f"http://127.0.0.1:{value['port']}/"
    if not value["healthcheck_url"].startswith(prefix):
        raise ManifestError("manifest.healthcheck_url: порт должен совпадать с manifest.port")
    # Эти имена изменили бы поведение Python/Docker вместо указания пути данных.
    if value["data_home_env"] in {"PATH", "HOME", "TMPDIR", "PYTHONHOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "LD_PRELOAD", "LD_LIBRARY_PATH"}:
        raise ManifestError("manifest.data_home_env: зарезервированное имя окружения")
    return deepcopy(value)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("JSON: повторяющиеся имена полей")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ManifestError("JSON: нечисловые константы запрещены")


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Прочитать только указанный JSON; BOM UTF-8 допускается."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_object, parse_constant=_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ManifestError("Не удалось прочитать корректный JSON-манифест") from None
    return validate_manifest(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить манифест без запуска продукта")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as error:
        print(error)
        return 2
    print(f"Манифест корректен: {manifest['product_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
