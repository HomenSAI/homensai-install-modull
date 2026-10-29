"""Внешний клиент установленного prerequisites SDK/CLI.

Запускается только из отдельного venv вне checkout. Подписи, downloader,
планы, журнал и locks настоящие; transport, inventory и installer синтетические.
Клиент не запускает процессы и не обращается к сети.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import ssl  # Инициализация stdlib до audit hook; socket остаётся настоящим.
import sys
import traceback


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


class BlockedEffect(RuntimeError):
    pass


def install_guard(checkout, denied):
    def guard(event, arguments):
        if event in {"open", "os.listdir", "os.scandir"} and arguments and isinstance(arguments[0], (str, bytes)):
            path = Path(os.fsdecode(arguments[0])).resolve()
            if path.is_relative_to(checkout):
                denied.append("checkout_read")
                raise BlockedEffect("Чтение source checkout запрещено")
        if event in {"socket.bind", "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg", "subprocess.Popen",
                     "os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty"}:
            denied.append(event)
            raise BlockedEffect("Сеть/дочерние процессы запрещены: " + event)
    sys.addaudithook(guard)


class Response:
    def __init__(self, content):
        self.status = 200
        self.headers = {"Content-Length": str(len(content)), "ETag": '"installed-synthetic-v1"'}
        self.stream = io.BytesIO(content)

    def read(self, size):
        return self.stream.read(size)

    def close(self):
        self.stream.close()


class Host:
    """Явный trusted bootstrap с fixed adapters, не production registry."""
    def __init__(self, config, case, *, tampered=False):
        from homensai_install.api.v1 import InstallerAPI
        from homensai_install.downloads import DownloadManager
        from homensai_install.prerequisite_catalog import SignedPrerequisiteCatalog
        from homensai_install.prerequisite_execution import PrerequisiteCoordinator
        from homensai_install.prerequisite_runner import CommandResult, FixedCommandAdapter
        from homensai_install.trust import SignedPayloadVerifier

        self.config, self.root = config, Path(config["work"]) / "cases" / case
        self.operation_id = "installed_" + case
        self.ledger_path, self.inventory_path = self.root / "ledger.json", self.root / "inventory.json"
        self.artifacts = {name: (spec["url"], base64.b64decode(spec["content_base64"]))
                          for name, spec in config["artifacts"].items()}
        trust = read_json(config["trust"])
        verifier = SignedPayloadVerifier({name: base64.b64decode(value) for name, value in trust["keys"].items()},
                                         allow_test_keys=True)
        options = read_json(config["options"])
        if tampered:
            options["signature_file"] = config["bad_signature"]
        catalog = SignedPrerequisiteCatalog(read_json(config["manifest"]), config["target"], options,
                                            verifier=verifier)
        host = self

        class Transport:
            def open(self, url, *, headers, timeout):
                for name, (expected, content) in host.artifacts.items():
                    if url == expected:
                        host.record("download_calls", name)
                        return Response(content)
                raise AssertionError("Transport получил неизвестный fixed artifact")

        executable = Path(sys.executable).resolve()
        executable_hash = sha(executable.read_bytes())
        adapters = {}
        for name in self.artifacts:
            def factory(allowed_commands, *, executable_hashes, environment=None, component=name):
                class Runner:
                    def run(self, argv, *, timeout, elevated):
                        assert tuple(argv) in allowed_commands and not elevated
                        assert executable_hashes == {str(executable): executable_hash}
                        assert Path(argv[-1]).read_bytes() == host.artifacts[component][1]
                        state = host.api.prerequisites_status(host.operation_id)
                        progress = host.check_progress(state, component=component, verified=True)
                        assert state["status"] == "RUNNING" and state["ready"] is False
                        assert state["steps"][component]["state"] == "INSTALLING"
                        host.record("progress_checks", {"component_id": component,
                            "artifact_sha256": progress["artifact_sha256"], "sequence": progress["sequence"],
                            "transfer_id": progress["transfer_id"], "verified_before_installer": True,
                            "operation_ready_before_installer": state["ready"]})
                        inventory = read_json(host.inventory_path)
                        if component == "client":
                            assert inventory["components"]["base"]["status"] == "present", "Нарушен dependency order"
                        host.record("installer_calls", component)
                        inventory["components"][component].update(status="present", version="1.0.0")
                        write_json(host.inventory_path, inventory)
                        return CommandResult(0, True)
                return Runner()
            adapters["install-" + name] = FixedCommandAdapter(
                "install-" + name, os_name="linux", argv=(str(executable), "-I", "-B", "{artifact_0}"),
                artifact_suffixes=(".py",), executable_sha256=executable_hash, runner_factory=factory)
        downloads = DownloadManager(self.root / "cache", trusted_hosts=["prerequisites.example.invalid"],
                                    transport=Transport(), retries=0)
        coordinator = PrerequisiteCoordinator(catalog, state_dir=self.root / "prerequisite-state",
            owner_id="installed-synthetic-owner", downloads=downloads, adapters=adapters, discover=self.discover,
            markers=lambda: {"boot_id": "synthetic-boot", "session_id": "synthetic-session"},
            product_consent_refs=config["product_consent_refs"])
        self.api = InstallerAPI.from_bootstrap(self.root / "product-state", trust_config=config["trust"],
                                               allow_development=True, prerequisites=coordinator)

    def record(self, field, value):
        ledger = read_json(self.ledger_path)
        ledger.setdefault(field, []).append(value)
        write_json(self.ledger_path, ledger)

    def discover(self):
        ledger = read_json(self.ledger_path)
        ledger["discovery_calls"] += 1
        write_json(self.ledger_path, ledger)
        return read_json(self.inventory_path)

    def counts(self):
        return read_json(self.ledger_path)

    def mark_manually_present(self):
        inventory = read_json(self.inventory_path)
        for name in ("base", "client"):
            inventory["components"][name].update(status="present", version="1.0.0")
        write_json(self.inventory_path, inventory)

    def save_plan(self, plan):
        write_json(self.root / "reviewed-plan.json", plan)

    def saved_plan(self):
        value = read_json(self.root / "reviewed-plan.json")
        return value.get("result", value)  # CLI сохраняет публичный envelope.

    def redaction(self, *values):
        public = json.dumps(values)
        assert self.config["redaction_sentinel"] not in public, "Synthetic secret попал в public result"

    def check_progress(self, state, *, component=None, verified=False):
        """Проверка публичного контракта без чтения внутренних journal/classes."""
        progress = state["download_progress"]
        assert set(progress) == {"schema_version", "plan_digest", "component_id", "artifact_id",
            "artifact_sha256", "transfer_id", "attempt", "state", "completed", "total", "unit",
            "source", "resumed", "verified", "percent", "sequence", "updated_at"}
        assert progress["schema_version"] == 1
        component = component or progress["component_id"]
        assert component in self.artifacts and progress["component_id"] == component
        expected = self.artifacts[component][1]
        plan = self.saved_plan()
        assert progress["plan_digest"] == state["plan_digest"] == plan["plan_digest"]
        item, = [item for item in plan["components"] if item["component_id"] == component]
        artifact, = item["artifacts"]
        assert progress["artifact_id"] == artifact["artifact_id"] == component + "-package"
        assert progress["artifact_sha256"] == artifact["sha256"] == sha(expected)
        assert type(progress["total"]) is int and progress["total"] == artifact["size"] == len(expected)
        assert type(progress["completed"]) is int and 0 <= progress["completed"] <= progress["total"]
        assert type(progress["sequence"]) is int and 0 < progress["sequence"] <= state["revision"]
        assert type(progress["attempt"]) is int and 0 <= progress["attempt"] <= 7
        assert re.fullmatch(r"[0-9a-f]{32}", progress["transfer_id"])
        assert progress["unit"] == "bytes" and isinstance(progress["updated_at"], str)
        assert type(progress["resumed"]) is bool and type(progress["verified"]) is bool
        assert progress["source"] in {"unknown", "network", "cache", "partial", "local"}
        assert progress["state"] in {"preparing", "transferring", "verifying", "retrying", "verified", "failed", "canceled"}
        if progress["state"] == "verified":
            assert progress["verified"] and progress["completed"] == progress["total"] and progress["percent"] == 100
        elif progress["state"] == "transferring":
            assert not progress["verified"] and progress["completed"] < progress["total"]
            assert type(progress["percent"]) is int and progress["percent"] == progress["completed"] * 100 // progress["total"]
        else:
            assert not progress["verified"] and progress["percent"] is None
        if verified:
            assert progress["state"] == "verified" and progress["source"] == "network"
        public = json.dumps(progress)
        for value in (self.config["work"], self.config["checkout"], self.config["redaction_sentinel"],
                      *[source for source, _ in self.artifacts.values()]):
            assert value not in public, "URL/path/sentinel попал в download_progress"
        return progress


def expect_error(code, function):
    from homensai_install.errors import InstallerError
    try:
        function()
    except InstallerError as error:
        assert error.code == code, (error.code, code)
        return error.to_dict()
    raise AssertionError("Ожидаемый отказ отсутствует: " + code)


def observation(host, operation_id):
    api = host.api
    state = api.prerequisites_status(operation_id)
    listing = api.prerequisites_operations_list(unfinished_only=False, limit=1)
    assert [item["operation_id"] for item in listing["operations"]] == [operation_id]
    assert api.prerequisites_operations_list(unfinished_only=False, after_id=operation_id)["operations"] == []
    first = api.prerequisites_events(operation_id, after_sequence=0, limit=1)
    assert len(first["events"]) == 1
    assert api.prerequisites_events(operation_id, after_sequence=0, limit=1)["events"] == first["events"]
    events, cursor, pages = list(first["events"]), first["events"][-1]["sequence"], 1
    while True:
        following = api.prerequisites_events(operation_id, after_sequence=cursor, limit=3)
        if not following["events"]:
            break
        assert following["events"][0]["sequence"] > cursor
        events.extend(following["events"])
        cursor = following["events"][-1]["sequence"]  # Никогда не перескакивать по snapshot.revision.
        assert following["snapshot"]["revision"] >= cursor
        pages += 1
    sequences = [item["sequence"] for item in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert sequences[-1] == state["revision"]
    assert not api.prerequisites_events(operation_id, after_sequence=sequences[-1])["events"]
    attempts = {name: api.prerequisites_attempts(operation_id, name)["attempts"] for name in ("base", "client")}
    host.redaction(state, listing, events, attempts)
    assert all(key not in json.dumps((state, listing)) for key in ('"plan"', '"consents"', '"_dispatch"'))
    progress_events = [event for event in events if event["type"] == "prerequisite_download"]
    verified_artifacts = []
    for event in progress_events:
        progress = host.check_progress({**state, "revision": event["sequence"], "download_progress": event["download_progress"]})
        assert progress["sequence"] == event["sequence"] and progress["component_id"] == event["component_id"]
        if progress["verified"]:
            verified_artifacts.append(progress["artifact_id"])
    if progress_events:
        progress = host.check_progress(state, component="client", verified=True)
        assert state["download_progress"] == progress_events[-1]["download_progress"]
        assert verified_artifacts == ["base-package", "client-package"] and pages > 2
        proofs = host.counts()["progress_checks"]
        assert [item["component_id"] for item in proofs] == ["base", "client"]
        assert len({item["transfer_id"] for item in proofs}) == 2
        assert all(item["verified_before_installer"] and not item["operation_ready_before_installer"] for item in proofs)
    else:
        assert "download_progress" not in state and all("download_progress" not in item for item in listing["operations"])
        assert not host.counts().get("progress_checks")
    public = json.dumps((state, listing, events, attempts))
    assert host.config["work"] not in public and host.config["checkout"] not in public
    assert all(source not in public for source, _ in host.artifacts.values())
    return {"operation_id": operation_id, "status": state["status"], "ready": state["ready"],
            "plan_digest": state["plan_digest"], "event_count": len(events),
            "attempt_counts": {name: len(items) for name, items in attempts.items()},
            "redaction_checked": True, "cursor_replay_checked": True, "event_pages": pages,
            "download_event_count": len(progress_events), "verified_artifacts": verified_artifacts,
            "legacy_no_progress_compatible": not progress_events,
            "progress_before_installer_checks": len(host.counts().get("progress_checks", []))}


def lifecycle(config, phase):
    if phase == "tampered":
        refusal = expect_error("INTEGRITY_FAILED", lambda: Host(config, "tampered", tampered=True))
        root = Path(config["work"]) / "cases/tampered"
        assert not (root / "prerequisite-state").exists()
        assert read_json(root / "ledger.json")["installer_calls"] == []
        return {"error_code": refusal["code"], "refused_before_coordinator_or_effect": True}
    case, action = phase.split("_", 1)
    host = Host(config, case)
    api, operation_id = host.api, "installed_" + case
    if action == "prepare":
        plan = api.prerequisites_plan({"base": "auto", "client": "auto"} if case == "auto" else {}, locale="en")
        host.save_plan(plan)
        if case == "auto":
            error = expect_error("CONSENT_REQUIRED", lambda: api.prerequisites_start("missing_consent", plan))
            assert api.prerequisites_operations_list()["operations"] == []
            assert host.counts()["installer_calls"] == [] and host.counts()["download_calls"] == []
            host.redaction(error, plan)
            # Возврат start намеренно не переносится следующему процессу.
            api.prerequisites_start(operation_id, plan, plan["required_consents"])
            assert host.counts()["installer_calls"] == ["base", "client"]
            assert host.counts()["download_calls"] == ["base", "client"]
            assert len(host.counts()["progress_checks"]) == 2
            return {"operation_id": operation_id, "ack_transferred_to_next_client": False,
                    "missing_consent_refused": True, "fake_dependency_order": ["base", "client"],
                    "progress_before_installer_checks": 2}
        state = api.prerequisites_start(operation_id, plan, plan["required_consents"])
        assert state["status"] == "AWAITING_USER" and not state["ready"]
        if case == "cancel":
            state = api.prerequisites_cancel(operation_id)
            assert state["status"] == "CANCELED" and state["cancel_requested"]
        assert host.counts()["installer_calls"] == [] and host.counts()["download_calls"] == []
        return observation(host, operation_id)
    before = host.counts()
    original = host.saved_plan()
    if case == "manual":
        assert api.prerequisites_status(operation_id)["status"] == "AWAITING_USER"
        host.mark_manually_present()
        assert api.prerequisites_recheck(operation_id)["status"] == "READY"
    elif case == "cancel":
        assert api.prerequisites_status(operation_id)["status"] == "CANCELED"
        assert api.prerequisites_resume(operation_id)["status"] == "CANCELED"
    else:
        assert api.prerequisites_status(operation_id)["status"] == "READY"
    replay = api.prerequisites_start(operation_id, original, original["required_consents"])
    assert replay["operation_id"] == operation_id and replay["plan_digest"] == original["plan_digest"]
    result = observation(host, operation_id)
    after = host.counts()
    assert after["installer_calls"] == before["installer_calls"]
    assert after["download_calls"] == before["download_calls"]
    if case != "manual":
        assert after == before, "Status/replay/resume terminal операции вызвал discovery или эффект"
    expected = 1 if case == "auto" else 0
    assert result["attempt_counts"] == {"base": expected, "client": expected}
    assert api.prerequisites_operations_list()["operations"] == []
    return {**result, "independent_process_restoration": True, "idempotent_replay_no_effect": True}


def cli_lifecycle(config, phase):
    from homensai_install import API_VERSION
    from homensai_install.cli import main as cli_main
    host = Host(config, "cli")
    commands = []

    def invoke(*arguments, expected=0):
        argv = ["--json", "--locale", "de", "prerequisites", *map(str, arguments)]
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli_main(argv, api=host.api)
        commands.append({"argv": argv, "exit_code": code, "envelope_sha256": sha(output.getvalue().encode())})
        write_json(host.root / (phase + "-commands.json"), commands)
        assert code == expected, (arguments[0], code, expected, output.getvalue())
        assert not errors.getvalue(), "Неожиданный stderr CLI: " + errors.getvalue()[:1000]
        envelope = json.loads(output.getvalue())
        assert envelope["api_version"] == API_VERSION, "Неверная версия CLI envelope"
        host.redaction(envelope)
        return envelope

    plan_file = host.root / "reviewed-plan.json"
    if phase == "cli_prepare":
        assert invoke("capabilities")["result"]["configured"] is True, "CLI потерял trusted registration"
        terms = invoke("license", "base")["result"]
        assert sha(terms["text"].encode()) == terms["text_sha256"], "CLI изменил bytes signed license text"
        choices = host.root / "choices.json"
        write_json(choices, {"base": "auto", "client": "auto"})
        envelope = invoke("plan", "--choices", choices)
        assert envelope["result"]["locale"] == "de"
        write_json(plan_file, envelope)
        refusal = invoke("start", "installed_cli", "--plan", plan_file, expected=2)
        assert refusal["error"]["code"] == "CONSENT_REQUIRED"
        assert host.counts()["installer_calls"] == []
        plan = envelope["result"]
    else:
        plan = read_json(plan_file)["result"]
        state = invoke("status", "installed_cli")["result"]
        assert state["status"] == "READY"
        host.check_progress(state, component="client", verified=True)
    consent_args = [value for ref in plan["required_consents"] for value in ("--consent-ref", ref)]
    before = host.counts()
    started = invoke("start", "installed_cli", "--plan", plan_file, *consent_args)["result"]
    assert started["status"] == "READY" and started["ready"]
    assert started["plan_digest"] == plan["plan_digest"]
    progress = host.check_progress(started, component="client", verified=True)
    assert host.counts()["installer_calls"] == ["base", "client"]
    if phase == "cli_restore":
        assert invoke("resume", "installed_cli")["result"]["status"] == "READY"
        assert len(invoke("operations", "--all")["result"]["operations"]) == 1
        page = invoke("events", "installed_cli", "--after-sequence", progress["sequence"] - 1, "--limit", "1")["result"]
        assert len(page["events"]) == 1 and page["events"][0]["sequence"] == progress["sequence"]
        assert page["events"][0]["download_progress"] == progress
        host.check_progress(page["snapshot"], component="client", verified=True)
        assert len(invoke("attempts", "installed_cli", "base")["result"]["attempts"]) == 1
        restored = observation(host, "installed_cli")
        assert restored["verified_artifacts"] == ["base-package", "client-package"]
        assert host.counts() == before
    return {"commands": commands, "status": "READY", "trusted_injection": True,
            "fake_dependency_order": host.counts()["installer_calls"],
            "independent_process_restoration": phase == "cli_restore",
            "progress_before_installer_checks": len(host.counts()["progress_checks"]),
            "progress_status_checked": True, "progress_event_and_replay_checked": phase == "cli_restore"}


def check_origins(installed):
    import cffi
    import cryptography
    import pycparser
    origins = {}
    for name, module in list(sys.modules.items()):
        if name in {"shared", "_cffi_backend"} or name.startswith(("homensai_install", "shared.", "cryptography", "cffi", "pycparser")):
            filename = getattr(module, "__file__", None)
            if filename:
                path = Path(filename).resolve()
                assert path.is_relative_to(installed), "Import вне установленного site-packages: " + name
                origins[name] = path.relative_to(installed).as_posix()
    assert any(name.startswith("homensai_install") for name in origins)
    assert "cryptography" in origins and "cffi" in origins and "pycparser" in origins
    return origins


def main():
    config, phase = read_json(sys.argv[1]), sys.argv[2]
    checkout, installed = Path(config["checkout"]).resolve(), Path(config["site_packages"]).resolve()
    denied, result, exit_code = [], {"phase": phase, "pid": os.getpid(), "status": "FAIL"}, 1
    install_guard(checkout, denied)
    try:
        assert not Path.cwd().resolve().is_relative_to(checkout)
        assert not any(Path(item or os.getcwd()).resolve().is_relative_to(checkout) for item in sys.path)
        assert sys.flags.isolated and sys.dont_write_bytecode
        if phase == "audit_guard":
            import socket
            import subprocess
            for action in (lambda: (checkout / "pyproject.toml").read_bytes(),
                           lambda: socket.create_connection(("127.0.0.1", 9)),
                           lambda: subprocess.run([sys.executable, "-c", "raise SystemExit(99)"], check=False)):
                try:
                    action()
                except BlockedEffect:
                    continue
                raise AssertionError("Audit hook пропустил запрещённый эффект")
            assert denied == ["checkout_read", "socket.getaddrinfo", "subprocess.Popen"]
            from homensai_install.api.v1 import InstallerAPI
            result["observation"] = {"expected_denials": denied[:], "effects_blocked_before_execution": True}
        else:
            result["observation"] = cli_lifecycle(config, phase) if phase.startswith("cli_") else lifecycle(config, phase)
            assert not denied, "Lifecycle вызвал запрещённый эффект"
        result["module_origins"] = check_origins(installed)
        result["distribution_versions"] = {
            name: importlib.metadata.version(name) for name in ("homensai-install-module", "cryptography", "cffi", "pycparser")}
        for dependency in config["dependencies"]:
            assert result["distribution_versions"][dependency["name"]] == dependency["version"]
        for path in (Path(config["work"]) / "install-root", Path(config["work"]) / "product-recovery"):
            assert not path.exists(), "Создан продуктовый install/recovery root"
        assert not any((Path(config["work"]) / "cases" / case / "product-state").exists()
                       for case in ("manual", "auto", "cancel", "cli", "tampered"))
        result["status"], exit_code = "PASS", 0
    except Exception as error:
        result["error"] = {"type": type(error).__name__,
                           "message": str(error).replace(config["redaction_sentinel"], "[REDACTED]")[:2000],
                           "frames": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                                      for frame in traceback.extract_tb(error.__traceback__)]}
        command_log = Path(config["work"]) / "cases/cli" / (phase + "-commands.json")
        if phase.startswith("cli_") and command_log.exists():
            result["cli_commands_before_failure"] = read_json(command_log)
    result["denied_attempts"] = denied
    write_json(Path(config["work"]) / "reports" / (phase + ".json"), result)
    print(json.dumps({"phase": phase, "status": result["status"]}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
