"""Пути только вымышленного продукта; никакого чтения окружения или живых данных."""

from copy import deepcopy
from pathlib import Path

import pytest

from shared.datahome import DataHome, DataHomeError, checked_path, relocate_plan, resolve_datahome
from shared.manifest import ManifestError, load_manifest


@pytest.fixture
def product():
    manifest = load_manifest(Path(__file__).resolve().parents[1] / "examples/rent.manifest.json")
    manifest["product_id"] = "sample-product"
    manifest["product_name"] = "Пример"
    manifest["paths"] = {key: key for key in manifest["paths"]}
    return manifest


def test_resolve_relative_no_side_effects(product, tmp_path):
    home = resolve_datahome(product, tmp_path / "installation")
    assert home.product_id == "sample-product"
    assert home.app == tmp_path / "installation/app"
    assert home.releases == tmp_path / "installation/releases"
    assert dict(home.roots) == {name: tmp_path / "installation" / name
                                for name in ("data", "backup", "services", "config", "runtime")}
    assert not list(tmp_path.iterdir())
    with pytest.raises(TypeError):
        home.roots["data"] = tmp_path


def test_absolute_paths(product, tmp_path):
    product["paths"] = {name: str(tmp_path / name) for name in product["paths"]}
    assert resolve_datahome(product, tmp_path).roots["data"] == tmp_path / "data"


@pytest.mark.parametrize("name", ["data", "backup", "services", "config", "runtime"])
@pytest.mark.parametrize("protected", ["app", "releases"])
def test_refuse_data_inside_code(product, tmp_path, name, protected):
    product["paths"][name] = f"{protected}/persistent"
    with pytest.raises((DataHomeError, ManifestError)):
        resolve_datahome(product, tmp_path)


def test_refuse_code_inside_data(product, tmp_path):
    product["paths"]["app"] = "data/code"
    with pytest.raises(DataHomeError, match="app/releases"):
        resolve_datahome(product, tmp_path)


@pytest.mark.parametrize("other", ["data", "data/config"])
def test_refuse_overlapping_roots(product, tmp_path, other):
    product["paths"]["config"] = other
    with pytest.raises(DataHomeError, match="пересекаются"):
        resolve_datahome(product, tmp_path)


def test_casefold_and_segment_comparison(product, tmp_path):
    product["paths"]["data"] = "app-data"
    assert resolve_datahome(product, tmp_path).roots["data"] == tmp_path / "app-data"
    product["paths"]["config"] = "APP-DATA"
    with pytest.raises(DataHomeError):
        resolve_datahome(product, tmp_path)


def test_v1_requires_explicit_adapter_values(tmp_path, monkeypatch):
    manifest = load_manifest(Path(__file__).resolve().parents[1] / "examples/jobpilot.manifest.json")
    manifest["data_home_env"] = "SAMPLE_DATA_HOME"
    monkeypatch.setenv("SAMPLE_DATA_HOME", str(tmp_path / "unrequested"))
    with pytest.raises(DataHomeError, match="v1"):
        resolve_datahome(manifest, tmp_path)
    home = resolve_datahome(manifest, tmp_path, environ={"SAMPLE_DATA_HOME": str(tmp_path / "persistent")},
                            app_dir=tmp_path / "app", releases_dir=tmp_path / "releases")
    assert dict(home.roots) == {"data": tmp_path / "persistent"}
    with pytest.raises(DataHomeError):
        resolve_datahome(manifest, tmp_path, environ={"SAMPLE_DATA_HOME": str(tmp_path / "app/data")},
                         app_dir=tmp_path / "app", releases_dir=tmp_path / "releases")


@pytest.mark.parametrize("value", ["relative", "../escape", "C:relative", "bad\npath",
                                  "//server/share", "C:/sample:stream", "/sample/trailing."])
def test_bad_paths(value):
    with pytest.raises(DataHomeError):
        checked_path(value)


def test_drive_root_refused(tmp_path):
    with pytest.raises(DataHomeError):
        DataHome("sample", tmp_path / "app", tmp_path / "releases", {"data": Path(tmp_path.anchor)})


def test_foreign_windows_path_on_posix(product, tmp_path):
    import os
    if os.name == "nt":
        pytest.skip("Проверка пути Windows на POSIX")
    product["paths"] = {key: "C:/sample/" + key for key in product["paths"]}
    with pytest.raises(DataHomeError, match="другой ОС"):
        resolve_datahome(product, tmp_path)


def test_foreign_posix_path_on_windows(product, tmp_path):
    import os
    if os.name != "nt":
        pytest.skip("Проверка пути POSIX на Windows")
    product["paths"] = {key: "/sample/" + key for key in product["paths"]}
    with pytest.raises(DataHomeError, match="другой ОС"):
        resolve_datahome(product, tmp_path)


def test_reparse_attribute_refused_without_symlink_privileges(tmp_path, monkeypatch):
    from types import SimpleNamespace
    original = Path.lstat
    def simulated(self, *args, **kwargs):
        info = original(self, *args, **kwargs)
        if self == tmp_path:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", simulated)
    with pytest.raises(DataHomeError, match="reparse"):
        checked_path(tmp_path / "child")


def test_relocation_cannot_target_old_application(product, tmp_path):
    source = resolve_datahome(product, tmp_path / "old")
    target = resolve_datahome(product, source.app)
    with pytest.raises(DataHomeError, match="app/releases"):
        relocate_plan(source, target)


def test_relocation_only_plan(product, tmp_path):
    before = deepcopy(product)
    source = resolve_datahome(product, tmp_path / "old")
    target = resolve_datahome(product, tmp_path / "new")
    plan = relocate_plan(source, target)
    assert [entry.root for entry in plan] == sorted(source.roots)
    assert all(entry.source == source.roots[entry.root] and entry.destination == target.roots[entry.root]
               for entry in plan)
    assert product == before
    assert not list(tmp_path.iterdir())
    assert relocate_plan(source, source) == ()


def test_relocation_refuses_nonempty_or_conflicting_product(product, tmp_path):
    source = resolve_datahome(product, tmp_path / "old")
    target = resolve_datahome(product, tmp_path / "new")
    target.roots["data"].mkdir(parents=True)
    (target.roots["data"] / "keep").write_bytes(b"sample")
    with pytest.raises(DataHomeError, match="пустым"):
        relocate_plan(source, target)
    other = DataHome("other", target.app, target.releases, target.roots)
    with pytest.raises(DataHomeError, match="разным"):
        relocate_plan(source, other)


def test_relocation_refuses_source_overlap(product, tmp_path):
    source = resolve_datahome(product, tmp_path)
    target = DataHome(source.product_id, source.app, source.releases,
                      {name: path / "new" for name, path in source.roots.items()})
    with pytest.raises(DataHomeError, match="исходными"):
        relocate_plan(source, target)


def test_symlink_ancestor_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("ОС не разрешает создание symlink без дополнительных привилегий")
    with pytest.raises(DataHomeError, match="reparse"):
        checked_path(link / "child")
