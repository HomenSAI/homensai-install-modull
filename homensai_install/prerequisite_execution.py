"""Signed prerequisites execution and durable manual/reboot/relogin recovery.

There is no implicit production catalogue. A trusted bootstrap supplies signed
metadata, fixed platform adapters and read-only discovery. GUI/API requests can
select only catalogue component IDs and auto/manual/use/skip strategies.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path

from .api.journal import owned_directory
from .api.model import bounded_json, canonical, digest
from .downloads import ArtifactSpec, DownloadManager, _open
from .errors import InstallerError, utcnow
from .i18n import normalize_locale
from .prerequisite_catalog import SignedPrerequisiteCatalog, fail, identifier
from .prerequisite_runner import FixedCommandAdapter, system_markers
from .prerequisite_state import PrerequisiteStore
from .prerequisites import check_requirements
from shared.datahome import checked_path

_TERMINAL = {"READY", "CANCELED"}
_UNCERTAIN = {"INSTALLING", "UNKNOWN"}
_CHOICES = {"auto", "manual", "use", "skip"}
_PLAN_KEYS = {"schema_version", "catalog_digest", "product_id", "target", "owner_id", "locale", "context_digest",
              "choices", "components", "offline", "local_artifacts", "product_consent_refs",
              "plan_digest", "required_consents"}


def _references(values, *, allow_empty=False):
    if (not isinstance(values, (list, tuple, set)) or not (0 if allow_empty else 1) <= len(values) <= 8192
            or any(type(value) is not str or not value or len(value) > 512
                   or any(ord(char) < 33 for char in value) for value in values)):
        fail("INVALID_REQUEST", "prerequisite_consent_references")
    return sorted(set(values))


class PrerequisiteCoordinator:
    """One original-owner operation, with explicit start/resume side effects.

    discover() and markers() are trusted read-only ports. reconcile(entry,
    receipt, inventory), if supplied, is also read-only and returns exactly
    applied/not_applied/running/unknown. It must prove a result, not infer it
    from a PID or the absence of an installer window.

    state_dir is shared by all prerequisite operations for this controller's
    original owner. Elevated installation belongs to a constrained helper;
    owner_id and the authoritative user journal do not change to the helper UID.
    """
    def __init__(self, catalog, *, state_dir, owner_id, downloads, adapters, discover,
                 product_consent_refs, markers=system_markers, event_callback=None, reconcile=None):
        if not isinstance(catalog, SignedPrerequisiteCatalog) or not isinstance(downloads, DownloadManager):
            fail("INTEGRITY_FAILED", "prerequisite_trust_ports_required")
        if not identifier(owner_id) or not callable(discover) or not callable(markers):
            fail("INVALID_REQUEST", "prerequisite_owner_or_discovery")
        if (type(adapters) is not dict or len(adapters) > 128 or any(not identifier(key)
                or not isinstance(value, FixedCommandAdapter) or value.adapter_id != key for key, value in adapters.items())):
            fail("INVALID_REQUEST", "prerequisite_trusted_adapter_registry")
        if any(value is not None and not callable(value) for value in (event_callback, reconcile)):
            fail("INVALID_REQUEST", "prerequisite_callback")
        self.catalog, self.owner_id, self.downloads = catalog, owner_id, downloads
        self.adapters, self.discover, self.markers = dict(adapters), discover, markers
        self.product_consent_refs = _references(product_consent_refs)
        if len(self.product_consent_refs) > 64:
            fail("INVALID_REQUEST", "prerequisite_product_consent_limit")
        self.event_callback, self.reconcile = event_callback, reconcile
        self.store = PrerequisiteStore(state_dir)

    def identity(self):
        """Stable registration identity, without a system probe or execution."""
        return deepcopy({"product_id": self.catalog.manifest["product_id"], "target": self.catalog.target,
                         "catalog_digest": self.catalog.catalog_digest, "owner_id": self.owner_id})

    def capabilities(self):
        """Verify metadata before advertising only the configured source policies."""
        entries = self.catalog.entries()
        automatic = []
        for entry in entries:
            adapter = self.adapters.get(entry["adapter_id"])
            if (adapter is not None and adapter.os_name == self.catalog.target["os"]
                    and len(adapter.artifact_suffixes) == len(entry["artifacts"])
                    and (adapter.os_name != "windows" or adapter.publisher_check is not None)
                    and self._license_allowed(entry)):
                automatic.append(entry["requirement"]["component_id"])
        return {**self.identity(), "signed_catalog": True, "manual_recheck": True, "durable_resume": True,
                "background_execution": True, "durable_operation_listing": True,
                "automatic_component_ids": automatic, "automatic_reboot": False,
                "boundary_choice": True, "platform_acceptance": "not_verified"}

    def _observe(self):
        try:
            inventory = bounded_json(self.discover())
        except InstallerError:
            raise
        except Exception:
            fail("PREREQUISITE_MISSING", "prerequisite_discovery_failed")
        if (type(inventory) is not dict or type(inventory.get("components")) is not dict
                or type(inventory.get("platform")) is not dict
                or inventory["platform"].get("os") != self.catalog.target["os"]
                or inventory["platform"].get("arch") != self.catalog.target["arch"]):
            fail("PLAN_STALE", "prerequisite_discovery_target")
        return inventory

    def _markers(self):
        try:
            values = bounded_json(self.markers())
        except Exception:
            fail("PREREQUISITE_MISSING", "prerequisite_marker_unavailable")
        if (type(values) is not dict or set(values) != {"boot_id", "session_id"}
                or any(value is not None and (type(value) is not str or not value
                       or len(value) > 256 or any(ord(char) < 33 for char in value)) for value in values.values())):
            fail("INVALID_REQUEST", "prerequisite_marker_format")
        # Journal stores opaque hashes, never a raw login identifier.
        return {key: digest(value) if value is not None else None for key, value in values.items()}

    def _context(self, inventory, entries):
        managed = {entry["requirement"]["kind"] for entry in entries}
        identity = {key: value for key, value in inventory.get("identity", {}).items() if key != "elevated"}
        return digest({"owner_id": self.owner_id, "platform": inventory["platform"], "identity": identity,
            "resources": inventory.get("resources", {}), "ports": inventory.get("ports", []),
            "disks": [{key: value for key, value in disk.items() if key != "free_bytes"} for disk in inventory.get("disks", [])],
            "external_components": {key: value for key, value in inventory["components"].items() if key not in managed},
            "material_context": inventory.get("material_context", {})})

    @staticmethod
    def _license_allowed(entry):
        return entry["license"]["decision"] in {"allowed", "conditional"}

    def _consents(self, body):
        refs = set(self.product_consent_refs)
        for item in body["components"]:
            if item["choice"] == "skip":
                continue
            license = item["license"]
            binding = digest(license)
            if license["acceptance_required"]:
                refs.add("prerequisite-license:" + item["component_id"] + ":" + binding)
            for condition in license["conditions"]:
                refs.add("prerequisite-condition:" + condition + ":" + binding)
            if item["choice"] == "auto" and not item["initial_ready"]:
                refs.add("prerequisite-auto:" + item["component_id"] + ":" + body["catalog_digest"])
                if item["elevated"]:
                    refs.add("prerequisite-elevation:" + item["component_id"] + ":" + item["adapter_policy_digest"])
        return sorted(refs | {"prerequisites:review:" + digest(body)})

    def plan(self, choices=None, *, locale="ru", offline=False, local_artifacts=None):
        entries = self.catalog.entries()
        inventory = self._observe()
        if locale is not None and type(locale) is not str:
            fail("INVALID_REQUEST", "prerequisite_locale")
        locale = normalize_locale(locale)
        choices = {} if choices is None else bounded_json(choices)
        if (type(choices) is not dict or type(offline) is not bool
                or any(type(value) is not str or value not in _CHOICES for value in choices.values())):
            fail("INVALID_REQUEST", "prerequisite_strategy")
        names = {entry["requirement"]["component_id"] for entry in entries}
        if set(choices) - names:
            fail("INVALID_REQUEST", "unknown_prerequisite_choice")
        checks = {item["component_id"]: item for item in check_requirements(
            [entry["requirement"] for entry in entries], inventory, locale)}
        selected = {entry["requirement"]["component_id"] for entry in entries
                    if entry["requirement"]["required"] or choices.get(entry["requirement"]["component_id"], "skip") != "skip"}
        for entry in reversed(entries):
            if entry["requirement"]["component_id"] in selected:
                selected.update(entry["depends_on"])
        effective = {}
        components = []
        auto_artifacts = set()
        for entry in entries:
            name = entry["requirement"]["component_id"]
            check = checks[name]
            choice = choices.get(name, "use" if check["ready"] else "manual") if name in selected else "skip"
            if name in selected and choice == "skip":
                fail("INVALID_REQUEST", "required_prerequisite_cannot_be_skipped", name)
            if choice == "use" and not check["ready"]:
                fail("PREREQUISITE_MISSING", "prerequisite_use_requires_present", name)
            adapter = self.adapters.get(entry["adapter_id"])
            if choice == "auto" and not check["ready"]:
                if not self._license_allowed(entry):
                    fail("CAPABILITY_UNSUPPORTED", "prerequisite_license_not_approved", name)
                if adapter is None:
                    fail("CAPABILITY_UNSUPPORTED", "prerequisite_automatic_adapter_missing", name)
                if (adapter.os_name != self.catalog.target["os"] or len(adapter.artifact_suffixes) != len(entry["artifacts"])
                        or (adapter.os_name == "windows" and adapter.publisher_check is None)):
                    fail("CAPABILITY_UNSUPPORTED", "prerequisite_adapter_target", name)
                auto_artifacts.update(artifact["artifact_id"] for artifact in entry["artifacts"])
            effective[name] = choice
            license = {key: value for key, value in entry["license"].items() if key != "text_path"}
            ownership = entry["ownership"]
            if check["ready"] or (ownership == "managed_private" and (choice != "auto" or check["status"] != "missing")):
                ownership = "external"
            components.append({"component_id": name, "kind": entry["requirement"]["kind"],
                "required": entry["requirement"]["required"], "depends_on": entry["depends_on"],
                "ownership": ownership, "choice": choice, "initial_status": check["status"],
                "initial_ready": check["ready"], "detected_version": check["detected_version"],
                "scope": entry["requirement"]["scope"], "adapter_id": entry["adapter_id"],
                "adapter_policy_digest": adapter.policy_digest if adapter and choice == "auto" else None,
                "elevated": adapter.elevated if adapter and choice == "auto" else False,
                "artifacts": [ArtifactSpec.from_dict(raw).public_metadata() for raw in entry["artifacts"]],
                "license": license, "manual": check["manual"]})
        local = {} if local_artifacts is None else bounded_json(local_artifacts)
        if type(local) is not dict or set(local) - auto_artifacts:
            fail("INVALID_REQUEST", "prerequisite_local_artifact_identity")
        for key, value in local.items():
            if type(value) is not str or not Path(value).is_absolute():
                fail("INVALID_REQUEST", "prerequisite_local_artifact_path")
            try:
                local[key] = str(checked_path(Path(value)))
            except (OSError, ValueError):
                fail("INTEGRITY_FAILED", "prerequisite_local_artifact_path")
        body = {"schema_version": 1, "catalog_digest": self.catalog.catalog_digest,
                "product_id": self.catalog.manifest["product_id"], "target": self.catalog.target,
                "owner_id": self.owner_id, "locale": locale, "context_digest": self._context(inventory, entries),
                "choices": effective, "components": components, "offline": offline, "local_artifacts": local,
                "product_consent_refs": self.product_consent_refs}
        result = {**body, "plan_digest": digest(body), "required_consents": self._consents(body)}
        if len(canonical(result).encode("utf-8")) > 512 * 1024:
            fail("INVALID_REQUEST", "prerequisite_plan_size")
        return deepcopy(result)

    def _validate_plan(self, plan, consents):
        plan = bounded_json(plan)
        if type(plan) is not dict or set(plan) != _PLAN_KEYS or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
            fail("INVALID_REQUEST", "prerequisite_plan_fields")
        body = {key: value for key, value in plan.items() if key not in {"plan_digest", "required_consents"}}
        try:
            valid = (digest(body) == plan["plan_digest"] and self._consents(body) == plan["required_consents"]
                     and plan["product_consent_refs"] == self.product_consent_refs and plan["owner_id"] == self.owner_id
                     and plan["product_id"] == self.catalog.manifest["product_id"])
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            fail("INTEGRITY_FAILED", "prerequisite_plan_binding")
        given = _references(consents, allow_empty=True)
        if not set(plan["required_consents"]) <= set(given):
            fail("CONSENT_REQUIRED", "prerequisite_plan_consents")
        # Unrelated supplied acknowledgements are not an audit receipt for this
        # operation and are not copied into the durable journal.
        return plan, list(plan["required_consents"])

    def _emit(self, event):
        if self.event_callback is not None:
            try:
                self.event_callback(deepcopy(event))
            except Exception:
                # Diagnostics must never turn an installed prerequisite into a retry.
                pass

    def _save(self, operation, message, *, create=False):
        operation, event = self.store.save(operation, message, create=create)
        self._emit(event)
        return operation

    def _get(self, operation_id):
        value = self.store.get(operation_id)
        if value is None:
            fail("OPERATION_NOT_FOUND", "prerequisite_operation_missing")
        if value.get("owner_id") != self.owner_id:
            fail("PERMISSION_REQUIRED", "prerequisite_original_owner_mismatch")
        if value.get("product_id") != self.catalog.manifest["product_id"] or value.get("target") != self.catalog.target:
            fail("PERMISSION_REQUIRED", "prerequisite_product_or_target_mismatch")
        return value

    def _public(self, operation, *, summary=False):
        from .prerequisite_async import execution_status
        hidden = {"plan", "consents", "_dispatch", "_initial_validation_pending"}
        if summary:
            hidden |= {"steps", "checks", "blocking_components", "host_action", "observed_at"}
        result = {key: deepcopy(value) for key, value in operation.items() if key not in hidden}
        result["execution"] = execution_status(self, operation)
        if result["execution"]["state"] == "recovery_required":
            result["recorded_status"] = result["status"]
            result["status"], result["ready"] = "RECOVERY_REQUIRED", False
            result["required_action"] = ("reconcile_installer" if any(
                step["state"] in _UNCERTAIN for step in operation["steps"].values()) else "resume")
        if not summary:
            result["events"] = self.store.events(operation["operation_id"], after=max(0, operation["revision"] - 100))
        return result

    def status(self, operation_id):
        """Historical durable status; performs no discovery/download/execution."""
        return self._public(self._get(operation_id))

    def busy(self):
        """Conservative read-only guard for this store, including hidden claims.

        No operation identity is disclosed across catalogue/product bindings.
        This observation is not an atomic lease shared with a product executor.
        """
        return self.store.has_claim() or self.store.controller_busy() is not False

    def operations_list(self, *, unfinished_only=True, limit=100, after_id=None):
        """Read-only recovery index for this exact owner/product/target/catalogue."""
        operations, cursor = self.store.operations(self.identity(), unfinished_only=unfinished_only,
                                                   limit=limit, after_id=after_id)
        return {"operations": [self._public(item, summary=True) for item in operations], "next_cursor": cursor}

    def events(self, operation_id, *, after=0, limit=100):
        self._get(operation_id)
        return self.store.events(operation_id, after=after, limit=limit)

    def attempts(self, operation_id, component_id, *, after=0, limit=100):
        self._get(operation_id)
        return self.store.attempts(operation_id, component_id, after=after, limit=limit)

    def _existing_start(self, operation_id, plan):
        old = self.store.get(operation_id)
        if old is not None and (old["owner_id"] != self.owner_id
                or old["product_id"] != self.catalog.manifest["product_id"]
                or old["target"] != self.catalog.target or old["plan_digest"] != plan["plan_digest"]):
            fail("OPERATION_CONFLICT", "prerequisite_operation_plan_conflict")
        return old

    def _prepare_start(self, operation_id, plan, consents, *, defer_validation=False):
        """Build a new record under controller lock; caller persists it once."""
        if plan["catalog_digest"] != self.catalog.catalog_digest or plan["target"] != self.catalog.target:
            fail("PLAN_STALE", "prerequisite_catalog_changed")
        if not defer_validation:
            current = self.plan(plan["choices"], locale=plan["locale"], offline=plan["offline"], local_artifacts=plan["local_artifacts"])
            if current != plan:
                fail("PLAN_STALE", "prerequisite_plan_changed_before_start")
        return {"schema_version": 1, "operation_id": operation_id, "owner_id": self.owner_id,
                "product_id": self.catalog.manifest["product_id"], "target": self.catalog.target,
                "catalog_digest": plan["catalog_digest"], "plan_digest": plan["plan_digest"], "plan": plan,
                "consents": consents, "status": "AWAITING_USER", "ready": False, "revision": 0,
                "created_at": utcnow(), "checks": [], "blocking_components": [], "required_action": "resume",
                "_initial_validation_pending": defer_validation,
                "error": None, "cancel_requested": False, "system_changes_started": False,
                "steps": {item["component_id"]: {"state": "SKIPPED" if item["choice"] == "skip" else "READY" if item["initial_ready"] else "PENDING",
                    "attempt": 0, "ownership": item["ownership"], "receipt": None,
                    "boundary_choice": None} for item in plan["components"]}}

    def start(self, operation_id, plan, *, consents):
        if not identifier(operation_id):
            fail("INVALID_REQUEST", "prerequisite_operation_id")
        plan, consents = self._validate_plan(plan, consents)
        with self.store.lock():
            old = self._existing_start(operation_id, plan)
            if old is not None:
                return self._public(old)
            operation = self._prepare_start(operation_id, plan, consents)
            operation = self._save(operation, "prerequisite_operation_created", create=True)
            return self._public(self._drive(operation, execute=True))

    def start_background(self, operation_id, plan, *, consents):
        from .prerequisite_async import start
        return start(self, operation_id, plan, consents=consents)

    def resume_background(self, operation_id, *, consents=None):
        from .prerequisite_async import resume
        return resume(self, operation_id, consents=consents)

    def _validated_context(self, operation):
        plan, _ = self._validate_plan(operation["plan"], operation["consents"])
        entries = self.catalog.entries()
        if plan["catalog_digest"] != self.catalog.catalog_digest or plan["target"] != self.catalog.target:
            fail("PLAN_STALE", "prerequisite_catalog_changed")
        by_id = {entry["requirement"]["component_id"]: entry for entry in entries}
        if set(by_id) != set(operation["steps"]):
            fail("INTEGRITY_FAILED", "prerequisite_operation_graph")
        for item in plan["components"]:
            if item["choice"] == "auto" and not item["initial_ready"]:
                adapter = self.adapters.get(item["adapter_id"])
                if adapter is None or adapter.policy_digest != item["adapter_policy_digest"]:
                    fail("PLAN_STALE", "prerequisite_execution_policy_changed")
        inventory = self._observe()
        if self._context(inventory, entries) != plan["context_digest"]:
            fail("PLAN_STALE", "prerequisite_material_context_changed")
        return entries, inventory

    def _refresh(self, operation, entries, inventory):
        plan = operation["plan"]
        checks = check_requirements([entry["requirement"] for entry in entries], inventory, plan["locale"])
        by_id = {entry["requirement"]["component_id"]: entry for entry in entries}
        observed = self._markers()
        for check in checks:
            name = check["component_id"]
            step = operation["steps"][name]
            check["selected"] = plan["choices"][name] != "skip"
            check["ownership"] = step["ownership"]
            check["license_allowed"] = self._license_allowed(by_id[name])
            if not check["selected"]:
                continue
            if not check["license_allowed"]:
                check["ready"] = False
                check["reason"] = "prerequisite_license_not_approved"
            receipt = step["receipt"] or {}
            if step["state"] in _UNCERTAIN:
                result = "unknown"
                before = receipt.get("markers", {})
                if before.get("boot_id") is not None and observed["boot_id"] is not None and before["boot_id"] != observed["boot_id"] and check["ready"]:
                    result = "applied"
                elif self.reconcile is not None:
                    result = self.reconcile(deepcopy(by_id[name]), deepcopy(receipt), deepcopy(inventory))
                    if result not in {"applied", "not_applied", "running", "unknown"}:
                        fail("RECOVERY_REQUIRED", "prerequisite_reconciliation_invalid", name)
                if result == "applied":
                    step["state"] = "VERIFYING"
                    receipt["reconciled"] = "applied"
                elif result == "not_applied":
                    step["state"] = "PENDING"
                    receipt["reconciled"] = "not_applied"
                else:
                    step["state"] = "UNKNOWN"
                    receipt["reconciled"] = result
                    check["ready"] = False
                step["receipt"] = receipt
            if step["state"] in {"WAITING_REBOOT", "WAITING_RELOGIN"}:
                key = "boot_id" if step["state"] == "WAITING_REBOOT" else "session_id"
                before = receipt.get("markers", {}).get(key)
                if before is not None and observed[key] is not None and before != observed[key]:
                    step["state"] = "VERIFYING"
                    receipt["boundary_observed"] = True
                else:
                    check["ready"] = False
                    check["reason"] = "prerequisite_boundary_not_observed" if before is not None and observed[key] is not None else "prerequisite_marker_unavailable"
            if step["state"] in _UNCERTAIN | {"WAITING_REBOOT", "WAITING_RELOGIN"}:
                continue
            if check["ready"]:
                step["state"] = "READY"
            elif check["status"] in {"reboot_required", "relogin_required"}:
                step["state"] = "WAITING_REBOOT" if check["status"] == "reboot_required" else "WAITING_RELOGIN"
                step["receipt"] = {**receipt, "markers": observed, "boundary_observed": False}
            elif step["state"] == "READY":
                # An already accepted dependency disappearing is not permission
                # to reinstall it automatically under an old plan.
                step["state"] = "VERIFYING"
        operation["checks"] = checks
        operation["blocking_components"] = [item["component_id"] for item in checks if item["selected"] and not item["ready"]]
        operation["observed_at"] = utcnow()
        return observed

    def _settle(self, operation):
        selected = [step for step in operation["steps"].values() if step["state"] != "SKIPPED"]
        operation["ready"] = False
        if any(step["state"] in _UNCERTAIN for step in selected):
            operation["status"], operation["required_action"] = "RECOVERY_REQUIRED", "reconcile_installer"
        elif operation["cancel_requested"]:
            operation["status"], operation["required_action"] = "CANCELED", None
        elif any(step["state"] == "WAITING_REBOOT" for step in selected):
            operation["status"], operation["required_action"] = "REBOOT_REQUIRED", "choose_reboot_time"
        elif any(step["state"] == "WAITING_RELOGIN" for step in selected):
            operation["status"], operation["required_action"] = "RELOGIN_REQUIRED", "choose_relogin_time"
        elif not operation["blocking_components"] and all(step["state"] == "READY" for step in selected):
            operation["status"], operation["required_action"], operation["ready"] = "READY", None, True
        else:
            items = {item["component_id"]: item for item in operation["plan"]["components"]}
            pending_auto = any(operation["plan"]["choices"][name] == "auto" and step["state"] in {"PENDING", "DOWNLOADING", "NOT_STARTED"}
                               and all(operation["steps"][dep]["state"] == "READY" for dep in items[name]["depends_on"])
                               for name, step in operation["steps"].items())
            operation["status"] = "AWAITING_USER"
            operation["required_action"] = "resume" if pending_auto else "manual_recheck"
        return operation

    def _progress(self, operation_id, component_id, event):
        allowed = {"bytes_done", "bytes_total", "bytes_delta", "percent", "attempt_id", "retryable", "status", "completed", "total", "unit"}
        self._emit({"type": "prerequisite_download", "phase": "prerequisites", "operation_id": operation_id,
                    "component_id": component_id, **{key: value for key, value in event.items() if key in allowed}})

    def _artifacts(self, operation, entry, adapter):
        name = entry["requirement"]["component_id"]
        step = operation["steps"][name]
        directory = owned_directory(self.store.directory / "artifacts" / operation["operation_id"] / name / str(step["attempt"] + 1), create=True)
        paths = []
        for index, raw in enumerate(entry["artifacts"]):
            spec = ArtifactSpec.from_dict(raw)
            source = operation["plan"]["local_artifacts"].get(spec.artifact_id)
            if source is None:
                source = self.downloads.fetch(spec, offline=operation["plan"]["offline"],
                    progress=lambda event: self._progress(operation["operation_id"], name, event),
                    cancel_requested=lambda: self._get(operation["operation_id"])["cancel_requested"])["path"]
            if not self.downloads.verify(spec, source):
                fail("INTEGRITY_FAILED", "prerequisite_local_artifact_hash", name)
            destination = directory / ("artifact_" + str(index) + adapter.artifact_suffixes[index])
            if not self.downloads.verify(spec, destination):
                with os.fdopen(_open(Path(source), os.O_RDONLY), "rb") as src:
                    with os.fdopen(_open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), "wb") as dst:
                        remaining = spec.size + 1
                        while remaining:
                            chunk = src.read(min(remaining, 128 * 1024))
                            if not chunk:
                                break
                            dst.write(chunk)
                            remaining -= len(chunk)
                        dst.flush()
                        os.fsync(dst.fileno())
                if not self.downloads.verify(spec, destination):
                    fail("INTEGRITY_FAILED", "prerequisite_staged_artifact_hash", name)
            if os.name != "nt" and adapter.argv[0] == "{artifact_" + str(index) + "}":
                destination.chmod(0o700)
            paths.append(destination)
        return paths

    def _drive(self, operation, *, execute):
        if operation["status"] in _TERMINAL:
            return operation
        try:
            if operation.get("_initial_validation_pending"):
                plan = operation["plan"]
                current = self.plan(plan["choices"], locale=plan["locale"], offline=plan["offline"],
                                    local_artifacts=plan["local_artifacts"])
                if current != plan:
                    fail("PLAN_STALE", "prerequisite_plan_changed_before_start")
                operation["_initial_validation_pending"] = False
                operation = self._save(operation, "prerequisite_initial_plan_verified")
            entries, inventory = self._validated_context(operation)
            self._refresh(operation, entries, inventory)
            operation["error"] = None
            operation = self._save(self._settle(operation), "prerequisite_rechecked")
            if not execute or operation["status"] in _TERMINAL | {"REBOOT_REQUIRED", "RELOGIN_REQUIRED", "RECOVERY_REQUIRED"}:
                return operation
            for entry in entries:
                name = entry["requirement"]["component_id"]
                step = operation["steps"][name]
                if operation["plan"]["choices"][name] != "auto" or step["state"] not in {"PENDING", "DOWNLOADING", "NOT_STARTED"}:
                    continue
                if any(operation["steps"][dep]["state"] != "READY" for dep in entry["depends_on"]):
                    continue
                adapter = self.adapters[entry["adapter_id"]]
                adapter.check(entry, inventory)
                step["state"] = "DOWNLOADING"
                operation["status"], operation["required_action"] = "RUNNING", None
                operation = self._save(operation, "prerequisite_downloading")
                if operation["cancel_requested"]:
                    return self._save(self._settle(operation), "prerequisite_canceled_before_download")
                paths = self._artifacts(operation, entry, adapter)
                # Recheck material context and dependencies after a long download.
                current_entries, inventory = self._validated_context(operation)
                observed = self._refresh(operation, current_entries, inventory)
                step = operation["steps"][name]
                operation["cancel_requested"] = self._get(operation["operation_id"])["cancel_requested"]
                if operation["cancel_requested"]:
                    return self._save(self._settle(operation), "prerequisite_canceled_before_execution")
                if step["state"] == "READY":
                    continue
                if (step["state"] not in {"PENDING", "DOWNLOADING", "NOT_STARTED"}
                        or any(operation["steps"][dep]["state"] != "READY" for dep in entry["depends_on"])):
                    return self._save(self._settle(operation), "prerequisite_execution_deferred")
                adapter.check(entry, inventory)
                step["state"], step["attempt"] = "INSTALLING", step["attempt"] + 1
                step["receipt"] = {"attempt": step["attempt"], "markers": observed, "started_at": utcnow(),
                    "artifact_pins": [{"artifact_id": raw["artifact_id"], "sha256": raw["sha256"]} for raw in entry["artifacts"]],
                    "adapter_policy_digest": adapter.policy_digest, "outcome": "unknown"}
                if operation["system_changes_started"] is not True:
                    operation["system_changes_started"] = None  # Intent is durable; launch is not yet known.
                operation = self._save(operation, "prerequisite_execution_intent")
                operation["cancel_requested"] = self._get(operation["operation_id"])["cancel_requested"]
                if operation["cancel_requested"]:
                    step = operation["steps"][name]
                    step["state"] = "NOT_STARTED"
                    step["receipt"].update(started=False, outcome="NOT_STARTED", finished_at=utcnow())
                    if operation["system_changes_started"] is None and not any(
                            other["receipt"] and "started" not in other["receipt"] for other in operation["steps"].values()):
                        operation["system_changes_started"] = False
                    return self._save(self._settle(operation), "prerequisite_canceled_before_launch")
                result = adapter.execute(entry, paths, downloads=self.downloads, inventory=inventory)
                step = operation["steps"][name]
                outcome = adapter.outcome(result)
                step["receipt"].update(returncode=result.returncode, pid=result.pid, started=result.started,
                    timed_out=result.timed_out, outcome=outcome, finished_at=utcnow())
                if result.started:
                    operation["system_changes_started"] = True
                elif operation["system_changes_started"] is None:
                    uncertain_previous = any(other["receipt"] and "started" not in other["receipt"]
                        and other["receipt"].get("reconciled") != "not_applied" for other in operation["steps"].values())
                    operation["system_changes_started"] = None if uncertain_previous else False
                step["state"] = {"VERIFY": "VERIFYING", "REBOOT_REQUIRED": "WAITING_REBOOT",
                    "RELOGIN_REQUIRED": "WAITING_RELOGIN", "UNKNOWN": "UNKNOWN", "NOT_STARTED": "NOT_STARTED",
                    "CANCELED": "FAILED", "FAILED": "FAILED"}[outcome]
                if outcome == "CANCELED":
                    operation["cancel_requested"] = True
                elif outcome in {"FAILED", "NOT_STARTED", "UNKNOWN"}:
                    operation["error"] = {"code": "RECOVERY_REQUIRED" if outcome == "UNKNOWN" else "PREREQUISITE_MISSING",
                                          "reason": "prerequisite_installer_" + outcome.lower(), "component_id": name}
                # Commit installer outcome before running any detector.
                operation = self._save(operation, "prerequisite_execution_result")
                if operation["cancel_requested"]:
                    return self._save(self._settle(operation), "prerequisite_canceled_after_installer")
                current_entries, inventory = self._validated_context(operation)
                self._refresh(operation, current_entries, inventory)
                operation = self._save(self._settle(operation), "prerequisite_postcondition_checked")
                if operation["status"] in _TERMINAL | {"REBOOT_REQUIRED", "RELOGIN_REQUIRED", "RECOVERY_REQUIRED"} or outcome != "VERIFY" or operation["steps"][name]["state"] != "READY":
                    return operation
            return self._save(self._settle(operation), "prerequisite_pass_complete")
        except InstallerError as error:
            if error.code == "OPERATION_CANCELED":
                operation["cancel_requested"] = True
                return self._save(self._settle(operation), "prerequisite_canceled_at_safe_boundary")
            operation["error"] = {"code": error.code, "reason": error.context.get("details", {}).get("reason", "prerequisite_step_failed")}
            operation["ready"] = False
            if any(step["state"] in _UNCERTAIN for step in operation["steps"].values()):
                operation["status"], operation["required_action"] = "RECOVERY_REQUIRED", "reconcile_installer"
            else:
                operation["status"] = "AWAITING_USER"
                operation["required_action"] = "replan" if error.code == "PLAN_STALE" else "resolve_prerequisite_error"
            return self._save(operation, "prerequisite_failed_closed")
        except Exception:
            # Unknown adapter/discovery exceptions are never exposed and never
            # interpreted as no side effect. Persist the original intent.
            operation["error"] = {"code": "RECOVERY_REQUIRED", "reason": "prerequisite_unexpected_port_failure"}
            operation["ready"] = False
            uncertain = any(step["state"] in _UNCERTAIN for step in operation["steps"].values())
            operation["status"] = "RECOVERY_REQUIRED" if uncertain else "AWAITING_USER"
            operation["required_action"] = "reconcile_installer" if uncertain else "resolve_prerequisite_error"
            return self._save(operation, "prerequisite_failed_closed")

    def recheck(self, operation_id):
        """Read-only probes plus journal; no downloads, commands or host restart."""
        with self.store.lock():
            operation = self._get(operation_id)
            if "_dispatch" in operation and operation["status"] not in _TERMINAL:
                del operation["_dispatch"]
                operation = self._save(operation, "prerequisite_synchronous_recheck")
            self._drive(operation, execute=False)
            # A final observer may have persisted cancellation after the probe's
            # commit. Report the latest durable revision, not its stale input.
            return self._public(self._get(operation_id))

    def resume(self, operation_id, *, consents=None):
        with self.store.lock():
            operation = self._get(operation_id)
            if consents is not None:
                _, operation["consents"] = self._validate_plan(operation["plan"], consents)
            if "_dispatch" in operation and operation["status"] not in _TERMINAL:
                del operation["_dispatch"]
                operation = self._save(operation, "prerequisite_synchronous_resume")
            return self._public(self._drive(operation, execute=True))

    def choose_boundary(self, operation_id, when):
        """Record now/later intent. A trusted host UI performs the actual action.

        The returned host_action is a request, never evidence of a reboot/login.
        No shutdown command or automatic login task is created here.
        """
        if type(when) is not str or when not in {"now", "later"}:
            fail("INVALID_REQUEST", "prerequisite_boundary_choice")
        with self.store.lock():
            operation = self._get(operation_id)
            if operation["status"] not in {"REBOOT_REQUIRED", "RELOGIN_REQUIRED"}:
                fail("INVALID_REQUEST", "prerequisite_boundary_not_pending")
            for step in operation["steps"].values():
                if step["state"] in {"WAITING_REBOOT", "WAITING_RELOGIN"}:
                    step["boundary_choice"] = when
            operation["host_action"] = {"action": "reboot" if operation["status"] == "REBOOT_REQUIRED" else "relogin",
                                        "when": when, "performed": False}
            return self._public(self._save(operation, "prerequisite_boundary_choice_recorded"))

    def cancel(self, operation_id):
        self._get(operation_id)  # Enforce original owner/product before either path.
        try:
            with self.store.lock():
                operation = self._get(operation_id)
                if operation["status"] in _TERMINAL:
                    return self._public(operation)
                operation["cancel_requested"] = True
                # Does not uninstall shared prerequisites or kill an unknown process.
                return self._public(self._save(self._settle(operation), "prerequisite_cancel_requested"))
        except InstallerError as error:
            if error.code != "OPERATION_CONFLICT" or error.context.get("details", {}).get("reason") != "prerequisite_controller_busy":
                raise
            operation, event = self.store.request_cancel(operation_id)
            if event is not None:
                self._emit(event)
            return self._public(operation)

    def replace_plan(self, operation_id, plan, *, consents):
        """Renew stale context/choices under the same operation and fresh consent.

        Completed effects remain in receipts. An unresolved/partly applied
        installer cannot be made retryable merely by replacing the plan.
        """
        plan, consents = self._validate_plan(plan, consents)
        with self.store.lock():
            operation = self._get(operation_id)
            if operation["status"] in _TERMINAL or any(step["state"] in _UNCERTAIN for step in operation["steps"].values()):
                fail("RECOVERY_REQUIRED", "prerequisite_replan_unsafe")
            current = self.plan(plan["choices"], locale=plan["locale"], offline=plan["offline"], local_artifacts=plan["local_artifacts"])
            if current != plan:
                fail("PLAN_STALE", "prerequisite_replacement_plan_stale")
            old_steps = operation["steps"]
            new_steps = {}
            for item in plan["components"]:
                old = deepcopy(old_steps.get(item["component_id"], {"attempt": 0, "receipt": None}))
                if (item["choice"] == "auto" and not item["initial_ready"] and old["attempt"] > 0
                        and old.get("state") != "NOT_STARTED" and (old.get("receipt") or {}).get("reconciled") != "not_applied"):
                    fail("RECOVERY_REQUIRED", "prerequisite_retry_needs_reconciliation", item["component_id"])
                if old.get("state") in {"WAITING_REBOOT", "WAITING_RELOGIN"}:
                    fail("RECOVERY_REQUIRED", "prerequisite_boundary_still_pending", item["component_id"])
                old.update(state="SKIPPED" if item["choice"] == "skip" else "PENDING", ownership=item["ownership"], boundary_choice=None)
                new_steps[item["component_id"]] = old
            if any(step["attempt"] for key, step in old_steps.items() if key not in new_steps):
                fail("RECOVERY_REQUIRED", "prerequisite_effect_cannot_be_removed_from_graph")
            operation.update(plan=plan, consents=consents, plan_digest=plan["plan_digest"], catalog_digest=plan["catalog_digest"],
                             steps=new_steps, status="AWAITING_USER", ready=False, error=None, required_action="resume")
            return self._public(self._save(operation, "prerequisite_plan_replaced"))
