"""Публичный фасад v1: одинаковые планы/операции для SDK, CLI и web-мастера."""

from __future__ import annotations

from copy import deepcopy
import hmac
import hashlib
from importlib import resources
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading
import time
import uuid

from homensai_install import API_VERSION, __version__
from homensai_install.process_liveness import process_liveness
from .journal import Journal, now
from .model import (ProductAdapter, bounded_json, canonical, checked_id, checked_product_id, digest,
                    fail, normalize_manifest, parse_json, read_manifest, target_model)

ALL_ACTIONS = ("build", "install", "update", "modify_components", "repair", "reinstall", "uninstall",
               "backup", "restore", "rollback", "start", "stop")
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "FAILED_ROLLED_BACK", "CANCELED"})
RESUMABLE = frozenset({"RECOVERY_REQUIRED", "AWAITING_USER", "REBOOT_REQUIRED", "RELOGIN_REQUIRED", "FAILED", "CANCELED"})
OPTION_KEYS = frozenset({"install_root", "data_home", "release", "modes", "payload_dir",
                         "inventory", "inventory_file", "signature", "signature_file",
                         "runtime_executable", "work_dir", "backup_password_ref", "secret_refs", "archive_path",
                         "acknowledge_data_loss", "development_runtime", "eula_digest", "eula_locale", "eula_version",
                         "language", "start_after_install", "port", "product_version", "migration_review_id"})
EVENT_KEYS = frozenset({"type", "phase", "component_id", "step_id", "status", "percent", "current",
                        "total", "completed", "bytes_done", "bytes_total", "progress", "message_code", "error",
                        "duration_ms", "unit", "bytes_delta", "attempt_id", "retryable"})
_JOBS: dict[tuple[str, str], threading.Thread] = {}
_JOBS_LOCK = threading.RLock()


def _process_identity(pid: int) -> tuple[str, str, int] | None:
    """One proc snapshot: state, birth ticks, thread count; unknown stays unknown."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0], fields[19], int(fields[17])
    except (OSError, IndexError, ValueError, UnicodeError):
        return None


def _process_start(pid: int) -> str | None:
    identity = _process_identity(pid)
    return identity[1] if identity is not None else None


def _owner() -> dict:
    return {"pid": os.getpid(), "process_start": _process_start(os.getpid())}


def _owner_alive(owner: dict) -> bool:
    pid = owner.get("pid")
    if type(pid) is not int or pid <= 0:
        return False
    alive = process_liveness(pid)
    if alive is not True:
        return alive is None  # Неизвестный результат не доказывает смерть владельца.
    identity = _process_identity(pid)
    if identity is None:
        return True  # Read/access failure is not evidence that an owner died.
    state, actual, threads = identity
    if owner.get("process_start") not in (None, actual):
        return False
    # kill(pid, 0) also succeeds for an unreaped zombie. Keep the result
    # conservative if the thread group still reports additional threads.
    return not (state in {"Z", "X", "x"} and 0 <= threads <= 1)


def _plan_digest(plan: dict) -> str:
    return digest({key: value for key, value in plan.items()
                   if key not in {"plan_digest", "review_consent_ref"}})


def _options(options: dict | None) -> dict:
    value = bounded_json(options or {})
    if type(value) is not dict or set(value) - OPTION_KEYS:
        fail("INVALID_REQUEST", details={"field_path": "options"})
    if "migration_review_id" in value and not _sha256_identifier(value["migration_review_id"]):
        fail("INVALID_REQUEST", details={"field_path": "options.migration_review_id"})
    return value


def _sha256_identifier(value) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _functional_result(result: dict, plan: dict) -> dict:
    """Keep reviewed local artifact addresses usable; redact all diagnostic text.

    This is a structural projection, not another artifact verification. The engine
    verifies ciphertext and ownership. No result-supplied path is opened here, so
    status/reopen does not depend on an artifact still being at its old address.
    """
    from homensai_install.errors import redact
    from shared.datahome import _windows_reserved

    locators = []
    options, action = plan['options'], plan['action']
    windows = plan['target'].get('os') == 'windows'
    path_type = PureWindowsPath if windows else PurePosixPath

    def invalid():
        fail('UNEXPECTED_ERROR', details={'reason': 'invalid_artifact_locator'})

    def path(value):
        if (type(value) is not str or not value or _windows_reserved(value)
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or '..' in value.replace('\\', '/').split('/')
                or value.startswith(('//', '\\\\'))
                or (not windows and ('\\' in value or PureWindowsPath(value).drive))):
            invalid()
        parsed = path_type(value)
        if not parsed.is_absolute():
            invalid()
        return parsed

    def keep(container, field, expected):
        record = result.get(container)
        if type(record) is not dict or path(record.get(field)) != expected:
            invalid()
        locators.append((container, field, record[field]))

    def encrypted(record, hash_key):
        if (type(record) is not dict or record.get('encrypted') is not True
                or not _sha256_identifier(record.get(hash_key))):
            invalid()

    mode = plan['target'].get('mode', 'native')
    known = ((mode == 'native' and any(k in result for k in ('archive', 'before_recovery_archive')))
             or (mode == 'docker' and any(k in result for k in ('backup', 'recovery'))))
    if known:
        root = path(options.get('install_root'))
        suffix = '-docker-recovery' if mode == 'docker' else '-recovery'
        work = path(options.get('work_dir', str(root.parent / ('.' + root.name + suffix))))
        token = digest([plan['plan_id'], plan['plan_digest']])[:32]
        if mode == 'native':
            if 'archive' in result:
                if action != 'backup':
                    invalid()
                encrypted(result['archive'], 'sha256')
                keep('archive', 'path', path(options['archive_path']) if 'archive_path' in options
                     else work / (token + '.hsac'))
            if 'before_recovery_archive' in result:
                archive, receipt = result['before_recovery_archive'], result.get('before_recovery')
                encrypted(archive, 'sha256')
                reviewed_source = plan.get('engine_plan', {}).get('recovery_source_sha256')
                if (action not in {'restore', 'rollback'} or type(receipt) is not dict
                        or receipt.get('kind') != 'before_recovery_backup'
                        or receipt.get('recovery_action') != action
                        or receipt.get('operation_id') != 'op_' + token
                        or receipt.get('archive_sha256') != archive['sha256']
                        or type(archive.get('release')) is not str
                        or receipt.get('from_release') != archive['release']
                        or not _sha256_identifier(receipt.get('source_archive_sha256'))
                        or (reviewed_source is not None and receipt['source_archive_sha256'] != reviewed_source)):
                    invalid()
                keep('before_recovery_archive', 'path', work / (token + '-before-' + action + '.hsac'))
        else:
            if 'backup' in result:
                if action != 'backup':
                    invalid()
                encrypted(result['backup'], 'archive_sha256')
                expected = (path(options['archive_path']) if 'archive_path' in options else
                            work / ('data-' + hashlib.sha256(plan['plan_id'].encode()).hexdigest() + '.haes'))
                if expected.parent != work:
                    invalid()
                keep('backup', 'archive_path', expected)
            if 'recovery' in result:
                record = result['recovery']
                if action not in {'install', 'uninstall'} or type(record) is not dict:
                    invalid()
                metadata = path(record.get('metadata_path'))
                directory = metadata.parent
                release = options.get('release')
                if (type(release) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', release) is None
                        or directory.parent != root / '.hinstall' / 'docker-recovery'
                        or re.fullmatch(re.escape(release) + r'-[0-9a-f]{16}', directory.name) is None):
                    invalid()
                contract = plan.get('engine_plan', {}).get('provisioning_spec', {}).get('contract_sha256')
                if contract is not None and (not _sha256_identifier(contract) or directory.name != release + '-' + contract[:16]):
                    invalid()
                keep('recovery', 'metadata_path', directory / 'recovery.json')
                keep('recovery', 'payload_dir', directory / 'payload')
    safe = redact(result)
    for container, field, original in locators:
        if type(safe.get(container)) is not dict:
            invalid()
        safe[container][field] = original
    return safe


class InstallerAPI:
    """Локальный SDK. state_dir должен быть приватным каталогом текущего владельца.

    Методы возвращают JSON-совместимые dict, ошибки — InstallerError.
    validate/discover не создают состояние; plan сохраняет только собственные
    метаданные, не выполняя установку или продуктовые hooks.
    """

    def __init__(self, state_dir: str | Path, *, engine=None, locale: str = "ru", diagnostics=None,
                 prerequisites=None, execution_domain=None):
        self.state_dir = Path(state_dir).absolute()
        self._engine_instance = engine
        self.locale = locale
        self._diagnostic_instance = diagnostics
        self._diagnostic_unavailable = False
        if prerequisites is not None:
            # Lazy: coordinator itself imports the model/journal package.
            from homensai_install.prerequisite_execution import PrerequisiteCoordinator
            if not isinstance(prerequisites, PrerequisiteCoordinator):
                fail("INVALID_REQUEST", details={"reason": "explicit_prerequisite_coordinator_required"})
        self._prerequisite_instance = prerequisites
        from homensai_install.execution_lease import ExecutionDomain
        if execution_domain is not None and not isinstance(execution_domain, ExecutionDomain):
            fail("INVALID_REQUEST", details={"reason": "trusted_execution_domain_required"})
        if prerequisites is not None:
            if execution_domain is not None and not prerequisites.execution_domain.same_domain(execution_domain):
                fail("INVALID_REQUEST", details={"reason": "prerequisite_execution_domain_mismatch"})
            execution_domain = prerequisites.execution_domain
        # Unregistered legacy APIs retain their existing per-product claims.
        # Cross-controller exclusion requires the trusted common domain port.
        self.execution_domain = execution_domain
        if execution_domain is not None:
            execution_domain.register_participant("product", self.state_dir)

    @classmethod
    def from_bootstrap(cls, state_dir: str | Path, *, trust_config: str | Path | None = None,
                       allow_development: bool = False, development_runtime: str | Path | None = None,
                       docker_cli_config: str | Path | None = None, secret_provider=None,
                       locale: str = "ru", prerequisites=None, execution_domain=None,
                       product_adapter_descriptor: str | None = None) -> "InstallerAPI":
        """Создать общий router из доверенных локальных настроек bootstrap.

        Эти настройки задаёт приложение-хост, не manifest/options/HTTP body.
        Конструктор не исполняет hooks и не проверяет payload: общий engine
        проверяет подпись и полное дерево непосредственно перед исполнением.
        Без trust_config планирование доступно, исполнение не получает доверия.
        """
        from homensai_install.backends import configured_backends
        from homensai_install.engine import EngineError
        from homensai_install.errors import InstallerError
        from homensai_install.trust import load_trust_config

        if type(allow_development) is not bool:
            fail("INVALID_REQUEST", details={"field_path": "allow_development"})
        if development_runtime is not None and not allow_development:
            fail("CONSENT_REQUIRED")
        verifier = (load_trust_config(trust_config, allow_development=allow_development)
                    if trust_config is not None else None)
        try:
            engine = configured_backends(verifier=verifier, secret_provider=secret_provider,
                                         development_runtime=development_runtime,
                                         docker_cli_config=docker_cli_config,
                                         product_adapter_descriptor=product_adapter_descriptor)
        except EngineError as error:
            raise InstallerError(error.code, details=error.details) from None
        return cls(state_dir, engine=engine, locale=locale, prerequisites=prerequisites,
                   execution_domain=execution_domain)

    def _engine(self):
        if self._engine_instance is None:
            from homensai_install.backends import BackendRouter
            self._engine_instance = BackendRouter()
        return self._engine_instance

    def _engine_call(self, method: str, *args):
        from homensai_install.errors import InstallerError, redact
        try:
            return getattr(self._engine(), method)(*args)
        except InstallerError:
            raise
        except Exception as error:
            code = getattr(error, "code", "UNEXPECTED_ERROR")
            if type(code) is not str or not code or len(code) > 80 or not all(c.isupper() or c.isdigit() or c == "_" for c in code):
                code = "UNEXPECTED_ERROR"
            fail(code, details=redact(getattr(error, "details", {})))

    def _journal(self, *, create: bool = False) -> Journal:
        try:
            return Journal(self.state_dir, create=create)
        except FileNotFoundError:
            fail("OPERATION_NOT_FOUND")

    def describe(self) -> dict:
        return {
            "api_version": API_VERSION, "module_version": __version__, "adapter_protocol_version": "1.0",
            "error_catalog_version": "1.0.0", "manifest_schemas": [1, 2], "normalized_model_version": 1,
            "sdk": "homensai_install.api.v1.InstallerAPI", "cli": "hinstall",
            "product_adapter": {"sdk": "homensai_install.adapters.ExecutableProductAdapter",
                                "protocol_version": "1.0", "registration": "explicit_sdk",
                                "lifecycle_engine_registration": True,
                                "lifecycle_registration": "trusted_native_engine_constructor",
                                "lifecycle_engine_actions": ["install", "start", "stop", "backup", "update", "restore", "rollback"],
                                "migration_preparation": "explicit_sdk_backend_extension",
                                "migration_execution": True, "migration_actions": ["install", "update"],
                                "migration_max_steps": 1, "default_registration": False,
                                "recovery_execution": True, "recovery_actions": ["restore", "rollback"],
                                "recovery_failure_policy": "preserve_before_recovery_snapshot_no_automatic_retry",
                                "execution_platforms": ["linux"], "execution_mode": "native",
                                "execution_scope": "user"},
            "scaffold": {"sdk": "homensai_install.scaffold.scaffold_product",
                         "cli": "hinstall init-adapter", "missing_inputs": "DRAFT_BLOCKED"},
            "locales": ["ru", "en", "de"],
            "backends": {"sdk": "homensai_install.backends.BackendRouter",
                         "bootstrap_sdk": "homensai_install.api.v1.InstallerAPI.from_bootstrap",
                         "registration": "explicit_bootstrap", "default": "native:user",
                         "selection": "reviewed_target", "implicit_fallback": False,
                         "availability": "capabilities_for_exact_target"},
            "prerequisites": {"registration": "explicit_sdk", "execution": "synchronous_default_background_opt_in",
                              "background_ack": "durable_queued_operation", "recovery_listing": True,
                              "http_transport": "explicit_wizard_session", "http_plan_input": "server_plan_id",
                              "cli_transport": "explicit_trusted_sdk", "cli_execution": "synchronous",
                              "busy_probe": "registered_store_claim_or_controller",
                              "durable_byte_progress": True,
                              "automatic_default_factory": False, "gui_auto_flow": True,
                              "readiness": "source_contract", "production_ready": False},
            "operations": ["describe", "capabilities", "validate", "discover", "prepare_migration", "plan", "submit",
                           "operations.list", "status", "events", "cancel", "resume", "errors.describe", "errors.search",
                           "diagnostics.get", "diagnostics.export", "statistics.query",
                           "prerequisites.capabilities", "prerequisites.identity", "prerequisites.busy", "prerequisites.license",
                           "prerequisites.plan", "prerequisites.start", "prerequisites.status",
                           "prerequisites.operations.list",
                           "prerequisites.events", "prerequisites.attempts", "prerequisites.recheck",
                           "prerequisites.resume", "prerequisites.cancel", "prerequisites.choose_boundary",
                           "prerequisites.replace_plan"],
            "schemas": {"manifest": "shared/manifest.schema.json", "request": "api/schemas/request.schema.json",
                        "plan": "api/schemas/plan.schema.json", "operation": "api/schemas/operation.schema.json",
                        "event": "api/schemas/event.schema.json"},
            "documentation": {"api": "api/resources/INSTALLER_API.md",
                              "integration": "api/resources/INTEGRATION_GUIDE.md",
                              "product_adapter": "api/resources/PRODUCT_ADAPTER_EXECUTION.md",
                              "http": "api/resources/openapi.yaml"},
            "readiness": "development", "production_ready": False,
            "limits": {"input_json_bytes": 2097152, "json_depth": 48, "event_page_size": 1000,
                       "operation_list_size": 200},
        }

    def capabilities(self, target: dict | None = None) -> dict:
        target = target_model(target)
        result = bounded_json(self._engine_call("capabilities", target))
        if type(result) is not dict:
            fail("UNEXPECTED_ERROR")
        return {"api_version": API_VERSION, "target": target, **result}

    def _prerequisite_call(self, method, *args, **kwargs):
        """Transport facade only: the registered coordinator owns every effect.

        Background start/resume is explicitly opted in. A prerequisite READY
        never claims product installation success. GUI bootstrap must supply its
        own reviewed catalogue; HTTP/CLI only delegate to that registration.
        """
        if self._prerequisite_instance is None:
            fail("CAPABILITY_UNSUPPORTED", details={"reason": "prerequisites_not_configured"})
        from homensai_install.errors import InstallerError, redact
        try:
            port = self._prerequisite_instance
            function = port.catalog.license_text if method == "license_text" else getattr(port, method)
            return bounded_json(function(*args, **kwargs))
        except InstallerError:
            raise
        except Exception as error:
            code = getattr(error, "code", "UNEXPECTED_ERROR")
            if type(code) is not str or not code or len(code) > 80 or not all(c.isupper() or c.isdigit() or c == "_" for c in code):
                code = "UNEXPECTED_ERROR"
            fail(code, details=redact(getattr(error, "details", {})))

    @staticmethod
    def _prerequisite_consents(values):
        if not isinstance(values, (list, tuple)):
            fail("INVALID_REQUEST")
        return bounded_json(list(values))

    def prerequisites_identity(self) -> dict:
        value = self._prerequisite_call("identity")
        return {**value, "manifest_digest": digest(self._prerequisite_instance.catalog.manifest),
                "payload_binding": self._prerequisite_instance.catalog.payload_binding()}

    def prerequisites_capabilities(self) -> dict:
        if self._prerequisite_instance is None:
            return {"api_version": API_VERSION, "configured": False, "automatic_component_ids": [],
                    "manual_recheck": False, "durable_resume": False, "production_ready": False}
        return {"api_version": API_VERSION, "configured": True,
                **self._prerequisite_call("capabilities"), "production_ready": False}

    def prerequisites_busy(self) -> bool:
        """Есть claim или controller не доказанно свободен; чужие ID не выдаются.

        Это read-only проверка зарегистрированного store, не межпроцессная
        блокировка совместной product/prerequisite транзакции.
        """
        result = self._prerequisite_call("busy")
        if type(result) is not bool:
            fail("UNEXPECTED_ERROR")
        return result

    def prerequisites_license(self, component_id: str) -> dict:
        if type(component_id) is not str or len(component_id) > 128:
            fail("INVALID_REQUEST")
        return self._prerequisite_call("license_text", component_id)

    def prerequisites_plan(self, choices: dict | None = None, *, locale: str | None = None,
                           offline: bool = False, local_artifacts: dict | None = None) -> dict:
        return self._prerequisite_call("plan", bounded_json(choices), locale=self.locale if locale is None else locale,
                                       offline=offline, local_artifacts=bounded_json(local_artifacts))

    def prerequisites_start(self, operation_id: str, plan: dict, consent_refs=(), *, background: bool = False) -> dict:
        if type(background) is not bool:
            fail("INVALID_REQUEST")
        return self._prerequisite_call("start_background" if background else "start", checked_id(operation_id), bounded_json(plan),
                                       consents=self._prerequisite_consents(consent_refs))

    def prerequisites_status(self, operation_id: str) -> dict:
        return self._prerequisite_call("status", checked_id(operation_id))

    def prerequisites_operations_list(self, *, unfinished_only: bool = True, limit: int = 100,
                                      after_id: str | None = None) -> dict:
        return self._prerequisite_call("operations_list", unfinished_only=unfinished_only, limit=limit,
                                       after_id=checked_id(after_id) if after_id is not None else None)

    def prerequisites_events(self, operation_id: str, *, after_sequence: int = 0, limit: int = 100) -> dict:
        events = self._prerequisite_call("events", checked_id(operation_id), after=after_sequence, limit=limit)
        return {"events": events, "snapshot": self.prerequisites_status(operation_id)}

    def prerequisites_attempts(self, operation_id: str, component_id: str, *, after: int = 0, limit: int = 100) -> dict:
        return {"attempts": self._prerequisite_call("attempts", checked_id(operation_id), component_id,
                                                  after=after, limit=limit)}

    def prerequisites_recheck(self, operation_id: str) -> dict:
        return self._prerequisite_call("recheck", checked_id(operation_id))

    def prerequisites_resume(self, operation_id: str, consent_refs=None, *, background: bool = False) -> dict:
        if type(background) is not bool:
            fail("INVALID_REQUEST")
        consents = self._prerequisite_consents(consent_refs) if consent_refs is not None else None
        return self._prerequisite_call("resume_background" if background else "resume", checked_id(operation_id), consents=consents)

    def prerequisites_cancel(self, operation_id: str) -> dict:
        return self._prerequisite_call("cancel", checked_id(operation_id))

    def prerequisites_choose_boundary(self, operation_id: str, when: str) -> dict:
        return self._prerequisite_call("choose_boundary", checked_id(operation_id), when)

    def prerequisites_replace_plan(self, operation_id: str, plan: dict, consent_refs=()) -> dict:
        return self._prerequisite_call("replace_plan", checked_id(operation_id), bounded_json(plan),
                                       consents=self._prerequisite_consents(consent_refs))

    def schema(self, name: str) -> dict:
        if name not in {"manifest", "request", "plan", "operation", "event", "normalized"}:
            fail("INVALID_REQUEST")
        if name == "manifest":
            text = resources.files("shared").joinpath("manifest.schema.json").read_text(encoding="utf-8")
        else:
            text = resources.files("homensai_install").joinpath("api", "schemas", name + ".schema.json").read_text(encoding="utf-8")
        return parse_json(text)

    def validate(self, manifest: dict | str | Path) -> dict:
        source = read_manifest(manifest)
        normalized = normalize_manifest(source)
        return {"valid": True, "source_manifest": source, "normalized_manifest": normalized,
                "manifest_digest": digest(source), "warnings": normalized["warnings"]}

    def discover(self, manifest: dict | str | Path, target: dict | None = None,
                 options: dict | None = None) -> dict:
        source = read_manifest(manifest)
        target = target_model(target)
        options = _options(options)
        result = bounded_json(self._engine_call("discover", source, target, options))
        if type(result) is not dict:
            fail("UNEXPECTED_ERROR")
        facts = result.get("fingerprint_data", {key: value for key, value in result.items()
                                               if key not in {"fingerprint", "timestamp", "observed_at"}})
        return {**result, "fingerprint": digest(facts), "target": target}

    def prepare_migration(self, action: str, manifest: dict | str | Path,
                          target: dict | None = None, options: dict | None = None) -> dict:
        """Explicit SDK preparation; plan/discover never invoke product hooks.

        Only a trusted registered backend can run the signed read-only product
        migration probe and save its immutable local review. This method does
        not apply migrations or grant consent for subsequent lifecycle effects.
        CLI/HTTP transports do not expose it implicitly.
        """
        if type(action) is not str or action not in {"install", "update"}:
            fail("INVALID_REQUEST", details={"field_path": "action"})
        source = self.validate(manifest)["source_manifest"]
        target = target_model(target)
        options = _options(options)
        if "migration_review_id" in options:
            fail("INVALID_REQUEST", details={"field_path": "options.migration_review_id"})
        if not callable(getattr(self._engine(), "prepare_migration", None)):
            fail("CAPABILITY_UNSUPPORTED", details={"reason": "migration_preparation_not_configured"})
        result = bounded_json(self._engine_call("prepare_migration", action, source, target, options))
        required = {"migration_review_id", "action", "product_plan", "from_release",
                    "to_release", "installation_id", "binding_sha256"}
        if (type(result) is not dict or set(result) != required or result["action"] != action
                or not _sha256_identifier(result["migration_review_id"])
                or not _sha256_identifier(result["binding_sha256"])
                or type(result["product_plan"]) is not dict
                or type(result["installation_id"]) is not str or not result["installation_id"]
                or type(result["to_release"]) is not str or not result["to_release"]
                or result["to_release"] != options.get("release")
                or (result["from_release"] is not None and
                    (type(result["from_release"]) is not str or not result["from_release"]))
                or (action == "install" and result["from_release"] is not None)
                or (action == "update" and result["from_release"] is None)):
            fail("UNEXPECTED_ERROR", details={"reason": "migration_preparation_result_invalid"})
        product_plan = result["product_plan"]
        if (not _sha256_identifier(product_plan.get("plan_digest"))
                or digest({k: v for k, v in product_plan.items() if k != "plan_digest"})
                    != product_plan["plan_digest"]):
            fail("UNEXPECTED_ERROR", details={"reason": "migration_preparation_plan_digest_invalid"})
        return result

    def _supported(self, action: str, target: dict):
        if action not in ALL_ACTIONS:
            fail("INVALID_REQUEST", details={"field_path": "action"})
        caps = self.capabilities(target)
        actions = caps.get("actions", caps.get("supported_actions", []))
        if type(actions) is list:
            supported = action in actions
        elif type(actions) is dict:
            value = actions.get(action, False)
            supported = value.get("supported") is True if type(value) is dict else value is True
        else:
            supported = False
        if not supported:
            fail("CAPABILITY_UNSUPPORTED", details={"action": action, "target": target})

    def plan(self, action: str, manifest: dict | str | Path, target: dict | None = None,
             inventory: dict | None = None, options: dict | None = None) -> dict:
        validated = self.validate(manifest)
        source = validated["source_manifest"]
        target = target_model(target)
        options = _options(options)
        self._supported(action, target)
        current = self.discover(source, target, options)
        if inventory is not None:
            previous = bounded_json(inventory)
            if type(previous) is not dict or previous.get("fingerprint") != current["fingerprint"]:
                fail("PLAN_STALE")
        engine_plan = bounded_json(self._engine_call("plan", action, source, target, current, options))
        if type(engine_plan) is not dict:
            fail("UNEXPECTED_ERROR")
        required = engine_plan.get("required_consents", [])
        if (type(required) is not list or len(required) > 32
                or any(type(item) is not str or not item or len(item) > 512 for item in required)):
            fail("UNEXPECTED_ERROR")
        plan = {
            "api_version": API_VERSION, "plan_id": "plan_" + uuid.uuid4().hex,
            "created_at": now(), "action": action,
            "source_manifest": source, "normalized_manifest": validated["normalized_manifest"],
            "manifest_digest": validated["manifest_digest"], "target": target, "options": options,
            "inventory_fingerprint": current["fingerprint"], "engine_plan": engine_plan,
            "required_consents": sorted(set(required)),
        }
        plan["plan_digest"] = _plan_digest(plan)
        plan["review_consent_ref"] = "plan:" + plan["plan_digest"]
        bounded_json(plan)
        self._journal(create=True).save_plan(plan)
        return deepcopy(plan)

    def get_plan(self, plan_id: str) -> dict:
        try:
            plan = self._journal().plan(plan_id)
        except Exception as error:
            if getattr(error, "code", None) == "OPERATION_NOT_FOUND":
                fail("PLAN_NOT_FOUND")
            raise
        if not hmac.compare_digest(str(plan.get("plan_digest", "")), _plan_digest(plan)):
            fail("INTEGRITY_FAILED")
        return plan

    def submit(self, plan_id: str, plan_digest: str, consent_refs=(), *,
               idempotency_key: str, background: bool = True) -> dict:
        if (type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 256
                or any(ord(c) < 32 for c in idempotency_key) or type(background) is not bool):
            fail("INVALID_REQUEST")
        if not isinstance(consent_refs, (list, tuple)) or len(consent_refs) > 64:
            fail("INVALID_REQUEST")
        if any(type(item) is not str or not item or len(item) > 512 for item in consent_refs):
            fail("INVALID_REQUEST")
        plan = self.get_plan(plan_id)
        if type(plan_digest) is not str or not hmac.compare_digest(plan_digest, plan["plan_digest"]):
            fail("INTEGRITY_FAILED")
        consents = sorted(set(consent_refs))
        required = set(plan["required_consents"]) | {plan["review_consent_ref"]}
        if not required.issubset(consents):
            fail("CONSENT_REQUIRED", details={"required_consents": sorted(required)})
        request_digest = digest({"plan_id": plan_id, "plan_digest": plan_digest, "consent_refs": consents})
        journal = self._journal()
        old = journal.by_key(idempotency_key)
        if old:
            if old["request_digest"] != request_digest:
                fail("OPERATION_CONFLICT")
            return self.status(old["operation"]["operation_id"])
        self._supported(plan["action"], plan["target"])
        root = plan["options"].get("install_root", plan["source_manifest"]["product_id"])
        resource_key = digest({"root": os.path.normcase(os.path.abspath(root)), "scope": plan["target"]["scope"]})
        operation = {"api_version": API_VERSION, "operation_id": "op_" + uuid.uuid4().hex,
                     "plan_id": plan_id, "plan_digest": plan_digest, "action": plan["action"],
                     "status": "QUEUED", "cancel_requested": False, "last_sequence": 0,
                     "created_at": now(), "updated_at": now(), "owner": _owner(),
                     "consent_refs": consents, "attempt": 0, "result": None, "error": None}
        lease, run = None, None
        if self.execution_domain is not None:
            try:
                lease = self.execution_domain.reserve("product", operation["operation_id"],
                    journal.lease_binding(idempotency_key, request_digest), participant=self.state_dir)
                operation["operation_id"] = lease.operation_id
            except Exception:
                # A concurrent exact submit may already have published its ACK.
                old = journal.by_key(idempotency_key)
                if old and old["request_digest"] == request_digest:
                    return self.status(old["operation"]["operation_id"])
                raise
        try:
            # The final reviewed-inventory check belongs inside shared
            # ownership, before publication. A prerequisite may have completed
            # between an earlier GUI probe and acquisition of this domain.
            try:
                current = self.discover(plan["source_manifest"], plan["target"], plan["options"])
                if not hmac.compare_digest(current["fingerprint"], plan["inventory_fingerprint"]):
                    fail("PLAN_STALE")
            except BaseException:
                if lease is not None:
                    lease.close(release=not lease.uncertain)
                raise
            # Register under a short lock; synchronous engine/callback work is
            # deliberately outside it so status and cancel stay responsive.
            with _JOBS_LOCK:
                operation, created = journal.create_operation(operation, idempotency_key, request_digest, resource_key)
                if created:
                    if lease is not None:
                        lease.queued()
                    run = self._launch(operation["operation_id"], plan, background=background,
                                       resume=False, lease=lease)
                elif lease is not None:
                    lease.close(release=not lease.uncertain)
            if run is not None:
                run()
        except BaseException:
            if lease is not None:
                lease.close()
            raise
        return self.status(operation["operation_id"])

    def _diagnostics(self, method: str, *args, _strict: bool = False, **kwargs):
        try:
            if self._diagnostic_instance is None:
                from homensai_install.diagnostics import DiagnosticStore
                self._diagnostic_instance = DiagnosticStore(self.state_dir / "diagnostics.sqlite3")
            result = getattr(self._diagnostic_instance, method)(*args, **kwargs)
            if isinstance(result, dict) and result.get("stored") in {"fallback", "unavailable"}:
                self._diagnostic_unavailable = True
            return result
        except Exception:
            if _strict:
                raise
            self._diagnostic_unavailable = True
            return None

    def _emit(self, operation_id: str, event: dict):
        event = bounded_json(event)
        if type(event) is not dict:
            fail("UNEXPECTED_ERROR")
        safe = {key: value for key, value in event.items() if key in EVENT_KEYS}
        from homensai_install.errors import redact
        safe = redact(safe)
        if "type" not in safe:
            safe["type"] = "step.progress"
        event = self._journal().append_event(operation_id, safe)
        self._diagnostics("record_event", operation_id, event)

    def _launch(self, operation_id: str, plan: dict, *, background: bool, resume: bool, decisions=None, lease=None):
        key = (str(self.state_dir), operation_id)
        if background:
            worker = threading.Thread(target=self._run, args=(operation_id, plan),
                                      kwargs={"resume": resume, "decisions": decisions, "lease": lease},
                                      name="hinstall-" + operation_id, daemon=True)
            with _JOBS_LOCK:
                if key in _JOBS and _JOBS[key].is_alive():
                    fail("OPERATION_CONFLICT")
                _JOBS[key] = worker
                worker.start()
        else:
            with _JOBS_LOCK:
                _JOBS[key] = threading.current_thread()
            return lambda: self._run(operation_id, plan, resume=resume, decisions=decisions, lease=lease)

    def _run(self, operation_id: str, plan: dict, *, resume: bool, decisions=None, lease=None):
        started = time.monotonic()
        attempt_id = None
        release_lease = False
        previous = {}
        try:
            journal = self._journal()
            previous = journal.operation(operation_id)
            if previous.get("cancel_requested"):
                safe = lease is None or not lease.uncertain
                journal.update(operation_id, {"status": "CANCELED", "result": {
                                   "status": "CANCELED", "execution_quiescent": safe, "resume_required": not safe}},
                               {"type": "operation.canceled", "status": "CANCELED"}, release=True)
                release_lease = safe
                return
            if lease is not None:
                lease.active()
            operation = journal.update(operation_id, {"status": "RUNNING", "owner": _owner(),
                                                      "attempt": previous.get("attempt", 0) + 1},
                                       {"type": "operation.started", "status": "RUNNING"})
            self._diagnostics("start_operation", plan["action"], operation_id=operation_id,
                              product_id=plan["source_manifest"]["product_id"],
                              product_version=plan["options"].get("product_version") or plan["options"].get("release") or "",
                              target=plan["target"], metadata={"plan_digest": plan["plan_digest"]})
            attempt_id = self._diagnostics("start_attempt", operation_id, kind="resume" if resume else "execution")
            emit = lambda event: self._emit(operation_id, event)
            canceled = lambda: self._journal().operation(operation_id).get("cancel_requested", False)
            if resume:
                execution_plan = deepcopy(plan)
                execution_plan["resume_decisions"] = decisions or {}
                result = self._engine_call("resume", execution_plan, previous, emit, canceled)
            else:
                result = self._engine_call("execute", deepcopy(plan), emit, canceled)
            result = bounded_json(result)
            if type(result) is not dict:
                fail("UNEXPECTED_ERROR")
            result = _functional_result(result, plan)
            status = result.get("status", "SUCCEEDED")
            if status not in TERMINAL | RESUMABLE:
                fail("UNEXPECTED_ERROR")
            result.setdefault("status", status)
            attempt_duration = round((time.monotonic() - started) * 1000)
            prior_duration = previous.get("duration_ms", 0 if previous.get("attempt", 0) == 0 else None)
            cumulative_duration = (prior_duration + attempt_duration) if prior_duration is not None else None
            updates = {"status": status, "result": result, "error": None,
                       "duration_ms": cumulative_duration,
                       "diagnostics_unavailable": self._diagnostic_unavailable}
            journal.update(operation_id, updates, {"type": "operation.finished", "status": status},
                           release=status in TERMINAL)
            if attempt_id:
                self._diagnostics("finish_attempt", attempt_id, status, duration_ms=attempt_duration)
            self._diagnostics("finish_operation", operation_id, status, duration_ms=updates["duration_ms"])
            # Only authoritative completion releases generic engine ownership.
            # CANCELED/FAILED alone do not prove absence of an external child.
            release_lease = (not result.get("resume_required", False) and
                (status in {"SUCCEEDED", "FAILED_ROLLED_BACK"}
                 or (status in {"CANCELED", "FAILED"} and result.get("execution_quiescent") is True)))
        except Exception as error:
            # Нельзя считать произвольный сбой engine безопасно откатанным.
            try:
                from homensai_install.errors import InstallerError
                report = error.to_dict(locale=self.locale) if isinstance(error, InstallerError) else InstallerError("UNEXPECTED_ERROR").to_dict(locale=self.locale)
                failed_status = "CANCELED" if getattr(error, "code", None) == "OPERATION_CANCELED" else "RECOVERY_REQUIRED"
                attempt_duration = round((time.monotonic() - started) * 1000)
                old_duration = previous.get("duration_ms", 0 if previous.get("attempt", 0) == 0 else None)
                total_duration = old_duration + attempt_duration if old_duration is not None else None
                self._journal().update(operation_id, {"status": failed_status, "error": report,
                                                      "duration_ms": total_duration},
                                       {"type": "operation.failed", "status": failed_status, "error": report},
                                       release=failed_status == "CANCELED")
                self._diagnostics("record_error", operation_id, report, attempt_id=attempt_id)
                if attempt_id:
                    self._diagnostics("finish_attempt", attempt_id, failed_status, duration_ms=attempt_duration)
                self._diagnostics("finish_operation", operation_id, failed_status, duration_ms=total_duration)
            except Exception:
                # Если журнал недоступен, прежний RUNNING будет обнаружен после
                # исчезновения owner. Никакого ложного terminal SUCCESS.
                pass
        finally:
            with _JOBS_LOCK:
                key = (str(self.state_dir), operation_id)
                if _JOBS.get(key) is threading.current_thread():
                    _JOBS.pop(key, None)
            if lease is not None:
                try:
                    lease.close(release=release_lease)
                except Exception:
                    # A terminal engine receipt must not erase a failed durable
                    # lease release. Preserve its result for explicit recovery.
                    try:
                        self._journal().update(operation_id, {"status": "RECOVERY_REQUIRED",
                            "execution_lease_recovery_required": True},
                            {"type": "operation.failed", "status": "RECOVERY_REQUIRED",
                             "error": {"code": "STATE_STORAGE_UNAVAILABLE"}})
                    except Exception:
                        pass

    def status(self, operation_id: str) -> dict:
        journal = self._journal()
        result = journal.operation(operation_id)
        if result["status"] in {"RUNNING", "QUEUED"}:
            owner = result.get("owner", {})
            alive = _owner_alive(owner)
            if owner.get("pid") == os.getpid():
                with _JOBS_LOCK:
                    worker = _JOBS.get((str(self.state_dir), operation_id))
                    alive = worker is not None and worker.is_alive()
            if not alive:
                result = journal.mark_interrupted(result)
        return result

    def operations_list(self, product_id: str | None = None, unfinished_only: bool = True,
                        limit: int = 100) -> dict:
        """Найти операции после потери UI-сессии; исполнение автоматически не возобновляется."""
        if (type(limit) is not int or not 1 <= limit <= 200
                or type(unfinished_only) is not bool):
            fail("INVALID_REQUEST")
        if product_id is not None:
            checked_product_id(product_id)
        try:
            journal = Journal(self.state_dir, create=False)
        except FileNotFoundError:
            return {"operations": [], "has_more": False, "limit": limit}
        candidates = journal.operations_list(product_id, unfinished_only=unfinished_only, limit=limit + 1)
        result = []
        for candidate in candidates[:limit]:
            operation = self.status(candidate["operation_id"])
            if (unfinished_only and operation["status"] in TERMINAL
                    and not (operation.get("result") or {}).get("resume_required", False)):
                continue  # Worker успел закончить между SQL-страницей и проверкой owner.
            for field in ("operation_id", "plan_id"):
                checked_id(operation[field], field=field)
            checked_product_id(candidate["product_id"])
            if (operation["action"] not in ALL_ACTIONS
                    or operation["status"] not in TERMINAL | RESUMABLE | {"QUEUED", "RUNNING"}
                    or type(operation["updated_at"]) is not str or len(operation["updated_at"]) > 64):
                fail("RECOVERY_REQUIRED")
            result.append({"operation_id": operation["operation_id"], "plan_id": operation["plan_id"],
                           "action": operation["action"], "status": operation["status"],
                           "product_id": candidate["product_id"], "updated_at": operation["updated_at"]})
        return {"operations": result, "has_more": len(candidates) > limit, "limit": limit}

    def events(self, operation_id: str, after_sequence: int = 0) -> dict:
        if type(after_sequence) is not int or after_sequence < 0:
            fail("INVALID_REQUEST")
        self.status(operation_id)  # Сначала при необходимости отметить interruption.
        return self._journal().event_page(operation_id, after_sequence)

    def cancel(self, operation_id: str) -> dict:
        self.status(operation_id)  # First observe a dead controller if necessary.
        operation, event = self._journal().request_cancel(operation_id)
        if event:
            self._diagnostics("record_event", operation_id, event)
            if operation["status"] == "CANCELED":
                self._diagnostics("finish_operation", operation_id, "CANCELED",
                                  duration_ms=operation.get("duration_ms"))
        return operation

    def resume(self, operation_id: str, decisions: dict | None = None, *, background: bool = True) -> dict:
        if type(background) is not bool:
            fail("INVALID_REQUEST")
        decisions = bounded_json(decisions or {})
        if type(decisions) is not dict:
            fail("INVALID_REQUEST")
        operation = self.status(operation_id)
        journal = self._journal()
        binding = journal.operation_lease_binding(operation_id) if self.execution_domain is not None else None
        result = operation.get("result") or {}
        terminal_status = (result.get("status") if operation.get("execution_lease_recovery_required")
                           else operation["status"])
        terminal_proven = (not result.get("resume_required", False) and
            (terminal_status in {"SUCCEEDED", "FAILED_ROLLED_BACK"}
             or (terminal_status in {"CANCELED", "FAILED"} and result.get("execution_quiescent") is True)))
        if (terminal_proven and self.execution_domain is not None
                and self.execution_domain.pending("product", operation_id, binding) is True):
            # Journal committed before a final observer/crash/release failure.
            # Explicit resume clears only this proven completed intent; no
            # engine execution is repeated by constructor/status/duplicate ACK.
            lease = self.execution_domain.reserve("product", operation_id, binding, resume=True,
                                                  participant=self.state_dir)
            lease.close(release=True)
            if operation.get("execution_lease_recovery_required"):
                journal.update(operation_id, {"status": terminal_status, "execution_lease_recovery_required": False},
                    {"type": "operation.lease_recovered", "status": terminal_status}, release=True)
            return self.status(operation_id)
        if operation["status"] not in RESUMABLE or not callable(getattr(self._engine(), "resume", None)):
            fail("RECOVERY_REQUIRED")
        plan = self.get_plan(operation["plan_id"])
        lease = (self.execution_domain.reserve("product", operation_id, binding, resume=True,
                                               participant=self.state_dir)
                 if self.execution_domain is not None else None)
        try:
            with _JOBS_LOCK:
                worker = _JOBS.get((str(self.state_dir), operation_id))
                if worker is not None and worker.is_alive():
                    fail("OPERATION_CONFLICT")
                journal.claim_for_resume(operation_id, owner=_owner())
                if lease is not None:
                    lease.queued()
                run = self._launch(operation_id, plan, background=background, resume=True,
                                   decisions=decisions, lease=lease)
            if run is not None:
                run()
        except BaseException:
            if lease is not None:
                lease.close()
            raise
        return self.status(operation_id)

    def errors_describe(self, code: str) -> dict:
        from homensai_install.errors import ErrorCatalog
        return ErrorCatalog().describe(code, locale=self.locale)

    def errors_search(self, query: str, *, limit: int = 20) -> list:
        from homensai_install.errors import ErrorCatalog
        return ErrorCatalog().search(query, locale=self.locale, limit=limit)

    def diagnostics_get(self, operation_id: str) -> dict:
        self.status(operation_id)
        value = self._diagnostics("get", operation_id, _strict=True)
        if value is None:
            fail("STATE_STORAGE_UNAVAILABLE")
        return value

    def diagnostics_export_preview(self, operation_id: str | None = None) -> dict:
        if operation_id:
            self.status(operation_id)
        return self._diagnostics("export_preview", operation_id=operation_id, _strict=True)

    def diagnostics_export(self, path: str | Path, *, operation_id: str | None = None,
                           confirmed: bool = False, expected_digest: str | None = None) -> dict:
        if operation_id:
            self.status(operation_id)
        if confirmed is not True or type(expected_digest) is not str or len(expected_digest) != 64:
            fail("CONSENT_REQUIRED")
        value = self._diagnostics("export", path, operation_id=operation_id, confirmed=confirmed,
                                  expected_digest=expected_digest, _strict=True)
        if value is None:
            fail("STATE_STORAGE_UNAVAILABLE")
        return value

    def statistics_query(self, **filters) -> dict:
        if (set(filters) - {"product_id", "product_version", "action", "status", "target", "since", "until"}
                or any(type(value) is not str for value in filters.values())):
            fail("INVALID_REQUEST")
        value = self._diagnostics("query_statistics", _strict=True, **filters)
        if value is None:
            fail("STATE_STORAGE_UNAVAILABLE")
        return value

    def contract_test(self, manifest: dict | str | Path, target: dict | None = None, options: dict | None = None) -> dict:
        validated = self.validate(manifest)
        discovered = self.discover(manifest, target, options)
        return {"status": "PASS", "level": "read-only-contract", "lifecycle_proven": False,
                "manifest_digest": validated["manifest_digest"], "inventory_fingerprint": discovered["fingerprint"],
                "capabilities": self.capabilities(target)}


__all__ = ["InstallerAPI", "ProductAdapter", "ALL_ACTIONS"]
