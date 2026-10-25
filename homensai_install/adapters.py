"""Явный ProductAdapter JSON protocol v1 поверх проверенного signed payload.

Модуль не реализует установку/обновление/архивацию и не вызывается из plan.
Подтверждённый replay хранится отдельно от lifecycle journal; неопределённый
результат эффекта никогда не превращается в автоматическую повторную попытку.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import platform
import re
import signal
import sqlite3
import stat
import subprocess
import threading
import time
from typing import Mapping
import uuid

from .api.journal import now, owned_directory
from .api.model import (ProductAdapter, bounded_json, canonical, checked_id,
                        checked_product_id, digest, parse_json, read_manifest, target_model)
from .errors import InstallerError, redact
from .trust import SignedPayloadVerifier
from shared.datahome import checked_path, contains, DataHomeError


PROTOCOL_VERSION = "1.0"
MAX_INPUT_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_STDERR_BYTES = 64 * 1024
HOOKS = {"describe": False, "health": False, "readiness": False,
         "migration_plan": False, "operation_status": False, "quiesce": True,
         "migration_apply": True, "backup": True, "restore": True,
         "post_restore": True, "activate": True}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_ENV_DENIED = {"PATH", "HOME", "USERPROFILE", "COMSPEC", "PATHEXT", "SYSTEMROOT", "WINDIR",
               "BASH_ENV", "ENV", "IFS", "SHELLOPTS", "CDPATH"}


def _fail(code: str, reason: str) -> None:
    raise InstallerError(code, phase="product_adapter", details={"reason": reason})


def _relative(value: str) -> str:
    if (type(value) is not str or not value or len(value) > 1024 or "\\" in value
            or value.startswith("/") or any(ord(c) < 32 for c in value)
            or ":" in value or any(part in {"", ".", ".."} for part in value.split("/"))):
        _fail("INVALID_REQUEST", "adapter_relative_path")
    return value


def _safe_name(value, *, maximum=128) -> bool:
    return type(value) is str and len(value) <= maximum and _NAME.fullmatch(value) is not None


def _strict_keys(value, required, optional=()):
    if type(value) is not dict or set(value) - (set(required) | set(optional)) or set(required) - set(value):
        _fail("ADAPTER_PROTOCOL_INVALID", "unexpected_fields")


def _process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class _Ledger:
    def __init__(self, directory):
        self.directory = owned_directory(Path(directory), create=True)
        self.path = self.directory / "adapter-invocations.sqlite3"
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            _fail("STATE_STORAGE_UNAVAILABLE", "adapter_ledger_permissions")
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
                operation_id TEXT NOT NULL, hook TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT, owner_pid INTEGER NOT NULL, updated_at TEXT NOT NULL)
            """)

    @contextmanager
    def transaction(self):
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except sqlite3.Error:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            _fail("STATE_STORAGE_UNAVAILABLE", "adapter_ledger_unavailable")
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            if connection is not None:
                connection.close()

    def get(self, key):
        with self.transaction() as db:
            return self._read(db, key)

    @staticmethod
    def _read(db, key):
        row = db.execute("SELECT request_digest,operation_id,hook,state,result,owner_pid FROM calls WHERE call_id=?",
                         (key,)).fetchone()
        if row is None:
            return None
        return {"request_digest": row[0], "operation_id": row[1], "hook": row[2], "state": row[3],
                "result": parse_json(row[4]) if row[4] is not None else None, "owner_pid": row[5]}

    def begin(self, key, request_digest, operation_id, hook, *, resume):
        with self.transaction() as db:
            old = self._read(db, key)
            if old:
                if old["request_digest"] != request_digest:
                    _fail("OPERATION_CONFLICT", "adapter_idempotency_key_reused")
                if old["state"] == "SUCCEEDED":
                    return old["result"]
                if old["state"] != "NOT_APPLIED" or not resume:
                    code = "OPERATION_CONFLICT" if old["state"] == "RUNNING" and _process_alive(old["owner_pid"]) else "RECOVERY_REQUIRED"
                    _fail(code, "adapter_outcome_requires_reconciliation")
                db.execute("UPDATE calls SET state='RUNNING',owner_pid=?,updated_at=? WHERE call_id=?",
                           (os.getpid(), now(), key))
            else:
                db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
                           (key, request_digest, operation_id, hook, "RUNNING", None, os.getpid(), now()))
        return None

    def finish(self, key, state, result=None):
        with self.transaction() as db:
            db.execute("UPDATE calls SET state=?,result=?,updated_at=? WHERE call_id=?",
                       (state, canonical(result) if result is not None else None, now(), key))

    def claim_reconciliation(self, key):
        with self.transaction() as db:
            old = self._read(db, key)
            if old is None:
                _fail("OPERATION_NOT_FOUND", "adapter_invocation_missing")
            if old["state"] in {"RUNNING", "RECONCILING"} and _process_alive(old["owner_pid"]):
                _fail("OPERATION_CONFLICT", "adapter_invocation_still_running")
            if old["state"] != "SUCCEEDED":
                db.execute("UPDATE calls SET state='RECONCILING',owner_pid=?,updated_at=? WHERE call_id=?",
                           (os.getpid(), now(), key))
            return old


class ExecutableProductAdapter:
    """ProductAdapter-compatible execution boundary; lifecycle остаётся в NativeEngine.

    Constructor/capabilities только читают подписанные данные. invoke/reconcile
    запускают код лишь по явному запросу; Linux process group ограничивает timeout.
    Windows execution пока явно unsupported, пока не принят Job Object backend.
    """

    def __init__(self, manifest: dict, target: dict, options: dict, *,
                 verifier: SignedPayloadVerifier, state_dir: str | Path,
                 descriptor: str = "installer/adapter.json", environment: Mapping[str, str] | None = None,
                 timeout: float = 30):
        if not isinstance(verifier, SignedPayloadVerifier):
            _fail("INTEGRITY_FAILED", "signed_payload_verifier_required")
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            _fail("INVALID_REQUEST", "adapter_timeout_range")
        self.manifest = read_manifest(manifest)
        self.target = target_model(target)
        self.options = bounded_json(options)
        self.verifier = verifier
        self.state_dir = Path(state_dir).absolute()
        self.descriptor_path = _relative(descriptor)
        self.timeout = float(timeout)
        if environment is not None and not isinstance(environment, Mapping):
            _fail("INVALID_REQUEST", "adapter_environment_mapping")
        self.environment = dict(environment or {})
        self._descriptor, self._inventory = self._verified_descriptor()
        self._descriptor_digest = digest(self._descriptor)
        self._validate_environment()

    def _verified_descriptor(self):
        inventory = self.verifier(self.manifest, self.target, self.options)
        if self.descriptor_path not in inventory:
            _fail("INTEGRITY_FAILED", "adapter_descriptor_not_signed")
        try:
            root = checked_path(Path(self.options["payload_dir"]).absolute())
            descriptor_path = checked_path(root / self.descriptor_path)
            with descriptor_path.open("rb") as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
            if (len(raw) > MAX_INPUT_BYTES or hashlib.sha256(raw).hexdigest() != inventory[self.descriptor_path]):
                _fail("INTEGRITY_FAILED", "adapter_descriptor_hash")
            descriptor = parse_json(raw, error_code="ADAPTER_PROTOCOL_INVALID")
            _strict_keys(descriptor, {"schema_version", "protocol_version", "product_id", "adapter_id",
                                      "adapter_version", "command", "capabilities", "environment_allowlist"})
            if (type(descriptor["schema_version"]) is not int or descriptor["schema_version"] != 1
                    or descriptor["protocol_version"] != PROTOCOL_VERSION):
                _fail("API_UNSUPPORTED", "adapter_protocol_version")
            if descriptor["product_id"] != self.manifest["product_id"]:
                _fail("INTEGRITY_FAILED", "adapter_product_mismatch")
            if not _safe_name(descriptor["adapter_id"]) or not _safe_name(descriptor["adapter_version"]):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_identity")
            command = descriptor["command"]
            if (type(command) is not list or not 1 <= len(command) <= 64
                    or any(type(arg) is not str or not arg or len(arg) > 4096 or "\0" in arg for arg in command)
                    or sum(len(arg) for arg in command) > 32768):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_argv")
            runtime = _relative(command[0])
            if runtime != self.options["runtime_executable"] or runtime not in inventory:
                _fail("INTEGRITY_FAILED", "adapter_runtime_not_signed")
            caps = descriptor["capabilities"]
            if type(caps) is not dict or not caps or set(caps) - set(HOOKS) or "describe" not in caps:
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_capability_set")
            for hook, policy in caps.items():
                _strict_keys(policy, {"mutating", "idempotency"})
                if type(policy["mutating"]) is not bool or policy["mutating"] != HOOKS[hook]:
                    _fail("ADAPTER_PROTOCOL_INVALID", "adapter_mutation_policy")
                if policy["idempotency"] not in {"read_only", "application", "none"}:
                    _fail("ADAPTER_PROTOCOL_INVALID", "adapter_replay_policy")
                if ((not HOOKS[hook] and policy["idempotency"] != "read_only")
                        or (HOOKS[hook] and policy["idempotency"] == "read_only")
                        or (policy["idempotency"] == "application" and "operation_status" not in caps)):
                    _fail("ADAPTER_PROTOCOL_INVALID", "adapter_replay_policy")
            allow = descriptor["environment_allowlist"]
            if (type(allow) is not list or len(allow) > 64 or len(set(allow)) != len(allow)
                    or any(type(name) is not str or not _ENV.fullmatch(name) for name in allow)):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_environment_allowlist")
            for name in allow:
                if name in _ENV_DENIED or name.startswith(("LD_", "DYLD_", "PYTHON", "GIT_", "NODE_", "RUBY", "PERL")):
                    _fail("ADAPTER_PROTOCOL_INVALID", "adapter_loader_environment_forbidden")
            return descriptor, inventory
        except InstallerError:
            raise
        except (KeyError, OSError, ValueError, TypeError, DataHomeError):
            _fail("INTEGRITY_FAILED", "adapter_payload_unavailable")

    def _validate_environment(self):
        if set(self.environment) - set(self._descriptor["environment_allowlist"]):
            _fail("INVALID_REQUEST", "adapter_environment_not_declared")
        if any(type(value) is not str or len(value) > 32768 or "\0" in value for value in self.environment.values()):
            _fail("INVALID_REQUEST", "adapter_environment_value")

    def capabilities(self) -> Mapping[str, object]:
        # Не исполняет даже describe: plan/validate могут безопасно читать статический контракт.
        descriptor, _ = self._verified_descriptor()
        if digest(descriptor) != self._descriptor_digest:
            _fail("INTEGRITY_FAILED", "adapter_descriptor_changed")
        return {"protocol_version": PROTOCOL_VERSION, "product_id": descriptor["product_id"],
                "adapter_id": descriptor["adapter_id"], "adapter_version": descriptor["adapter_version"],
                "hooks": deepcopy(descriptor["capabilities"]), "capability_source": "signed_descriptor",
                "readiness": "development", "execution_platforms": ["linux"],
                "execution_mode": "native", "execution_scope": "user"}

    def _identity(self, key):
        if type(key) is not str or not _safe_name(key):
            _fail("INVALID_REQUEST", "adapter_idempotency_key")
        return digest({"product_id": self.manifest["product_id"], "adapter_id": self._descriptor["adapter_id"], "key": key})

    def _request(self, hook, request):
        if type(hook) is not str:
            _fail("INVALID_REQUEST", "adapter_hook_name")
        if hook not in self._descriptor["capabilities"] or hook == "operation_status":
            _fail("CAPABILITY_UNSUPPORTED", "adapter_hook_not_declared")
        request = bounded_json(dict(request) if isinstance(request, Mapping) else request)
        _strict_keys(request, {"operation_id", "idempotency_key", "phase", "data"})
        checked_id(request["operation_id"], field="operation_id")
        self._identity(request["idempotency_key"])
        if request["phase"] not in {"execute", "resume", "diagnostic"}:
            _fail("INVALID_REQUEST", "adapter_not_callable_during_plan_or_validate")
        if request["phase"] == "diagnostic" and HOOKS[hook]:
            _fail("INVALID_REQUEST", "adapter_mutation_in_diagnostics")
        if type(request["data"]) is not dict:
            _fail("INVALID_REQUEST", "adapter_request_data")
        if hook == "migration_apply":
            if (not _HASH.fullmatch(str(request["data"].get("plan_digest", "")))
                    or not _safe_name(request["data"].get("backup_receipt_id"))):
                _fail("INVALID_REQUEST", "migration_plan_and_backup_receipt_required")
        if len(canonical(request).encode()) > MAX_INPUT_BYTES - 2048:
            _fail("INVALID_REQUEST", "adapter_request_too_large")
        request_digest = digest({"hook": hook, "operation_id": request["operation_id"], "data": request["data"],
                                 "descriptor_digest": self._descriptor_digest, "release": self.options["release"]})
        return request, request_digest

    def invoke(self, hook: str, request: Mapping[str, object]) -> Mapping[str, object]:
        request, request_digest = self._request(hook, request)
        self.capabilities()  # Полная повторная проверка подписи/байтов до записи intent и exec.
        self._execution_context()
        ledger = _Ledger(self.state_dir)
        key = self._identity(request["idempotency_key"])
        old_result = ledger.begin(key, request_digest, request["operation_id"], hook,
                                  resume=request["phase"] == "resume")
        if old_result is not None:
            return {"hook": hook, "request_digest": request_digest, "result": old_result, "replayed": True}
        try:
            result = self._execute(hook, request, request_digest)
            ledger.finish(key, "SUCCEEDED", result)
            return {"hook": hook, "request_digest": request_digest, "result": result, "replayed": False}
        except BaseException as error:
            # Timeout/nonzero/malformed output не доказывают отсутствия изменений.
            ledger.finish(key, "UNKNOWN")
            if isinstance(error, Exception) and not isinstance(error, InstallerError):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_transport_or_response_invalid")
            raise

    def reconcile(self, idempotency_key: str) -> Mapping[str, object]:
        self.capabilities()
        if "operation_status" not in self._descriptor["capabilities"]:
            _fail("CAPABILITY_UNSUPPORTED", "adapter_operation_status_not_declared")
        key = self._identity(idempotency_key)
        ledger = _Ledger(self.state_dir)
        old = ledger.claim_reconciliation(key)
        if old["state"] == "SUCCEEDED":
            return {"outcome": "applied", "result": old["result"], "replayed": True}
        request = {"operation_id": old["operation_id"], "idempotency_key": idempotency_key,
                   "phase": "diagnostic", "data": {"hook": old["hook"], "request_digest": old["request_digest"]}}
        try:
            result = self._execute("operation_status", request, old["request_digest"])
            if result["request_digest"] != old["request_digest"]:
                _fail("ADAPTER_PROTOCOL_INVALID", "reconciliation_digest_mismatch")
            if result["outcome"] == "applied":
                completed = self._validate_result(old["hook"], result["result"])
                ledger.finish(key, "SUCCEEDED", completed)
            elif result["outcome"] == "not_applied":
                ledger.finish(key, "NOT_APPLIED")
            else:
                ledger.finish(key, "UNKNOWN")
            return result
        except BaseException as error:
            ledger.finish(key, "UNKNOWN")
            if isinstance(error, Exception) and not isinstance(error, InstallerError):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_reconciliation_invalid")
            raise

    def _execution_context(self):
        try:
            return self._execution_context_checked()
        except InstallerError:
            raise
        except (OSError, DataHomeError, TypeError, ValueError):
            _fail("INTEGRITY_FAILED", "adapter_execution_context_unavailable")

    def _execution_context_checked(self):
        if self.target["mode"] != "native" or self.target["scope"] != "user":
            _fail("CAPABILITY_UNSUPPORTED", "adapter_native_user_scope_only")
        if self.target["os"] != "linux" or platform.system() != "Linux" or not hasattr(os, "WNOWAIT"):
            _fail("CAPABILITY_UNSUPPORTED", "adapter_process_group_backend_linux_only")
        host = target_model()
        if self.target["arch"] != host["arch"]:
            _fail("CAPABILITY_UNSUPPORTED", "adapter_architecture_mismatch")
        root = checked_path(Path(self.options["payload_dir"]).absolute())
        if contains(root, self.state_dir) or contains(self.state_dir, root):
            _fail("INVALID_REQUEST", "adapter_ledger_overlaps_payload")
        for path in [root, *root.rglob("*")]:
            info = path.lstat()
            if (info.st_uid != os.getuid() or info.st_mode & 0o022
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                _fail("INTEGRITY_FAILED", "adapter_payload_must_be_owned_and_not_shared_writable")
        executable = checked_path(root / self._descriptor["command"][0])
        with executable.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                _fail("INTEGRITY_FAILED", "adapter_requires_signed_native_runtime_not_shebang")
        if not os.access(executable, os.X_OK):
            _fail("PREREQUISITE_MISSING", "adapter_runtime_not_executable")
        return root, [str(executable), *self._descriptor["command"][1:]]

    def _execute(self, hook, request, request_digest):
        self.capabilities()
        root, argv = self._execution_context()
        request_id = "adapter_" + uuid.uuid4().hex
        envelope = {"protocol_version": PROTOCOL_VERSION, "product_id": self.manifest["product_id"],
                    "request_id": request_id, "request_digest": request_digest, "hook": hook,
                    **request}
        payload = canonical(envelope).encode() + b"\n"
        if len(payload) > MAX_INPUT_BYTES:
            _fail("INVALID_REQUEST", "adapter_request_too_large")
        # Только явно переданные и объявленные значения; no inherited PATH/loader variables/secrets.
        env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1",
               "PYTHONDONTWRITEBYTECODE": "1", **self.environment}
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=root, env=env, shell=False,
                                       start_new_session=True, close_fds=True)
        except OSError:
            _fail("ADAPTER_EXECUTION_FAILED", "adapter_process_start")
        stdout = bytearray()
        overflow = threading.Event()
        read_error = threading.Event()

        def consume(stream, maximum, retain):
            count = 0
            try:
                while block := stream.read(8192):
                    count += len(block)
                    if count > maximum:
                        overflow.set()
                        return
                    if retain:
                        stdout.extend(block)
            except (OSError, ValueError):
                read_error.set()
            finally:
                stream.close()

        def send():
            try:
                process.stdin.write(payload)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        threads = [threading.Thread(target=consume, args=(process.stdout, MAX_OUTPUT_BYTES, True), daemon=True),
                   threading.Thread(target=consume, args=(process.stderr, MAX_STDERR_BYTES, False), daemon=True),
                   threading.Thread(target=send, daemon=True)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout
        reason = None
        leader_exited = False
        try:
            while True:
                # Не reaping через poll(): резервируем PID/group до завершения cleanup,
                # чтобы PID reuse не превратил killpg в сигнал чужому процессу.
                if not leader_exited:
                    observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    leader_exited = observed is not None
                if leader_exited and not any(thread.is_alive() for thread in threads):
                    break
                if overflow.is_set():
                    reason = "output_limit"
                    break
                if time.monotonic() >= deadline:
                    reason = "deadline"
                    break
                time.sleep(0.01)
        finally:
            # Также закрыть оставшихся потомков после выхода launcher: adapter — конечный hook, не server.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=1)
        if reason == "deadline":
            _fail("ADAPTER_TIMEOUT", "adapter_deadline_exceeded_outcome_unknown")
        if reason or overflow.is_set() or read_error.is_set():
            _fail("ADAPTER_PROTOCOL_INVALID", "adapter_bounded_stream_violation")
        if process.returncode:
            _fail("ADAPTER_EXECUTION_FAILED", "adapter_nonzero_exit_outcome_unknown")
        response = parse_json(bytes(stdout), error_code="ADAPTER_PROTOCOL_INVALID")
        _strict_keys(response, {"protocol_version", "product_id", "request_id", "request_digest", "hook", "status"},
                     {"result", "error"})
        if (response["protocol_version"] != PROTOCOL_VERSION or response["product_id"] != self.manifest["product_id"]
                or response["request_id"] != request_id or response["request_digest"] != request_digest
                or response["hook"] != hook):
            _fail("ADAPTER_PROTOCOL_INVALID", "adapter_response_identity")
        if response["status"] == "error":
            if "result" in response:
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_ambiguous_response")
            _fail("ADAPTER_EXECUTION_FAILED", "adapter_reported_failure_outcome_unknown")
        if response["status"] != "ok" or "error" in response or "result" not in response:
            _fail("ADAPTER_PROTOCOL_INVALID", "adapter_response_status")
        return self._validate_result(hook, response["result"])

    def _validate_result(self, hook, result):
        if hook == "describe":
            _strict_keys(result, {"protocol_version", "product_id", "adapter_version", "capabilities"})
            if (result["protocol_version"] != PROTOCOL_VERSION or result["product_id"] != self.manifest["product_id"]
                    or result["adapter_version"] != self._descriptor["adapter_version"]
                    or result["capabilities"] != sorted(self._descriptor["capabilities"])):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_handshake_mismatch")
        elif hook in {"health", "readiness"}:
            _strict_keys(result, {"ready", "checks"})
            if type(result["ready"]) is not bool or type(result["checks"]) is not list or len(result["checks"]) > 128:
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_health_shape")
            for check in result["checks"]:
                _strict_keys(check, {"id", "status"}, {"message_code"})
                if (not _safe_name(check["id"]) or check["status"] not in {"ok", "failed", "unknown"}
                        or ("message_code" in check and not _safe_name(check["message_code"]))):
                    _fail("ADAPTER_PROTOCOL_INVALID", "adapter_health_check")
            if result["ready"] and (not result["checks"] or any(c["status"] != "ok" for c in result["checks"])):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_health_inconsistent")
        elif hook == "migration_plan":
            _strict_keys(result, {"from_version", "to_version", "steps", "plan_digest"})
            if (not _safe_name(result["from_version"]) or not _safe_name(result["to_version"])
                    or type(result["steps"]) is not list or len(result["steps"]) > 128):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_migration_plan")
            names = set()
            for step in result["steps"]:
                _strict_keys(step, {"id", "backup_required", "reversible"})
                if (not _safe_name(step["id"]) or step["id"] in names
                        or type(step["backup_required"]) is not bool or type(step["reversible"]) is not bool):
                    _fail("ADAPTER_PROTOCOL_INVALID", "adapter_migration_step")
                names.add(step["id"])
            if result["plan_digest"] != digest({key: value for key, value in result.items() if key != "plan_digest"}):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_migration_plan_digest")
        elif hook == "operation_status":
            _strict_keys(result, {"outcome", "request_digest"}, {"result"})
            if result["outcome"] not in {"applied", "not_applied", "unknown"} or not _HASH.fullmatch(str(result["request_digest"])):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_reconciliation_shape")
            if ("result" in result) != (result["outcome"] == "applied"):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_reconciliation_result")
        elif hook == "quiesce":
            _strict_keys(result, {"quiesced", "active_writers"})
            if (type(result["quiesced"]) is not bool or type(result["active_writers"]) is not int
                    or not 0 <= result["active_writers"] <= 100000
                    or (result["quiesced"] and result["active_writers"] != 0)):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_quiesce_shape")
        elif hook == "backup":
            _strict_keys(result, {"artifact_id", "sha256"})
            if not _safe_name(result["artifact_id"]) or not _HASH.fullmatch(str(result["sha256"])):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_backup_receipt")
        else:
            _strict_keys(result, {"applied", "receipt_id"}, {"state_version"})
            if (type(result["applied"]) is not bool or not result["applied"]
                    or not _safe_name(result["receipt_id"])
                    or ("state_version" in result and not _safe_name(result["state_version"]))):
                _fail("ADAPTER_PROTOCOL_INVALID", "adapter_mutation_receipt")
        known = tuple(value for value in self.environment.values() if len(value) >= 8)
        if redact(result, known_secrets=known) != result:
            _fail("ADAPTER_PROTOCOL_INVALID", "adapter_response_contains_sensitive_values")
        return deepcopy(result)


__all__ = ["ExecutableProductAdapter", "ProductAdapter", "PROTOCOL_VERSION"]
