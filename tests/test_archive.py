"""Архивы только синтетических данных tmp_path, без сети и реальных секретов."""

import hashlib
import json
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import zipfile

import pytest

import shared.archive as module
from shared.archive import ArchiveError, build_archive, restore_archive, verify_archive
from shared.datahome import DataHome


@pytest.fixture
def home(tmp_path):
    roots = {name: tmp_path / "source" / name for name in ("data", "backup", "services", "config", "runtime")}
    for root in roots.values():
        root.mkdir(parents=True)
    (roots["data"] / "z.bin").write_bytes(bytes(range(256)) * 4097)
    (roots["data"] / "empty").mkdir()
    (roots["data"] / "sub").mkdir()
    (roots["data"] / "sub/пример.txt").write_bytes("Синтетический пример\n".encode())
    (roots["data"] / "a.txt").write_bytes(b"first")
    (roots["config"] / "sample.json").write_bytes(b'{"example": true}')
    (roots["backup"] / "previous.bin").write_bytes(b"synthetic previous backup")
    (roots["runtime"] / "empty-file").write_bytes(b"")
    return DataHome("sample-product", tmp_path / "app", tmp_path / "releases", roots)


def snapshot(root):
    return {path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*")}


def rewrite(source, destination, transform):
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as output:
        for info in original.infolist():
            data = original.read(info)
            info, data = transform(info, data)
            output.writestr(info, data)


def test_create_verify_and_restore_roundtrip(home, tmp_path):
    before = {name: snapshot(root) for name, root in home.roots.items()}
    path = tmp_path / "full.zip"
    manifest = build_archive(home, path)
    assert verify_archive(path, expected_product_id=home.product_id) == manifest
    assert manifest["roots"] == sorted(home.roots)
    assert len(manifest["files"]) == 6
    for item in manifest["files"]:
        parts = item["path"].split("/")
        content = home.roots[parts[1]].joinpath(*parts[2:]).read_bytes()
        assert item["size"] == len(content)
        assert item["sha256"] == hashlib.sha256(content).hexdigest()
    target = tmp_path / "restored"
    target.mkdir()
    assert restore_archive(path, target, expected_product_id=home.product_id) == manifest
    assert {name: snapshot(target / "roots" / name) for name in home.roots} == before
    assert {name: snapshot(root) for name, root in home.roots.items()} == before


def test_deterministic_order_and_bytes(home, tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    manifest = build_archive(home, first)
    for root in home.roots.values():
        for path in root.rglob("*"):
            if path.is_file():
                path.touch()
    build_archive(home, second)
    assert first.read_bytes() == second.read_bytes()
    assert manifest["directories"] == sorted(manifest["directories"])
    assert manifest["files"] == sorted(manifest["files"], key=lambda item: item["path"])
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist()[-1] == "manifest.json"
        assert archive.namelist()[:-1] == sorted(archive.namelist()[:-1])


def test_corrupt_byte_detected_before_restore_writes(home, tmp_path):
    path = tmp_path / "full.zip"
    build_archive(home, path)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("roots/data/a.txt")
        offset = info.header_offset
    raw = bytearray(path.read_bytes())
    name_size, extra_size = struct.unpack_from("<HH", raw, offset + 26)
    raw[offset + 30 + name_size + extra_size] ^= 1
    path.write_bytes(raw)
    with pytest.raises(ArchiveError):
        verify_archive(path)
    target = tmp_path / "restored"
    target.mkdir()
    with pytest.raises(ArchiveError):
        restore_archive(path, target)
    assert not list(target.iterdir())


def test_sha256_detects_crc_valid_modification(home, tmp_path):
    path, corrupt = tmp_path / "full.zip", tmp_path / "modified.zip"
    build_archive(home, path)
    rewrite(path, corrupt, lambda info, data: (info, b"other" if info.filename == "roots/data/a.txt" else data))
    with pytest.raises(ArchiveError, match="SHA-256"):
        verify_archive(corrupt)


@pytest.mark.parametrize("name", ["../escaped", "/absolute", "C:/escape", "roots/data/../../escape",
                                  "roots\\data\\escape", "roots/data/file:stream", "roots/data/CON",
                                  "roots/data/trailing.", "roots/data/./alias", "//server/share",
                                  "roots/data/x\x00ignored"])
def test_zip_slip_and_ambiguous_names(home, tmp_path, name):
    path = tmp_path / "malicious.zip"
    # Небезопасное имя проверяется до чтения manifest.json.
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"synthetic")
    with pytest.raises(ArchiveError):
        verify_archive(path)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ArchiveError):
        restore_archive(path, target)
    assert not list(target.iterdir())
    assert not (tmp_path / "escaped").exists()


def test_manifest_path_traversal(home, tmp_path):
    path, corrupt = tmp_path / "full.zip", tmp_path / "malicious.zip"
    build_archive(home, path)
    def change(info, data):
        if info.filename == "manifest.json":
            manifest = json.loads(data)
            manifest["files"][0]["path"] = "../escaped"
            data = json.dumps(manifest).encode()
        return info, data
    rewrite(path, corrupt, change)
    with pytest.raises(ArchiveError):
        verify_archive(corrupt)


def test_nonempty_restore_refused(home, tmp_path):
    path = tmp_path / "full.zip"
    build_archive(home, path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_bytes(b"unchanged")
    with pytest.raises(ArchiveError, match="пустой"):
        restore_archive(path, target)
    assert snapshot(target) == {"keep": b"unchanged"}


def test_restore_requires_existing_target(home, tmp_path):
    path = tmp_path / "full.zip"
    build_archive(home, path)
    with pytest.raises(ArchiveError):
        restore_archive(path, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_product_identity(home, tmp_path):
    path = tmp_path / "full.zip"
    build_archive(home, path)
    target = tmp_path / "target"
    target.mkdir()
    for operation in (lambda: verify_archive(path, expected_product_id="other"),
                      lambda: restore_archive(path, target, expected_product_id="other")):
        with pytest.raises(ArchiveError, match="другого продукта"):
            operation()
    assert not list(target.iterdir())


def test_space_refusal_build_and_restore(home, tmp_path, monkeypatch):
    path = tmp_path / "full.zip"
    build_archive(home, path)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    failed = tmp_path / "failed.zip"
    with pytest.raises(ArchiveError, match="места"):
        build_archive(home, failed)
    assert not failed.exists()
    assert not list(tmp_path.glob(".archive-*.tmp"))
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ArchiveError, match="места"):
        restore_archive(path, target)
    assert not list(target.iterdir())


def test_unreadable_file_refuses_full_archive(home, tmp_path, monkeypatch, capsys):
    denied = home.roots["config"] / "sample.json"
    original = Path.open
    def guarded(self, *args, **kwargs):
        if self == denied:
            raise PermissionError("synthetic error text must not be emitted")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    path = tmp_path / "failed.zip"
    with pytest.raises(ArchiveError) as caught:
        build_archive(home, path)
    assert "synthetic" not in str(caught.value)
    assert not path.exists()
    assert capsys.readouterr() == ("", "")


def test_missing_root_refuses_full_archive(home, tmp_path):
    home.roots["services"].rmdir()
    with pytest.raises(ArchiveError, match="Отсутствует"):
        build_archive(home, tmp_path / "failed.zip")


def test_no_overwrite_or_recursive_archive(home, tmp_path):
    existing = tmp_path / "keep.zip"
    existing.write_bytes(b"keep")
    for path in (existing, home.roots["backup"] / "recursive.zip", home.app / "archive.zip"):
        with pytest.raises(ArchiveError):
            build_archive(home, path)
    assert existing.read_bytes() == b"keep"


def test_reject_zip_symlink_and_extra_member(home, tmp_path):
    path, corrupt = tmp_path / "full.zip", tmp_path / "symlink.zip"
    build_archive(home, path)
    def change(info, data):
        if info.filename == "roots/data/a.txt":
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        return info, data
    rewrite(path, corrupt, change)
    with pytest.raises(ArchiveError):
        verify_archive(corrupt)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("extra", b"synthetic")
    with pytest.raises(ArchiveError, match="Состав"):
        verify_archive(path)


def test_duplicate_or_case_alias_rejected(home, tmp_path):
    path = tmp_path / "full.zip"
    build_archive(home, path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("roots/data/A.TXT", b"synthetic")
    with pytest.raises(ArchiveError):
        verify_archive(path)


def test_unstable_source_refuses_archive(home, tmp_path, monkeypatch):
    original = module._inventory
    def mutate_after_inventory(layout):
        result = original(layout)
        (home.roots["data"] / "a.txt").write_bytes(b"modified")
        return result
    monkeypatch.setattr(module, "_inventory", mutate_after_inventory)
    with pytest.raises(ArchiveError, match="изменились"):
        build_archive(home, tmp_path / "failed.zip")
    assert not list(tmp_path.glob(".archive-*.tmp"))
    assert not (tmp_path / "failed.zip").exists()


def test_no_partial_publication_on_write_error(home, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic disk full")
    monkeypatch.setattr(module.zipfile.ZipFile, "writestr", fail)
    with pytest.raises(ArchiveError):
        build_archive(home, tmp_path / "failed.zip")
    assert not list(tmp_path.glob(".archive-*.tmp"))
    assert not (tmp_path / "failed.zip").exists()


def test_restore_checks_written_bytes(home, tmp_path, monkeypatch):
    path = tmp_path / "full.zip"
    build_archive(home, path)
    target = tmp_path / "target"
    target.mkdir()
    original = module._digest
    def corrupt_output(stream, output=None):
        actual = original(stream, output)
        if output is not None and actual[0]:
            output.seek(0)
            output.write(b"!")
        return actual
    monkeypatch.setattr(module, "_digest", corrupt_output)
    with pytest.raises(ArchiveError, match="восстановленного"):
        restore_archive(path, target)


def test_source_link_refused(home, tmp_path):
    link = home.roots["data"] / "link"
    try:
        link.symlink_to(home.roots["config"], target_is_directory=True)
    except OSError:
        pytest.skip("ОС не разрешает создание symlink без дополнительных привилегий")
    with pytest.raises(ArchiveError, match="reparse"):
        build_archive(home, tmp_path / "failed.zip")


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO])
def test_special_source_file_refused_without_privileges(home, tmp_path, monkeypatch, kind):
    original = Path.lstat
    special = home.roots["data"] / "a.txt"
    def simulated(self, *args, **kwargs):
        info = original(self, *args, **kwargs)
        if self == special:
            return SimpleNamespace(st_mode=kind | 0o600, st_file_attributes=0)
        return info
    monkeypatch.setattr(Path, "lstat", simulated)
    with pytest.raises(ArchiveError):
        build_archive(home, tmp_path / "failed.zip")
    assert not (tmp_path / "failed.zip").exists()


def test_inventory_discovery_error_is_not_silently_skipped(home, tmp_path, monkeypatch):
    original = module.os.scandir
    def denied(path):
        if Path(path) == home.roots["data"] / "sub":
            raise PermissionError("synthetic")
        return original(path)
    monkeypatch.setattr(module.os, "scandir", denied)
    with pytest.raises(ArchiveError):
        build_archive(home, tmp_path / "failed.zip")


@pytest.mark.parametrize("change", [
    lambda value: value.update(format=True),
    lambda value: value.update(roots=["unknown"]),
    lambda value: value["directories"].remove("roots/data/sub"),
    lambda value: value["files"][0].update(size=True),
    lambda value: value["files"][0].update(sha256="invalid"),
    lambda value: value["files"].reverse(),
    lambda value: value["files"].append(value["files"][0]),
])
def test_invalid_manifest_contract(home, tmp_path, change):
    path, invalid = tmp_path / "full.zip", tmp_path / "invalid.zip"
    build_archive(home, path)
    def transform(info, data):
        if info.filename == "manifest.json":
            value = json.loads(data)
            change(value)
            data = json.dumps(value).encode()
        return info, data
    rewrite(path, invalid, transform)
    with pytest.raises(ArchiveError):
        verify_archive(invalid)


def test_v1_single_root_archive(tmp_path):
    root = tmp_path / "datahome"
    root.mkdir()
    (root / "sample").write_bytes(b"synthetic")
    home = DataHome("sample-product", tmp_path / "app", tmp_path / "releases", {"data": root})
    path = tmp_path / "full.zip"
    assert build_archive(home, path)["roots"] == ["data"]
    assert verify_archive(path)["files"][0]["path"] == "roots/data/sample"
