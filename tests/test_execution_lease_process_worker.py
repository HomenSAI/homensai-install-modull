"""Отдельный interpreter для synthetic lease acceptance, только own temp files.

This is a test helper, not an installer entrypoint. Every command below is
fixed by tests; it never runs OS packages, product hooks or network transport.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import select
import sys
import threading


def main():
    source, directory, command, kind, phase, mode = sys.argv[1:]
    source, directory = Path(source), Path(directory)
    sys.path[:0] = [str(source), str(source / "tests")]
    from cryptography.hazmat.primitives import serialization
    from homensai_install.api.journal import Journal
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.downloads import DownloadManager
    from homensai_install.errors import InstallerError
    from homensai_install.execution_lease import ExecutionDomain
    from homensai_install.trust import SignedPayloadVerifier
    from test_prerequisites_execution import (
        Transport, bundle as fixture_bundle, coordinator, present,
    )
    from test_public_api import Engine, submit

    role = "holder" if command in {"hold", "duplicate", "cancel", "resume"} else "contender"
    base = directory / role
    config_path = base / "fixture.json"
    if not config_path.exists():
        base.mkdir(parents=True, exist_ok=True)
        values = fixture_bundle.__wrapped__(base)
        public = values["private"].public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        config = {key: deepcopy(values[key]) for key in (
            "manifest", "target", "options", "descriptor", "inventory", "markers")}
        config["public"] = public.hex()
        config["files"] = {key: value.hex() for key, value in values["transport"].files.items()}
        config_path.write_text(json.dumps(config), encoding="utf-8")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    values = {key: deepcopy(config[key]) for key in (
        "manifest", "target", "options", "descriptor", "inventory", "markers")}
    values.update(tmp_path=base, payload=base / "payload", state_dir=base / "prerequisite-state",
                  transport=Transport({key: bytes.fromhex(value) for key, value in config["files"].items()}),
                  calls=[], results={}, effects={}, verifier=SignedPayloadVerifier(
                      {"synthetic-test": bytes.fromhex(config["public"])}, allow_test_keys=True))
    values["downloads"] = DownloadManager(base / "cache", trusted_hosts=["downloads.example.org"],
                                           transport=values["transport"], retries=0)
    domain = ExecutionDomain(directory / "common-domain", owner_id="original-user-1000")
    gated = False
    operation_id = "prerequisite_" + role

    def output(value):
        print(json.dumps(value), flush=True)

    def gate(observed_phase):
        nonlocal gated
        if command != "hold" or gated or phase != observed_phase:
            return
        gated = True
        output({"event": "gate", "kind": kind, "phase": observed_phase,
                "operation_id": operation_id, "pid": os.getpid(), "busy": domain.busy()})
        ready, _, _ = select.select([sys.stdin], [], [], 15)
        if not ready:
            raise AssertionError("Parent did not release its own synthetic worker")
        decision = sys.stdin.readline().strip()
        if decision == "crash":
            os._exit(73)
        if decision != "release":
            raise AssertionError("Unknown synthetic test gate command")

    def observe(event):
        if mode == "background" and event.get("message_code") == "prerequisite_background_finished":
            gate("terminal")
        elif mode == "synchronous" and event.get("status") == "READY":
            gate("terminal")

    def prerequisite_effect():
        (base / "effect-entered").write_text("prerequisite", encoding="utf-8")
        gate("effect")
        present(values, "base")

    values["effects"]["base"] = prerequisite_effect
    port = coordinator(values, execution_domain=domain, event_callback=observe)

    class ProductEngine(Engine):
        def execute(self, plan, emit, canceled):
            (base / "effect-entered").write_text("product", encoding="utf-8")
            gate("effect")
            return {"status": "CANCELED" if canceled() else "SUCCEEDED"}

    engine = ProductEngine(base)
    # No explicit API domain: it must inherit the trusted prerequisite domain.
    api = InstallerAPI(base / "product-state", engine=engine, prerequisites=port)
    workers = []
    if command == "hold" and mode == "background":
        original_thread_start = threading.Thread.start

        def start_thread(worker, *args, **kwargs):
            if worker.name.startswith("hinstall-"):
                workers.append(worker)
            return original_thread_start(worker, *args, **kwargs)

        threading.Thread.start = start_thread
    plan_path = base / (kind + "-plan.json")
    if command in {"duplicate", "cancel", "resume"}:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    elif kind == "product":
        plan = api.plan("install", values["manifest"], values["target"],
                        options={"install_root": str(base / "install")})
    else:
        plan = api.prerequisites_plan({"base": "auto", "client": "auto"})
    if command == "hold":
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

    if command == "hold" and kind == "product":
        original_create = Journal.create_operation
        original_launch = api._launch
        original_diagnostics = api._diagnostics

        def create(self, operation, *args, **kwargs):
            nonlocal operation_id
            operation_id = operation["operation_id"]
            gate("enqueue")
            return original_create(self, operation, *args, **kwargs)

        def launch(*args, **kwargs):
            gate("queued")
            return original_launch(*args, **kwargs)

        def diagnostics(method, *args, **kwargs):
            result = original_diagnostics(method, *args, **kwargs)
            if method == "finish_operation":
                gate("terminal")
            return result

        Journal.create_operation = create
        api._launch = launch
        api._diagnostics = diagnostics
    elif command == "hold":
        original_save = port.store.save

        def save(operation, message_code, **kwargs):
            if kwargs.get("create"):
                gate("enqueue")
            return original_save(operation, message_code, **kwargs)

        port.store.save = save
        if phase == "queued":
            from homensai_install import prerequisite_async
            original_launch = prerequisite_async._Worker.launch

            def launch(worker):
                gate("queued")
                return original_launch(worker)

            prerequisite_async._Worker.launch = launch

    try:
        if command in {"cancel", "resume"}:
            if kind == "product":
                existing = api._journal().by_key("same-request")
                operation_id = existing["operation"]["operation_id"]
                result = (api.cancel(operation_id) if command == "cancel" else
                          api.resume(operation_id, background=False))
            else:
                result = (api.prerequisites_cancel(operation_id) if command == "cancel" else
                          api.prerequisites_resume(operation_id, background=False))
        elif kind == "product":
            result = submit(api, plan, "same-request", background=mode == "background")
        else:
            result = api.prerequisites_start(operation_id, plan, plan["required_consents"],
                                             background=mode == "background")
        operation_id = result["operation_id"]
        if command == "hold" and mode == "background":
            # Registration removal is not thread exit: the worker may still be
            # closing its durable lease. Join the actual threads we created so
            # this helper cannot exit early and kill its own daemon cleanup.
            assert workers, "Synthetic background dispatch did not create its worker"
            for worker in workers:
                worker.join(timeout=15)
                assert not worker.is_alive(), "Synthetic background worker did not settle"
            result = api.status(operation_id) if kind == "product" else api.prerequisites_status(operation_id)
        output({"event": "result", "result": result, "busy": domain.busy(),
                "effect": (base / "effect-entered").exists(), "calls": values["calls"]})
    except InstallerError as error:
        output({"event": "error", "code": error.code, "busy": domain.busy(),
                "effect": (base / "effect-entered").exists(), "calls": values["calls"]})


if __name__ == "__main__":
    main()
