"""Единый пакет: целостность, отказ от опасного ввода и отсутствие перезаписи."""

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import zipfile

import pytest

from shared.archive import _info
from shared.manifest import load_manifest
from shared.package import PackageError, build_package, verify_package

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def product():
    return load_manifest(ROOT / "examples/jobpilot.manifest.json")


@pytest.fixture
def payload(tmp_path):
    path = tmp_path / "payload"
    (path / "windows").mkdir(parents=True)
    (path / "linux").mkdir()
    (path / "windows" / "app.txt").write_bytes(b"synthetic windows")
    (path / "linux" / "app.txt").write_bytes(b"synthetic linux")
    return path


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def test_build_both_platforms_and_reproducible(tmp_path, payload, product):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    result = build_package(product, "1.0", payload, first)
    assert result == build_package(product, "1.0", payload, second)
    assert first.read_bytes() == second.read_bytes()
    meta = verify_package(first, expected_sha256=result["sha256"])
    assert meta["product_id"] == product["product_id"]
    assert [entry["path"] for entry in meta["files"]] == ["linux/app.txt", "windows/app.txt"]
    assert meta["manifest"] == product


def test_no_overwrite_or_output_inside_payload(tmp_path, payload, product):
    target = tmp_path / "existing.zip"
    target.write_bytes(b"preserve")
    with pytest.raises(PackageError):
        build_package(product, "1", payload, target)
    assert target.read_bytes() == b"preserve"
    with pytest.raises(PackageError):
        build_package(product, "1", payload, payload / "output.zip")
    assert not (payload / "output.zip").exists()


def test_external_trusted_hash_is_required_and_rejects_changes(tmp_path, payload, product):
    target = tmp_path / "bundle.zip"
    expected = build_package(product, "1", payload, target)["sha256"]
    for bad in ("", "0" * 64, None, "G" * 64):
        with pytest.raises(PackageError):
            verify_package(target, expected_sha256=bad)
    with target.open("ab") as stream:
        stream.write(b"change")
    with pytest.raises(PackageError):
        verify_package(target, expected_sha256=expected)


def write_forged(path, metadata, content, *, extras=(), info=None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(_info("package.json"), json.dumps(metadata).encode())
        archive.writestr(info or _info("payload/" + metadata["files"][0]["path"]), content)
        for entry in extras:
            archive.writestr(_info(entry), b"unlisted")


@pytest.mark.parametrize("mutation", ["traversal", "extra", "wrong_hash", "product", "collision", "symlink"])
def test_reject_forged_internal_structure_even_with_matching_outer_hash(tmp_path, product, mutation):
    data = b"synthetic"
    metadata = {"format": 1, "product_id": product["product_id"], "version": "1",
                "manifest": deepcopy(product),
                "files": [{"path": "app.txt", "size": len(data), "sha256": sha256(data).hexdigest()}]}
    extras = ()
    info = None
    if mutation == "traversal":
        metadata["files"][0]["path"] = "../escape"
    elif mutation == "extra":
        extras = ("payload/extra",)
    elif mutation == "wrong_hash":
        metadata["files"][0]["sha256"] = "a" * 64
    elif mutation == "product":
        metadata["product_id"] = "other-product"
    elif mutation == "collision":
        metadata["files"].append({"path": "APP.TXT", "size": len(data), "sha256": sha256(data).hexdigest()})
    elif mutation == "symlink":
        info = _info("payload/app.txt")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
    target = tmp_path / "forged.zip"
    write_forged(target, metadata, data, extras=extras, info=info)
    with pytest.raises(PackageError):
        verify_package(target, expected_sha256=digest(target))


def test_symlink_in_source_is_refused(tmp_path, payload, product):
    try:
        os.symlink(payload / "linux/app.txt", payload / "link")
    except (OSError, NotImplementedError):
        pytest.skip("Символические ссылки недоступны на этой ОС")
    target = tmp_path / "bundle.zip"
    with pytest.raises(PackageError):
        build_package(product, "1", payload, target)
    assert not target.exists()


def test_invalid_release_identifier(product, payload, tmp_path):
    with pytest.raises(PackageError):
        build_package(product, "../escape", payload, tmp_path / "bundle.zip")


def test_v2_requires_declared_runtime_files_and_hashes(payload, tmp_path):
    manifest = load_manifest(ROOT / "examples/rent.manifest.json")
    target = tmp_path / "bundle.zip"
    with pytest.raises(PackageError):
        build_package(manifest, "1", payload, target)
    assert not target.exists()
    artifacts = [*manifest["runtime"]["lock_files"].values(), *manifest["runtime"]["vendor_wheels"]]
    for index, item in enumerate(artifacts):
        data = ("synthetic runtime artifact " + str(index)).encode()
        file = payload / item["file"]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        item["sha256"] = sha256(data).hexdigest()
    result = build_package(manifest, "1", payload, target)
    assert verify_package(target, expected_sha256=result["sha256"])["manifest"] == manifest


@pytest.mark.parametrize('as_directory', [False, True])
def test_incomplete_crypto_block_is_never_packaged(tmp_path, payload, product, as_directory):
    block = payload / 'nested' / '.HOMENSAI-CRYPTO-INCOMPLETE'
    block.parent.mkdir()
    if as_directory:
        block.mkdir()
    else:
        block.write_text('incomplete')
    target = tmp_path / 'package.zip'
    with pytest.raises(PackageError, match='Незавершённый'):
        build_package(product, '1', payload, target)
    assert not target.exists()


def test_verifier_rejects_incomplete_crypto_marker_in_forged_package(tmp_path, product):
    data = b'incomplete'
    metadata = {'format': 1, 'product_id': product['product_id'], 'version': '1',
                'manifest': product,
                'files': [{'path': 'nested/.homensai-crypto-incomplete', 'size': len(data),
                           'sha256': sha256(data).hexdigest()}]}
    target = tmp_path / 'forged.zip'
    write_forged(target, metadata, data)
    with pytest.raises(PackageError, match='Незавершённый'):
        verify_package(target, expected_sha256=digest(target))


def test_reverification_reads_bytes_after_shared_mmap_change(tmp_path, payload, product):
    import mmap
    package = tmp_path / 'mmap.zip'
    receipt = build_package(product, '1.0', payload, package)
    with package.open('r+b') as stream, mmap.mmap(stream.fileno(), 0) as mapped:
        mapped[0:1] = mapped[0:1]  # страница уже отображена для записи и «грязная»
        assert verify_package(package, expected_sha256=receipt['sha256'])['version'] == '1.0'
        mapped[-1:] = bytes([mapped[-1] ^ 1])  # изменение без write(): stat-подпись может не измениться
        with pytest.raises(PackageError):
            verify_package(package, expected_sha256=receipt['sha256'])
