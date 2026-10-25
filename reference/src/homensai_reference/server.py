"""Настоящий reference web product: только loopback, SQLite и синтетические данные."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import html
import json
import os
import signal
import sqlite3
import threading
from urllib.parse import urlsplit

from .migrate import validate_schema
from .profile import ReferenceProductError, api_key_configured, load_profile, safe_path, validate_profile
from .storage import DEFAULT_SETTINGS, load_settings


def readiness(database: str | Path, *, profile: dict | None = None, product_id: str,
              installation_id: str, release: str, environ=None, config_home=None) -> dict:
    """Helper не создаёт БД, не слушает порт и не обращается к сети."""
    selected = load_profile() if profile is None else validate_profile(profile)
    schema = None
    database_ok = False
    try:
        database = safe_path(database)
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            validate_schema(db, schema)
            database_ok = schema == selected["database_schema"] and db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    except (OSError, sqlite3.Error, ReferenceProductError):
        pass
    settings_ok = True
    try:
        load_settings(config_home)
    except ReferenceProductError:
        settings_ok = False
    configured = api_key_configured(environ)
    checks = {"database": database_ok, "settings": settings_ok,
              "api_key": not selected["require_api_key"] or configured,
              "profile": not selected["health_failure"]}
    return {"product_id": product_id, "installation_id": installation_id, "release": release,
            "product_version": selected["product_version"], "database_schema": schema,
            "expected_database_schema": selected["database_schema"],
            "api_key_configured": configured, "api_key_required": selected["require_api_key"],
            "ready": all(checks.values()), "checks": checks}


def read_notes(database: str | Path, schema: int | None) -> list[dict]:
    if schema not in {1, 2, 3}:
        return []
    fields = ["id", "text"]
    if schema >= 2:
        fields += ["normalized_text", "text_length", "category"]
    if schema >= 3:
        fields += ["content_sha256", "is_archived"]
    try:
        path = safe_path(database)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            rows = db.execute("SELECT " + ",".join(fields) + " FROM notes ORDER BY id LIMIT 100").fetchall()
        notes = []
        for row in rows:
            if any(type(value) not in {str, int} for value in row):
                return []
            notes.append({name: value[:2048] if type(value) is str else value for name, value in zip(fields, row)})
        return notes
    except (OSError, sqlite3.Error, ReferenceProductError):
        return []


def render_page(health: dict, notes: list[dict], settings: dict | None = None) -> bytes:
    """HTML только из whitelist-полей с escaping пользовательских записей/настроек."""
    settings = dict(DEFAULT_SETTINGS) if settings is None else settings
    language = settings.get("language") if settings.get("language") in {"ru", "en", "de"} else "ru"
    theme = settings.get("theme") if settings.get("theme") in {"system", "light", "dark"} else "system"
    labels = {
        "ru": {"ready": "Установлено", "waiting": "Требуется восстановление", "version": "Версия продукта",
               "schema": "Схема базы", "release": "Выпуск", "notes": "Записи", "derived": "Производные данные",
               "key": "API-ключ настроен", "yes": "Да", "no": "Нет"},
        "en": {"ready": "Installed", "waiting": "Recovery required", "version": "Product version",
               "schema": "Database schema", "release": "Release", "notes": "Notes", "derived": "Derived data",
               "key": "API key configured", "yes": "Yes", "no": "No"},
        "de": {"ready": "Installiert", "waiting": "Wiederherstellung erforderlich", "version": "Produktversion",
               "schema": "Datenbankschema", "release": "Release", "notes": "Notizen", "derived": "Abgeleitete Daten",
               "key": "API-Schlüssel eingerichtet", "yes": "Ja", "no": "Nein"},
    }[language]

    def escaped(value):
        return html.escape(str(value), quote=True)

    title = escaped(settings.get("site_title", "HomenSAI Reference"))
    status = labels["ready"] if health["ready"] else labels["waiting"]
    items = []
    for note in notes:
        detail = ""
        if "normalized_text" in note:
            detail += "<p>" + labels["derived"] + ": <code>" + escaped(note["normalized_text"]) + "</code></p>"
        if "content_sha256" in note:
            detail += "<p>SHA-256: <code>" + escaped(note["content_sha256"]) + "</code></p>"
        items.append("<li><p>" + escaped(note.get("text", "")) + "</p>" + detail + "</li>")
    release = ("<div><dt>" + labels["release"] + "</dt><dd>" + escaped(health["release"]) + "</dd></div>") if settings.get("show_release", True) else ""
    page = ("<!doctype html><html lang='" + language + "' class='theme-" + theme + "'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><title>" + title + "</title>"
            "<style>body{margin:0;background:#f4f6fa;color:#18233a;font:16px system-ui,sans-serif}"
            "main{max-width:900px;margin:48px auto;padding:32px;background:white;border-radius:18px}"
            "h1{margin-top:0}dl{display:flex;flex-wrap:wrap;gap:24px}dt{font-size:13px;color:#55627b}"
            "dd{margin:6px 0;font-weight:600}li{margin:18px 0}code{overflow-wrap:anywhere}"
            ".status{padding:8px 12px;background:#e7eef8;border-radius:8px;display:inline-block}"
            ".theme-dark body{background:#101723;color:#eef2fb}.theme-dark main{background:#1b2638}"
            ".theme-dark dt{color:#b9c7df}.theme-dark .status{background:#2b3e5b}"
            "@media(prefers-color-scheme:dark){.theme-system body{background:#101723;color:#eef2fb}"
            ".theme-system main{background:#1b2638}.theme-system dt{color:#b9c7df}"
            ".theme-system .status{background:#2b3e5b}}"
            "@media(max-width:600px){main{margin:0;padding:20px;border-radius:0}}</style></head><body><main>"
            "<h1>" + title + "</h1><p class='status'>" + status + "</p><dl>"
            "<div><dt>" + labels["version"] + "</dt><dd>" + escaped(health["product_version"]) + "</dd></div>"
            "<div><dt>" + labels["schema"] + "</dt><dd>" + escaped(health["database_schema"]) + "</dd></div>"
            + release + "<div><dt>" + labels["key"] + "</dt><dd>"
            + (labels["yes"] if health["api_key_configured"] else labels["no"]) + "</dd></div></dl>"
            "<h2>" + labels["notes"] + "</h2><ul>" + "".join(items) + "</ul>"
            "<p>HomenSAI Reference · local SQLite · synthetic reference product</p></main></body></html>")
    return page.encode("utf-8")


def main():
    try:
        selected = load_profile()
        product = os.environ["HOMENSAI_PRODUCT_ID"]
        instance = os.environ["HOMENSAI_INSTALLATION_ID"]
        release = os.environ["HOMENSAI_RELEASE"]
        home = safe_path(Path(os.environ["HOMENSAI_DATA_HOME"]))
        database = home / "reference.sqlite"
        config = safe_path(Path(os.environ["HOMENSAI_CONFIG_HOME"])) if os.environ.get("HOMENSAI_CONFIG_HOME") else home / "config"
        port = int(os.environ["HOMENSAI_WEB_PORT"])
        if not 1024 <= port <= 65535:
            raise ValueError
    except ReferenceProductError as error:
        print(json.dumps({"error": error.code}))
        return 1
    except (KeyError, ValueError, OSError):
        print(json.dumps({"error": "REFERENCE_CONFIGURATION_INVALID"}))
        return 1

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            expected_hosts = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            if self.headers.get("Host") not in expected_hosts:
                self.send_error(400)
                return
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                self.send_error(400)
                return
            health = readiness(database, profile=selected, product_id=product, installation_id=instance,
                               release=release, config_home=config)
            if parsed.path == "/healthz":
                data = json.dumps(health).encode()
                kind = "application/json"
                code = 200 if health["ready"] else 503
            elif parsed.path == "/":
                try:
                    settings = load_settings(config)
                except ReferenceProductError:
                    settings = dict(DEFAULT_SETTINGS)
                data = render_page(health, read_notes(database, health["database_schema"]), settings)
                kind = "text/html; charset=utf-8"
                code = 200 if health["ready"] else 503
            else:
                data, kind, code = b"Not found", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", port), Handler)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
