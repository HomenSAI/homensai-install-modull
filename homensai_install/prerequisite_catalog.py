"""Подписанный граф prerequisites; данные каталога не задают shell/argv.

Корень доверия и каталог поступают от bootstrap, а не из запроса GUI.
Подпись проверяет существующий SignedPayloadVerifier, включая весь payload.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
from pathlib import Path
import re

from .api.model import bounded_json, digest, parse_json, read_manifest, target_model
from .downloads import ArtifactSpec
from .errors import InstallerError
from .prerequisites import DependencyRequirement
from .trust import SignedPayloadVerifier
from shared.datahome import checked_path

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
MAX_CATALOG_BYTES = 512 * 1024


def fail(code, reason, component_id=None):
    raise InstallerError(code, phase="prerequisites", component_id=component_id,
                         details={"reason": reason})


def identifier(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def exact(value, required, optional=()):
    if type(value) is not dict or set(value) - set(required) - set(optional) or set(required) - set(value):
        fail("INVALID_REQUEST", "prerequisite_fields")


def relative(value):
    if (type(value) is not str or not value or len(value) > 1024 or "\\" in value
            or ":" in value or any(ord(c) < 32 for c in value)
            or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in value.split("/"))):
        fail("INVALID_REQUEST", "prerequisite_relative_path")
    return value


class SignedPrerequisiteCatalog:
    """Неизменяемый catalogue для одного product/release/target.

    Entries содержат requirements/зависимости, exact artifact pins и licence
    records. adapter_id выбирает независимо зарегистрированный Python adapter.
    Ни команд, ни elevated capabilities в JSON нет.
    """
    def __init__(self, manifest, target, options, *, verifier, descriptor="installer/prerequisites.json"):
        if not isinstance(verifier, SignedPayloadVerifier):
            fail("INTEGRITY_FAILED", "signed_prerequisite_verifier_required")
        self.manifest = read_manifest(manifest)
        self.target = target_model(target)
        self.options = bounded_json(options)
        self.verifier = verifier
        self.descriptor = relative(descriptor)
        data = self._load()
        self._digest = self._loaded_binding
        self._data = data

    @property
    def catalog_digest(self):
        return self._digest

    def _file(self, name, files, limit):
        relative(name)
        if name not in files:
            fail("INTEGRITY_FAILED", "prerequisite_metadata_not_signed")
        path = checked_path(checked_path(Path(self.options["payload_dir"]).absolute()) / name)
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit or hashlib.sha256(raw).hexdigest() != files[name]:
            fail("INTEGRITY_FAILED", "prerequisite_metadata_hash")
        return raw

    def _load(self):
        try:
            files = self.verifier(self.manifest, self.target, self.options)
            data = parse_json(self._file(self.descriptor, files, MAX_CATALOG_BYTES))
            exact(data, {"schema_version", "catalog_id", "catalog_version", "product_id", "target", "entries"})
            if type(data["schema_version"]) is not int or data["schema_version"] != 1:
                fail("API_UNSUPPORTED", "prerequisite_catalog_schema")
            if (not identifier(data["catalog_id"]) or not identifier(data["catalog_version"])
                    or data["catalog_version"].lower() in {"latest", "main", "master", "nightly"}
                    or data["product_id"] != self.manifest["product_id"]
                    or data["target"] != self.target):
                fail("INTEGRITY_FAILED", "prerequisite_catalog_binding")
            entries = data["entries"]
            if type(entries) is not list or not 1 <= len(entries) <= 128:
                fail("INVALID_REQUEST", "prerequisite_catalog_size")
            names, artifact_ids = set(), set()
            for entry in entries:
                exact(entry, {"requirement", "depends_on", "ownership", "adapter_id", "artifacts", "license"})
                requirement = DependencyRequirement.from_dict(entry["requirement"])
                name = requirement.component_id
                if name in names:
                    fail("INVALID_REQUEST", "duplicate_prerequisite", name)
                names.add(name)
                # Canonical defaults remove equivalent-but-differently-shaped requests.
                entry["requirement"] = asdict(requirement)
                if entry["ownership"] not in {"shared", "external", "managed_private"}:
                    fail("INVALID_REQUEST", "prerequisite_ownership", name)
                depends = entry["depends_on"]
                if (type(depends) is not list or len(depends) > 128
                        or any(not identifier(item) for item in depends) or len(set(depends)) != len(depends)):
                    fail("INVALID_REQUEST", "prerequisite_dependencies", name)
                if entry["adapter_id"] is not None and not identifier(entry["adapter_id"]):
                    fail("INVALID_REQUEST", "prerequisite_adapter_id", name)
                license = entry["license"]
                exact(license, {"license_id", "version", "text_path", "text_sha256", "locale", "decision",
                                "acceptance_required", "conditions"})
                if (not identifier(license["license_id"]) or not identifier(license["version"])
                        or license["version"].lower() in {"latest", "main", "master", "nightly"}
                        or license["locale"] not in {"ru", "en", "de"}
                        or license["decision"] not in {"allowed", "conditional", "blocked", "unknown"}
                        or type(license["acceptance_required"]) is not bool
                        or type(license["text_sha256"]) is not str or not _HASH.fullmatch(license["text_sha256"])):
                    fail("INVALID_REQUEST", "prerequisite_license", name)
                conditions = license["conditions"]
                if (type(conditions) is not list or len(conditions) > 32
                        or any(not identifier(item) for item in conditions) or len(set(conditions)) != len(conditions)
                        or (license["decision"] == "conditional" and not conditions)
                        or (license["decision"] == "allowed" and conditions)):
                    fail("INVALID_REQUEST", "prerequisite_license_conditions", name)
                text = self._file(license["text_path"], files, MAX_CATALOG_BYTES)
                if hashlib.sha256(text).hexdigest() != license["text_sha256"] or not text.decode("utf-8").strip():
                    fail("INTEGRITY_FAILED", "prerequisite_license_text", name)
                if type(entry["artifacts"]) is not list or len(entry["artifacts"]) > 16:
                    fail("INVALID_REQUEST", "prerequisite_artifact_count", name)
                for raw in entry["artifacts"]:
                    spec = ArtifactSpec.from_dict(raw)
                    if spec.artifact_id in artifact_ids or spec.license_ref != license["license_id"]:
                        fail("INTEGRITY_FAILED", "prerequisite_artifact_license_or_identity", name)
                    artifact_ids.add(spec.artifact_id)
                if entry["adapter_id"] is not None and not entry["artifacts"]:
                    # System feature commands need a separately pinned helper artifact.
                    fail("INVALID_REQUEST", "prerequisite_auto_requires_pinned_artifact", name)
            self._order(entries)
            self._loaded_binding = digest({"catalog": data, "files": files, "manifest": self.manifest,
                "target": self.target, "release": self.options.get("release"),
                "runtime_executable": self.options.get("runtime_executable")})
            return data
        except InstallerError:
            raise
        except (ValueError, TypeError, KeyError, OSError, UnicodeError):
            fail("INTEGRITY_FAILED", "prerequisite_catalog_unavailable")

    @staticmethod
    def _order(entries):
        by_id = {entry["requirement"]["component_id"]: entry for entry in entries}
        pending, result = set(by_id), []
        if any(set(entry["depends_on"]) - set(by_id) for entry in entries):
            fail("INVALID_REQUEST", "unknown_prerequisite_dependency")
        while pending:
            ready = sorted(name for name in pending if not set(by_id[name]["depends_on"]) & pending)
            if not ready:
                fail("INVALID_REQUEST", "cyclic_prerequisites")
            result.extend(ready)
            pending.difference_update(ready)
        return result

    def verified(self):
        data = self._load()
        if self._loaded_binding != self._digest:
            fail("PLAN_STALE", "prerequisite_catalog_changed")
        return deepcopy(data)

    def entries(self):
        data = self.verified()
        by_id = {entry["requirement"]["component_id"]: entry for entry in data["entries"]}
        return [by_id[name] for name in self._order(data["entries"])]

    def license_text(self, component_id):
        entries = self.entries()
        entry = next((entry for entry in entries if entry["requirement"]["component_id"] == component_id), None)
        if entry is None:
            fail("INVALID_REQUEST", "unknown_prerequisite")
        files = self.verifier(self.manifest, self.target, self.options)
        license = entry["license"]
        text = self._file(license["text_path"], files, MAX_CATALOG_BYTES)
        if hashlib.sha256(text).hexdigest() != license["text_sha256"]:
            fail("INTEGRITY_FAILED", "prerequisite_license_text", component_id)
        return {**deepcopy(license), "text": text.decode("utf-8")}
