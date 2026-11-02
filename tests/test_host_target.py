"""Среда процесса определяется консервативно и не является проверкой Docker."""
import pytest

import shared.host_target as host


@pytest.mark.parametrize('system,machine,expected', [
    ('linux', 'x86_64', 'linux'), ('win32', 'AMD64', 'windows'), ('win32', 'x86_64', 'windows'),
])
def test_supported_native_process(monkeypatch, system, machine, expected):
    monkeypatch.setattr(host.sys, 'platform', system)
    monkeypatch.setattr(host.platform, 'machine', lambda: machine)
    monkeypatch.setattr(host.struct, 'calcsize', lambda _: 8)
    assert host.native_target() == {'os': expected, 'architecture': 'x86_64', 'execution': 'native'}


@pytest.mark.parametrize('system,machine,pointer_size', [
    ('darwin', 'x86_64', 8), ('linux', 'aarch64', 8), ('win32', 'ARM64', 8),
    ('linux', 'x86_64', 4), ('win32', 'x86', 4), ('linux', '', 8),
])
def test_unknown_platform_never_guessed(monkeypatch, system, machine, pointer_size):
    monkeypatch.setattr(host.sys, 'platform', system)
    monkeypatch.setattr(host.platform, 'machine', lambda: machine)
    monkeypatch.setattr(host.struct, 'calcsize', lambda _: pointer_size)
    with pytest.raises(host.HostTargetError):
        host.native_target()


def test_platform_inspection_failure_is_safe(monkeypatch):
    def fail():
        raise OSError('SECRET-SENTINEL')
    monkeypatch.setattr(host.platform, 'machine', fail)
    with pytest.raises(host.HostTargetError) as error:
        host.native_target()
    assert 'SECRET-SENTINEL' not in str(error.value)
