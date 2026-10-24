# HomenS.AI · © 2026 Serhii Khomenko
"""Полный ZIP в потоковой AES-256-GCM оболочке; cryptography необязателен.

Пароль поступает только из внедрённого callable. Формат и ограничения KDF
описаны в docs/ARCHIVE.md. До restore проверяются все теги, SHA-256 и ZIP.
"""

from __future__ import annotations

from contextlib import contextmanager
import getpass
import hashlib
import hmac
import io
import os
from pathlib import Path
import re
import struct
import warnings
import uuid

from shared import archive as plain
from shared.datahome import checked_path, check_invariants, contains, DataHomeError


MAGIC = b"HSAICRY\x00"
VERSION = 1
SCRYPT_N, SCRYPT_R, SCRYPT_P = 32768, 8, 1
CHUNK_SIZE = 1024 * 1024
MIN_CHUNK, MAX_CHUNK = 4096, 16 * 1024 * 1024
# magic, version, KDF id, N/r/p, salt, chunk size, size, SHA-256, nonce prefix
HEADER = struct.Struct(">8sBBIII16sIQ32s4s")
TAG_SIZE = 16


class ArchiveCryptoError(plain.ArchiveError):
    """Фиксированное сообщение, без пароля и исходного исключения."""


class CryptoUnavailableError(ArchiveCryptoError):
    pass


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise CryptoUnavailableError(
            "Шифрование недоступно: установите необязательный пакет cryptography "
            "из requirements-crypto.txt после заполнения владельцем хэшей"
        ) from None
    return AESGCM


def password_provider(*, env_var: str | None = None, environ=None, prompt=None):
    """Создать ленивый provider: только указанное имя env либо скрытый stdin prompt.

    Нет перебора окружения, чтения файлов или fallback при отсутствующей env.
    Неинтерактивный getpass с эхом запрещён. Источники внедряются для тестов.
    """
    if env_var is not None and (type(env_var) is not str or re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", env_var) is None):
        raise ArchiveCryptoError("Недопустимое имя переменной пароля")

    def provide():
        try:
            if env_var is not None:
                return (os.environ if environ is None else environ).get(env_var)
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return (getpass.getpass if prompt is None else prompt)("Пароль архива: ")
        except Exception:
            raise ArchiveCryptoError("Не удалось получить пароль архива без эха") from None
    return provide


def _password(provider):
    try:
        password = provider()
        if type(password) is not str or not password:
            raise ValueError
        return password
    except Exception:
        raise ArchiveCryptoError("Не удалось получить непустой пароль архива") from None


def _key(provider, salt):
    try:
        password = _password(provider)
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                              r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    except Exception:
        raise ArchiveCryptoError("Не удалось получить пароль архива или вычислить ключ scrypt") from None


def read_header(stream) -> dict:
    """Разобрать ограниченный заголовок без cryptography и без вычисления KDF.

    Это непроверенные метаданные: подлинность подтверждает только decrypt.
    """
    raw = stream.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise ArchiveCryptoError("Заголовок шифрованного архива обрезан")
    magic, version, kdf, n, r, p, salt, chunk, size, sha256, prefix = HEADER.unpack(raw)
    if (magic != MAGIC or version != VERSION or kdf != 1
            or (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
            or not MIN_CHUNK <= chunk <= MAX_CHUNK
            or max(1, (size + chunk - 1) // chunk) > 2**32):
        raise ArchiveCryptoError("Неподдерживаемый заголовок шифрованного архива")
    return {"version": version, "kdf": "scrypt", "n": n, "r": r, "p": p,
            "salt": salt, "chunk_size": chunk, "size": size, "sha256": sha256,
            "nonce_prefix": prefix, "raw": raw}


def is_encrypted(path: str | Path) -> bool:
    try:
        with checked_path(path).open("rb") as stream:
            return stream.read(len(MAGIC)) == MAGIC
    except (OSError, DataHomeError):
        raise ArchiveCryptoError("Архив недоступен") from None


@contextmanager
def _temporary(parent):
    """Эксклюзивный файл 0600 на POSIX; Windows наследует ACL каталога."""
    path = checked_path(parent / (".archive-crypto-" + uuid.uuid4().hex + ".tmp"))
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _destination(path):
    path = checked_path(path)
    if path.exists() or not path.parent.is_dir():
        raise ArchiveCryptoError("Архив уже существует или отсутствует каталог назначения")
    return path


def _frames(header):
    remaining = header["size"]
    for index in range(max(1, (remaining + header["chunk_size"] - 1) // header["chunk_size"])):
        length = min(remaining, header["chunk_size"])
        remaining -= length
        counter = index.to_bytes(8, "big")
        yield length, header["nonce_prefix"] + counter, header["raw"] + counter + struct.pack(">I", length)


def encrypt_archive(source: str | Path, destination: str | Path, *, password_provider,
                    chunk_size: int = CHUNK_SIZE) -> dict:
    """Проверить полный ZIP и опубликовать шифрованную копию без перезаписи."""
    aesgcm = _aesgcm()  # Даже неверные аргументы не скрывают отсутствие зависимости.
    try:
        if type(chunk_size) is not int or not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
            raise ArchiveCryptoError("Недопустимый размер блока шифрования")
        source, destination = checked_path(source), _destination(destination)
        manifest = plain.verify_archive(source)
        with source.open("rb") as incoming:
            size, digest = plain._digest(incoming)
            salt, prefix = os.urandom(16), os.urandom(4)
            raw = HEADER.pack(MAGIC, VERSION, 1, SCRYPT_N, SCRYPT_R, SCRYPT_P,
                              salt, chunk_size, size, bytes.fromhex(digest), prefix)
            incoming.seek(0)
            # Ограничение количества блоков такое же, как у читателя.
            header = read_header(io.BytesIO(raw))
            password = _password(password_provider)
            cipher = aesgcm(_key(lambda: password, salt))
            count = max(1, (size + chunk_size - 1) // chunk_size)
            plain._space(destination.parent, HEADER.size + size + count * TAG_SIZE)
            with _temporary(destination.parent) as temporary:
                with temporary.open("wb") as output:
                    output.write(raw)
                    actual = hashlib.sha256()
                    for length, nonce, aad in _frames(header):
                        block = incoming.read(length)
                        if len(block) != length:
                            raise ArchiveCryptoError("Архив изменился при шифровании")
                        actual.update(block)
                        output.write(cipher.encrypt(nonce, block, aad))
                    if incoming.read(1) or actual.hexdigest() != digest:
                        raise ArchiveCryptoError("Архив изменился при шифровании")
                    output.flush()
                    os.fsync(output.fileno())
                # Полная обратная проверка, включая ZIP, до публикации.
                verify_archive(temporary, password_provider=lambda: password,
                               expected_product_id=manifest["product_id"])
                os.link(temporary, destination)
        return manifest
    except (OSError, DataHomeError, OverflowError):
        raise ArchiveCryptoError("Не удалось создать проверенный шифрованный архив") from None


@contextmanager
def decrypted_archive(path: str | Path, *, password_provider,
                      expected_product_id: str | None = None):
    """Временный полный ZIP доступен только после GCM/SHA-256/verify_archive.

    Временный plaintext удаляется при выходе, в том числе после ошибки restore.
    Каталог шифрованного архива должен быть доступен для временной записи.
    """
    aesgcm = _aesgcm()
    try:
        from cryptography.exceptions import InvalidTag
        path = checked_path(path)
        with path.open("rb") as incoming:
            header = read_header(incoming)
            # Проверка точной длины до KDF/временной записи; нет доверия к size.
            count = max(1, (header["size"] + header["chunk_size"] - 1) // header["chunk_size"])
            if os.fstat(incoming.fileno()).st_size != HEADER.size + header["size"] + count * TAG_SIZE:
                raise ArchiveCryptoError("Шифрованный архив обрезан или содержит лишние блоки")
            cipher = aesgcm(_key(password_provider, header["salt"]))
            plain._space(path.parent, header["size"])
            with _temporary(path.parent) as temporary:
                digest = hashlib.sha256()
                with temporary.open("wb") as output:
                    for length, nonce, aad in _frames(header):
                        block = incoming.read(length + TAG_SIZE)
                        if len(block) != length + TAG_SIZE:
                            raise ArchiveCryptoError("Шифрованный архив обрезан")
                        try:
                            decoded = cipher.decrypt(nonce, block, aad)
                        except InvalidTag:
                            raise ArchiveCryptoError("Неверный пароль или повреждён шифрованный архив") from None
                        digest.update(decoded)
                        output.write(decoded)
                    if incoming.read(1) or not hmac.compare_digest(digest.digest(), header["sha256"]):
                        raise ArchiveCryptoError("Не совпадает SHA-256 полного архива")
                    output.flush()
                    os.fsync(output.fileno())
                manifest = plain.verify_archive(temporary, expected_product_id=expected_product_id)
                yield temporary, manifest
    except (OSError, DataHomeError):
        raise ArchiveCryptoError("Не удалось расшифровать или прочитать архив") from None


def build_encrypted_archive(home, destination: str | Path, *, password_provider) -> dict:
    """Полный архив остановленных данных; промежуточный ZIP удаляется."""
    _aesgcm()
    try:
        check_invariants(home)
        destination = _destination(destination)
        if any(contains(root, destination) for root in (home.app, home.releases, *home.roots.values())):
            raise ArchiveCryptoError("Архив должен быть вне app/releases и архивируемых корней")
        # Отдельный каталог позволяет build_archive эксклюзивно опубликовать ZIP.
        directory = destination.parent / (".archive-build-" + uuid.uuid4().hex)
        # На Windows Python 3.13+ трактует 0700 как специальную DACL.
        # Наследуем ACL подготовленного вызывающей стороной каталога.
        directory.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        try:
            source = directory / "full.zip"
            plain.build_archive(home, source)
            return encrypt_archive(source, destination, password_provider=password_provider)
        finally:
            (directory / "full.zip").unlink(missing_ok=True)
            directory.rmdir()
    except (OSError, DataHomeError):
        raise ArchiveCryptoError("Не удалось создать полный шифрованный архив") from None


def verify_archive(path: str | Path, *, password_provider=None,
                   expected_product_id: str | None = None) -> dict:
    """Автоопределение по magic, независимо от расширения; ZIP работает без crypto."""
    if not is_encrypted(path):
        return plain.verify_archive(path, expected_product_id=expected_product_id)
    with decrypted_archive(path, password_provider=password_provider,
                           expected_product_id=expected_product_id) as (_, manifest):
        return manifest


def restore_archive(path: str | Path, target: str | Path, *, password_provider=None,
                    expected_product_id: str | None = None) -> dict:
    """Ни одной записи в target до полной аутентификации и проверки inner ZIP."""
    if not is_encrypted(path):
        return plain.restore_archive(path, target, expected_product_id=expected_product_id)
    with decrypted_archive(path, password_provider=password_provider,
                           expected_product_id=expected_product_id) as (temporary, _):
        return plain.restore_archive(temporary, target, expected_product_id=expected_product_id)
