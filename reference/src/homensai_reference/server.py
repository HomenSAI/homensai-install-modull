"""Малый настоящий web product без внешних запросов и runtime dependencies."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import html
import json
import os
import signal
import sqlite3
import threading


def main():
    product = os.environ["HOMENSAI_PRODUCT_ID"]
    instance = os.environ["HOMENSAI_INSTALLATION_ID"]
    release = os.environ["HOMENSAI_RELEASE"]
    database = Path(os.environ["HOMENSAI_DATA_HOME"]) / "reference.sqlite"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ready = False
            notes = []
            try:
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    notes = [row[0] for row in db.execute("SELECT text FROM notes ORDER BY id")]
                    ready = db.execute("PRAGMA user_version").fetchone()[0] == 1
            except sqlite3.Error:
                pass
            if self.path == "/healthz":
                data = json.dumps({"product_id": product, "installation_id": instance,
                                   "release": release, "ready": ready}).encode()
                kind = "application/json"
                code = 200 if ready else 503
            elif self.path == "/":
                body = "".join("<li>" + html.escape(note) + "</li>" for note in notes)
                data = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                        "<title>HomenSAI Reference</title><body><main>"
                        "<h1>HomenSAI Reference</h1><p>Installed · Установлено · Installiert</p>"
                        "<p>Release: " + html.escape(release) + "</p><ul>" + body +
                        "</ul><p>Local SQLite database is available.</p></main></body></html>").encode()
                kind, code = "text/html; charset=utf-8", 200
            else:
                data, kind, code = b"Not found", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True

    server = Server(("127.0.0.1", int(os.environ["HOMENSAI_WEB_PORT"])), Handler)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
