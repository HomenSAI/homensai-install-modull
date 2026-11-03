# HomenS.AI · © 2026 Serhii Khomenko
"""Предварительная проверка путей свежей установки v2, без записи и запуска.

Это частичная проверка, не разрешение выполнять установку. Оценку пространства
задаёт вызывающая сторона; поддержка продукта и реальные права не доказываются.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from shared.datahome import DataHomeError, checked_path, contains, resolve_datahome
from shared.manifest import ManifestError, validate_manifest

NAMES = ("app", "releases", "data", "backup", "services", "config", "runtime")
MAX_BYTES = 2**63 - 1


class PreflightError(ValueError):
    """Проверка не пройдена; не содержит имён файлов или данных пользователя."""


def _nearest_directory(path: Path) -> Path:
    """Ближайший существующий предок без ссылок; непапка запрещена."""
    current = checked_path(path)
    while not current.exists():
        if current.parent == current:
            raise PreflightError("Существующий каталог недоступен")
        current = checked_path(current.parent)
    if not current.is_dir():
        raise PreflightError("Существующий предок не является каталогом")
    return current


def preflight_fresh(manifest: dict[str, Any], install_root: str | Path, *,
                    required_bytes: dict[str, int], reserve_bytes: int = 64 * 1024 * 1024,
                    preserved: frozenset = frozenset()) -> dict:
    """Проверить свободные пути и расчёт места для одного свежего v2-сценария.

    Нет mkdir, записи, установки, создания блокировки, анализа прав или payload.
    Вызывающий код обязан заново проверить состояние под блокировкой до записи.
    preserved — данные, копии и настройки, оставленные подтверждённым удалением;
    только они могут быть непустыми.
    """
    try:
        manifest = validate_manifest(manifest)
        if manifest["schema_version"] != 2:
            raise PreflightError("Для проверки требуется манифест v2")
        if (type(required_bytes) is not dict or set(required_bytes) != set(NAMES)
                or any(type(value) is not int or not 0 <= value <= MAX_BYTES for value in required_bytes.values())
                or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= MAX_BYTES):
            raise PreflightError("Требуется полный допустимый расчёт пространства")
        if type(preserved) is not frozenset or not preserved <= {"data", "backup", "config"}:
            raise PreflightError("Недопустимый набор сохранённых каталогов")
        root = checked_path(install_root)
        if root == Path(root.anchor):
            raise PreflightError("Корень диска не может быть корнем установки")
        home = resolve_datahome(manifest, root)
        paths = {"app": home.app, "releases": home.releases, **dict(home.roots)}
        if contains(home.app, home.releases) or contains(home.releases, home.app):
            raise PreflightError("Каталоги приложения и выпусков пересекаются")
        devices = {}
        for name in NAMES:
            path = checked_path(paths[name])
            if path == Path(path.anchor):
                raise PreflightError("Корень диска не является каталогом продукта")
            if path.exists():
                if not path.is_dir():
                    raise PreflightError("Путь продукта не является каталогом")
                if name not in preserved:
                    with os.scandir(path) as entries:
                        if next(entries, None) is not None:
                            raise PreflightError("Каталог продукта не пуст; свежая установка запрещена")
            parent = _nearest_directory(path)
            device = os.stat(parent).st_dev
            free = shutil.disk_usage(parent).free
            if type(free) is not int or free < 0:
                raise PreflightError("Свободное пространство недоступно")
            group = devices.setdefault(device, {"roots": [], "required_bytes": 0, "free_bytes": free})
            group["roots"].append(name)
            group["required_bytes"] += required_bytes[name]
            group["free_bytes"] = min(group["free_bytes"], free)
        volumes = []
        for group in devices.values():
            needed = group["required_bytes"] + reserve_bytes
            if needed > MAX_BYTES or group["free_bytes"] < needed:
                raise PreflightError("Недостаточно свободного места")
            volumes.append({**group, "reserve_bytes": reserve_bytes})
        volumes.sort(key=lambda item: item["roots"])
        return {"product_id": manifest["product_id"], "checks_passed": True,
                "directories": {name: str(paths[name]) for name in NAMES}, "volumes": volumes,
                "not_checked": ["writability", "permissions", "platform_compatibility", "process_ownership",
                                "package_authenticity", "services", "runtime_compatibility"]}
    except (DataHomeError, ManifestError, OSError, ValueError, OverflowError) as error:
        if isinstance(error, PreflightError):
            raise
        raise PreflightError("Не удалось проверить пути или пространство") from None
