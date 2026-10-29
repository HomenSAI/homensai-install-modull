"""Проверка фактического PyInstaller data archive на границе DEV-сборки."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("PyInstaller", reason="DEV packaging tools are required")
from PyInstaller.archive.writers import CArchiveWriter

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reference_resource_builder", ROOT / "packaging/build_reference.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source with spaces"
    (root / "shared").mkdir(parents=True)
    (root / "homensai_install/wizard").mkdir(parents=True)
    (root / "shared/manifest.schema.json").write_bytes(b'{"type":"object"}')
    (root / "homensai_install/wizard/index.html").write_bytes(b"<main>Installer</main>")
    (root / "pyproject.toml").write_text(
        '[tool.setuptools.package-data]\nshared = ["manifest.schema.json"]\n'
        'homensai_install = ["wizard/*.html"]\n', encoding="utf-8")
    return root


def archive(tmp_path, source, names):
    path = tmp_path / "frozen.pkg"
    entries = [(name, str(source / name), True, "x") for name in names]
    CArchiveWriter(str(path), entries, pylib_name="libpython3.14.so.1.0")
    return path


def test_actual_archive_resources_are_checked_from_another_working_directory(source, tmp_path, monkeypatch):
    expected = BUILDER.frozen_resource_hashes(source)
    frozen = archive(tmp_path, source, expected)
    elsewhere = tmp_path / "unrelated caller"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = BUILDER.verify_frozen_resources(frozen, expected)
    assert result["files_checked"] == 2
    assert len(result["manifest_sha256"]) == 64


def test_actual_archive_missing_manifest_schema_is_rejected(source, tmp_path):
    expected = BUILDER.frozen_resource_hashes(source)
    frozen = archive(tmp_path, source, ["homensai_install/wizard/index.html"])
    with pytest.raises(RuntimeError, match="resource inventory"):
        BUILDER.verify_frozen_resources(frozen, expected)


def test_actual_archive_with_changed_resource_is_rejected(source, tmp_path):
    expected = BUILDER.frozen_resource_hashes(source)
    (source / "homensai_install/wizard/index.html").write_bytes(b"changed resource")
    frozen = archive(tmp_path, source, expected)
    with pytest.raises(RuntimeError, match="resource bytes"):
        BUILDER.verify_frozen_resources(frozen, expected)


def test_actual_archive_with_undeclared_resource_is_rejected(source, tmp_path):
    expected = BUILDER.frozen_resource_hashes(source)
    (source / "shared/unlisted.json").write_bytes(b"{}")
    frozen = archive(tmp_path, source, [*expected, "shared/unlisted.json"])
    with pytest.raises(RuntimeError, match="resource inventory"):
        BUILDER.verify_frozen_resources(frozen, expected)


def test_missing_declared_resource_stops_before_freeze(source):
    (source / "shared/manifest.schema.json").unlink()
    with pytest.raises(RuntimeError, match="pattern is empty"):
        BUILDER.frozen_resource_hashes(source)


def test_optional_empty_wheel_patterns_and_unlisted_files_are_not_packaged(source):
    project = source / "pyproject.toml"
    project.write_text(project.read_text().replace('["wizard/*.html"]',
                       '["wizard/*.html", "legal/*", "wizard/locales/*.json"]'), encoding="utf-8")
    (source / "shared/.gitkeep").write_bytes(b"")
    assert set(BUILDER.frozen_resource_hashes(source)) == {
        "shared/manifest.schema.json", "homensai_install/wizard/index.html"}
