# HomenS.AI · © 2026 Serhii Khomenko
"""Локальный web-мастер: один публичный API, защищённая loopback-сессия."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from copy import deepcopy
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
from .api.model import bounded_json, checked_id, digest
from .backends import configured_backends
from .errors import ErrorCatalog, InstallerError
from .i18n import normalize_locale
from .local_artifacts import LocalArtifactUploads, MAX_UPLOAD_BYTES
from .trust import load_trust_config

ASSETS = Path(__file__).with_name("wizard")
LOCALES = ("ru", "en", "de")
MAX_BODY = 65536
MAX_PREREQUISITE_PLANS = 16
MAX_PREREQUISITE_OBSERVATIONS = 64


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
                 development=False, mode_options=None):
        self.api, self.manifest = api, manifest
        self.options = dict(options)
        supplied = {} if mode_options is None else mode_options
        artifact_keys = {"payload_dir", "inventory_file", "signature_file", "release",
                         "runtime_executable", "product_version"}
        required = artifact_keys - {"product_version"}
        if (type(supplied) is not dict or set(supplied) - {"docker"}
                or any(type(value) is not dict or set(value) - artifact_keys
                       or not required.issubset(value)
                       or any(type(item) is not str or not item or len(item) > 4096
                              for item in value.values()) for value in supplied.values())):
            raise InstallerError("INVALID_REQUEST")
        self.mode_options = {mode: dict(value) for mode, value in supplied.items()}
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
        self._lock = threading.RLock()
        self._plan_locales = {}
        self._archive_password = None
        self._product_secrets = {}
        self._operations = set()
        self._review_generation = 0
        self._plan_generations = {}
        self._prerequisite_plans = OrderedDict()
        self._prerequisite_observations = OrderedDict()
        self._prerequisite_inflight = set()
        self._prerequisite_binding = self._bind_prerequisites()
        self._local_uploads = LocalArtifactUploads(api.state_dir)

    def _bind_prerequisites(self):
        """Только доверенный bootstrap; один раз сравнить полные signed bytes."""
        identity = getattr(self.api, "prerequisites_identity", None)
        if identity is None:
            return None
        try:
            binding = bounded_json(identity())
        except InstallerError as error:
            if (error.code == "CAPABILITY_UNSUPPORTED"
                    and error.context.get("details", {}).get("reason") == "prerequisites_not_configured"):
                return None
            raise
        from .trust import SignedPayloadVerifier
        if not isinstance(self.verifier, SignedPayloadVerifier):
            raise InstallerError("INTEGRITY_FAILED", details={"reason": "prerequisite_session_verifier_required"})
        if type(binding) is not dict or set(binding) != {
                "product_id", "owner_id", "target", "catalog_digest", "manifest_digest", "payload_binding"}:
            raise InstallerError("INTEGRITY_FAILED", details={"reason": "prerequisite_session_identity"})
        mode = binding.get("target", {}).get("mode") if type(binding.get("target")) is dict else None
        options = self.options_for(mode)
        target = self.target(mode)
        files = self.verifier(self.manifest, target, options)
        expected = {"manifest_digest": digest(self.manifest), "target": target,
                    "release": options.get("release"), "runtime_executable": options.get("runtime_executable"),
                    "files_digest": digest(dict(files))}
        if (binding["product_id"] != self.manifest["product_id"]
                or binding["manifest_digest"] != expected["manifest_digest"]
                or binding["target"] != target or binding["payload_binding"] != expected
                or type(binding["owner_id"]) is not str or not binding["owner_id"]
                or type(binding["catalog_digest"]) is not str):
            raise InstallerError("INTEGRITY_FAILED", details={"reason": "prerequisite_session_payload_mismatch"})
        return deepcopy(binding)

    def _prerequisites_bound(self, mode=None):
        binding = self._prerequisite_binding
        if binding is None:
            raise InstallerError("CAPABILITY_UNSUPPORTED", details={"reason": "prerequisites_not_configured"})
        # Identity is a pinned read-only snapshot. Do not rehash source bytes or
        # probe the host on status/cancel: source loss must not prevent cancel.
        if self.api.prerequisites_identity() != binding:
            raise InstallerError("PLAN_STALE", details={"reason": "prerequisite_session_binding_changed"})
        if mode is not None and mode != binding["target"]["mode"]:
            raise InstallerError("CAPABILITY_UNSUPPORTED", details={"reason": "prerequisite_session_target"})
        return binding

    @staticmethod
    def _fields(value, allowed, required=()):
        if type(value) is not dict or set(value) - set(allowed) or set(required) - set(value):
            raise InstallerError("INVALID_REQUEST")

    @staticmethod
    def _query(query, allowed):
        WizardSession._fields(query, allowed)
        if any(type(values) is not list or len(values) != 1 or type(values[0]) is not str
               for values in query.values()):
            raise InstallerError("INVALID_REQUEST")
        return {key: values[0] for key, values in query.items()}

    def _invalidate_review(self):
        # Caller holds the short session lock. SDK product plans remain an audit
        # record, but this browser session no longer authorizes their submission.
        self._review_generation += 1
        self._plan_locales.clear()
        self._plan_generations.clear()

    def _prerequisite_snapshot(self, value, *, observe=True):
        binding = self._prerequisites_bound()
        if (type(value) is not dict or any(value.get(key) != binding[key]
                for key in ("product_id", "owner_id", "target", "catalog_digest"))):
            raise InstallerError("OPERATION_NOT_FOUND")
        checked_id(value.get("operation_id"))
        if observe:
            marker = (value.get("revision"), value.get("status"), value.get("ready"))
            with self._lock:
                old = self._prerequisite_observations.get(value["operation_id"])
                if old is not None and type(marker[0]) is int and type(old[0]) is int and marker[0] < old[0]:
                    return value  # Concurrent old GET cannot rewind observation.
                if old != marker:
                    self._invalidate_review()
                self._prerequisite_observations[value["operation_id"]] = marker
                self._prerequisite_observations.move_to_end(value["operation_id"])
                while len(self._prerequisite_observations) > MAX_PREREQUISITE_OBSERVATIONS:
                    self._prerequisite_observations.popitem(last=False)
        return value

    def _prerequisite_status(self, operation_id, *, observe=True):
        self._prerequisites_bound()
        return self._prerequisite_snapshot(self.api.prerequisites_status(operation_id), observe=observe)

    def _product_prerequisite_guard(self):
        """Сессионный gate, не второй executor и не глобальный OS-lock."""
        if self._prerequisite_binding is None:
            return
        self._prerequisites_bound()
        if self._prerequisite_inflight:
            raise InstallerError("OPERATION_CONFLICT", details={"reason": "prerequisite_request_in_progress"})
        # A prior catalogue/owner is deliberately absent from filtered lists,
        # but its unresolved claim still forbids a product operation here.
        if self.api.prerequisites_busy() is not False:
            raise InstallerError("OPERATION_CONFLICT", details={"reason": "prerequisites_store_busy"})
        pending = self.api.prerequisites_operations_list(unfinished_only=True, limit=100)
        for value in pending["operations"]:
            self._prerequisite_snapshot(value)
        # Observe completion even if the browser has not polled READY. Otherwise
        # an old review could be submitted in the interval after the worker ends.
        controller_active = False
        for operation_id in tuple(self._prerequisite_observations):
            observed = self._prerequisite_status(operation_id)
            controller_active |= observed.get("execution", {}).get("active") is not False
        if pending["operations"] or pending.get("next_cursor") or controller_active:
            raise InstallerError("OPERATION_CONFLICT", details={"reason": "prerequisites_unfinished"})

    def _reserve_prerequisite_change(self, operation_id):
        with self._lock:
            pending = self.api.operations_list(product_id=self.manifest["product_id"], unfinished_only=True, limit=100)
            if pending["operations"] or pending.get("has_more"):
                raise InstallerError("OPERATION_CONFLICT", details={"reason": "product_operation_unfinished"})
            for operation in tuple(self._operations):
                if self.api.status(operation)["status"] in {"RUNNING", "QUEUED", "PENDING", "CANCEL_PENDING"}:
                    raise InstallerError("OPERATION_CONFLICT", details={"reason": "product_operation_active"})
            if self._prerequisite_inflight:
                raise InstallerError("OPERATION_CONFLICT", details={"reason": "prerequisite_request_in_progress"})
            self._prerequisite_inflight.add(operation_id)
            self._invalidate_review()

    def _prerequisite_review(self, body):
        self._fields(body, {"plan_id", "plan_digest", "consent_refs", "accepted_plan", "accepted_terms", "terms_digest"},
                     {"plan_id", "plan_digest", "consent_refs", "accepted_plan", "accepted_terms", "terms_digest"})
        checked_id(body["plan_id"], field="plan_id")
        with self._lock:
            stored = deepcopy(self._prerequisite_plans.get(body["plan_id"]))
        if stored is None:
            raise InstallerError("PLAN_NOT_FOUND")
        if body["plan_digest"] != stored["plan"]["plan_digest"]:
            raise InstallerError("PLAN_STALE")
        if (body["accepted_plan"] is not True or body["accepted_terms"] is not True
                or body["terms_digest"] != stored["terms_digest"]):
            raise InstallerError("CONSENT_REQUIRED")
        refs = body["consent_refs"]
        if type(refs) is not list or len(refs) > 1024 or any(type(ref) is not str for ref in refs):
            raise InstallerError("INVALID_REQUEST")
        if not set(stored["plan"]["required_consents"]) <= set(refs):
            raise InstallerError("CONSENT_REQUIRED")
        self._prerequisites_bound(stored["plan"]["target"]["mode"])
        return stored, refs

    def _route_prerequisites(self, method, path, query, body):
        """HTTP only transports explicit SDK calls; no installer/worker here."""
        api = self.api
        if method == "GET" and path == "/prerequisites/capabilities":
            values = self._query(query, {"mode"})
            self._fields(body, set())
            mode = values.get("mode", "native")
            if mode not in {"native", "docker"}:
                raise InstallerError("INVALID_REQUEST")
            if self._prerequisite_binding is None or mode != self._prerequisite_binding["target"]["mode"]:
                return {"configured": False, "target": self.target(mode), "automatic_component_ids": [],
                        "http_transport": True, "gui_auto_flow": False, "production_ready": False}
            binding = self._prerequisites_bound(mode)
            return {**api.prerequisites_capabilities(), "binding": binding, "http_transport": True,
                    "gui_auto_flow": True, "durable_byte_progress": True,
                    "local_artifact_upload": True, "local_upload_max_bytes": MAX_UPLOAD_BYTES,
                    "production_ready": False}
        self._prerequisites_bound()
        if method == "POST" and path == "/prerequisites/uploads":
            self._query(query, set())
            self._fields(body, {"plan_id", "plan_digest", "component_id", "artifact_id"},
                         {"plan_id", "plan_digest", "component_id", "artifact_id"})
            checked_id(body["plan_id"], field="plan_id")
            if any(type(body[key]) is not str for key in ("component_id", "artifact_id", "plan_digest")):
                raise InstallerError("INVALID_REQUEST")
            with self._lock:
                stored = deepcopy(self._prerequisite_plans.get(body["plan_id"]))
            if stored is None:
                raise InstallerError("PLAN_NOT_FOUND")
            if (stored["plan"]["plan_digest"] != body["plan_digest"]
                    or stored["generation"] != self._review_generation):
                raise InstallerError("PLAN_STALE")
            for component in stored["plan"]["components"]:
                if component["component_id"] != body["component_id"] or component["choice"] != "auto":
                    continue
                for artifact in component["artifacts"]:
                    if artifact["artifact_id"] == body["artifact_id"]:
                        return self._local_uploads.create(component["component_id"], artifact,
                                                          binding=deepcopy(self._prerequisite_binding))
            raise InstallerError("INVALID_REQUEST")
        if path.startswith("/prerequisites/uploads/"):
            self._query(query, set())
            self._fields(body, set())
            parts = path.strip("/").split("/")
            if method == "GET" and len(parts) == 3:
                return self._local_uploads.status(parts[2])
            if method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                return self._local_uploads.cancel(parts[2])
            raise InstallerError("INVALID_REQUEST")
        if method == "GET" and path.startswith("/prerequisites/licenses/"):
            self._query(query, set())
            component = path.removeprefix("/prerequisites/licenses/")
            if "/" in component:
                raise InstallerError("INVALID_REQUEST")
            return api.prerequisites_license(component)
        if method == "POST" and path == "/prerequisites/plans":
            self._query(query, set())
            self._fields(body, {"mode", "locale", "choices", "offline", "local_uploads"})
            mode = body.get("mode", "native")
            if type(mode) is not str or mode not in {"native", "docker"} or body.get("locale", self.locale) not in LOCALES:
                raise InstallerError("INVALID_REQUEST")
            self._prerequisites_bound(mode)
            locale = normalize_locale(body.get("locale", self.locale))
            with self._lock:
                generation = self._review_generation
            # Full signature reads/discovery run outside the session mutex.
            terms = self.terms(locale, mode)
            uploads = body.get("local_uploads", {})
            with self._local_uploads.selection(uploads, binding=self._prerequisite_binding) as local:
                plan = api.prerequisites_plan(body.get("choices"), locale=locale, offline=body.get("offline", False),
                                             local_artifacts=local)
                for key in ("product_id", "owner_id", "target", "catalog_digest"):
                    if plan.get(key) != self._prerequisite_binding[key]:
                        raise InstallerError("PLAN_STALE")
                plan_id, operation_id = "prereqplan_" + uuid.uuid4().hex, "prep_" + uuid.uuid4().hex
                stored = {"plan_id": plan_id, "operation_id": operation_id, "plan": deepcopy(plan),
                          "terms_digest": terms["digest"], "generation": generation}
                with self._lock:
                    if generation != self._review_generation:
                        raise InstallerError("PLAN_STALE")
                    self._prerequisite_plans[plan_id] = stored
                    self._local_uploads.retain(uploads)
                    while len(self._prerequisite_plans) > MAX_PREREQUISITE_PLANS:
                        self._prerequisite_plans.popitem(last=False)
                public_plan = {**plan, "local_artifacts": dict(uploads)}
                return {"plan_id": plan_id, "operation_id": operation_id, "plan_digest": plan["plan_digest"],
                        "plan": public_plan, "terms_digest": terms["digest"], "locale": locale,
                        "production_ready": False}
        if method == "POST" and path == "/prerequisites/operations":
            self._query(query, set())
            stored, refs = self._prerequisite_review(body)
            operation_id = stored["operation_id"]
            try:
                previous = self._prerequisite_status(operation_id, observe=False)
            except InstallerError as error:
                if error.code != "OPERATION_NOT_FOUND":
                    raise
                previous = None
            if previous is not None:
                # No implicit resume, including a lost ACK or a failed worker.
                if previous["plan_digest"] != stored["plan"]["plan_digest"]:
                    raise InstallerError("PLAN_STALE")
                return self._prerequisite_snapshot(previous)
            with self._lock:
                if stored["generation"] != self._review_generation:
                    raise InstallerError("PLAN_STALE")
                self._reserve_prerequisite_change(operation_id)
            try:
                result = api.prerequisites_start(operation_id, stored["plan"], refs, background=True)
                return self._prerequisite_snapshot(result)
            finally:
                with self._lock:
                    self._prerequisite_inflight.discard(operation_id)
        if method == "GET" and path == "/prerequisites/operations":
            values = self._query(query, {"unfinished_only", "limit", "after_id"})
            unfinished = values.get("unfinished_only", "true")
            if unfinished not in {"true", "false"}:
                raise InstallerError("INVALID_REQUEST")
            result = api.prerequisites_operations_list(unfinished_only=unfinished == "true",
                limit=int(values.get("limit", "100")), after_id=values.get("after_id"))
            for operation in result["operations"]:
                self._prerequisite_snapshot(operation)
            return result
        if path.startswith("/prerequisites/operations/"):
            parts = path.strip("/").split("/")
            if len(parts) not in {3, 4, 5}:
                raise InstallerError("INVALID_REQUEST")
            operation_id, tail = parts[2], parts[3] if len(parts) >= 4 else ""
            # Cancel deliberately avoids the UI mutex, including observation
            # bookkeeping, while another request is doing a long probe.
            status = self._prerequisite_status(operation_id, observe=tail != "cancel")
            if method == "GET" and not tail:
                self._query(query, set())
                return status
            if method == "GET" and tail == "events" and len(parts) == 4:
                values = self._query(query, {"after_sequence", "limit"})
                result = api.prerequisites_events(operation_id, after_sequence=int(values.get("after_sequence", "0")),
                                                 limit=int(values.get("limit", "100")))
                self._prerequisite_snapshot(result["snapshot"])
                return result
            if method == "GET" and tail == "attempts" and len(parts) == 5:
                values = self._query(query, {"after", "limit"})
                return api.prerequisites_attempts(operation_id, parts[4], after=int(values.get("after", "0")),
                                                  limit=int(values.get("limit", "100")))
            if method == "POST" and len(parts) == 4:
                self._query(query, set())
                if tail == "cancel":
                    self._fields(body, set())
                    return self._prerequisite_snapshot(api.prerequisites_cancel(operation_id), observe=False)
                if tail == "boundary":
                    self._fields(body, {"when"}, {"when"})
                    return self._prerequisite_snapshot(api.prerequisites_choose_boundary(operation_id, body["when"]))
                if tail in {"resume", "recheck", "replace-plan"}:
                    if tail == "replace-plan":
                        stored, refs = self._prerequisite_review(body)
                    else:
                        self._fields(body, {"consent_refs"} if tail == "resume" else set())
                        if "consent_refs" in body and (type(body["consent_refs"]) is not list
                                or any(type(ref) is not str for ref in body["consent_refs"])):
                            raise InstallerError("INVALID_REQUEST")
                    self._reserve_prerequisite_change(operation_id)
                    try:
                        if tail == "resume":
                            result = api.prerequisites_resume(operation_id, body.get("consent_refs"), background=True)
                        elif tail == "recheck":
                            result = api.prerequisites_recheck(operation_id)
                        else:
                            result = api.prerequisites_replace_plan(operation_id, stored["plan"], refs)
                        return self._prerequisite_snapshot(result)
                    finally:
                        with self._lock:
                            self._prerequisite_inflight.discard(operation_id)
        raise InstallerError("CAPABILITY_UNSUPPORTED")

    def receive_local_artifact(self, path, query, stream, length):
        """Authenticated HTTP handler only; never receives a browser file path."""
        self._prerequisites_bound()
        self._query(query, set())
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["prerequisites", "uploads"] or parts[3] != "content":
            raise InstallerError("INVALID_REQUEST")
        return self._local_uploads.receive(parts[2], stream, length, binding=self._prerequisite_binding)

    def secret(self, reference):
        with self._lock:
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

    def options_for(self, mode):
        if type(mode) is not str or mode not in {"native", "docker"}:
            raise InstallerError("INVALID_REQUEST")
        if mode == "docker" and mode not in self.mode_options:
            raise InstallerError("CAPABILITY_UNSUPPORTED")
        return {**self.options, **self.mode_options.get(mode, {})}

    def terms(self, locale, mode="native"):
        locale = normalize_locale(locale)
        if self.verifier is None:
            raise InstallerError("INTEGRITY_FAILED")
        options = self.options_for(mode)
        verified = self.verifier(self.manifest, self.target(mode), options)
        name = "legal/" + locale + ".txt"
        if name not in verified:
            raise InstallerError("CONSENT_REQUIRED")
        path = Path(options["payload_dir"]) / name
        data = path.read_bytes()
        if len(data) > 512000 or hashlib.sha256(data).hexdigest() != verified[name]:
            raise InstallerError("INTEGRITY_FAILED")
        return {"locale": locale, "digest": verified[name],
                "text": data.decode("utf-8"), "development": self.development}

    def route(self, method, path, query, body):
        if path == "/prerequisites" or path.startswith("/prerequisites/"):
            # SDK background dispatch owns the controller. Slow read-only
            # planning/recheck must not serialize HTTP status or cancel.
            return self._route_prerequisites(method, path, query, body)
        if method == "POST" and path in {"/plans", "/discover"}:
            return self._route(method, path, query, body)
        # Secret updates cannot slip between submit/resume and registration of
        # the active operation. A worker reads values after this lock is released.
        if method == "POST":
            with self._lock:
                return self._route(method, path, query, body)
        return self._route(method, path, query, body)

    def _route(self, method, path, query, body):
        api = self.api
        locale = normalize_locale(body.get("locale", query.get("locale", [self.locale])[0]))
        if method == "GET" and path == "/session":
            return {"product_id": self.manifest["product_id"], "locale": self.preferred_locale,
                    "locales": list(LOCALES), "development": self.development,
                    "options": {k: self.options.get(k) for k in ("install_root", "release")},
                    "secrets": [{"name": item["name"], "required": item.get("required", False)}
                                for item in self.manifest.get("secrets", [])],
                    "product_modes": self.manifest.get("modes", []),
                    "selected_modes": self.options.get("modes", []),
                    "target": self.target()}
        if method == "GET" and path.startswith("/locales/"):
            name = path.removeprefix("/locales/")
            if name not in LOCALES:
                raise InstallerError("INVALID_REQUEST")
            return json.loads((ASSETS / "locales.json").read_text(encoding="utf-8"))[name]
        if method == "GET" and path.startswith("/terms/"):
            return self.terms(path.removeprefix("/terms/"), query.get("mode", ["native"])[0])
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
            mode = query.get("mode", ["native"])[0]
            if type(mode) is not str or mode not in {"native", "docker"}:
                raise InstallerError("INVALID_REQUEST")
            if mode == "docker" and mode not in self.mode_options:
                return {"target": self.target(mode), "actions": {},
                        "payload_configured": False, "production_ready": False}
            return {**api.capabilities(self.target(mode)), "payload_configured": True}
        if method == "POST" and path == "/validate":
            return api.validate(self.manifest)
        if method == "POST" and path in ("/discover", "/plans"):
            if set(body) - {"install_root", "mode", "action", "locale", "archive_path", "acknowledge_data_loss", "product_modes"}:
                raise InstallerError("INVALID_REQUEST")
            mode = body.get("mode", "native")
            with self._lock:
                if path == "/plans":
                    self._product_prerequisite_guard()
                generation = self._review_generation
                options = self.options_for(mode)
            if "install_root" in body:
                if type(body["install_root"]) is not str:
                    raise InstallerError("INVALID_REQUEST")
                options["install_root"] = body["install_root"]
            if mode not in ("native", "docker"):
                raise InstallerError("INVALID_REQUEST")
            target = self.target(mode)
            if "product_modes" in body:
                modes = body["product_modes"]
                allowed = {item["name"] for item in self.manifest.get("modes", [])}
                if type(modes) is not list or not modes or any(type(item) is not str for item in modes) or set(modes) - allowed or len(modes) != len(set(modes)):
                    raise InstallerError("INVALID_REQUEST")
                options["modes"] = modes
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
            terms = self.terms(locale, mode)
            options["eula_locale"], options["eula_digest"] = locale, terms["digest"]
            # Recovery secrets и произвольные argv не принимаются из web request.
            result = api.plan(action, self.manifest, target=target, options=options)
            with self._lock:
                if generation != self._review_generation:
                    raise InstallerError("PLAN_STALE")
                self._plan_locales[result["plan_id"]] = (locale, terms["digest"])
                self._plan_generations[result["plan_id"]] = generation
            return result
        if method == "POST" and path == "/operations":
            self._product_prerequisite_guard()
            if body.get("accepted_terms") is not True or body.get("accepted_plan") is not True:
                raise InstallerError("CONSENT_REQUIRED")
            with self._lock:
                accepted = self._plan_locales.get(body.get("plan_id"))
                if (self._prerequisite_binding is not None
                        and self._plan_generations.get(body.get("plan_id")) != self._review_generation):
                    raise InstallerError("PLAN_STALE")
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
                self._product_prerequisite_guard()
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
    class LimitedServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, *args, **kwargs):
            self._slots = threading.BoundedSemaphore(32)
            super().__init__(*args, **kwargs)

        def process_request(self, request, address):
            if not self._slots.acquire(blocking=False):
                self.close_request(request)
                return
            try:
                super().process_request(request, address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self._slots.release()

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
            if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != expected:
                return self.send(403, {"error": {"code": "INVALID_REQUEST"}})
            origin = self.headers.get("Origin")
            if len(self.headers.get_all("Origin", [])) > 1 or (origin and origin != "http://" + expected):
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
            if len(self.headers.get_all("X-HInstall-Token", [])) != 1 or not secrets.compare_digest(token, session.token):
                return self.send(403, {"error": {"code": "PERMISSION_REQUIRED"}})
            if not parsed.path.startswith("/api/v1/"):
                return self.send(404, {"error": {"code": "INVALID_REQUEST"}})
            try:
                body = {}
                if self.command == "POST":
                    if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                        raise InstallerError("INVALID_REQUEST")
                    length_text = self.headers.get("Content-Length", "")
                    if not length_text.isascii() or not length_text.isdecimal() or len(length_text) > 20:
                        raise InstallerError("INVALID_REQUEST")
                    length = int(length_text)
                    relative_path = parsed.path[len("/api/v1"):]
                    if relative_path.startswith("/prerequisites/uploads/") and relative_path.endswith("/content"):
                        if (len(self.headers.get_all("Content-Type", [])) != 1
                                or self.headers.get("Content-Type") != "application/octet-stream"):
                            raise InstallerError("INVALID_REQUEST")
                        result = session.receive_local_artifact(relative_path,
                            parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20), self.rfile, length)
                        return self.send(200, {"api_version": "1", "result": result})
                    if self.headers.get_content_type() != "application/json":
                        raise InstallerError("INVALID_REQUEST")
                    if not 0 < length <= MAX_BODY:
                        raise InstallerError("INVALID_REQUEST")
                    body = _json(self.rfile.read(length))
                result = session.route(self.command, parsed.path[len("/api/v1"):],
                                       parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20), body)
                self.send(200, {"api_version": "1", "result": result})
            except InstallerError as exc:
                self.send(400, {"api_version": "1", "error": exc.to_dict(locale=session.locale)})
            except (ValueError, TypeError, KeyError, UnicodeError):
                self.send(400, {"error": {"code": "INVALID_REQUEST"}})
            except Exception:
                self.send(500, {"error": {"code": "RECOVERY_REQUIRED"}})

        do_GET = handle_request
        do_POST = handle_request

    server = LimitedServer(("127.0.0.1", port), Handler)
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
    parser.add_argument("--docker-cli-config")
    parser.add_argument("--docker-payload-options",
                        help="Доверенный JSON с отдельными подписанными Docker payload bindings")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--url-file")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if args.development_runtime and not args.allow_development:
        parser.error("--development-runtime requires --allow-development")
    from shared.manifest import load_manifest
    manifest = load_manifest(args.manifest)
    verifier = load_trust_config(args.trust_config, allow_development=args.allow_development)
    session_ref = {}
    def secret_provider(reference):
        return session_ref["session"].secret(reference)
    engine = configured_backends(verifier=verifier, secret_provider=secret_provider,
                                 development_runtime=args.development_runtime,
                                 docker_cli_config=args.docker_cli_config)
    api = InstallerAPI(args.state_dir, engine=engine, locale=normalize_locale(args.locale))
    options = {key: getattr(args, key) for key in ("payload_dir", "inventory_file", "signature_file",
                                                  "release", "runtime_executable", "install_root")}
    if args.mode:
        options["modes"] = args.mode
    options["backup_password_ref"] = "wizard_archive"
    mode_options = None
    if args.docker_payload_options:
        from shared.datahome import checked_path
        file = checked_path(Path(args.docker_payload_options).absolute())
        if not file.is_file() or file.stat().st_size > MAX_BODY:
            raise InstallerError("INVALID_REQUEST")
        with file.open("rb") as stream:
            raw = stream.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise InstallerError("INVALID_REQUEST")
        mode_options = {"docker": _json(raw)}
    session = WizardSession(api, manifest, options, locale=args.locale, verifier=verifier,
                            development=args.allow_development, mode_options=mode_options)
    session_ref["session"] = session
    server = create_server(session, port=args.port)
    url = f"http://127.0.0.1:{server.server_port}/#token={session.token}"
    from shared.datahome import checked_path
    if args.url_file:
        url_path = checked_path(Path(args.url_file).absolute())
    else:
        directory = checked_path(Path(args.state_dir).absolute())
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        url_path = directory / ("wizard-session-" + uuid.uuid4().hex + ".url")
    fd = os.open(url_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(url + "\n")
    if not args.no_browser:
        webbrowser.open(url)
    print(f"HomenSAI installer: http://127.0.0.1:{server.server_port}/")
    print(f"Private browser launch link: {url_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
