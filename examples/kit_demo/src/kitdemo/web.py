# HomenS.AI · © 2026 Serhii Khomenko
"""HTTP-служба на 127.0.0.1 с проверкой здоровья и отчётом о двоичной зависимости."""
from __future__ import annotations

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

import cffi

from kitdemo.common import arguments, database, graceful_stop, release


def main() -> int:
    args = arguments('kitdemo web', port=True)
    graceful_stop()
    product, version = release()
    # cffi использует двоичный модуль _cffi_backend из pydeps выпуска.
    ffi = cffi.FFI()
    (args.data_dir).mkdir(parents=True, exist_ok=True)
    (args.data_dir / 'deps.json').write_text(json.dumps(
        {'release': version, 'cffi': cffi.__version__, 'sizeof_int': ffi.sizeof('int'),
         'cffi_path': str(Path(cffi.__file__).parent)}), encoding='utf-8')
    with closing(database(args.data_dir)):
        pass
    healthy = not (Path(__file__).with_name('FAIL_HEALTH')).exists()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/healthz':
                body = json.dumps({'product': product, 'release': version, 'healthy': healthy}).encode()
                self.send_response(200 if healthy else 503)
            elif self.path == '/notes':
                with closing(database(args.data_dir)) as connection:
                    rows = [row[0] for row in connection.execute('SELECT text FROM notes ORDER BY id')]
                body = json.dumps(rows).encode()
                self.send_response(200)
            else:
                body = b'{}'
                self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *values):
            return

    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.serve_forever(poll_interval=0.2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
