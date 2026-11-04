# HomenS.AI · © 2026 Serhii Khomenko
"""Основа локальных веб-страниц HomenS.AI (установщик, сборщик): HTTP из стандартной библиотеки.

Защита, общая для обеих страниц:
- обслуживаются только заранее перечисленные файлы shared/web и маршруты /api/;
- заголовок Host — из разрешённого списка (защита от DNS rebinding);
- каждый запрос /api/ несёт случайный токен сеанса в заголовке X-HomenSAI-Token;
  чужой сайт не может задать этот заголовок без CORS, которого сервер не отвечает;
- Origin, если он есть, совпадает с адресом страницы; тело — JSON до 64 КиБ;
- строгий CSP без inline-кода, no-store, без журнала запросов (в теле бывают секреты).
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit

ASSETS = Path(__file__).with_name('web')
TOKEN_HEADER = 'X-HomenSAI-Token'
MAX_BODY = 64 * 1024
_TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
          '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
          '.svg': 'image/svg+xml'}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")


class HttpError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass
class Download:
    """Ответ-файл: отдаётся потоком как вложение."""
    path: Path
    name: str


Route = Callable[[dict, dict | None], 'dict | Download']


class WebApp:
    def __init__(self, *, page: str, assets: tuple[str, ...], routes: dict[tuple[str, str], Route],
                 token: str, hosts: set[str]):
        self.assets = {'/' + name: ASSETS / name for name in assets}
        self.assets['/'] = ASSETS / page
        self.routes, self.token = routes, token
        self.hosts = {host.lower() for host in hosts}
        self.last_request = time.monotonic()


def _handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'HomenSAI'
        sys_version = ''

        def log_message(self, format, *args):  # noqa: A002 — сигнатура базового класса
            pass

        def do_GET(self):
            self._handle('GET')

        def do_POST(self):
            self._handle('POST')

        def _handle(self, method):
            try:
                host = (self.headers.get('Host') or '').lower()
                if host not in app.hosts:
                    raise HttpError(421, 'host')
                url = urlsplit(self.path)
                if url.path.startswith('/api/'):
                    self._check_api(host, method)
                    app.last_request = time.monotonic()
                    route = app.routes.get((method, url.path))
                    if route is None:
                        raise HttpError(404, 'not_found')
                    query = {name: values[-1] for name, values in parse_qs(url.query).items()}
                    result = route(query, self._body() if method == 'POST' else None)
                    if isinstance(result, Download):
                        self._send_file(result)
                    else:
                        self._send(200, _TYPES['.json'], json.dumps(result, ensure_ascii=False).encode('utf-8'))
                    return
                asset = app.assets.get(url.path) if method == 'GET' else None
                if asset is None:
                    raise HttpError(404, 'not_found')
                self._send(200, _TYPES[asset.suffix], asset.read_bytes())
            except HttpError as error:
                self._send(error.status, _TYPES['.json'], json.dumps({'error': error.code}).encode('ascii'))
            except Exception:  # Подробности не уходят в браузер.
                self._send(500, _TYPES['.json'], b'{"error": "internal"}')

        def _check_api(self, host, method):
            if not hmac.compare_digest(self.headers.get(TOKEN_HEADER, '').encode('utf-8', 'replace'),
                                       app.token.encode('ascii')):
                raise HttpError(403, 'token')
            origin = self.headers.get('Origin')
            if origin is not None and origin.lower() != 'http://' + host:
                raise HttpError(403, 'origin')
            if self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none'):
                raise HttpError(403, 'origin')
            if method == 'POST' and not (self.headers.get('Content-Type') or '').startswith('application/json'):
                raise HttpError(415, 'content_type')

        def _body(self) -> dict:
            try:
                length = int(self.headers.get('Content-Length') or '0')
            except ValueError:
                raise HttpError(400, 'length') from None
            if not 0 < length <= MAX_BODY:
                raise HttpError(413 if length > MAX_BODY else 400, 'length')
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8'))
            except (UnicodeError, ValueError):
                raise HttpError(400, 'json') from None
            if type(body) is not dict:
                raise HttpError(400, 'json')
            return body

        def _headers(self, status, content_type, length):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', _CSP)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')

        def _send(self, status, content_type, data: bytes):
            self._headers(status, content_type, len(data))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, download: Download):
            size = download.path.stat().st_size
            self._headers(200, 'application/octet-stream', size)
            self.send_header('Content-Disposition', 'attachment; filename="' + download.name + '"')
            self.end_headers()
            with download.path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    self.wfile.write(block)

    return Handler


def start(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    """Создать сервер и запустить его в фоновом потоке; порт 0 — свободный порт системы."""
    server = ThreadingHTTPServer((host, port), _handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True).start()
    return server


def text(body: dict, name: str, *, limit: int = 4096, required: bool = False) -> str | None:
    """Строковое поле тела запроса; пустое — None."""
    value = body.get(name)
    if value is None or value == '':
        if required:
            raise HttpError(400, 'field:' + name)
        return None
    if type(value) is not str or len(value) > limit or '\x00' in value:
        raise HttpError(400, 'field:' + name)
    return value


def flag(body: dict, name: str) -> bool:
    value = body.get(name, False)
    if type(value) is not bool:
        raise HttpError(400, 'field:' + name)
    return value
