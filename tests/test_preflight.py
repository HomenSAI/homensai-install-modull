"""Предварительная проверка без записи в тестовую установку."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.manifest import load_manifest
from shared.preflight import NAMES, PreflightError, preflight_fresh

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def manifest():
    value = load_manifest(ROOT / "examples/rent.manifest.json")
    value["product_id"] = "test-product"
    value["paths"] = {name: name for name in value["paths"]}
    return value


def sizes(value=1):
    return {name: value for name in NAMES}


def test_fresh_plan_does_not_create_directories(manifest, tmp_path):
    root = tmp_path / "new-installation"
    plan = preflight_fresh(manifest, root, required_bytes=sizes(), reserve_bytes=100)
    assert plan["checks_passed"] is True and plan["product_id"] == "test-product"
    assert plan["directories"]["data"] == str(root / "data")
    assert plan["volumes"][0]["required_bytes"] == 7
    assert plan["volumes"][0]["reserve_bytes"] == 100
    assert "writability" in plan["not_checked"] and "package_authenticity" in plan["not_checked"]
    assert not root.exists()


def test_existing_empty_directories_are_not_modified(manifest, tmp_path):
    root = tmp_path / "install"
    (root / "data").mkdir(parents=True)
    assert preflight_fresh(manifest, root, required_bytes=sizes(0), reserve_bytes=0)["checks_passed"]
    assert list((root / "data").iterdir()) == []
    assert sorted(p.name for p in root.iterdir()) == ["data"]


@pytest.mark.parametrize("name", NAMES)
def test_refuse_nonempty_roots_and_preserve_data(manifest, tmp_path, name):
    root = tmp_path / "install"
    path = root / name
    path.mkdir(parents=True)
    (path / "keep.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(PreflightError):
        preflight_fresh(manifest, root, required_bytes=sizes(), reserve_bytes=0)
    assert (path / "keep.txt").read_text(encoding="utf-8") == "preserve"


def test_refuse_file_at_target_or_ancestor(manifest, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "data").write_bytes(b"preserve")
    with pytest.raises(PreflightError):
        preflight_fresh(manifest, root, required_bytes=sizes())
    assert (root / "data").read_bytes() == b"preserve"


def test_refuse_code_directory_overlap(manifest, tmp_path):
    manifest["paths"]["app"] = "releases/app"
    with pytest.raises(PreflightError):
        preflight_fresh(manifest, tmp_path / "install", required_bytes=sizes())


@pytest.mark.parametrize("bad", [-1, True, 1.5, 2**63, "1"])
def test_invalid_size_estimates(manifest, tmp_path, bad):
    requirements = sizes()
    requirements["data"] = bad
    with pytest.raises(PreflightError):
        preflight_fresh(manifest, tmp_path / "install", required_bytes=requirements)


def test_complete_size_map_is_required(manifest, tmp_path):
    requirements = sizes()
    del requirements["backup"]
    with pytest.raises(PreflightError):
        preflight_fresh(manifest, tmp_path / "install", required_bytes=requirements)


def test_space_is_summed_per_device_and_reserve_applied_once(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr("shared.preflight.shutil.disk_usage", lambda _: SimpleNamespace(free=109))
    with pytest.raises(PreflightError, match="места"):
        preflight_fresh(manifest, tmp_path / "install", required_bytes=sizes(10), reserve_bytes=40)
    monkeypatch.setattr("shared.preflight.shutil.disk_usage", lambda _: SimpleNamespace(free=110))
    assert preflight_fresh(manifest, tmp_path / "install", required_bytes=sizes(10), reserve_bytes=40)["checks_passed"]


def test_reject_symlinked_root(manifest, tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Символические ссылки недоступны на этой ОС")
    with pytest.raises(PreflightError):
        preflight_fresh(manifest, link / "installation", required_bytes=sizes())
    assert not list(actual.iterdir())


def test_reject_v1_and_root_installation(manifest, tmp_path):
    v1 = load_manifest(ROOT / "examples/jobpilot.manifest.json")
    with pytest.raises(PreflightError):
        preflight_fresh(v1, tmp_path / "install", required_bytes=sizes())
    with pytest.raises(PreflightError):
        preflight_fresh(manifest, Path(tmp_path.anchor), required_bytes=sizes())
