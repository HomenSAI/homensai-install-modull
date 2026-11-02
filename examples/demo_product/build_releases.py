# HomenS.AI · © 2026 Serhii Khomenko
"""Подготовка двух отдельных вымышленных payload в пустой тестовый каталог."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


def build(destination: Path) -> None:
    source = Path(__file__).resolve().parent
    destination = destination.resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("Каталог назначения пересекается с исходниками")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Требуется пустой каталог назначения")
    destination.mkdir(parents=True, exist_ok=True)
    for version in ("1", "2"):
        release = destination / ("app-" + version)
        release.mkdir()
        shutil.copyfile(source / "main.py", release / "main.py")
        shutil.copyfile(source / "releases" / version / "release.json", release / "release.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Собрать тестовые выпуски")
    parser.add_argument("destination", type=Path)
    build(parser.parse_args().destination)
