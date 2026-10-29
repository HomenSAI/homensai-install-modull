"""Строгий профиль synthetic reference release, подписываемый вместе с payload."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat


PROFILE_FILENAME = "reference-profile.json"
DEFAULT_PROFILE = {"schema_version": 1, "product_version": "1.0.0", "database_schema": 1,
                   "health_failure": False, "migration_failure": False, "require_api_key": False}
MAX_JSON_BYTES = 65536


class ReferenceProductError(RuntimeError):
    """Стабильный код без путей, текста записей и значений окружения."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def safe_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in str(path)):
        raise ReferenceProductError("REFERENCE_STORAGE_PATH_INVALID")
    for parent in (*reversed(path.parents), path):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReferenceProductError("REFERENCE_STORAGE_LINK_REJECTED")
    return path


def read_json(path: Path) -> dict:
    path = safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_JSON_BYTES:
        raise ReferenceProductError("REFERENCE_JSON_INVALID")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def nonfinite(_):
        raise ValueError("nonfinite number")

    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("too large")
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
        if type(value) is not dict:
            raise ValueError("object required")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ReferenceProductError("REFERENCE_JSON_INVALID") from None


def validate_profile(value: dict) -> dict:
    required = {"schema_version", "product_version", "database_schema"}
    if type(value) is not dict or set(value) - set(DEFAULT_PROFILE) or required - set(value):
        raise ReferenceProductError("REFERENCE_PROFILE_INVALID")
    result = {**DEFAULT_PROFILE, **value}
    if (type(result["schema_version"]) is not int or result["schema_version"] != 1
            or type(result["database_schema"]) is not int or result["database_schema"] not in {1, 2, 3}
            or type(result["product_version"]) is not str
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9][A-Za-z0-9.-]{0,63})?",
                            result["product_version"]) is None
            or len(result["product_version"]) > 96
            or any(type(result[key]) is not bool for key in ("health_failure", "migration_failure", "require_api_key"))):
        raise ReferenceProductError("REFERENCE_PROFILE_INVALID")
    return result


def load_profile(release_dir: str | Path | None = None) -> dict:
    """Всегда cwd активного release, если вызывающий код явно не передал тестовый каталог."""
    directory = safe_path(Path.cwd() if release_dir is None else Path(release_dir))
    path = directory / PROFILE_FILENAME
    try:
        value = read_json(path)
    except FileNotFoundError:
        return dict(DEFAULT_PROFILE)
    except (OSError, ReferenceProductError):
        raise ReferenceProductError("REFERENCE_PROFILE_INVALID") from None
    return validate_profile(value)


def api_key_configured(environ=None) -> bool:
    value = (os.environ if environ is None else environ).get("REFERENCE_API_KEY")
    return type(value) is str and bool(value.strip())
