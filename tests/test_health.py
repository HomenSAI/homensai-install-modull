"""Локальная проверка идентичности: подставные ответы без сетевых запросов."""
import json

import pytest

import shared.health as health


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size):
        return self.body[:size]


def fake_response(monkeypatch, body, status=200):
    seen = []
    class Opener:
        def open(self, url, timeout):
            seen.append((url, timeout))
            return Response(body, status)
    def opener(proxy, redirect):
        assert proxy.proxies == {}
        assert redirect.redirect_request(None, None, 302, '', {}, 'http://evil.invalid') is None
        return Opener()
    monkeypatch.setattr(health.urllib.request, 'build_opener', opener)
    return seen


def test_good_response_and_fixed_loopback(monkeypatch):
    body = json.dumps({'product': 'demo', 'release': '1.0', 'healthy': True}).encode()
    seen = fake_response(monkeypatch, body)
    assert health.check_local_health(8765, product_id='demo', version='1.0') is None
    assert seen == [('http://127.0.0.1:8765/health', 3.0)]


@pytest.mark.parametrize('body,status', [
    (b'{"product":"other","release":"1","healthy":true}', 200),
    (b'{"product":"demo","release":"2","healthy":true}', 200),
    (b'{"product":"demo","release":"1","healthy":false}', 200),
    (b'{"product":"demo","release":"1","healthy":1}', 200),
    (b'{"product":"demo","release":"1","healthy":true,"extra":1}', 200),
    (b'{"product":"demo","product":"demo","release":"1","healthy":true}', 200),
    (b'{"product":"SECRET-SENTINEL", "healthy":NaN}', 200),
    (b'\xff', 200), (b' ' * (health.MAX_RESPONSE + 1), 200), (b'{}', 503),
])
def test_untrusted_or_invalid_response_fails_without_echo(monkeypatch, body, status):
    fake_response(monkeypatch, body, status)
    with pytest.raises(health.HealthError) as error:
        health.check_local_health(8765, product_id='demo', version='1')
    assert 'SECRET-SENTINEL' not in str(error.value)


@pytest.mark.parametrize('change', [
    {'port': 0}, {'port': True}, {'port': 65536}, {'port': '8765'},
    {'product_id': '../secret'}, {'version': '1\n'}, {'path': 'http://example.invalid'},
    {'path': '/health?secret=x'}, {'path': '/health\n'}, {'timeout': float('nan')},
    {'timeout': 0}, {'timeout': 10**1000}, {'timeout': True},
])
def test_invalid_inputs_never_open_socket(monkeypatch, change):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid input must not invoke network')
    monkeypatch.setattr(health.urllib.request, 'build_opener', forbidden)
    args = {'port': 8765, 'product_id': 'demo', 'version': '1'}
    args.update(change)
    with pytest.raises(health.HealthError):
        health.check_local_health(**args)


def test_connection_failure_does_not_expose_exception(monkeypatch):
    class Broken:
        def open(self, url, timeout):
            raise OSError('SECRET-SENTINEL')
    monkeypatch.setattr(health.urllib.request, 'build_opener', lambda *args: Broken())
    with pytest.raises(health.HealthError) as error:
        health.check_local_health(8765, product_id='demo', version='1')
    assert 'SECRET-SENTINEL' not in str(error.value)
