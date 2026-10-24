# HomenS.AI · © 2026 Serhii Khomenko
"""Проверка фиксированной схемы манифеста v1/v2, только stdlib, без исполнения."""

from __future__ import annotations

import argparse
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


class ManifestError(ValueError):
    """Манифест не соответствует контракту; значения полей не выводятся."""


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("manifest.schema.json").read_text(encoding="utf-8"))


def _check(value: Any, rule: dict[str, Any] | bool, root: dict[str, Any], path: str) -> None:
    # Поддерживается только набор ключевых слов нашей фиксированной схемы,
    # не произвольные внешние JSON Schema. Сеть и удалённые $ref не используются.
    if rule is False:
        raise ManifestError(f"{path}: поле запрещено в этой версии")
    if rule is True:
        return
    if "$ref" in rule:
        rule = root["$defs"][rule["$ref"].removeprefix("#/$defs/")]
        return _check(value, rule, root, path)
    expected = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}.get(rule.get("type"))
    if expected is not None and type(value) is not expected:
        raise ManifestError(f"{path}: ожидается {rule['type']}")
    if "const" in rule and value != rule["const"]:
        raise ManifestError(f"{path}: недопустимое значение константы")
    if "enum" in rule and value not in rule["enum"]:
        raise ManifestError(f"{path}: недопустимое значение")
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise ManifestError(f"{path}: ключи должны быть строками")
        if set(rule.get("required", [])) - value.keys():
            raise ManifestError(f"{path}: отсутствуют обязательные поля")
        properties = rule.get("properties", {})
        if rule.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ManifestError(f"{path}: неизвестные поля")
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], root, f"{path}.{key}")
    elif expected in (list, str):
        lower, upper = ("minItems", "maxItems") if expected is list else ("minLength", "maxLength")
        if len(value) < rule.get(lower, 0) or len(value) > rule.get(upper, float("inf")):
            raise ManifestError(f"{path}: недопустимая длина")
        if expected is list:
            if rule.get("uniqueItems") and any(item in value[:index] for index, item in enumerate(value)):
                raise ManifestError(f"{path}: повторяющиеся элементы")
            for index, item in enumerate(value):
                _check(item, rule["items"], root, f"{path}[{index}]")
        elif "pattern" in rule and re.search(rule["pattern"], value) is None:
            raise ManifestError(f"{path}: недопустимый формат")
    elif expected is int:
        if value < rule.get("minimum", value) or value > rule.get("maximum", value):
            raise ManifestError(f"{path}: вне допустимого диапазона")
    for part in rule.get("allOf", []):
        _check(value, part, root, path)
    if "if" in rule:
        try:
            _check(value, rule["if"], root, path)
        except ManifestError:
            branch = rule.get("else", True)
        else:
            branch = rule.get("then", True)
        _check(value, branch, root, path)


def _path(value: str, *, relative_file: bool = False) -> tuple[str, PurePosixPath]:
    """Лексический путь без stat/resolve: не читать файловую систему продукта."""
    text = value.replace("\\", "/")
    windows = re.match(r"^[A-Za-z]:/", text) is not None
    tail = text[2:] if windows else text
    if value.startswith("\\") or text.startswith("//") or ":" in tail:
        raise ManifestError("manifest: неоднозначный путь (UNC/диск/поток)")
    parts = text.split("/")
    if any(part == ".." or (part not in ("", ".") and part.endswith((" ", "."))) for part in parts):
        raise ManifestError("manifest: путь содержит обход или неоднозначный сегмент")
    if relative_file and (windows or text.startswith("/")):
        raise ManifestError("manifest: требуется относительный путь файла")
    if windows:
        namespace = text[:2].lower()
        normalized = PurePosixPath(text[2:].casefold())
    else:
        namespace = "/" if text.startswith("/") else "relative"
        # Консервативно и для относительных/POSIX путей: перенос на Windows
        # не должен превращать разные регистры в обход инварианта.
        normalized = PurePosixPath(text.casefold())
    if relative_file and (normalized == PurePosixPath(".") or parts[-1] in ("", ".")):
        raise ManifestError("manifest: требуется путь файла")
    return namespace, normalized


def _unique_names(items: list[dict[str, Any]], field: str) -> set[str]:
    names = {item["name"] for item in items}
    if len(names) != len(items):
        raise ManifestError(f"manifest.{field}: повторяющиеся имена")
    return names


def _validate_v2(value: dict[str, Any]) -> None:
    paths = {key: _path(item) for key, item in value["paths"].items()}
    kinds = {namespace if namespace in ("relative", "/") else "windows" for namespace, _ in paths.values()}
    if len(kinds) != 1:
        raise ManifestError("manifest.paths: нельзя смешивать относительные, POSIX и Windows пути")
    for key in ("data", "backup", "services", "config", "runtime"):
        child = paths[key][1]
        for namespace, parent in (paths["app"], paths["releases"]):
            if paths[key][0] == namespace and (child == parent or parent in child.parents):
                raise ManifestError("manifest.paths: постоянный каталог внутри app/releases")

    artifacts = [*value["runtime"]["lock_files"].values(), *value["runtime"]["vendor_wheels"]]
    files = [_path(item["file"], relative_file=True)[1] for item in artifacts]
    if len(set(files)) != len(files):
        raise ManifestError("manifest.runtime: повторяющиеся пути артефактов")
    for item in value["runtime"]["vendor_wheels"]:
        if not item["file"].endswith(".whl"):
            raise ManifestError("manifest.runtime.vendor_wheels: требуется wheel (.whl)")

    components = value["components"]
    names = _unique_names(components, "components")
    remaining = {item["name"]: set(item["after"]) for item in components}
    for item in components:
        if remaining[item["name"]] - names:
            raise ManifestError("manifest.components.after: неизвестный компонент")
        _path(item["heartbeat"]["file"], relative_file=True)
        if item["heartbeat"]["timeout"] < item["heartbeat"]["interval"]:
            raise ManifestError("manifest.components.heartbeat: timeout меньше interval")
        if item["restart"]["delay"] > item["restart"]["window"]:
            raise ManifestError("manifest.components.restart: delay больше window")
    # Итеративно: максимальный разрешённый граф не зависит от глубины рекурсии.
    while remaining:
        ready = {name for name, dependencies in remaining.items() if not dependencies}
        if not ready:
            raise ManifestError("manifest.components.after: циклическая зависимость")
        remaining = {name: dependencies - ready for name, dependencies in remaining.items() if name not in ready}

    modes = _unique_names(value["modes"], "modes")
    covered = set()
    for mode in value["modes"]:
        selected = set(mode["components"])
        if selected - names:
            raise ManifestError("manifest.modes: неизвестный компонент")
        covered |= selected
        if any(set(item["after"]) - selected for item in components if item["name"] in selected):
            raise ManifestError("manifest.modes: отсутствует зависимость компонента")
    if covered != names:
        raise ManifestError("manifest.modes: компонент не включён ни в один режим")
    exclusions = [frozenset(group) for group in value["mutual_exclusion"]]
    if len(set(exclusions)) != len(exclusions) or any(group - modes for group in exclusions):
        raise ManifestError("manifest.mutual_exclusion: неизвестный или повторяющийся набор режимов")

    _unique_names(value["secrets"], "secrets")
    if any(item["same_value"] and len(item["contexts"]) < 2 for item in value["secrets"]):
        raise ManifestError("manifest.secrets: same_value требует двух contexts")
    pointer = _path(value["update"]["pointer_file"], relative_file=True)[1]
    state = _path(value["update"]["state_file"], relative_file=True)[1]
    if pointer == state:
        raise ManifestError("manifest.update: pointer_file и state_file должны различаться")


def validate_manifest(value: Any) -> dict[str, Any]:
    """Проверить структуру и связи полей; вернуть независимую копию без исполнения."""
    schema = _schema()
    _check(value, schema, schema, "manifest")
    if value["schema_version"] == 2:
        _validate_v2(value)
        return deepcopy(value)
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
