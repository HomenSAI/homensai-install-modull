"""Собственные начальные файлы reference product; существующие данные не заменяются."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .profile import ReferenceProductError, read_json, safe_path


DEFAULT_SETTINGS = {"schema_version": 1, "site_title": "HomenSAI Reference", "language": "ru",
                    "theme": "system", "show_release": True}
WELCOME_FILE = Path("user-files") / "welcome.txt"
WELCOME_TEXT = ("HomenSAI reference: синтетический пользовательский файл.\n"
                "Измените этот файл для проверки сохранения данных при update/rollback/uninstall.\n")


def _create_if_missing(path: Path, content: bytes):
    path = safe_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReferenceProductError("REFERENCE_STORAGE_FILE_INVALID")
        return False
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def initialize_files(data_home: str | Path, config_home: str | Path | None = None) -> dict:
    data = safe_path(data_home)
    config = safe_path(config_home if config_home is not None else data / "config")
    return {"user_file_created": _create_if_missing(data / WELCOME_FILE, WELCOME_TEXT.encode("utf-8")),
            "settings_created": _create_if_missing(config / "settings.json",
                (json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))}


def load_settings(config_home: str | Path | None) -> dict:
    if config_home is None:
        return dict(DEFAULT_SETTINGS)
    try:
        value = read_json(safe_path(config_home) / "settings.json")
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)  # Совместимость с существующим legacy v1.
    except (OSError, ReferenceProductError):
        raise ReferenceProductError("REFERENCE_SETTINGS_INVALID") from None
    expected = set(DEFAULT_SETTINGS)
    if not expected <= set(value):
        raise ReferenceProductError("REFERENCE_SETTINGS_INVALID")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["site_title"]) is not str or not 1 <= len(value["site_title"]) <= 128
            or type(value["language"]) is not str or value["language"] not in {"ru", "en", "de"}
            or type(value["theme"]) is not str or value["theme"] not in {"system", "light", "dark"}
            or type(value["show_release"]) is not bool):
        raise ReferenceProductError("REFERENCE_SETTINGS_INVALID")
    # Чужие дополнительные настройки не отражаются в HTML и не переписываются.
    return {key: value[key] for key in DEFAULT_SETTINGS}
