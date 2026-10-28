"""Тесты не обращаются к сети; все создаваемые файлы — внутри tmp_path."""

import hashlib
import os
from pathlib import Path
import socket
import sys

import pytest


@pytest.fixture
def private_test_python(tmp_path):
    """Копия Python только для synthetic subprocess tests, без изменения toolcache."""
    source = Path(sys.executable).resolve(strict=True)
    if sys.platform != "linux":
        return source, hashlib.sha256(source.read_bytes()).hexdigest()
    payload = source.read_bytes()
    expected = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "private-test-python"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    path.chmod(0o700)
    assert path.stat().st_nlink == 1 and not path.stat().st_mode & 0o077
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    return path, expected


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Сетевые обращения запрещены в тестах")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
