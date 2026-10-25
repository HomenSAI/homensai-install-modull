"""Реальные процессные проверки используют только loopback, без внешней сети."""

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def loopback_network_only(monkeypatch):
    original_socket = socket.socket
    original_resolve = socket.getaddrinfo

    def allowed(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    class LoopbackSocket(original_socket):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                assert allowed(address[0]), "Integration tests prohibit external network"
            return super().connect(address)

        def connect_ex(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                assert allowed(address[0]), "Integration tests prohibit external network"
            return super().connect_ex(address)

        def bind(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                assert allowed(address[0]), "Integration tests bind only loopback"
            return super().bind(address)

    def resolve(host, *args, **kwargs):
        assert allowed(host), "Integration tests prohibit external DNS"
        return original_resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", LoopbackSocket)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
