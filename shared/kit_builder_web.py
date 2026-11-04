# HomenS.AI · © 2026 Serhii Khomenko
"""Веб-страница сборщика комплектов: те же команды `python -m shared.kit` в браузере.

Предназначена для контейнера (docker/builder): каталоги проектов, ключей и
готовых комплектов подключаются томами, страница доступна только с хоста
(порт публикуется на 127.0.0.1). Каждая команда — отдельный процесс
`python -m shared.kit …` с минимальным окружением; одновременно выполняется
одна. Выбор проекта, ключа и комплекта — только из перечней, которые сервер
сам составил по своим каталогам; произвольные пути из браузера не принимаются.
Пароль ключа подписи передаётся процессу через переменную окружения и нигде
не сохраняется.

Запуск без Docker: `python -m shared.kit_builder_web --projects DIR --keys DIR --output DIR`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading

from shared.kit import CONFIG_NAME, KitError, load_config
from shared.web_ui import Download, HttpError, WebApp, start, text

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ('hsi.css', 'common.js', 'builder.js', 'lang.json', 'mark.svg')
PASSPHRASE_ENV = 'HOMENSAI_BUILDER_KEY_PASSPHRASE'
TOKEN_ENV = 'HOMENSAI_BUILDER_TOKEN'
_VERSION = re.compile(r'[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\Z')
_SEQUENCE = re.compile(r'\d{1,9}(\.\d{1,9}){0,5}\Z')
_KEY_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_PRODUCT_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,62}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_MAX_LINES = 5000


class Job:
    """Один процесс `python -m shared.kit` и его вывод."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.title: str | None = None
        self.running = False
        self.code: int | None = None

    def run(self, title: str, arguments: list[str], secret: str | None = None):
        with self.lock:
            if self.running:
                raise HttpError(409, 'busy')
            self.lines, self.title, self.running, self.code = [], title, True, None
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
                       'HOME': os.environ.get('HOME', '/tmp'), 'PYTHONDONTWRITEBYTECODE': '1'}
        if secret is not None:
            environment[PASSPHRASE_ENV] = secret
        for name in ('PIP_INDEX_URL', 'PIP_CERT', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'SSL_CERT_FILE'):
            if name in os.environ:
                environment[name] = os.environ[name]
        try:
            process = subprocess.Popen([sys.executable, '-m', 'shared.kit', *arguments], cwd=ROOT,
                                       env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        except OSError:
            self._finish(127)
            raise HttpError(500, 'spawn') from None

        def follow():
            for line in process.stdout:
                with self.lock:
                    if len(self.lines) < _MAX_LINES:
                        self.lines.append(line.rstrip('\n'))
            self._finish(process.wait())
        threading.Thread(target=follow, daemon=True).start()

    def _finish(self, code: int):
        with self.lock:
            self.running, self.code = False, code

    def snapshot(self, after: int) -> dict:
        with self.lock:
            return {'title': self.title, 'lines': self.lines[after:], 'next': len(self.lines),
                    'running': self.running, 'code': self.code}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class BuilderWeb:
    def __init__(self, *, projects: Path, keys: Path, output: Path):
        self.projects, self.keys, self.output = projects.resolve(), keys.resolve(), output.resolve()
        self.job = Job()

    # --- перечни -------------------------------------------------------------------
    def project_list(self) -> dict[str, dict]:
        """Проекты: сам каталог projects или его подкаталоги с homensai-kit.json."""
        found = {}
        if not self.projects.is_dir():
            return found
        candidates = [self.projects] + sorted(item for item in self.projects.iterdir()
                                              if item.is_dir() and not item.is_symlink()
                                              and not item.name.startswith('.'))
        for directory in candidates:
            config = directory / CONFIG_NAME
            name = '.' if directory == self.projects else directory.name
            if not config.is_file():
                if directory != self.projects:
                    found[name] = {'id': name, 'configured': False}
                continue
            item = {'id': name, 'configured': True}
            try:
                raw = json.loads(config.read_text(encoding='utf-8'))
                manifest = json.loads((directory / raw['manifest']).read_text(encoding='utf-8'))
                item.update(product_id=manifest.get('product_id'), product_name=manifest.get('product_name'),
                            targets=raw.get('targets', ['linux-x86_64']),
                            secrets=[entry.get('name') for entry in raw.get('secrets', [])])
                loaded = load_config(config)
                item['wheels'] = sum(1 for _ in loaded['wheelhouse'].glob('*.whl'))
                item['ready'] = True
            except (OSError, ValueError, KeyError, TypeError, AttributeError, KitError) as error:
                item['ready'] = False
                item['problem'] = str(error) if isinstance(error, KitError) else type(error).__name__
            found[name] = item
        return found

    def key_list(self) -> dict[str, dict]:
        found = {}
        if not self.keys.is_dir():
            return found
        for private in sorted(self.keys.glob('*-ed25519.pem')):
            name = private.name[:-len('-ed25519.pem')]
            public = private.with_name(name + '-ed25519.pub')
            try:
                head = private.read_bytes()[:64]
                fingerprint = hashlib.sha256(public.read_bytes()).hexdigest() if public.is_file() else None
            except OSError:
                continue
            found[name] = {'name': name, 'encrypted': b'ENCRYPTED' in head, 'public_key_sha256': fingerprint}
        return found

    def kit_list(self) -> dict[str, dict]:
        found = {}
        if not self.output.is_dir():
            return found
        for directory in sorted(self.output.iterdir()):
            meta_path = directory / 'kit.json'
            if not directory.is_dir() or directory.is_symlink() or not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            archive = self.output / (directory.name + '.tar.gz')
            found[directory.name] = {
                'name': directory.name, 'product_id': meta.get('product_id'), 'version': meta.get('version'),
                'public_key_sha256': meta.get('public_key_sha256'), 'targets': meta.get('targets'),
                'archive': archive.name if archive.is_file() else None,
                'archive_bytes': archive.stat().st_size if archive.is_file() else None,
                'time': int(meta_path.stat().st_mtime)}
        return found

    def _pick(self, listing: dict, body: dict, field: str) -> dict:
        value = text(body, field, limit=256, required=True)
        if value not in listing:
            raise HttpError(404, 'unknown:' + field)
        return listing[value]

    def _config(self, project: dict) -> str:
        directory = self.projects if project['id'] == '.' else self.projects / project['id']
        return str(directory / CONFIG_NAME)

    # --- маршруты ------------------------------------------------------------------
    def overview(self, query, body):
        return {'projects': list(self.project_list().values()), 'keys': list(self.key_list().values()),
                'kits': list(self.kit_list().values()), 'job': self.job.snapshot(0)['running']}

    def build(self, query, body):
        project = self._pick(self.project_list(), body, 'project')
        if not project['configured']:
            raise HttpError(409, 'not_configured')
        key = self._pick(self.key_list(), body, 'key')
        version = text(body, 'version', limit=64, required=True)
        if _VERSION.fullmatch(version) is None:
            raise HttpError(400, 'field:version')
        sequence = text(body, 'sequence', limit=64)
        if sequence is not None and _SEQUENCE.fullmatch(sequence) is None:
            raise HttpError(400, 'field:sequence')
        passphrase = text(body, 'passphrase', limit=1024)
        if key['encrypted'] and passphrase is None:
            raise HttpError(400, 'field:passphrase')
        arguments = ['build', '--config', self._config(project), '--version', version,
                     '--signing-key', str(self.keys / (key['name'] + '-ed25519.pem')), '--output', str(self.output)]
        if sequence is not None:
            arguments += ['--sequence', sequence]
        if key['encrypted']:
            arguments += ['--passphrase-env', PASSPHRASE_ENV]
        self.output.mkdir(parents=True, exist_ok=True)
        self.job.run('build ' + (project['product_id'] or project['id']) + ' ' + version, arguments,
                     passphrase if key['encrypted'] else None)
        return {'started': True}

    def verify(self, query, body):
        kit = self._pick(self.kit_list(), body, 'kit')
        expected = text(body, 'expect_key_sha256', limit=64)
        if expected is not None and _HEX64.fullmatch(expected) is None:
            raise HttpError(400, 'field:expect_key_sha256')
        arguments = ['verify', str(self.output / kit['name'])]
        if expected is not None:
            arguments += ['--expect-key-sha256', expected]
        self.job.run('verify ' + kit['name'], arguments)
        return {'started': True}

    def fetch(self, query, body):
        project = self._pick(self.project_list(), body, 'project')
        if not project['configured']:
            raise HttpError(409, 'not_configured')
        self.job.run('fetch ' + project['id'], ['fetch', '--config', self._config(project)])
        return {'started': True}

    def keygen(self, query, body):
        name = text(body, 'name', limit=64, required=True)
        if _KEY_NAME.fullmatch(name) is None:
            raise HttpError(400, 'field:name')
        if name in self.key_list():
            raise HttpError(409, 'exists')
        passphrase = text(body, 'passphrase', limit=1024)
        arguments = ['keygen', str(self.keys), '--name', name]
        if passphrase is not None:
            arguments += ['--passphrase-env', PASSPHRASE_ENV]
        self.job.run('keygen ' + name, arguments, passphrase)
        return {'started': True}

    def init(self, query, body):
        project = self._pick(self.project_list(), body, 'project')
        if project['configured']:
            raise HttpError(409, 'exists')
        product_id = text(body, 'product_id', limit=63, required=True)
        if _PRODUCT_ID.fullmatch(product_id) is None:
            raise HttpError(400, 'field:product_id')
        product_name = text(body, 'product_name', limit=128, required=True)
        port = body.get('port', 8800)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise HttpError(400, 'field:port')
        self.job.run('init ' + project['id'], ['init', str(self.projects / project['id']), '--product-id',
                                                product_id, '--product-name', product_name, '--port', str(port)])
        return {'started': True}

    def progress(self, query, body):
        try:
            after = max(0, int(query.get('after', '0')))
        except ValueError:
            raise HttpError(400, 'field:after') from None
        return self.job.snapshot(after)

    def download(self, query, body):
        name = query.get('name', '')
        kit = next((item for item in self.kit_list().values() if item['archive'] == name), None)
        if kit is None:
            raise HttpError(404, 'unknown:name')
        return Download(self.output / name, name)

    def checksum(self, query, body):
        name = query.get('name', '')
        kit = next((item for item in self.kit_list().values() if item['archive'] == name), None)
        if kit is None:
            raise HttpError(404, 'unknown:name')
        return {'name': name, 'sha256': _sha256(self.output / name)}

    def app(self, token: str, hosts: set[str]) -> WebApp:
        return WebApp(page='builder.html', assets=ASSETS, token=token, hosts=hosts, routes={
            ('GET', '/api/overview'): self.overview, ('GET', '/api/progress'): self.progress,
            ('GET', '/api/download'): self.download, ('GET', '/api/checksum'): self.checksum,
            ('POST', '/api/build'): self.build, ('POST', '/api/verify'): self.verify,
            ('POST', '/api/fetch'): self.fetch, ('POST', '/api/keygen'): self.keygen,
            ('POST', '/api/init'): self.init})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m shared.kit_builder_web',
                                     description='Веб-страница сборщика комплектов HomenS.AI')
    parser.add_argument('--projects', type=Path, required=True)
    parser.add_argument('--keys', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--public-port', type=int,
                        help='порт на хосте, если он отличается от --port (Docker -p 127.0.0.1:N:8790)')
    parser.add_argument('--allow-host', action='append', default=[],
                        help='дополнительное разрешённое значение заголовка Host (имя:порт)')
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)
    if re.fullmatch(r'[A-Za-z0-9_-]{24,128}', token) is None:
        print(TOKEN_ENV + ': от 24 до 128 символов A-Z, a-z, 0-9, _ и -', file=sys.stderr)
        return 2
    public = args.public_port or args.port
    hosts = {'127.0.0.1:' + str(public), 'localhost:' + str(public), *args.allow_host}
    web = BuilderWeb(projects=args.projects, keys=args.keys, output=args.output)
    server = start(web.app(token, hosts), args.host, args.port)
    print('Сборщик HomenS.AI: http://127.0.0.1:' + str(public) + '/#t=' + token, flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt
    # В контейнере процесс — PID 1: без обработчика SIGTERM (docker stop) игнорируется.
    signal.signal(signal.SIGTERM, stop)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
