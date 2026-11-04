"""K2: только синтетические архивы, без сети, процессов и настоящих секретов."""

import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess

import pytest

from shared import archive as plain, archive_crypto as crypto, ctl
from shared.adapters_fs import FilesystemPorts
from shared.datahome import resolve_datahome
from shared.manifest import load_manifest
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _atomic_write, operation_lock


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/rent.manifest.json"
PASSWORD = "СИНТЕТИЧЕСКИЙ-пароль-K2"
PAYLOAD = b"SYNTHETIC-K2-DATA" * 800


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Процессы запрещены")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


@pytest.fixture
def aes():
    pytest.importorskip("cryptography", reason="Необязательный cryptography отсутствует: AES-256-GCM не проверялся")
    return crypto._aesgcm()


@pytest.fixture
def home(tmp_path):
    home = resolve_datahome(load_manifest(EXAMPLE), tmp_path / "installation")
    for root in home.roots.values():
        root.mkdir(parents=True)
        (root / "empty").mkdir()
        (root / "sample.bin").write_bytes(PAYLOAD)
        (root / "zero").write_bytes(b"")
    return home


# Раскладки и параметры KDF закреплены здесь независимо от модуля: изменение
# формата должно быть явным, а v1 — читаться так, как его писала прежняя версия.
LAYOUT = {1: struct.Struct(">8sBBIII16sIQ32s4s"), 2: struct.Struct(">8sBBIII16sIQ4s")}
KDF = {1: (32768, 8, 1), 2: (131072, 8, 1)}


def header_bytes(layout=2, **changes):
    n, r, p = KDF[layout]
    values = dict(magic=b"HSAICRY\x00", version=layout, kdf=1, n=n, r=r, p=p, salt=b"s" * 16,
                  chunk=4096, size=0)
    if layout == 1:
        values["sha256"] = hashlib.sha256(b"").digest()
    values["prefix"] = b"n" * 4
    values.update(changes)
    return LAYOUT[layout].pack(*values.values())


def missing_crypto(monkeypatch):
    original = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ModuleNotFoundError("synthetic import failure")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)


def no_scratch(tmp_path):
    assert not list(tmp_path.rglob(".archive-crypto-*"))
    assert not list(tmp_path.rglob(".archive-build-*"))
    assert not list(tmp_path.rglob(".restore-*"))


def test_format_constants_new_v2_and_legacy_v1():
    assert crypto.VERSION == 2 and crypto.MAGIC == b"HSAICRY\x00"
    assert crypto.HEADER.format == LAYOUT[2].format and crypto.HEADER.size == 54
    assert crypto.HEADER_V1.format == LAYOUT[1].format and crypto.HEADER_V1.size == 86
    assert (crypto.SCRYPT_N, crypto.SCRYPT_R, crypto.SCRYPT_P) == KDF[2]
    assert {version: value[:3] for version, value in crypto.FORMATS.items()} == KDF
    # Память scrypt 128 * r * (N + 2) должна помещаться в maxmem каждой версии.
    for n, r, p, maxmem, _ in crypto.FORMATS.values():
        assert 128 * r * (n + 2) + 128 * r * p < maxmem
    assert crypto.FORMATS[2][3] == 256 * 1024 * 1024


@pytest.mark.parametrize("layout", [1, 2])
def test_header_without_crypto(monkeypatch, layout):
    missing_crypto(monkeypatch)
    value = crypto.read_header(io.BytesIO(header_bytes(layout) + b"tail"))
    assert value["version"] == layout and value["size"] == 0
    assert (value["n"], value["r"], value["p"]) == KDF[layout]
    assert value["raw"] == header_bytes(layout) and len(value["raw"]) == LAYOUT[layout].size
    assert value["sha256"] == (hashlib.sha256(b"").digest() if layout == 1 else None)


@pytest.mark.parametrize("layout,size", [(layout, size) for layout in (1, 2)
                                         for size in (0, 1, 7, 8, 9, LAYOUT[layout].size - 1)])
def test_truncated_header_without_crypto(monkeypatch, layout, size):
    missing_crypto(monkeypatch)
    with pytest.raises(crypto.ArchiveCryptoError, match="обрезан"):
        crypto.read_header(io.BytesIO(header_bytes(layout)[:size]))


@pytest.mark.parametrize("layout,changes", [
    (2, {"magic": b"unknown!"}), (2, {"version": 0}), (2, {"version": 3}), (2, {"version": 255}),
    (2, {"kdf": 0}), (2, {"kdf": 2}), (2, {"n": 2**31}), (2, {"n": 32768}), (2, {"n": 16384}),
    (2, {"n": 2**18}), (2, {"r": 1}), (2, {"r": 16}), (2, {"p": 2}), (2, {"chunk": 0}),
    (2, {"chunk": 4095}), (2, {"chunk": crypto.MAX_CHUNK + 1}), (2, {"size": 4096 * 2**32 + 1}),
    (1, {"magic": b"unknown!"}), (1, {"kdf": 2}), (1, {"n": 131072}), (1, {"n": 16384}),
    (1, {"r": 1}), (1, {"p": 2}), (1, {"chunk": 4095}), (1, {"size": 4096 * 2**32 + 1}),
])
def test_unsupported_header_without_crypto(monkeypatch, layout, changes):
    """Параметры KDF зашиты по версии: заголовок не может ни ослабить, ни раздуть scrypt."""
    missing_crypto(monkeypatch)
    with pytest.raises(crypto.ArchiveCryptoError, match="Неподдерживаемый"):
        crypto.read_header(io.BytesIO(header_bytes(layout, **changes) + b"x" * 64))


@pytest.mark.parametrize("operation", ["encrypt", "decrypt", "build", "verify", "restore"])
def test_missing_dependency_refuses_before_password_or_restore(monkeypatch, tmp_path, home, operation):
    missing_crypto(monkeypatch)
    source, target = tmp_path / "input.any", tmp_path / "target"
    source.write_bytes(header_bytes() + b"0" * 16)
    target.mkdir()
    def forbidden():
        raise AssertionError("Provider не должен вызываться без cryptography")
    calls = {
        "encrypt": lambda: crypto.encrypt_archive(source, tmp_path / "out", password_provider=forbidden),
        "decrypt": lambda: crypto.decrypted_archive(source, password_provider=forbidden).__enter__(),
        "build": lambda: crypto.build_encrypted_archive(home, tmp_path / "out", password_provider=forbidden),
        "verify": lambda: crypto.verify_archive(source, password_provider=forbidden),
        "restore": lambda: crypto.restore_archive(source, target, password_provider=forbidden),
    }
    with pytest.raises(crypto.CryptoUnavailableError, match="cryptography.*requirements-crypto.txt"):
        calls[operation]()
    assert not list(target.iterdir()) and not (tmp_path / "out").exists()
    no_scratch(tmp_path)


def test_plain_roundtrip_without_crypto(monkeypatch, tmp_path, home):
    missing_crypto(monkeypatch)
    source, target = tmp_path / "plain.hsac", tmp_path / "target"
    expected = plain.build_archive(home, source)
    target.mkdir()
    def forbidden():
        raise AssertionError("Пароль для ZIP не нужен")
    assert not crypto.is_encrypted(source)
    assert crypto.verify_archive(source, password_provider=forbidden) == expected
    assert crypto.restore_archive(source, target, password_provider=forbidden) == expected
    assert (target / "roots/data/sample.bin").read_bytes() == PAYLOAD


def test_provider_reads_only_named_env_and_prompt():
    class NamedOnly:
        def get(self, name):
            assert name == "TEST_K2_PASSWORD"
            return PASSWORD
    assert crypto.password_provider(env_var="TEST_K2_PASSWORD", environ=NamedOnly())() == PASSWORD
    assert crypto.password_provider(prompt=lambda text: PASSWORD)() == PASSWORD
    assert crypto.password_provider(env_var="MISSING", environ={})() is None


@pytest.mark.parametrize("name", ["", "bad-name", "password=value", PASSWORD])
def test_invalid_env_name_is_not_echoed(name):
    with pytest.raises(crypto.ArchiveCryptoError) as error:
        crypto.password_provider(env_var=name)
    assert PASSWORD not in str(error.value)


def test_provider_exception_and_echo_fallback_redacted():
    def failed(text):
        raise RuntimeError(PASSWORD)
    with pytest.raises(crypto.ArchiveCryptoError) as error:
        crypto.password_provider(prompt=failed)()
    assert PASSWORD not in str(error.value)
    def echo(text):
        import warnings
        import getpass
        warnings.warn("synthetic", getpass.GetPassWarning)
    with pytest.raises(crypto.ArchiveCryptoError, match="без эха"):
        crypto.password_provider(prompt=echo)()


@pytest.mark.parametrize("version,n,maxmem", [(1, 32768, 64 * 1024 * 1024), (2, 131072, 256 * 1024 * 1024)])
def test_scrypt_documented_parameters(monkeypatch, version, n, maxmem):
    def derive(password, **kwargs):
        assert password == PASSWORD.encode("utf-8")
        assert kwargs == dict(salt=b"s" * 16, n=n, r=8, p=1, dklen=32, maxmem=maxmem)
        return b"k" * 32
    monkeypatch.setattr(hashlib, "scrypt", derive)
    assert crypto._key(lambda: PASSWORD, b"s" * 16, version) == b"k" * 32


def test_scrypt_and_frame_boundaries_without_crypto(monkeypatch):
    missing_crypto(monkeypatch)
    key = crypto._key(lambda: PASSWORD, b"s" * 16, 2)
    assert key == hashlib.scrypt(PASSWORD.encode(), salt=b"s" * 16, n=131072, r=8, p=1,
                                 dklen=32, maxmem=256 * 1024 * 1024)
    assert key != crypto._key(lambda: PASSWORD, b"t" * 16, 2)
    assert key != crypto._key(lambda: PASSWORD, b"s" * 16, 1)  # Версия разделяет ключи.
    for layout in (1, 2):
        raw = header_bytes(layout, size=8193)
        value = crypto.read_header(io.BytesIO(raw))
        frames = list(crypto._frames(value))
        assert [length for length, _, _ in frames] == [4096, 4096, 1]
        assert [nonce for _, nonce, _ in frames] == [b"n" * 4 + i.to_bytes(8, "big") for i in range(3)]
        # AAD = весь заголовок (вместе с версией) || номер блока || длина блока.
        assert [aad for _, _, aad in frames] == [raw + i.to_bytes(8, "big") + length.to_bytes(4, "big")
                                                 for i, length in enumerate((4096, 4096, 1))]


def test_temporary_plaintext_cleanup_without_crypto(tmp_path, monkeypatch):
    missing_crypto(monkeypatch)
    with pytest.raises(RuntimeError):
        with crypto._temporary(tmp_path) as path:
            path.write_bytes(b"synthetic temporary plaintext")
            raise RuntimeError("synthetic failure")
    no_scratch(tmp_path)


@pytest.mark.parametrize("password", [None, "", b"bytes"])
def test_empty_or_invalid_password_refused_without_crypto(password):
    for version in (1, 2):
        with pytest.raises(crypto.ArchiveCryptoError):
            crypto._key(lambda: password, b"s" * 16, version)


@pytest.mark.parametrize("password,accepted", [
    (None, False), (b"b" * 12, False), ("", False), ("a" * 11, False), ("ж" * 11, False),
    ("a" * 12, True), ("ж" * 12, True), (" " * 12, True), (PASSWORD, True),
])
def test_new_password_policy_without_crypto(monkeypatch, password, accepted):
    """Единое правило: не менее 12 кодовых точек Unicode, только при создании."""
    missing_crypto(monkeypatch)
    assert crypto.MIN_PASSWORD_LENGTH == 12
    assert crypto.new_password_acceptable(password) is accepted
    if accepted:
        assert crypto.require_new_password(password) is password
    else:
        with pytest.raises(crypto.PasswordPolicyError, match="не менее 12") as error:
            crypto.require_new_password(password)
        assert isinstance(error.value, crypto.ArchiveCryptoError)
        assert not password or str(password) not in str(error.value)


def test_cli_missing_dependency_and_no_password_argv(monkeypatch, tmp_path, capsys, home):
    missing_crypto(monkeypatch)
    common = ["--manifest", str(EXAMPLE), "--json"]
    assert ctl.main(common + ["archive-create", "--install-root", str(tmp_path / "installation"),
                             "--archive", str(tmp_path / "out"), "--encrypt"],
                    password_provider=lambda: PASSWORD) == 1
    output = capsys.readouterr()
    assert "cryptography" in json.loads(output.out)["message"] and not output.err
    assert PASSWORD not in json.dumps(json.loads(output.out), ensure_ascii=False)
    assert ctl.main(common + ["archive-verify", "--archive", str(tmp_path / "out"),
                             "--password", PASSWORD]) == 2
    assert PASSWORD not in json.dumps(json.loads(capsys.readouterr().out), ensure_ascii=False)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("command", ["archive-verify", "archive-restore"])
def test_cli_detects_encrypted_without_dependency(monkeypatch, tmp_path, capsys, command):
    missing_crypto(monkeypatch)
    source, target = tmp_path / "encrypted.zip", tmp_path / "target"
    source.write_bytes(header_bytes() + b"x" * 16)
    target.mkdir()
    args = ["--manifest", str(EXAMPLE), "--json", command, "--archive", str(source)]
    if command == "archive-restore":
        args += ["--install-root", str(tmp_path / "installation"), "--target", str(target)]
    assert ctl.main(args, password_provider=lambda: PASSWORD) == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert "cryptography" in value["message"] and not output.err
    assert PASSWORD not in json.dumps(value, ensure_ascii=False)
    assert not list(target.iterdir())
    no_scratch(tmp_path)


def test_roundtrip_full_archive(aes, tmp_path, home):
    source, encrypted, restored = tmp_path / "plain.zip", tmp_path / "encrypted.zip", tmp_path / "restored"
    expected = plain.build_archive(home, source)
    calls = []
    def provider():
        calls.append(True)
        return PASSWORD
    assert crypto.encrypt_archive(source, encrypted, password_provider=provider, chunk_size=4096) == expected
    assert len(calls) == 1  # Обратная проверка не запрашивает пароль повторно.
    assert PAYLOAD[:50] not in encrypted.read_bytes() and PASSWORD.encode() not in encrypted.read_bytes()
    with encrypted.open("rb") as stream:
        header = crypto.read_header(stream)
    assert header["version"] == 2 and header["sha256"] is None
    assert (header["n"], header["r"], header["p"]) == KDF[2]
    assert header["size"] == source.stat().st_size
    frames = list(crypto._frames(header))
    assert len({nonce for _, nonce, _ in frames}) == len(frames) > 2
    assert encrypted.stat().st_size == LAYOUT[2].size + header["size"] + len(frames) * crypto.TAG_SIZE
    assert crypto.verify_archive(encrypted, password_provider=provider) == expected
    restored.mkdir()
    assert crypto.restore_archive(encrypted, restored, password_provider=provider) == expected
    for name in home.roots:
        assert (restored / "roots" / name / "sample.bin").read_bytes() == PAYLOAD
        assert (restored / "roots" / name / "empty").is_dir()
        assert (restored / "roots" / name / "zero").read_bytes() == b""
    no_scratch(tmp_path)


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason='Linux O_TMPFILE')
def test_public_decrypted_stream_is_anonymous_verified_and_closed(aes, tmp_path, home):
    import zipfile
    encrypted = tmp_path / 'encrypted'
    expected = crypto.build_encrypted_archive(home, encrypted, password_provider=lambda: PASSWORD)
    ciphertext_hash = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    with crypto.decrypted_archive_stream(encrypted, password_provider=lambda: PASSWORD,
                                         expected_product_id=home.product_id,
                                         expected_ciphertext_sha256=ciphertext_hash) as (stream, manifest):
        assert manifest == expected and os.fstat(stream.fileno()).st_nlink == 0
        assert not stream.writable()
        with zipfile.ZipFile(stream) as archive:
            assert archive.read('roots/data/sample.bin') == PAYLOAD
        no_scratch(tmp_path)
    assert stream.closed
    with pytest.raises(plain.ArchiveError):
        with crypto.decrypted_archive_stream(encrypted, password_provider=lambda: PASSWORD,
                                             expected_ciphertext_sha256='0' * 64):
            pytest.fail('unverified stream exposed')
    no_scratch(tmp_path)


MUTATIONS = ["password", "truncate", "drop", "reorder", "duplicate", "flip", "append",
             "salt", "prefix", "size", "chunk", "magic"]


@pytest.mark.parametrize("layout,mutation", [(2, mutation) for mutation in MUTATIONS]
                         + [(1, mutation) for mutation in MUTATIONS + ["hash"]])
def test_authentication_failure_before_any_restore(aes, tmp_path, home, monkeypatch, layout, mutation):
    source, encrypted = tmp_path / "plain.zip", tmp_path / "encrypted"
    plain.build_archive(home, source)
    if layout == 2:
        crypto.encrypt_archive(source, encrypted, password_provider=lambda: PASSWORD, chunk_size=4096)
    else:
        seal(encrypted, source.read_bytes(), layout=1)
    raw = bytearray(encrypted.read_bytes())
    start, frame = LAYOUT[layout].size, 4096 + crypto.TAG_SIZE
    if mutation == "truncate":
        raw = raw[:-1]
    elif mutation == "drop":
        del raw[-frame:]
    elif mutation == "reorder":
        raw[start:start + 2 * frame] = raw[start + frame:start + 2 * frame] + raw[start:start + frame]
    elif mutation == "duplicate":
        raw[start + frame:start + 2 * frame] = raw[start:start + frame]
    elif mutation == "flip":
        raw[start + 2 * frame + 100] ^= 1
    elif mutation == "append":
        raw += b"extra"
    elif mutation in ("hash", "salt", "prefix", "size", "chunk", "magic"):
        fields = list(LAYOUT[layout].unpack(raw[:start]))
        index = {"hash": 9, "salt": 6, "prefix": len(fields) - 1, "size": 8, "chunk": 7, "magic": 0}[mutation]
        if mutation in ("size", "chunk"):
            fields[index] += 4096 if mutation == "chunk" else 1
        else:
            fields[index] = bytes([fields[index][0] ^ 1]) + fields[index][1:]
        raw[:start] = LAYOUT[layout].pack(*fields)
    encrypted.write_bytes(raw)
    target = tmp_path / "target"
    target.mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError("Restore не должен быть вызван")
    if mutation != "magic":  # Неизвестная сигнатура отклоняется проверкой ZIP.
        monkeypatch.setattr(plain, "restore_archive", forbidden)
    with pytest.raises(plain.ArchiveError) as error:
        crypto.restore_archive(encrypted, target,
                               password_provider=lambda: "wrong" if mutation == "password" else PASSWORD)
    assert PASSWORD not in str(error.value)
    assert not list(target.iterdir())
    no_scratch(tmp_path)


def seal(path, data, *, layout=2, declared_digest=None, password=PASSWORD, header=None):
    """Аутентичная оболочка с произвольным содержимым, независимая от кода модуля.

    Для layout=1 это побайтно формат прежней версии: так проверяется чтение v1.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, prefix, chunk = os.urandom(16), os.urandom(4), 4096
    extra = {"sha256": declared_digest or hashlib.sha256(data).digest()} if layout == 1 else {}
    raw = header if header is not None else header_bytes(layout, salt=salt, prefix=prefix, chunk=chunk,
                                                         size=len(data), **extra)
    n, r, p = KDF[layout]
    salt, prefix = raw[22:38], raw[-4:]
    cipher = AESGCM(hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32,
                                   maxmem=256 * 1024 * 1024))
    output = bytearray(raw)
    for index in range(max(1, -(-len(data) // chunk))):
        block = data[index * chunk:(index + 1) * chunk]
        counter = index.to_bytes(8, "big")
        output += cipher.encrypt(prefix + counter, block, raw + counter + len(block).to_bytes(4, "big"))
    path.write_bytes(output)


@pytest.mark.parametrize("layout,invalid", [(layout, invalid) for layout in (1, 2)
                                            for invalid in ("not-zip", "manifest", "product", "empty")]
                         + [(1, "digest")])
def test_authenticated_but_invalid_inner_archive(aes, tmp_path, home, monkeypatch, layout, invalid):
    source, encrypted = tmp_path / "plain.zip", tmp_path / "encrypted"
    plain.build_archive(home, source)
    data = source.read_bytes()
    if invalid == "not-zip":
        data = b"synthetic invalid archive"
    elif invalid == "empty":
        data = b""
    elif invalid == "manifest":
        import zipfile
        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("extra.txt", b"unexpected")
        data = source.read_bytes()
    seal(encrypted, data, layout=layout, declared_digest=b"0" * 32 if invalid == "digest" else None)
    target = tmp_path / "target"
    target.mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError("Восстановление до проверки ZIP запрещено")
    monkeypatch.setattr(plain, "restore_archive", forbidden)
    monkeypatch.setattr(plain, "_restore_stream", forbidden)
    with pytest.raises(plain.ArchiveError) as error:
        crypto.restore_archive(encrypted, target, password_provider=lambda: PASSWORD,
                               expected_product_id="other-product" if invalid == "product" else home.product_id)
    if invalid == "digest":
        assert "SHA-256 полного архива" in str(error.value)
    assert not list(target.iterdir())
    no_scratch(tmp_path)


def test_legacy_v1_archive_still_reads_verifies_and_restores(aes, tmp_path, home):
    """v1 записан независимой реализацией прежнего формата; короткий пароль при чтении допустим."""
    source, legacy, target = tmp_path / "plain.zip", tmp_path / "legacy.hsac", tmp_path / "target"
    expected = plain.build_archive(home, source)
    seal(legacy, source.read_bytes(), layout=1, password="short")
    with legacy.open("rb") as stream:
        header = crypto.read_header(stream)
    assert header["version"] == 1 and header["sha256"] == hashlib.sha256(source.read_bytes()).digest()
    assert crypto.is_encrypted(legacy)
    digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
    assert crypto.verify_archive(legacy, password_provider=lambda: "short", require_encrypted=True,
                                 expected_ciphertext_sha256=digest,
                                 expected_product_id=home.product_id) == expected
    target.mkdir()
    assert crypto.restore_archive(legacy, target, password_provider=lambda: "short") == expected
    assert (target / "roots/data/sample.bin").read_bytes() == PAYLOAD
    with pytest.raises(crypto.ArchiveCryptoError, match="Неверный пароль"):
        crypto.verify_archive(legacy, password_provider=lambda: "shorT")
    no_scratch(tmp_path)


def test_v2_header_has_no_plaintext_digest_and_links_only_by_size(aes, tmp_path, home):
    source = tmp_path / "plain.zip"
    plain.build_archive(home, source)
    data = source.read_bytes()
    first, second = tmp_path / "first", tmp_path / "second"
    for path in (first, second):
        crypto.encrypt_archive(source, path, password_provider=lambda: PASSWORD)
    one, two = first.read_bytes(), second.read_bytes()
    for blob in (one, two):
        assert hashlib.sha256(data).digest() not in blob and hashlib.sha256(data).hexdigest().encode() not in blob
        assert blob[8] == 2 and len(blob) == 54 + len(data) + crypto.TAG_SIZE
    # Одинаковое содержимое: совпадают только константы и размер, но не соль, префикс nonce и шифртекст.
    fields_one, fields_two = LAYOUT[2].unpack(one[:54]), LAYOUT[2].unpack(two[:54])
    assert [a == b for a, b in zip(fields_one, fields_two)] == [True] * 6 + [False, True, True, False]
    assert one[54:] != two[54:]


def _downgraded(raw, data):
    """v2 → v1: сильнейший вариант — злоумышленник знает даже SHA-256 открытого ZIP."""
    _, _, kdf, n, r, p, salt, chunk, size, prefix = LAYOUT[2].unpack(raw[:54])
    header = LAYOUT[1].pack(b"HSAICRY\x00", 1, kdf, *KDF[1], salt, chunk, size,
                            hashlib.sha256(data).digest(), prefix)
    return header + raw[54:]


@pytest.mark.parametrize("attack", ["version-byte", "v1-header", "v1-header-keep-n", "v2-from-v1"])
def test_format_downgrade_and_upgrade_rejected_before_restore(aes, tmp_path, home, monkeypatch, attack):
    source, encrypted = tmp_path / "plain.zip", tmp_path / "encrypted"
    plain.build_archive(home, source)
    data = source.read_bytes()
    if attack == "v2-from-v1":
        seal(encrypted, data, layout=1)
        raw = encrypted.read_bytes()
        _, _, kdf, n, r, p, salt, chunk, size, _, prefix = LAYOUT[1].unpack(raw[:86])
        raw = LAYOUT[2].pack(b"HSAICRY\x00", 2, kdf, *KDF[2], salt, chunk, size, prefix) + raw[86:]
    else:
        crypto.encrypt_archive(source, encrypted, password_provider=lambda: PASSWORD)
        raw = bytearray(encrypted.read_bytes())
        if attack == "version-byte":
            raw[8] = 1
        else:
            raw = bytearray(_downgraded(bytes(raw), data))
            if attack == "v1-header-keep-n":  # Параметры v2 в заголовке v1 не принимаются.
                raw[10:14] = KDF[2][0].to_bytes(4, "big")
    encrypted.write_bytes(raw)
    calls = []
    def provider():
        calls.append(1)
        return PASSWORD
    def forbidden(*args, **kwargs):
        raise AssertionError("Восстановление подделки запрещено")
    monkeypatch.setattr(plain, "restore_archive", forbidden)
    monkeypatch.setattr(plain, "_restore_stream", forbidden)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(crypto.ArchiveCryptoError) as error:
        crypto.restore_archive(encrypted, target, password_provider=provider, require_encrypted=True)
    if attack in ("version-byte", "v1-header-keep-n"):
        assert "Неподдерживаемый" in str(error.value) and not calls  # До KDF.
    else:
        # Длина совпала, но версия входит в AAD и выбирает KDF: ни один тег не сходится.
        assert "Неверный пароль или повреждён" in str(error.value) and calls == [1]
    assert not list(target.iterdir())
    no_scratch(tmp_path)


def test_exact_length_checked_before_kdf(aes, tmp_path, home):
    encrypted = tmp_path / "encrypted"
    crypto.build_encrypted_archive(home, encrypted, password_provider=lambda: PASSWORD)
    encrypted.write_bytes(encrypted.read_bytes()[:-1])
    with pytest.raises(crypto.ArchiveCryptoError, match="обрезан"):
        crypto.verify_archive(encrypted, password_provider=lambda: pytest.fail("KDF до проверки длины"))


@pytest.mark.parametrize("operation", ["encrypt", "build"])
def test_creation_refuses_short_password_without_output(aes, tmp_path, home, operation):
    source, destination = tmp_path / "plain.zip", tmp_path / "out"
    plain.build_archive(home, source)
    calls = []
    def short():
        calls.append(1)
        return "ж" * 11
    with pytest.raises(crypto.PasswordPolicyError) as error:
        if operation == "encrypt":
            crypto.encrypt_archive(source, destination, password_provider=short)
        else:
            crypto.build_encrypted_archive(home, destination, password_provider=short)
    assert "ж" * 11 not in str(error.value) and calls == [1]
    assert not destination.exists()
    no_scratch(tmp_path)
    for value in (None, 0, "false"):
        with pytest.raises(crypto.ArchiveCryptoError, match="режим проверки пароля"):
            crypto.encrypt_archive(source, destination, password_provider=short, enforce_password_policy=value)
        with pytest.raises(crypto.ArchiveCryptoError, match="режим проверки пароля"):
            crypto.build_encrypted_archive(home, destination, password_provider=short,
                                           enforce_password_policy=value)
    assert calls == [1] and not destination.exists()


def test_policy_opt_out_continuation_and_reading_without_length_limit(aes, tmp_path, home):
    """Продолжение начатой операции (FilesystemPorts) не блокирует откат по длине пароля."""
    encrypted, target = tmp_path / "continued", tmp_path / "target"
    crypto.build_encrypted_archive(home, encrypted, password_provider=lambda: "short",
                                   enforce_password_policy=False)
    with encrypted.open("rb") as stream:
        assert crypto.read_header(stream)["version"] == 2
    assert crypto.verify_archive(encrypted, password_provider=lambda: "short")["product_id"] == home.product_id
    target.mkdir()
    crypto.restore_archive(encrypted, target, password_provider=lambda: "short")
    assert (target / "roots/data/sample.bin").read_bytes() == PAYLOAD
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / "installation", work_dir=tmp_path / "work",
                            payloads={}, effects=None, password_provider=lambda: "short")
    transaction = "a" * 32
    ports.backup(transaction + "/archive", "archive", ())
    home.roots["data"].joinpath("sample.bin").write_bytes(b"changed synthetic data")
    ports.backup(transaction + "/before_rollback", "before-rollback", ())
    ports.restore(transaction + "/restore", transaction + "/archive", ())
    assert (home.roots["data"] / "sample.bin").read_bytes() == PAYLOAD
    no_scratch(tmp_path)


@pytest.mark.parametrize("language", ["ru", "en", "de"])
def test_cli_create_refuses_short_password_but_reads_existing(aes, tmp_path, home, monkeypatch, capsys, language):
    from shared.i18n import translate
    encrypted = tmp_path / "short.hsac"
    common = ["--manifest", str(EXAMPLE), "--json", "--language", language]
    monkeypatch.setenv("TEST_K2_PASSWORD", "eleven-char")
    assert ctl.main(common + ["archive-create", "--install-root", str(tmp_path / "installation"),
                              "--archive", str(encrypted), "--encrypt", "--password-env", "TEST_K2_PASSWORD"]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["message"] == (translate(language, "cli.error_archive_password_short") if language != "ru"
                                else "Пароль нового шифрованного архива должен содержать не менее 12 символов")
    assert "eleven-char" not in json.dumps(value) and not encrypted.exists()
    crypto.build_encrypted_archive(home, encrypted, password_provider=lambda: "eleven-char",
                                   enforce_password_policy=False)
    assert ctl.main(common + ["archive-verify", "--archive", str(encrypted),
                              "--password-env", "TEST_K2_PASSWORD"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["encrypted"] is True


def test_build_randomization_no_overwrite_and_cleanup(aes, tmp_path, home):
    first, second = tmp_path / "first", tmp_path / "second"
    one = crypto.build_encrypted_archive(home, first, password_provider=lambda: PASSWORD)
    assert crypto.build_encrypted_archive(home, second, password_provider=lambda: PASSWORD) == one
    assert first.read_bytes() != second.read_bytes()
    before = first.read_bytes()
    with pytest.raises(crypto.ArchiveCryptoError, match="существует"):
        crypto.build_encrypted_archive(home, first, password_provider=lambda: PASSWORD)
    assert first.read_bytes() == before
    with pytest.raises(crypto.ArchiveCryptoError):
        crypto.build_encrypted_archive(home, tmp_path / "failed", password_provider=lambda: "")
    assert not (tmp_path / "failed").exists()
    no_scratch(tmp_path)


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason='Linux O_TMPFILE')
def test_encrypted_build_fails_closed_without_anonymous_temporary_files(aes, tmp_path, home, monkeypatch):
    monkeypatch.delattr(crypto.os, 'O_TMPFILE', raising=False)
    with pytest.raises(crypto.ArchiveCryptoError, match='Безымянный'):
        crypto.build_encrypted_archive(home, tmp_path / 'encrypted', password_provider=lambda: PASSWORD)
    assert not (tmp_path / 'encrypted').exists()
    no_scratch(tmp_path)


def test_restore_io_failure_cleans_plaintext(aes, tmp_path, home, monkeypatch):
    encrypted = tmp_path / "encrypted"
    crypto.build_encrypted_archive(home, encrypted, password_provider=lambda: PASSWORD)
    def failed(*args, **kwargs):
        raise OSError(PASSWORD)
    monkeypatch.setattr(plain, "_restore_stream" if __import__('sys').platform.startswith('linux')
                        else "restore_archive", failed)
    with pytest.raises(crypto.ArchiveCryptoError) as error:
        crypto.restore_archive(encrypted, tmp_path / "target", password_provider=lambda: PASSWORD)
    assert PASSWORD not in str(error.value)
    no_scratch(tmp_path)


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason='Linux O_TMPFILE and fork')
@pytest.mark.parametrize('operation', ['verify', 'restore'])
def test_sigkill_after_decryption_leaves_no_named_plaintext(aes, tmp_path, home, operation):
    import os
    import select
    import signal

    encrypted = tmp_path / 'encrypted'
    crypto.build_encrypted_archive(home, encrypted, password_provider=lambda: PASSWORD)
    target = tmp_path / 'target'
    target.mkdir()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        def stop_after_decryption(archive, expected_product_id):
            assert os.fstat(archive.fp.fileno()).st_nlink == 0
            os.write(write_fd, b'R')
            signal.pause()
        plain._verify = stop_after_decryption
        try:
            if operation == 'verify':
                crypto.verify_archive(encrypted, password_provider=lambda: PASSWORD, require_encrypted=True)
            else:
                crypto.restore_archive(encrypted, target, password_provider=lambda: PASSWORD, require_encrypted=True)
        except BaseException:
            os.write(write_fd, b'E')
        os._exit(1)
    os.close(write_fd)
    reaped = False
    try:
        ready, _, _ = select.select([read_fd], [], [], 20)
        assert ready and os.read(read_fd, 1) == b'R'
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        reaped = True
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        assert not list(tmp_path.glob('.archive-crypto-*'))
        assert not list(target.iterdir())
    finally:
        os.close(read_fd)
        if not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_cli_encrypted_roundtrip_and_env_provider(aes, tmp_path, home, monkeypatch, capsys):
    encrypted, target = tmp_path / "not-a-zip.zip", tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("TEST_K2_PASSWORD", PASSWORD)
    common = ["--manifest", str(EXAMPLE), "--json"]
    def call(args):
        assert ctl.main(common + args) == 0
        output = capsys.readouterr()
        value = json.loads(output.out)
        assert not output.err and PASSWORD not in json.dumps(value, ensure_ascii=False)
        assert value["data"]["encrypted"] is True
        return value["data"]
    extra = ["--archive", str(encrypted), "--password-env", "TEST_K2_PASSWORD"]
    created = call(["archive-create", "--install-root", str(tmp_path / "installation"), "--encrypt", *extra])
    assert call(["archive-verify", *extra]) == created
    assert call(["archive-restore", "--install-root", str(tmp_path / "installation"), "--target", str(target), *extra]) == created
    assert (target / "roots/data/sample.bin").read_bytes() == PAYLOAD
    monkeypatch.setenv("TEST_K2_PASSWORD", "wrong")
    assert ctl.main(common + ["archive-verify", *extra]) == 1
    assert PASSWORD not in capsys.readouterr().out


def test_fs_ports_no_plaintext_downgrade_without_crypto(tmp_path, home):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / "installation", work_dir=tmp_path / "work",
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    key = "a" * 32 + "/archive"
    ports.work.mkdir()
    plain.build_archive(home, ports.archive_path(key))
    with pytest.raises(UpdateError, match="обычный ZIP запрещён"):
        ports.backup(key, "archive", ())


def test_strict_encrypted_mode_never_falls_back_to_plain_zip(aes, tmp_path, home, monkeypatch):
    path = tmp_path / 'plain.zip'
    plain.build_archive(home, path)
    target = tmp_path / 'target'
    target.mkdir()
    monkeypatch.setattr(crypto, 'is_encrypted', lambda *args: pytest.fail('strict mode must not sniff and fall back'))
    with pytest.raises(plain.ArchiveError):
        crypto.verify_archive(path, password_provider=lambda: PASSWORD, require_encrypted=True)
    with pytest.raises(plain.ArchiveError):
        crypto.restore_archive(path, target, password_provider=lambda: PASSWORD, require_encrypted=True)
    assert not list(target.iterdir())


@pytest.mark.parametrize('value', [None, 0, 1, 'true', []])
def test_strict_encryption_flag_requires_bool(value, tmp_path):
    for operation in (crypto.verify_archive, crypto.restore_archive):
        args = [tmp_path / 'input'] + ([tmp_path / 'target'] if operation is crypto.restore_archive else [])
        with pytest.raises(crypto.ArchiveCryptoError):
            operation(*args, require_encrypted=value)


def test_fs_verifier_rejects_plaintext_after_encryption_probe(aes, tmp_path, home, monkeypatch):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    ports.work.mkdir()
    key = 'a' * 32 + '/archive'
    path = ports.archive_path(key)
    plain.build_archive(home, path)
    _atomic_write(path.with_suffix('.archive.json'), {'version': 1, 'key': key, 'product_id': home.product_id,
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'status': 'done'})
    monkeypatch.setattr(crypto, 'is_encrypted', lambda path: True)  # Файл подменён после первого опроса.
    with pytest.raises(plain.ArchiveError):
        ports._verify_archive(path)


def test_restore_rejects_plaintext_swap_after_verification(aes, tmp_path, home, monkeypatch):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    transaction = 'b' * 32
    archive_key = transaction + '/archive'
    ports.backup(archive_key, 'archive', ())
    ports.backup(transaction + '/before_rollback', 'before-rollback', ())
    live = home.roots['data'] / 'sample.bin'
    live.write_bytes(b'new synthetic data')
    plaintext = tmp_path / 'replacement.zip'
    plain.build_archive(home, plaintext)
    original = ports._verify_archive
    archive = ports.archive_path(archive_key)
    def verify_and_swap(path):
        receipt = original(path)
        if path == archive:
            os.replace(plaintext, archive)
        return receipt
    monkeypatch.setattr(ports, '_verify_archive', verify_and_swap)
    with pytest.raises(plain.ArchiveError):
        ports.restore(transaction + '/restore', archive_key, ())
    assert live.read_bytes() == b'new synthetic data'
    assert not ports._path(transaction + '/restore', '.json').exists()


def test_restore_rejects_different_valid_encrypted_snapshot_after_check(aes, tmp_path, home, monkeypatch):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    transaction = 'c' * 32
    key = transaction + '/archive'
    ports.backup(key, 'archive', ())
    live = home.roots['data'] / 'sample.bin'
    live.write_bytes(b'new synthetic data')
    rollback_key = transaction + '/before_rollback'
    ports.backup(rollback_key, 'before-rollback', ())
    archive = ports.archive_path(key)
    replacement = tmp_path / 'replacement.hsac'
    replacement.write_bytes(ports.archive_path(rollback_key).read_bytes())
    original = ports._verify_archive
    def verify_and_swap(path):
        receipt = original(path)
        if path == archive:
            os.replace(replacement, archive)
        return receipt
    monkeypatch.setattr(ports, '_verify_archive', verify_and_swap)
    with pytest.raises(plain.ArchiveError):
        ports.restore(transaction + '/restore', key, ())
    assert live.read_bytes() == b'new synthetic data'
    assert not ports._path(transaction + '/restore', '.json').exists()


def test_interrupted_encrypted_backup_remains_pending_and_is_not_repeated(tmp_path, home, monkeypatch):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    key = 'd' * 32 + '/archive'
    calls = []
    def interrupted(home, destination, **kwargs):
        calls.append(1)
        destination.write_bytes(b'partial')
        raise OSError('simulated interruption')
    monkeypatch.setattr(crypto, 'build_encrypted_archive', interrupted)
    with pytest.raises(EffectIndeterminate):
        ports.backup(key, 'archive', ())
    with pytest.raises(EffectIndeterminate):
        ports.backup(key, 'archive', ())
    from shared.effect_receipt import read_effect_receipt
    record = read_effect_receipt(ports.archive_path(key).with_suffix('.archive.json'))
    assert record['status'] == 'pending' and record['sha256'] is None and calls == [1]


def test_existing_encrypted_file_without_receipt_is_not_adopted(tmp_path, home):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    key = 'e' * 32 + '/archive'
    ports.work.mkdir()
    path = ports.archive_path(key)
    path.write_bytes(crypto.MAGIC + b'untracked')
    with pytest.raises(EffectIndeterminate):
        ports.backup(key, 'archive', ())
    assert path.read_bytes() == crypto.MAGIC + b'untracked'


@pytest.mark.parametrize('raw', [b'null', b'false', b'[]', b'0', b'"pending"'])
def test_corrupt_archive_receipt_never_starts_backup(tmp_path, home, monkeypatch, raw):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    key = 'e' * 32 + '/archive'
    ports.work.mkdir()
    path = ports.archive_path(key)
    binding = path.with_suffix('.archive.json')
    binding.write_bytes(raw)
    monkeypatch.setattr(crypto, 'build_encrypted_archive', lambda *args, **kwargs: pytest.fail('backup repeated'))
    with pytest.raises(EffectIndeterminate):
        ports.backup(key, 'archive', ())
    assert binding.read_bytes() == raw and not path.exists()


@pytest.mark.parametrize('missing', [False, True])
def test_recover_archive_binding_requires_independent_digest_and_matching_checkpoint(aes, tmp_path, home, missing):
    from shared.effect_receipt import read_effect_receipt
    manifest = load_manifest(EXAMPLE)
    ports = FilesystemPorts(manifest, tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    plan = UpdatePlan(manifest, tmp_path / 'installation', ports, release='2', modes=('autonomous',),
                      migration_ids=(), guard_effects=True)
    transaction = 'f' * 32
    key = transaction + '/archive'
    ports.backup(key, 'archive', ())
    path = ports.archive_path(key)
    original = path.read_bytes()
    binding = path.with_suffix('.archive.json')
    done = read_effect_receipt(binding)
    pending = {**done, 'sha256': None, 'status': 'pending'}
    if missing:
        binding.unlink()
    else:
        _atomic_write(binding, pending)
    state = {'version': 1, 'transaction': transaction, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('archive'),
             'failed_phase': None, 'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    before = read_effect_receipt(binding)
    with pytest.raises(UpdateError):
        ports.recover_archive_binding(plan, key, 'not-a-sha')
    with pytest.raises(EffectIndeterminate):
        ports.recover_archive_binding(plan, key, '0' * 64)
    with pytest.raises(UpdateError):
        ports.recover_archive_binding(plan, 'e' * 32 + '/archive', done['sha256'])
    assert read_effect_receipt(binding) == before
    with operation_lock(plan.lock_path):
        with pytest.raises(UpdateError):
            ports.recover_archive_binding(plan, key, done['sha256'])
    ports.recover_archive_binding(plan, key, done['sha256'])
    ports.recover_archive_binding(plan, key, done['sha256'])
    assert read_effect_receipt(binding) == done
    assert path.read_bytes() == original and plan.status() == state
    assert (home.roots['data'] / 'sample.bin').read_bytes() == PAYLOAD
    no_scratch(tmp_path)


def test_recover_archive_binding_refuses_wrong_password_and_unrelated_receipt(aes, tmp_path, home):
    from shared.effect_receipt import read_effect_receipt
    manifest = load_manifest(EXAMPLE)
    ports = FilesystemPorts(manifest, tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    plan = UpdatePlan(manifest, tmp_path / 'installation', ports, release='2', modes=('autonomous',),
                      migration_ids=(), guard_effects=True)
    key = 'a' * 32 + '/archive'
    ports.backup(key, 'archive', ())
    binding = ports.archive_path(key).with_suffix('.archive.json')
    done = read_effect_receipt(binding)
    pending = {**done, 'sha256': None, 'status': 'pending'}
    _atomic_write(binding, pending)
    plan._save({'version': 1, 'transaction': 'a' * 32, 'plan': plan.digest, 'old_release': '1',
                'release': '2', 'direction': 'forward', 'index': plan.forward.index('archive'),
                'failed_phase': None, 'archive_ready': False, 'outcome': 'running', 'indeterminate': True})
    ports.password_provider = lambda: 'wrong password'
    with pytest.raises(EffectIndeterminate):
        ports.recover_archive_binding(plan, key, done['sha256'])
    assert read_effect_receipt(binding) == pending
    ports.password_provider = lambda: PASSWORD
    unrelated = {**pending, 'key': 'b' * 32 + '/archive'}
    _atomic_write(binding, unrelated)
    with pytest.raises(EffectIndeterminate):
        ports.recover_archive_binding(plan, key, done['sha256'])
    assert read_effect_receipt(binding) == unrelated
    binding.write_bytes(b'null')
    with pytest.raises(EffectIndeterminate):
        ports.recover_archive_binding(plan, key, done['sha256'])
    assert binding.read_bytes() == b'null'


@pytest.mark.parametrize('phase,direction', [('backup:0', 'forward'), ('before_rollback', 'rollback')])
def test_recover_archive_binding_for_migration_and_rollback(aes, tmp_path, home, phase, direction):
    from shared.effect_receipt import read_effect_receipt
    manifest = load_manifest(EXAMPLE)
    ports = FilesystemPorts(manifest, tmp_path / 'installation', work_dir=tmp_path / 'work',
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    plan = UpdatePlan(manifest, tmp_path / 'installation', ports, release='2', modes=('autonomous',),
                      migration_ids=('migration',), guard_effects=True)
    key = 'c' * 32 + '/' + phase
    ports.work.mkdir()
    path = ports.archive_path(key)
    crypto.build_encrypted_archive(home, path, password_provider=lambda: PASSWORD)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    phases = plan.forward if direction == 'forward' else plan.rollback
    state = {'version': 1, 'transaction': 'c' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': direction, 'index': phases.index(phase),
             'failed_phase': None if direction == 'forward' else 'stage', 'archive_ready': True,
             'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    ports.recover_archive_binding(plan, key, digest)
    assert read_effect_receipt(path.with_suffix('.archive.json'))['sha256'] == digest
    assert plan.status() == state


def test_fs_ports_encrypted_backup_before_rollback_and_restore(aes, tmp_path, home):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / "installation", work_dir=tmp_path / "work",
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    transaction = "b" * 32
    key = transaction + "/archive"
    ports.backup(key, "archive", ())
    before = ports.archive_path(key).read_bytes()
    ports.backup(key, "archive", ())
    assert ports.archive_path(key).read_bytes() == before
    live = home.roots["data"] / "sample.bin"
    live.write_bytes(b"new synthetic data")
    with pytest.raises(EffectIndeterminate):
        ports.restore(transaction + "/restore", key, ())
    assert live.read_bytes() == b"new synthetic data"
    ports.backup(transaction + "/before_rollback", "before-rollback", ())
    ports.restore(transaction + "/restore", key, ())
    assert live.read_bytes() == PAYLOAD
    assert crypto.is_encrypted(ports.archive_path(transaction + "/before_rollback"))
    no_scratch(tmp_path)


def test_fs_ports_failed_reconciliation_cleans_and_allows_retry(aes, tmp_path, home, monkeypatch):
    ports = FilesystemPorts(load_manifest(EXAMPLE), tmp_path / "installation", work_dir=tmp_path / "work",
                            payloads={}, effects=None, password_provider=lambda: PASSWORD)
    transaction = "c" * 32
    archive_key = transaction + "/archive"
    ports.backup(archive_key, "archive", ())
    live = home.roots["data"] / "sample.bin"
    live.write_bytes(b"modified synthetic data")
    ports.backup(transaction + "/before_rollback", "before-rollback", ())
    original = ports._reconcile
    def fail(source, target, preserved):
        if os.name != "nt":
            assert source.parents[1].stat().st_mode & 0o777 == 0o700
        raise OSError("synthetic reconciliation failure")
    monkeypatch.setattr(ports, "_reconcile", fail)
    with pytest.raises(OSError):
        ports.restore(transaction + "/restore", archive_key, ())
    assert live.read_bytes() == b"modified synthetic data"
    assert not ports._path(transaction + "/restore", ".json").exists()
    no_scratch(tmp_path)
    monkeypatch.setattr(ports, "_reconcile", original)
    ports.restore(transaction + "/restore", archive_key, ())
    assert live.read_bytes() == PAYLOAD
    no_scratch(tmp_path)


@pytest.mark.parametrize("action,password,started", [
    ("start", "eleven-char", False), ("start", "twelve-chars", True), ("reopen", "eleven-char", True)])
def test_pointer_controller_applies_policy_only_to_new_transaction(monkeypatch, action, password, started):
    """API run-pointer-linux-update: новая транзакция создаёт архивы, reopen продолжает прежнюю."""
    from shared import pointer_controller
    calls = []
    def assemble(**kwargs):
        calls.append(kwargs["password_provider"]())
        raise RuntimeError("synthetic stop")
    monkeypatch.setattr(pointer_controller, "assemble_pointer_linux_update", assemble)
    monkeypatch.setenv("HOMENSAI_TEST_POLICY_PASSWORD", password)
    with pytest.raises(UpdateError) as error:
        pointer_controller.run_pointer_linux_update(password_env="HOMENSAI_TEST_POLICY_PASSWORD", action=action)
    assert calls == ([password] if started else [])
    assert password not in str(error.value)
