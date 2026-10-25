# HomenS.AI · © 2026 Serhii Khomenko
"""Локальный web-мастер: один публичный API, защищённая loopback-сессия."""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import uuid
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .api.v1 import InstallerAPI
from .engine import NativeEngine
from .errors import ErrorCatalog, InstallerError
from .i18n import normalize_locale
from .trust import load_trust_config

ASSETS = Path(__file__).with_name("wizard")
LOCALES = ("ru", "en", "de")
MAX_BODY = 65536


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise InstallerError("INVALID_REQUEST")
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise InstallerError("INVALID_REQUEST") from None
    if type(value) is not dict:
        raise InstallerError("INVALID_REQUEST")
    return value


class WizardSession:
    def __init__(self, api, manifest, options, *, locale=None, verifier=None,
                 development=False):
        self.api, self.manifest = api, manifest
        self.options = dict(options)
        from shared.datahome import checked_path
        self.preferences_path = checked_path(Path(api.state_dir) / "wizard-preferences.json")
        self.preferred_locale = normalize_locale(locale) if locale else None
        if self.preferred_locale is None and self.preferences_path.is_file():
            try:
                data = self.preferences_path.read_bytes()
                if len(data) <= 4096:
                    saved = _json(data).get("locale")
                    if saved in LOCALES:
                        self.preferred_locale = saved
            except (OSError, InstallerError):
                pass
        self.locale = self.preferred_locale or "en"
        self.verifier = verifier
        self.development = bool(development)
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._plan_locales = {}
        self._archive_password = None
        self._product_secrets = {}
        self._operations = set()

    def secret(self, reference):
        if type(reference) is str and reference.startswith("wizard_product:"):
            value = self._product_secrets.get(reference.split(":", 1)[1])
            if value is not None:
                return value
        if reference != "wizard_archive" or self._archive_password is None:
            raise InstallerError("PREREQUISITE_MISSING")
        return self._archive_password

    def target(self, mode="native"):
        import platform
        system = "windows" if os.name == "nt" else "linux"
        arch = {"AMD64": "x64", "amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(
            platform.machine(), platform.machine())
        return {"os": system, "arch": arch, "mode": mode, "scope": "user"}

    def bound_operation(self, operation_id):
        status = self.api.status(operation_id)
        plan = self.api.get_plan(status["plan_id"])
        if plan["source_manifest"]["product_id"] != self.manifest["product_id"]:
            raise InstallerError("OPERATION_NOT_FOUND")
        return status

    def terms(self, locale):
        locale = normalize_locale(locale)
        if self.verifier is None:
            raise InstallerError("INTEGRITY_FAILED")
        verified = self.verifier(self.manifest, self.target(), self.options)
        name = "legal/" + locale + ".txt"
        if name not in verified:
            raise InstallerError("CONSENT_REQUIRED")
        path = Path(self.options["payload_dir"]) / name
        data = path.read_bytes()
        if len(data) > 512000 or hashlib.sha256(data).hexdigest() != verified[name]:
            raise InstallerError("INTEGRITY_FAILED")
        return {"locale": locale, "digest": verified[name],
                "text": data.decode("utf-8"), "development": self.development}

    def route(self, method, path, query, body):
        api = self.api
        locale = normalize_locale(body.get("locale", query.get("locale", [self.locale])[0]))
        if method == "GET" and path == "/session":
            return {"product_id": self.manifest["product_id"], "locale": self.preferred_locale,
                    "locales": list(LOCALES), "development": self.development,
                    "options": {k: self.options.get(k) for k in ("install_root", "release")},
                    "secrets": [{"name": item["name"], "required": item.get("required", False)}
                                for item in self.manifest.get("secrets", [])],
                    "target": self.target()}
        if method == "GET" and path.startswith("/locales/"):
            name = path.removeprefix("/locales/")
            if name not in LOCALES:
                raise InstallerError("INVALID_REQUEST")
            return json.loads((ASSETS / "locales.json").read_text(encoding="utf-8"))[name]
        if method == "GET" and path.startswith("/terms/"):
            return self.terms(path.removeprefix("/terms/"))
        if method == "POST" and path == "/preferences":
            if set(body) != {"locale"} or body.get("locale") not in LOCALES:
                raise InstallerError("INVALID_REQUEST")
            from shared.datahome import checked_path
            from shared.update_plan import _atomic_write
            directory = checked_path(self.preferences_path.parent)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.locale = self.preferred_locale = body["locale"]
            _atomic_write(self.preferences_path, {"locale": self.locale})
            return {"locale": self.locale}
        if method == "POST" and path == "/secrets":
            password = body.get("archive_password")
            values = body.get("product_secrets", {})
            if (not body or set(body) - {"archive_password", "product_secrets"}
                    or password is not None and (type(password) is not str or not 12 <= len(password) <= 4096)
                    or type(values) is not dict):
                raise InstallerError("INVALID_REQUEST")
            declared = {item["name"] for item in self.manifest.get("secrets", [])}
            if set(values) - declared or any(type(v) is not str or not 1 <= len(v) <= 4096 for v in values.values()):
                raise InstallerError("INVALID_REQUEST")
            # Один секретный provider не меняет значения посреди операции.
            for operation in tuple(self._operations):
                if api.status(operation)["status"] in {"RUNNING", "QUEUED", "PENDING", "CANCEL_PENDING"}:
                    raise InstallerError("OPERATION_CONFLICT")
            if password is not None:
                self._archive_password = password
            self._product_secrets.update(values)
            self.options["secret_refs"] = {name: "wizard_product:" + name for name in self._product_secrets}
            return {"reference": "wizard_archive", "stored": "session_memory"}
        if method == "GET" and path == "/info":
            return api.describe()
        if method == "GET" and path == "/capabilities":
            return api.capabilities(self.target(query.get("mode", ["native"])[0]))
        if method == "POST" and path == "/validate":
            return api.validate(self.manifest)
        if method == "POST" and path in ("/discover", "/plans"):
            if set(body) - {"install_root", "mode", "action", "locale", "archive_path", "acknowledge_data_loss"}:
                raise InstallerError("INVALID_REQUEST")
            options = dict(self.options)
            if "install_root" in body:
                if type(body["install_root"]) is not str:
                    raise InstallerError("INVALID_REQUEST")
                options["install_root"] = body["install_root"]
            mode = body.get("mode", "native")
            if mode not in ("native", "docker"):
                raise InstallerError("INVALID_REQUEST")
            target = self.target(mode)
            if path == "/discover":
                return api.discover(self.manifest, target=target, options=options)
            action = body.get("action", "install")
            if action not in {"install", "update", "repair", "uninstall", "backup",
                              "restore", "rollback", "start", "stop", "modify_components"}:
                raise InstallerError("CAPABILITY_UNSUPPORTED")
            if body.get("archive_path"):
                if type(body["archive_path"]) is not str:
                    raise InstallerError("INVALID_REQUEST")
                options["archive_path"] = body["archive_path"]
            if action in {"restore", "rollback"}:
                if body.get("acknowledge_data_loss") is not True:
                    raise InstallerError("CONSENT_REQUIRED")
                options["acknowledge_data_loss"] = True
            terms = self.terms(locale)
            options["eula_locale"], options["eula_digest"] = locale, terms["digest"]
            # Recovery secrets и произвольные argv не принимаются из web request.
            result = api.plan(action, self.manifest, target=target, options=options)
            with self._lock:
                self._plan_locales[result["plan_id"]] = (locale, terms["digest"])
            return result
        if method == "POST" and path == "/operations":
            if body.get("accepted_terms") is not True or body.get("accepted_plan") is not True:
                raise InstallerError("CONSENT_REQUIRED")
            with self._lock:
                accepted = self._plan_locales.get(body.get("plan_id"))
            if accepted is None or body.get("terms_digest") != accepted[1]:
                raise InstallerError("CONSENT_REQUIRED")
            # API проверяет digest и полный список требуемых согласий плана.
            refs = body.get("consent_refs")
            if type(refs) is not list or not all(type(ref) is str for ref in refs):
                raise InstallerError("INVALID_REQUEST")
            result = api.submit(body["plan_id"], body["plan_digest"], consent_refs=refs,
                                idempotency_key=body["idempotency_key"], background=True)
            self._operations.add(result["operation_id"])
            return result
        if method == "GET" and path == "/operations":
            if set(query) - {"unfinished_only", "limit", "locale"}:
                raise InstallerError("INVALID_REQUEST")
            unfinished = query.get("unfinished_only", ["true"])[0]
            if unfinished not in {"true", "false"}:
                raise InstallerError("INVALID_REQUEST")
            return api.operations_list(product_id=self.manifest["product_id"],
                unfinished_only=unfinished == "true", limit=int(query.get("limit", [100])[0]))
        if path.startswith("/operations/"):
            parts = path.strip("/").split("/")
            if len(parts) not in (2, 3):
                raise InstallerError("INVALID_REQUEST")
            op = parts[1]
            tail = parts[2] if len(parts) == 3 else ""
            status = self.bound_operation(op)
            if method == "GET" and not tail:
                return status
            if method == "GET" and tail == "events":
                return api.events(op, after_sequence=int(query.get("after_sequence", [0])[0]))
            if method == "POST" and tail == "cancel":
                return api.cancel(op)
            if method == "POST" and tail == "resume":
                result = api.resume(op, decisions=body.get("decisions"), background=True)
                self._operations.add(op)
                return result
        if method == "GET" and path == "/errors":
            return {"errors": ErrorCatalog().search(query.get("query", [""])[0][:512], locale)}
        if method == "GET" and path.startswith("/errors/"):
            return ErrorCatalog().describe(path.removeprefix("/errors/"), locale)
        if method == "GET" and path.startswith("/diagnostics/"):
            operation = path.removeprefix("/diagnostics/")
            self.bound_operation(operation)
            return api.diagnostics_get(operation)
        if method == "POST" and path == "/diagnostics/export/preview":
            if set(body) - {"operation_id", "locale"}:
                raise InstallerError("INVALID_REQUEST")
            self.bound_operation(body.get("operation_id"))
            return api.diagnostics_export_preview(body.get("operation_id"))
        if method == "POST" and path == "/diagnostics/export":
            if set(body) - {"operation_id", "locale", "confirmed", "expected_digest"}:
                raise InstallerError("INVALID_REQUEST")
            self.bound_operation(body.get("operation_id"))
            directory = self.preferences_path.parent / "exports"
            destination = directory / ("diagnostics-" + uuid.uuid4().hex + ".json")
            result = api.diagnostics_export(destination, operation_id=body.get("operation_id"),
                confirmed=body.get("confirmed") is True, expected_digest=body.get("expected_digest"))
            return {"filename": destination.name, "sha256": result["sha256"],
                    "text": destination.read_text(encoding="utf-8"), "uploaded": False}
        if method == "GET" and path == "/statistics":
            return api.statistics_query(product_id=self.manifest["product_id"])
        raise InstallerError("CAPABILITY_UNSUPPORTED")


def create_server(session: WizardSession, *, port=0):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HomenSAI"
        sys_version = ""

        def log_message(self, *_):
            pass  # Не записывать URL, заголовки или токен сессии в access log.

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                return self.send(403, {"error": {"code": "INVALID_REQUEST"}})
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + expected:
                return self.send(403, {"error": {"code": "INVALID_REQUEST"}})
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.send(403, {"error": {"code": "INVALID_REQUEST"}})
            parsed = urlsplit(self.path)
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.command == "GET" and parsed.path in static:
                file, kind = static[parsed.path]
                return self.send(200, (ASSETS / file).read_bytes(), kind)
            token = self.headers.get("X-HInstall-Token", "")
            if not secrets.compare_digest(token, session.token):
                return self.send(403, {"error": {"code": "PERMISSION_REQUIRED"}})
            if not parsed.path.startswith("/api/v1/"):
                return self.send(404, {"error": {"code": "INVALID_REQUEST"}})
            try:
                body = {}
                if self.command == "POST":
                    if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                        raise InstallerError("INVALID_REQUEST")
                    if self.headers.get_content_type() != "application/json":
                        raise InstallerError("INVALID_REQUEST")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise InstallerError("INVALID_REQUEST")
                    body = _json(self.rfile.read(length))
                result = session.route(self.command, parsed.path[len("/api/v1"):],
                                       parse_qs(parsed.query, max_num_fields=20), body)
                self.send(200, {"api_version": "1", "result": result})
            except InstallerError as exc:
                self.send(400, {"api_version": "1", "error": exc.to_dict(locale=session.locale)})
            except (ValueError, TypeError, KeyError, UnicodeError):
                self.send(400, {"error": {"code": "INVALID_REQUEST"}})
            except Exception:
                self.send(500, {"error": {"code": "RECOVERY_REQUIRED"}})

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="HomenSAI installer — ru / en / de")
    for name in ("manifest", "state-dir", "payload-dir", "inventory-file", "signature-file",
                 "release", "runtime-executable", "trust-config", "install-root"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mode", action="append", default=[])
    parser.add_argument("--locale")
    parser.add_argument("--allow-development", action="store_true")
    parser.add_argument("--development-runtime")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--url-file")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if args.development_runtime and not args.allow_development:
        parser.error("--development-runtime requires --allow-development")
    from shared.manifest import load_manifest
    manifest = load_manifest(args.manifest)
    verifier = load_trust_config(args.trust_config, allow_development=args.allow_development)
    engine = NativeEngine(verifier=verifier, development_runtime=args.development_runtime)
    api = InstallerAPI(args.state_dir, engine=engine, locale=normalize_locale(args.locale))
    options = {key: getattr(args, key) for key in ("payload_dir", "inventory_file", "signature_file",
                                                  "release", "runtime_executable", "install_root")}
    if args.mode:
        options["modes"] = args.mode
    options["backup_password_ref"] = "wizard_archive"
    session = WizardSession(api, manifest, options, locale=args.locale, verifier=verifier,
                            development=args.allow_development)
    engine.secret_provider = session.secret
    server = create_server(session, port=args.port)
    url = f"http://127.0.0.1:{server.server_port}/#token={session.token}"
    if args.url_file:
        fd = os.open(args.url_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(url + "\n")
    if not args.no_browser:
        webbrowser.open(url)
    print(f"HomenSAI installer: http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
