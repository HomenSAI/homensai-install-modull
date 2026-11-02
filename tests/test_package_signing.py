"""Подпись пакета: только синтетические ключи и файлы во временном каталоге."""

import builtins
import json
from pathlib import Path

import pytest

from shared.manifest import load_manifest
from shared.package import PackageError, build_package
from shared.package_signing import sign_package, verify_signed_package

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def signed(tmp_path):
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    private_key = ed25519.Ed25519PrivateKey.generate()
    public = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    manifest = load_manifest(ROOT / "examples/jobpilot.manifest.json")
    source = tmp_path / "payload"
    source.mkdir()
    (source / "app.txt").write_bytes(b"synthetic payload")
    package = tmp_path / "package.zip"
    build_package(manifest, "1.0", source, package)
    envelope = sign_package(package, private_key=private_key,
                            expected_product_id=manifest["product_id"], expected_version="1.0")
    return package, envelope, public, private_key, manifest["product_id"]


def verify(signed, **changes):
    path, envelope, public, _, product = signed
    args = {"trusted_public_key": public, "expected_product_id": product, "expected_version": "1.0"}
    args.update(changes)
    return verify_signed_package(path, envelope, **args)


def test_sign_verify_roundtrip_and_determinism(signed):
    path, envelope, _, private, product = signed
    assert sign_package(path, private_key=private, expected_product_id=product, expected_version="1.0") == envelope
    metadata = verify(signed)
    assert metadata["product_id"] == product and metadata["version"] == "1.0"


def test_reject_unknown_key_and_untrusted_product_or_version(signed):
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    other = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    for options in ({"trusted_public_key": other}, {"expected_product_id": "other-product"},
                    {"expected_version": "2.0"}, {"trusted_public_key": b"short"}):
        with pytest.raises(PackageError):
            verify(signed, **options)
    path, _, _, private, _ = signed
    with pytest.raises(PackageError):
        sign_package(path, private_key=private, expected_product_id="other-product", expected_version="1.0")


@pytest.mark.parametrize("field,value", [
    ("format", True), ("format", 2), ("algorithm", "none"),
    ("product_id", "other-product"), ("version", "2.0"),
    ("sha256", "0" * 64), ("signature", "A" * 88),
    ("extra", "unexpected"),
])
def test_reject_tampered_envelope(signed, field, value):
    path, envelope, public, _, product = signed
    forged = json.loads(envelope)
    forged[field] = value
    with pytest.raises(PackageError):
        verify_signed_package(path, json.dumps(forged).encode(), trusted_public_key=public,
                              expected_product_id=product, expected_version="1.0")


def test_reject_modified_package(signed):
    path, _, _, _, _ = signed
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(PackageError):
        verify(signed)


def test_reject_duplicate_and_oversize_envelopes(signed):
    path, _, public, _, product = signed
    for envelope in (b'{"format":1,"format":1}', b" " * 4097, b"\xff", b"{}"):
        with pytest.raises(PackageError):
            verify_signed_package(path, envelope, trusted_public_key=public,
                                  expected_product_id=product, expected_version="1.0")


def test_missing_crypto_is_explicit_without_fallback(monkeypatch):
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("simulated")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(PackageError, match="cryptography"):
        verify_signed_package("/nonexistent", b"{}", trusted_public_key=b"a" * 32,
                              expected_product_id="demo", expected_version="1")
