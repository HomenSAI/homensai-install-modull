"""Извлечение только проверенного подписанного тестового пакета."""

from pathlib import Path
import os

import pytest

import shared.package_prepare as preparation
from shared.manifest import load_manifest
from shared.package import PackageError, build_package
from shared.package_signing import sign_package

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def signed(tmp_path):
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    ser = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    manifest = load_manifest(ROOT / "examples/jobpilot.manifest.json")
    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    (source / "bin/app.txt").write_bytes(b"synthetic app")
    (source / "config.json").write_bytes(b'{"synthetic":true}')
    package = tmp_path / "package.zip"
    build_package(manifest, "1.0", source, package)
    envelope = sign_package(package, private_key=private,
                            expected_product_id=manifest["product_id"], expected_version="1.0")
    return package, envelope, public, private, manifest, source


def prepare(signed, destination, **changes):
    package, envelope, public, _, manifest, _ = signed
    options = {"trusted_public_key": public, "expected_product_id": manifest["product_id"],
               "expected_version": "1.0"}
    options.update(changes)
    return preparation.prepare_signed_package(package, envelope, destination, **options)


def test_extract_verified_payload_to_new_directory(signed, tmp_path):
    destination = tmp_path / "prepared"
    result = prepare(signed, destination)
    assert result["version"] == "1.0"
    assert set(result["files"]) == {"bin/app.txt", "config.json"}
    assert (destination / "bin/app.txt").read_bytes() == b"synthetic app"
    assert (destination / "config.json").read_bytes() == b'{"synthetic":true}'
    assert not (destination / preparation.INCOMPLETE).exists()
    assert not list(tmp_path.glob(".homensai-prepare-*"))


def test_bad_signature_and_existing_target_write_nothing(signed, tmp_path):
    destination = tmp_path / "prepared"
    with pytest.raises(PackageError):
        prepare(signed, destination, trusted_public_key=b"x" * 32)
    assert not destination.exists()
    destination.mkdir()
    (destination / "keep").write_text("preserve")
    with pytest.raises(PackageError):
        prepare(signed, destination)
    assert (destination / "keep").read_text() == "preserve"


def test_source_replacement_after_snapshot_does_not_change_prepared_bytes(signed, tmp_path, monkeypatch):
    original = preparation.verify_signed_package
    calls = []
    def verify_then_replace(path, envelope, **kwargs):
        metadata = original(path, envelope, **kwargs)
        calls.append(path)
        if len(calls) == 1:
            signed[0].write_bytes(b"replaced untrusted source")
        return metadata
    monkeypatch.setattr(preparation, "verify_signed_package", verify_then_replace)
    destination = tmp_path / "prepared"
    prepare(signed, destination)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert (destination / "bin/app.txt").read_bytes() == b"synthetic app"


def test_snapshot_modification_before_publication_is_rejected(signed, tmp_path, monkeypatch):
    original = preparation.verify_signed_package
    calls = []
    def verify_then_modify(path, envelope, **kwargs):
        metadata = original(path, envelope, **kwargs)
        calls.append(path)
        if len(calls) == 1:
            with path.open("ab") as stream:
                stream.write(b"tamper")
        return metadata
    monkeypatch.setattr(preparation, "verify_signed_package", verify_then_modify)
    destination = tmp_path / "prepared"
    with pytest.raises(PackageError):
        prepare(signed, destination)
    assert not destination.exists()


def test_partial_publication_leaves_marker_and_never_overwrites(signed, tmp_path, monkeypatch):
    def fail_link(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(os, "link", fail_link)
    destination = tmp_path / "prepared"
    with pytest.raises(PackageError):
        prepare(signed, destination)
    assert (destination / preparation.INCOMPLETE).read_bytes() == b"incomplete\n"
    with pytest.raises(PackageError):
        prepare(signed, destination)


def test_reserved_marker_name_is_rejected(signed, tmp_path):
    _, _, _, private, manifest, source = signed
    (source / preparation.INCOMPLETE).write_text("synthetic")
    package = tmp_path / "reserved.zip"
    build_package(manifest, "1.0", source, package)
    envelope = sign_package(package, private_key=private,
                            expected_product_id=manifest["product_id"], expected_version="1.0")
    destination = tmp_path / "prepared"
    with pytest.raises(PackageError):
        preparation.prepare_signed_package(package, envelope, destination, trusted_public_key=signed[2],
                                           expected_product_id=manifest["product_id"], expected_version="1.0")
    assert not destination.exists()
