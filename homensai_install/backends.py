# HomenS.AI · © 2026 Serhii Khomenko
"""Явный выбор платформенного engine для одного журнала публичного API.

Registry создаёт доверенный bootstrap/SDK. HTTP options никогда не выбирают
Python class, executable, verifier или daemon endpoint. Отсутствующий backend
не заменяется автоматически native и не объявляется поддержанным.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import MappingProxyType

from .api.model import parse_json, target_model
from .engine import EngineError, NativeEngine

_ACTIONS = ("build", "install", "update", "modify_components", "repair", "reinstall",
            "uninstall", "backup", "restore", "rollback", "start", "stop")
_BINDING = "backend_routing"


class BackendRouter:
    """Dispatch только по зафиксированным mode/scope и сохранённому plan target.

    Старые CP-005 native/user планы допустимы на прежнем native engine. Старый
    прямой Docker plan требует прежнего explicit SDK engine либо нового review;
    отсутствие routing marker не даёт права переключать реализацию на resume.
    """

    def __init__(self, *, native=None, docker=None, native_service=None):
        entries = {("native", "user"): native if native is not None else NativeEngine()}
        if docker is not None:
            entries[("docker", "user")] = docker
        if native_service is not None:
            entries[("native", "service")] = native_service
        for backend in entries.values():
            if any(not callable(getattr(backend, name, None))
                   for name in ("capabilities", "discover", "plan", "execute", "resume")):
                raise EngineError("INVALID_REQUEST", "Backend не реализует контракт engine")
        self._backends = MappingProxyType(entries)

    def _select(self, target, *, required=True):
        target = target_model(target)
        backend = self._backends.get((target["mode"], target["scope"]))
        if backend is None and required:
            raise EngineError("CAPABILITY_UNSUPPORTED", details={"reason": "backend_not_configured"})
        return target, backend

    @staticmethod
    def _binding(target):
        return {"version": 1, "id": target["mode"] + ":" + target["scope"],
                "target": dict(target)}

    def capabilities(self, target):
        target, backend = self._select(target, required=False)
        if backend is None:
            return {"target": target, "actions": {
                action: {"supported": False, "readiness": "unavailable",
                         "reason": "CAPABILITY_UNSUPPORTED"} for action in _ACTIONS},
                "backend_configured": False, "production_ready": False}
        result = deepcopy(backend.capabilities(target))
        return {**result, "backend_configured": True, "backend": self._binding(target)}

    def discover(self, manifest, target, options):
        target, backend = self._select(target)
        # Keep the engine's own fingerprint shape: Docker binds its reviewed
        # resource_fingerprint to this exact structure. Routing is in plan digest.
        return backend.discover(manifest, target, options)

    def status(self, manifest, target, options):
        target, backend = self._select(target)
        method = getattr(backend, "status", backend.discover)
        return method(manifest, target, options)

    def plan(self, action, manifest, target, inventory, options):
        target, backend = self._select(target)
        result = deepcopy(backend.plan(action, manifest, target, inventory, options))
        if type(result) is not dict or _BINDING in result:
            raise EngineError("INTEGRITY_FAILED", details={"reason": "reserved_backend_binding"})
        return {**result, _BINDING: self._binding(target)}

    def _planned(self, plan):
        if type(plan) is not dict or type(plan.get("target")) is not dict:
            raise EngineError("INVALID_REQUEST")
        target, backend = self._select(plan["target"])
        engine_plan = plan.get("engine_plan", {})
        if type(engine_plan) is not dict:
            raise EngineError("INTEGRITY_FAILED")
        marker = engine_plan.get(_BINDING)
        if marker is None:
            if (target["mode"], target["scope"]) != ("native", "user"):
                raise EngineError("PLAN_STALE", details={"reason": "backend_review_required"})
        elif marker != self._binding(target):
            raise EngineError("PLAN_STALE", details={"reason": "backend_binding_changed"})
        return backend

    def execute(self, plan, emit=lambda event: None, cancel_requested=lambda: False):
        return self._planned(plan).execute(plan, emit, cancel_requested)

    def resume(self, plan, previous_operation, emit=lambda event: None,
               cancel_requested=lambda: False):
        return self._planned(plan).resume(plan, previous_operation, emit, cancel_requested)


def configured_backends(*, verifier=None, secret_provider=None, development_runtime=None,
                        docker_cli_config=None):
    """Bootstrap configuration; does not discover/launch Docker implicitly.

    docker_cli_config is a local developer/administrator file with exactly the
    four DockerCLI pins. It is not accepted from browser input or plan options.
    Constructor verifies executable bytes and private config; capabilities later
    probes the explicitly configured local daemon. Missing Docker is independent
    of native operations; a supplied invalid configuration fails explicitly.
    """
    native = NativeEngine(verifier=verifier, secret_provider=secret_provider,
                          development_runtime=development_runtime)
    docker = None
    if docker_cli_config is not None:
        from shared.datahome import checked_path
        from .docker_cli import DockerCLI
        from .docker_engine import DockerEngine
        try:
            file = checked_path(Path(docker_cli_config).absolute())
            if not file.is_file() or file.stat().st_size > 16384:
                raise EngineError("INVALID_REQUEST", details={"reason": "docker_bootstrap_config_invalid"})
            with file.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise EngineError("INVALID_REQUEST", details={"reason": "docker_bootstrap_config_invalid"})
            data = parse_json(raw)
            if (type(data) is not dict or set(data) != {"executable", "sha256", "endpoint", "config_dir"}
                    or any(type(value) is not str or not value or len(value) > 4096 for value in data.values())):
                raise EngineError("INVALID_REQUEST", details={"reason": "docker_bootstrap_config_invalid"})
            runner = DockerCLI(**data)
        except EngineError:
            raise
        except (OSError, TypeError, ValueError):
            raise EngineError("INVALID_REQUEST", details={"reason": "docker_bootstrap_config_unavailable"}) from None
        docker = DockerEngine(verifier=verifier, secret_provider=secret_provider, runner=runner)
    return BackendRouter(native=native, docker=docker)
