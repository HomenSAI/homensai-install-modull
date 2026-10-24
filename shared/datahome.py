# HomenS.AI · © 2026 Serhii Khomenko
"""Постоянные пути из манифеста; проверка и план, без создания/переноса данных."""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
import os
from pathlib import Path, PureWindowsPath
import stat
from types import MappingProxyType
from typing import Any, Mapping

from shared.manifest import validate_manifest


PERSISTENT_ROOTS = ("data", "backup", "services", "config", "runtime")


class DataHomeError(ValueError):
    """Небезопасная или недостаточно определённая схема размещения."""


def checked_path(value: str | Path) -> Path:
    """Абсолютный путь текущей ОС, без ссылок/reparse points в существующей части."""
    text = str(value)
    portable = text.replace("\\", "/")
    tail = portable[2:] if PureWindowsPath(text).drive and not portable.startswith("//") else portable
    if (not text or ntpath.isreserved(text) or any(ord(c) < 32 or ord(c) == 127 for c in text)
            or portable.startswith("//") or text.startswith("\\") or ":" in tail
            or any(part == ".." or (part not in ("", ".") and part.endswith((".", " ")))
                   for part in portable.split("/"))):
        raise DataHomeError("Недопустимый путь")
    if os.name != "nt" and (PureWindowsPath(text).drive or "\\" in text):
        raise DataHomeError("Путь другой ОС не разрешается на этом хосте")
    path = Path(value)
    if not path.is_absolute():
        raise DataHomeError("Требуется абсолютный путь")
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise DataHomeError("Ссылки и reparse points запрещены")
    return path.resolve(strict=False)


def contains(parent: Path, child: Path) -> bool:
    """Сравнение по сегментам, консервативно без учёта регистра на любой ОС."""
    left = tuple(part.casefold() for part in parent.parts)
    right = tuple(part.casefold() for part in child.parts)
    return right[:len(left)] == left


@dataclass(frozen=True)
class DataHome:
    product_id: str
    app: Path
    releases: Path
    roots: Mapping[str, Path]

    def __post_init__(self) -> None:
        object.__setattr__(self, "app", checked_path(self.app))
        object.__setattr__(self, "releases", checked_path(self.releases))
        object.__setattr__(self, "roots", MappingProxyType(
            {name: checked_path(path) for name, path in self.roots.items()}))
        check_invariants(self)


def check_invariants(home: DataHome) -> None:
    if not home.roots or set(home.roots) - set(PERSISTENT_ROOTS):
        raise DataHomeError("Неизвестный или пустой набор постоянных корней")
    protected = [checked_path(home.app), checked_path(home.releases)]
    roots = [checked_path(path) for path in home.roots.values()]
    for root in roots:
        if root == Path(root.anchor):
            raise DataHomeError("Постоянный каталог не может быть корнем диска")
        if any(contains(app, root) or contains(root, app) for app in protected):
            raise DataHomeError("Постоянные данные пересекаются с app/releases")
    for index, root in enumerate(roots):
        if any(contains(root, other) or contains(other, root) for other in roots[index + 1:]):
            raise DataHomeError("Постоянные корни пересекаются")


def resolve_datahome(
    manifest: dict[str, Any], install_root: str | Path, *,
    environ: Mapping[str, str] | None = None,
    app_dir: str | Path | None = None, releases_dir: str | Path | None = None,
) -> DataHome:
    """v2: paths относительно install_root. v1: явные env/app/releases от адаптера.

    Окружение процесса не читается. Содержимое каталогов и секреты не читаются.
    Отсутствующие каталоги допустимы для планирования; ничего не создаётся.
    """
    manifest = validate_manifest(manifest)
    base = checked_path(install_root)
    if manifest["schema_version"] == 2:
        paths = {}
        for name, value in manifest["paths"].items():
            if ((os.name != "nt" and PureWindowsPath(value).drive)
                    or (os.name == "nt" and value.startswith("/"))):
                raise DataHomeError("Путь другой ОС не разрешается на этом хосте")
            path = Path(value.replace("\\", "/"))
            paths[name] = checked_path(path if path.is_absolute() else base / path)
        return DataHome(manifest["product_id"], paths["app"], paths["releases"],
                        {name: paths[name] for name in PERSISTENT_ROOTS})
    value = (environ or {}).get(manifest["data_home_env"])
    if not value or app_dir is None or releases_dir is None:
        raise DataHomeError("v1 требует явные data_home_env, app_dir и releases_dir от адаптера")
    return DataHome(manifest["product_id"], checked_path(app_dir), checked_path(releases_dir),
                    {"data": checked_path(value)})


@dataclass(frozen=True)
class Relocation:
    root: str
    source: Path
    destination: Path


def relocate_plan(source: DataHome, destination: DataHome) -> tuple[Relocation, ...]:
    """Только план: нет mkdir, записи настроек или перемещения файлов."""
    check_invariants(source)
    check_invariants(destination)
    if source.product_id != destination.product_id or source.roots.keys() != destination.roots.keys():
        raise DataHomeError("Планы относятся к разным продуктам или наборам корней")
    result = []
    for name in sorted(source.roots):
        old, new = source.roots[name], destination.roots[name]
        if old == new:
            continue
        if any(contains(root, new) or contains(new, root) for root in source.roots.values()):
            raise DataHomeError("Назначение пересекается с исходными данными")
        if any(contains(code, new) or contains(new, code) for code in (source.app, source.releases)):
            raise DataHomeError("Назначение пересекается с исходными app/releases")
        if new.exists() and (not new.is_dir() or any(new.iterdir())):
            raise DataHomeError("Назначение переноса должно быть пустым каталогом")
        result.append(Relocation(name, old, new))
    return tuple(result)
