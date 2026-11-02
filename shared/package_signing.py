# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальная отдельная Ed25519-подпись для ZIP-пакета.

Доверенный ключ, ожидаемые продукт и выпуск передаются отдельно вызывающей
стороной. Модуль не управляет ключами, сетевым доверием или установкой.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import re
import stat

from shared.archive import ArchiveError, _signature
from shared.datahome import DataHomeError, checked_path
from shared.package import (MAX_METADATA_BYTES, MAX_TOTAL_BYTES, PackageError, _HASH,
                            _VERSION, _object, _reject_constant, _sha256, verify_package)

MAX_ENVELOPE = 4096
DOMAIN = b"HOMENSAI-PACKAGE-SIGNATURE-v1\0"
_PRODUCT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError:
        raise PackageError("Проверка подписи недоступна: требуется cryptography") from None
    return Ed25519PrivateKey, Ed25519PublicKey, InvalidSignature, UnsupportedAlgorithm


def _canonical(claims: dict) -> bytes:
    return DOMAIN + json.dumps(claims, ensure_ascii=True, sort_keys=True,
                               separators=(",", ":")).encode("ascii")


def _claims(value: object) -> dict:
    if type(value) is not dict or set(value) != {"format", "algorithm", "product_id", "version", "sha256"}:
        raise PackageError("Недопустимые сведения подписи")
    if (type(value["format"]) is not int or value["format"] != 1
            or value["algorithm"] != "Ed25519"
            or type(value["product_id"]) is not str or _PRODUCT.fullmatch(value["product_id"]) is None
            or type(value["version"]) is not str or _VERSION.fullmatch(value["version"]) is None
            or type(value["sha256"]) is not str or _HASH.fullmatch(value["sha256"]) is None):
        raise PackageError("Недопустимые сведения подписи")
    return value


def sign_package(path: str | Path, *, private_key, expected_product_id: str, expected_version: str) -> bytes:
    """Подписать проверенный пакет; ключ предоставляет доверенная сборка."""
    PrivateKey, _, _, UnsupportedAlgorithm = _crypto()
    if not isinstance(private_key, PrivateKey):
        raise PackageError("Требуется ключ подписи Ed25519")
    if (type(expected_product_id) is not str or _PRODUCT.fullmatch(expected_product_id) is None
            or type(expected_version) is not str or _VERSION.fullmatch(expected_version) is None):
        raise PackageError("Требуются ожидаемые продукт и версия")
    try:
        path = checked_path(path)
        info = _signature(path)
        if not stat.S_ISREG(info[2]):
            raise PackageError("Требуется обычный файл пакета")
        if info[3] > MAX_TOTAL_BYTES + MAX_METADATA_BYTES + 64 * 1024 * 1024:
            raise PackageError("Пакет слишком велик")
        with path.open("rb") as stream:
            digest = _sha256(stream)
        metadata = verify_package(path, expected_sha256=digest)
        if metadata["product_id"] != expected_product_id or metadata["version"] != expected_version:
            raise PackageError("Пакет относится к другому продукту или выпуску")
        claims = _claims({"format": 1, "algorithm": "Ed25519", "product_id": metadata["product_id"],
                          "version": metadata["version"], "sha256": digest})
        signature = private_key.sign(_canonical(claims))
        return json.dumps({**claims, "signature": base64.b64encode(signature).decode("ascii")},
                          sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    except (OSError, ValueError, DataHomeError, ArchiveError, UnsupportedAlgorithm) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError("Не удалось подписать пакет") from None


def verify_signed_package(path: str | Path, envelope: bytes, *, trusted_public_key: bytes,
                          expected_product_id: str, expected_version: str) -> dict:
    """Проверить подпись и весь пакет по независимо доверенным ожиданиям."""
    _, PublicKey, InvalidSignature, UnsupportedAlgorithm = _crypto()
    if (type(envelope) is not bytes or not 0 < len(envelope) <= MAX_ENVELOPE
            or type(trusted_public_key) is not bytes or len(trusted_public_key) != 32
            or type(expected_product_id) is not str or _PRODUCT.fullmatch(expected_product_id) is None
            or type(expected_version) is not str or _VERSION.fullmatch(expected_version) is None):
        raise PackageError("Требуются доверенные ключ, продукт и версия")
    try:
        value = json.loads(envelope.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
        if type(value) is not dict or set(value) != {"format", "algorithm", "product_id", "version", "sha256", "signature"}:
            raise PackageError("Недопустимый формат подписи")
        encoded = value.pop("signature")
        if type(encoded) is not str or len(encoded) != 88:
            raise PackageError("Недопустимый формат подписи")
        signature = base64.b64decode(encoded, validate=True)
        if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != encoded:
            raise PackageError("Недопустимый формат подписи")
        claims = _claims(value)
        if claims["product_id"] != expected_product_id or claims["version"] != expected_version:
            raise PackageError("Подпись относится к другому продукту или выпуску")
        PublicKey.from_public_bytes(trusted_public_key).verify(signature, _canonical(claims))
        metadata = verify_package(path, expected_sha256=claims["sha256"])
        if metadata["product_id"] != expected_product_id or metadata["version"] != expected_version:
            raise PackageError("Пакет относится к другому продукту или выпуску")
        return metadata
    except (UnicodeError, ValueError, RecursionError, binascii.Error, InvalidSignature,
            UnsupportedAlgorithm) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError("Подпись пакета недействительна") from None
