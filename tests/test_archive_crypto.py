"""K2: только синтетические архивы, без сети, процессов и настоящих секретов."""

import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess

import pytest

from shared import archive as plain, archive_crypto as crypto, ctl
from shared.adapters_fs import FilesystemPorts
from shared.datahome import resolve_datahome
from shared.manifest import load_manifest
from shared.update_plan import UpdateError


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


def header_bytes(**changes):
    values = dict(magic=crypto.MAGIC, version=1, kdf=1, n=crypto.SCRYPT_N,
                  r=crypto.SCRYPT_R, p=crypto.SCRYPT_P, salt=b"s" * 16,
                  chunk=4096, size=0, sha256=hashlib.sha256(b"").digest(), prefix=b"n" * 4)
    values.update(changes)
    return crypto.HEADER.pack(*values.values())


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


def test_header_without_crypto(monkeypatch):
    missing_crypto(monkeypatch)
    value = crypto.read_header(io.BytesIO(header_bytes()))
    assert value["version"] == 1 and value["size"] == 0
    assert (value["n"], value["r"], value["p"]) == (32768, 8, 1)


@pytest.mark.parametrize("size", [0, 1, 7, 8, crypto.HEADER.size - 1])
def test_truncated_header_without_crypto(monkeypatch, size):
    missing_crypto(monkeypatch)
    with pytest.raises(crypto.ArchiveCryptoError, match="обрезан"):
        crypto.read_header(io.BytesIO(header_bytes()[:size]))


@pytest.mark.parametrize("changes", [
    {"magic": b"unknown!"}, {"version": 2}, {"kdf": 2}, {"n": 2**31},
    {"n": 16384}, {"r": 1}, {"p": 2}, {"chunk": 0}, {"chunk": 4095},
    {"chunk": crypto.MAX_CHUNK + 1}, {"size": 4096 * 2**32 + 1},
])
def test_unsupported_header_without_crypto(monkeypatch, changes):
    missing_crypto(monkeypatch)
    with pytest.raises(crypto.ArchiveCryptoError, match="Неподдерживаемый"):
        crypto.read_header(io.BytesIO(header_bytes(**changes)))


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


def test_scrypt_documented_parameters(monkeypatch):
    def derive(password, **kwargs):
        assert password == PASSWORD.encode("utf-8")
        assert kwargs == dict(salt=b"s" * 16, n=32768, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        return b"k" * 32
    monkeypatch.setattr(hashlib, "scrypt", derive)
    assert crypto._key(lambda: PASSWORD, b"s" * 16) == b"k" * 32


def test_scrypt_and_frame_boundaries_without_crypto(monkeypatch):
    missing_crypto(monkeypatch)
    key = crypto._key(lambda: PASSWORD, b"s" * 16)
    assert len(key) == 32
    assert key != crypto._key(lambda: PASSWORD, b"t" * 16)
    value = crypto.read_header(io.BytesIO(header_bytes(size=8193)))
    frames = list(crypto._frames(value))
    assert [length for length, _, _ in frames] == [4096, 4096, 1]
    assert [nonce for _, nonce, _ in frames] == [b"n" * 4 + i.to_bytes(8, "big") for i in range(3)]
    assert len({aad for _, _, aad in frames}) == 3


def test_temporary_plaintext_cleanup_without_crypto(tmp_path, monkeypatch):
    missing_crypto(monkeypatch)
    with pytest.raises(RuntimeError):
        with crypto._temporary(tmp_path) as path:
            path.write_bytes(b"synthetic temporary plaintext")
            raise RuntimeError("synthetic failure")
    no_scratch(tmp_path)


@pytest.mark.parametrize("password", [None, "", b"bytes"])
def test_empty_or_invalid_password_refused_without_crypto(password):
    with pytest.raises(crypto.ArchiveCryptoError):
        crypto._key(lambda: password, b"s" * 16)


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
    assert header["sha256"] == hashlib.sha256(source.read_bytes()).digest()
    assert header["size"] == source.stat().st_size
    frames = list(crypto._frames(header))
    assert len({nonce for _, nonce, _ in frames}) == len(frames) > 2
    assert crypto.verify_archive(encrypted, password_provider=provider) == expected
    restored.mkdir()
    assert crypto.restore_archive(encrypted, restored, password_provider=provider) == expected
    for name in home.roots:
        assert (restored / "roots" / name / "sample.bin").read_bytes() == PAYLOAD
        assert (restored / "roots" / name / "empty").is_dir()
        assert (restored / "roots" / name / "zero").read_bytes() == b""
    no_scratch(tmp_path)


@pytest.mark.parametrize("mutation", ["password", "truncate", "drop", "reorder", "duplicate", "flip",
                                      "append", "hash", "salt", "prefix", "size", "magic"])
def test_authentication_failure_before_any_restore(aes, tmp_path, home, monkeypatch, mutation):
    source, encrypted = tmp_path / "plain.zip", tmp_path / "encrypted"
    plain.build_archive(home, source)
    crypto.encrypt_archive(source, encrypted, password_provider=lambda: PASSWORD, chunk_size=4096)
    raw = bytearray(encrypted.read_bytes())
    start, frame = crypto.HEADER.size, 4096 + crypto.TAG_SIZE
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
    elif mutation in ("hash", "salt", "prefix", "size", "magic"):
        fields = list(crypto.HEADER.unpack(raw[:start]))
        index = {"hash": 9, "salt": 6, "prefix": 10, "size": 8, "magic": 0}[mutation]
        fields[index] = fields[index] + 1 if mutation == "size" else bytes([fields[index][0] ^ 1]) + fields[index][1:]
        raw[:start] = crypto.HEADER.pack(*fields)
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


def seal(aes, path, data, *, declared_digest=None):
    """Синтетическая аутентичная оболочка с произвольным внутренним содержимым."""
    raw = header_bytes(size=len(data), sha256=declared_digest or hashlib.sha256(data).digest())
    header = crypto.read_header(io.BytesIO(raw))
    cipher = aes(crypto._key(lambda: PASSWORD, header["salt"]))
    output, offset = bytearray(raw), 0
    for length, nonce, aad in crypto._frames(header):
        output += cipher.encrypt(nonce, data[offset:offset + length], aad)
        offset += length
    path.write_bytes(output)


@pytest.mark.parametrize("invalid", ["not-zip", "manifest", "digest", "product", "empty"])
def test_authenticated_but_invalid_inner_archive(aes, tmp_path, home, monkeypatch, invalid):
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
    seal(aes, encrypted, data, declared_digest=b"0" * 32 if invalid == "digest" else None)
    target = tmp_path / "target"
    target.mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError("Восстановление до проверки ZIP запрещено")
    monkeypatch.setattr(plain, "restore_archive", forbidden)
    with pytest.raises(plain.ArchiveError):
        crypto.restore_archive(encrypted, target, password_provider=lambda: PASSWORD,
                               expected_product_id="other-product" if invalid == "product" else home.product_id)
    assert not list(target.iterdir())
    no_scratch(tmp_path)


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


def test_restore_io_failure_cleans_plaintext(aes, tmp_path, home, monkeypatch):
    encrypted = tmp_path / "encrypted"
    crypto.build_encrypted_archive(home, encrypted, password_provider=lambda: PASSWORD)
    def failed(*args, **kwargs):
        raise OSError(PASSWORD)
    monkeypatch.setattr(plain, "restore_archive", failed)
    with pytest.raises(crypto.ArchiveCryptoError) as error:
        crypto.restore_archive(encrypted, tmp_path / "target", password_provider=lambda: PASSWORD)
    assert PASSWORD not in str(error.value)
    no_scratch(tmp_path)


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
    with pytest.raises(plain.ArchiveError):
        ports.restore(transaction + "/restore", key, ())
    assert live.read_bytes() == b"new synthetic data"
    ports.backup(transaction + "/before_rollback", "before-rollback", ())
    ports.restore(transaction + "/restore", key, ())
    assert live.read_bytes() == PAYLOAD
    assert crypto.is_encrypted(ports.archive_path(transaction + "/before_rollback"))
    no_scratch(tmp_path)
