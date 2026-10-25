"""Signed synthetic prerequisites, fake platform installer, real durable journal.

No OS packages, Docker, service registration or live network are involved.
"""
import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from homensai_install.api.model import digest, target_model
from homensai_install.downloads import ArtifactSpec, DownloadManager
from homensai_install.errors import InstallerError
from homensai_install.prerequisite_catalog import SignedPrerequisiteCatalog
from homensai_install.prerequisite_execution import PrerequisiteCoordinator
from homensai_install.prerequisite_runner import CommandResult, FixedCommandAdapter
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest

ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, content):
        self.status = 200
        self.headers = {"Content-Length": str(len(content)), "ETag": '"synthetic-v1"'}
        self.content = io.BytesIO(content)

    def read(self, size):
        return self.content.read(size)

    def close(self):
        self.content.close()


class Transport:
    def __init__(self, files):
        self.files, self.calls = files, []

    def open(self, url, *, headers, timeout):
        self.calls.append(url)
        return Response(self.files[url])


def resign(bundle):
    payload = bundle["payload"]
    (payload / "installer/prerequisites.json").write_bytes(canonical_bytes(bundle["descriptor"]))
    inventory = {"schema_version": 1, "product_id": bundle["manifest"]["product_id"], "release": "1.0.0",
        "target": {key: bundle["target"][key] for key in ("os", "arch", "mode")},
        "files": {path.relative_to(payload).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(payload.rglob("*")) if path.is_file()},
        "development": True, "manifest_sha256": hash_manifest(bundle["manifest"]), "runtime_executable": "runtime"}
    signature = {"algorithm": "ed25519", "key_id": "synthetic-test",
                 "signature": base64.b64encode(bundle["private"].sign(canonical_bytes(inventory))).decode()}
    Path(bundle["options"]["inventory_file"]).write_bytes(canonical_bytes(inventory))
    Path(bundle["options"]["signature_file"]).write_bytes(canonical_bytes(signature))


@pytest.fixture
def bundle(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Synthetic host execution is Linux; Windows state is separately simulated")
    payload = tmp_path / "payload"
    (payload / "installer").mkdir(parents=True)
    (payload / "licenses").mkdir()
    (payload / "runtime").write_bytes(b"synthetic signed runtime, never executed")
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text())
    manifest["product_id"] = "prerequisite-test"
    target = target_model({"os": "linux", "arch": "x64", "mode": "native", "scope": "user"})
    entries, files = [], {}
    for name, depends, required in (("base", [], True), ("client", ["base"], True), ("metrics", [], False)):
        license_text = "Synthetic terms for " + name + "; no third party installation.\n"
        (payload / "licenses" / (name + ".txt")).write_text(license_text)
        artifact = ("# synthetic " + name + "\n").encode()
        url = "https://downloads.example.org/" + name + "?token=never-public"
        files[url] = artifact
        entries.append({"requirement": {"component_id": name, "kind": name, "required": required,
            "minimum_version": "1.0.0", "scope": "host"}, "depends_on": depends, "ownership": "shared",
            "adapter_id": "install-" + name, "artifacts": [{"artifact_id": name + "-package", "version": "1.0.0",
                "url": url, "sha256": hashlib.sha256(artifact).hexdigest(), "size": len(artifact), "license_ref": name + "-terms"}],
            "license": {"license_id": name + "-terms", "version": "2026-01", "text_path": "licenses/" + name + ".txt",
                "text_sha256": hashlib.sha256(license_text.encode()).hexdigest(), "locale": "en", "decision": "allowed",
                "acceptance_required": True, "conditions": []}})
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    value = {"payload": payload, "manifest": manifest, "target": target, "private": private,
        "verifier": SignedPayloadVerifier({"synthetic-test": public}, allow_test_keys=True),
        "options": {"payload_dir": str(payload), "inventory_file": str(tmp_path / "inventory.json"),
                    "signature_file": str(tmp_path / "signature.json"), "release": "1.0.0", "runtime_executable": "runtime"},
        "descriptor": {"schema_version": 1, "catalog_id": "prerequisites", "catalog_version": "1.0.0",
                       "product_id": manifest["product_id"], "target": target, "entries": entries},
        "transport": Transport(files), "calls": [], "results": {}, "effects": {},
        "markers": {"boot_id": "synthetic-boot-1", "session_id": "synthetic-session-1"},
        "inventory": {"platform": {"os": "linux", "arch": "x64", "release": "synthetic"},
            "identity": {"uid": 1000, "scope": "current_process", "elevated": False},
            "resources": {"cpu_count": 2}, "ports": [], "disks": [],
            "components": {name: {"status": "missing", "version": None, "scope": "host"} for name in ("base", "client", "metrics")}},
        "state_dir": tmp_path / "state", "tmp_path": tmp_path}
    resign(value)
    value["downloads"] = DownloadManager(tmp_path / "cache", trusted_hosts=["downloads.example.org"], transport=value["transport"], retries=0)
    return value


def catalog(bundle):
    return SignedPrerequisiteCatalog(bundle["manifest"], bundle["target"], bundle["options"], verifier=bundle["verifier"])


def present(bundle, name):
    bundle["inventory"]["components"][name].update(status="present", version="1.0.0")


def registry(bundle):
    result = {}
    for name in ("base", "client", "metrics"):
        def factory(allowed_commands, *, executable_hashes, environment=None, component=name):
            class Runner:
                def run(self, argv, *, timeout, elevated):
                    assert tuple(argv) in allowed_commands
                    assert Path(argv[-1]).read_bytes() == bundle["transport"].files["https://downloads.example.org/" + component + "?token=never-public"]
                    bundle["calls"].append(component)
                    effect = bundle["effects"].get(component, "ready")
                    if callable(effect):
                        effect()
                    elif effect == "ready":
                        present(bundle, component)
                    response = bundle["results"].get(component, CommandResult(0, True, pid=123))
                    if isinstance(response, BaseException):
                        raise response
                    return response
            return Runner()
        executable = Path(sys.executable).resolve()
        result["install-" + name] = FixedCommandAdapter("install-" + name, os_name="linux",
            argv=(str(executable), "-I", "-B", "{artifact_0}"), artifact_suffixes=(".py",),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            reboot_codes=(3010,), relogin_codes=(10,), canceled_codes=(1602,), runner_factory=factory)
    return result


def coordinator(bundle, **changes):
    values = dict(state_dir=bundle["state_dir"], owner_id="original-user-1000", downloads=bundle["downloads"],
                  adapters=registry(bundle), discover=lambda: deepcopy(bundle["inventory"]),
                  markers=lambda: deepcopy(bundle["markers"]), product_consent_refs=["product-eula:sha256-fixture"])
    values.update(changes)
    return PrerequisiteCoordinator(catalog(bundle), **values)


def start_auto(subject, operation_id="operation-1", **kwargs):
    plan = subject.plan({"base": "auto", "client": "auto"}, **kwargs)
    return subject.start(operation_id, plan, consents=plan["required_consents"])


def test_signed_catalog_topological_order_and_exact_license(bundle):
    value = catalog(bundle)
    assert [entry["requirement"]["component_id"] for entry in value.entries()] == ["base", "metrics", "client"]
    assert value.license_text("base")["text"].startswith("Synthetic terms")
    assert value.catalog_digest == catalog(bundle).catalog_digest


@pytest.mark.parametrize("change", ["cycle", "unknown_dependency", "unbound_license", "extra_argv", "latest", "duplicate"])
def test_signed_invalid_catalog_is_rejected(bundle, change):
    entries = bundle["descriptor"]["entries"]
    if change == "cycle": entries[0]["depends_on"] = ["client"]
    elif change == "unknown_dependency": entries[0]["depends_on"] = ["unknown"]
    elif change == "unbound_license": entries[0]["artifacts"][0]["license_ref"] = "different"
    elif change == "extra_argv": entries[0]["argv"] = ["sh", "unreviewed"]
    elif change == "latest": entries[0]["artifacts"][0]["version"] = "latest"
    else: entries.append(deepcopy(entries[0]))
    resign(bundle)
    with pytest.raises(InstallerError): catalog(bundle)
    assert not bundle["calls"] and not bundle["transport"].calls


def test_catalog_tamper_and_full_payload_release_binding(bundle):
    subject = catalog(bundle)
    (bundle["payload"] / "runtime").write_bytes(b"changed signed runtime")
    with pytest.raises(InstallerError) as caught: subject.entries()
    assert caught.value.code == "INTEGRITY_FAILED"
    resign(bundle)
    with pytest.raises(InstallerError) as caught: subject.entries()
    assert caught.value.code == "PLAN_STALE"


def test_missing_consents_and_bad_plan_reject_before_download(bundle):
    subject = coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "auto"})
    for consents in ([], [ref for ref in plan["required_consents"] if not ref.startswith("prerequisite-license:base")],
                     [ref for ref in plan["required_consents"] if not ref.startswith("prerequisite-auto:base")],
                     [ref for ref in plan["required_consents"] if not ref.startswith("product-eula:")]):
        with pytest.raises(InstallerError) as caught: subject.start("missing-consent", plan, consents=consents)
        assert caught.value.code == "CONSENT_REQUIRED"
    plan["offline"] = True
    with pytest.raises(InstallerError) as caught: subject.start("tampered", plan, consents=plan["required_consents"])
    assert caught.value.code == "INTEGRITY_FAILED"
    assert not bundle["transport"].calls and not bundle["calls"]


def test_actual_coordinator_dependency_order_postconditions_and_idempotent_start(bundle):
    subject = coordinator(bundle)
    plan = subject.plan({"base": "auto", "client": "auto"})
    state = subject.start("same-operation", plan, consents=plan["required_consents"])
    assert state["status"] == "READY" and state["ready"]
    assert bundle["calls"] == ["base", "client"]
    assert state["steps"]["base"]["receipt"]["artifact_pins"][0]["sha256"] == bundle["descriptor"]["entries"][0]["artifacts"][0]["sha256"]
    assert state["steps"]["metrics"]["state"] == "SKIPPED"
    assert state["steps"]["base"]["ownership"] == "shared"
    encoded = json.dumps(state)
    assert "never-public" not in encoded and "local_artifacts" not in encoded
    restored = coordinator(bundle)
    assert restored.start("same-operation", plan, consents=plan["required_consents"])["status"] == "READY"
    assert restored.resume("same-operation")["status"] == "READY"
    assert bundle["calls"] == ["base", "client"]
    events = restored.events("same-operation", limit=1000)
    assert [event["sequence"] for event in events] == list(range(1, state["revision"] + 1))


def test_manual_recheck_never_downloads_or_executes_and_does_not_autorun_following_auto(bundle):
    subject = coordinator(bundle)
    plan = subject.plan({"base": "manual", "client": "auto"})
    state = subject.start("manual", plan, consents=plan["required_consents"])
    assert state["status"] == "AWAITING_USER"
    assert not bundle["calls"] and not bundle["transport"].calls
    present(bundle, "base")
    state = coordinator(bundle).recheck("manual")
    assert state["status"] == "AWAITING_USER" and state["required_action"] == "resume"
    assert not bundle["calls"] and not bundle["transport"].calls
    assert coordinator(bundle).resume("manual")["ready"]
    assert bundle["calls"] == ["client"]


def test_exit_zero_is_not_daemon_or_component_readiness(bundle):
    bundle["effects"]["base"] = "not_ready"
    state = start_auto(coordinator(bundle))
    assert state["status"] == "AWAITING_USER" and not state["ready"]
    assert state["steps"]["base"]["state"] == "VERIFYING" and bundle["calls"] == ["base"]
    assert not coordinator(bundle).resume("operation-1")["ready"]
    assert bundle["calls"] == ["base"]
    present(bundle, "base")
    state = coordinator(bundle).recheck("operation-1")
    assert state["steps"]["base"]["state"] == "READY" and bundle["calls"] == ["base"]
    assert coordinator(bundle).resume("operation-1")["ready"]
    assert bundle["calls"] == ["base", "client"]


@pytest.mark.parametrize("returncode,status,marker", [(3010, "REBOOT_REQUIRED", "boot_id"), (10, "RELOGIN_REQUIRED", "session_id")])
def test_durable_boundary_requires_marker_not_browser_refresh(bundle, returncode, status, marker):
    bundle["results"]["base"] = CommandResult(returncode, True, pid=123)
    subject = coordinator(bundle)
    state = start_auto(subject)
    assert state["status"] == status and bundle["calls"] == ["base"]
    assert subject.choose_boundary("operation-1", "later")["host_action"]["performed"] is False
    restored = coordinator(bundle)
    assert restored.resume("operation-1")["status"] == status
    assert restored.choose_boundary("operation-1", "now")["host_action"]["performed"] is False
    assert bundle["calls"] == ["base"]
    bundle["markers"][marker] = "new-boundary"
    state = coordinator(bundle).recheck("operation-1")
    assert state["steps"]["base"]["state"] == "READY"
    assert state["status"] == "AWAITING_USER" and bundle["calls"] == ["base"]
    state = coordinator(bundle).resume("operation-1")
    assert state["ready"] and state["operation_id"] == "operation-1" and bundle["calls"] == ["base", "client"]


def test_unknown_markers_never_fabricate_reboot_proof(bundle):
    bundle["markers"] = {"boot_id": None, "session_id": None}
    bundle["results"]["base"] = CommandResult(3010, True)
    state = start_auto(coordinator(bundle))
    assert state["status"] == "REBOOT_REQUIRED"
    bundle["markers"]["boot_id"] = "now-known-but-no-old-marker"
    assert coordinator(bundle).resume("operation-1")["status"] == "REBOOT_REQUIRED"
    assert bundle["calls"] == ["base"]


class ControllerDied(BaseException):
    pass


def test_crash_after_intent_and_effect_cannot_replay_until_reconciled(bundle):
    bundle["results"]["base"] = ControllerDied()
    with pytest.raises(ControllerDied): start_auto(coordinator(bundle))
    assert bundle["calls"] == ["base"]
    restored = coordinator(bundle)
    assert restored.status("operation-1")["steps"]["base"]["state"] == "INSTALLING"
    state = restored.resume("operation-1")
    assert state["status"] == "RECOVERY_REQUIRED" and not state["ready"]
    assert state["steps"]["base"]["receipt"]["reconciled"] == "unknown"
    assert restored.cancel("operation-1")["status"] == "RECOVERY_REQUIRED"
    assert bundle["calls"] == ["base"]
    bundle["markers"]["boot_id"] = "new-boot-proves-old-installer-gone"
    state = coordinator(bundle).resume("operation-1")
    assert state["status"] == "CANCELED" and bundle["calls"] == ["base"]


def test_timeout_does_not_become_success_from_present_probe_in_same_boot(bundle):
    bundle["results"]["base"] = CommandResult(None, True, timed_out=True, pid=123)
    state = start_auto(coordinator(bundle))
    assert state["status"] == "RECOVERY_REQUIRED"
    assert coordinator(bundle).recheck("operation-1")["status"] == "RECOVERY_REQUIRED"
    assert bundle["calls"] == ["base"]
    reconciled = coordinator(bundle, reconcile=lambda entry, receipt, inventory: "applied")
    assert reconciled.resume("operation-1")["ready"]
    assert bundle["calls"] == ["base", "client"]


def test_not_applied_proof_allows_explicit_resume_not_recheck_to_retry(bundle):
    bundle["results"]["base"] = ControllerDied()
    bundle["effects"]["base"] = "not_ready"
    with pytest.raises(ControllerDied): start_auto(coordinator(bundle))
    restored = coordinator(bundle, reconcile=lambda entry, receipt, inventory: "not_applied")
    state = restored.recheck("operation-1")
    assert state["status"] == "AWAITING_USER" and bundle["calls"] == ["base"]
    bundle["results"]["base"] = CommandResult(0, True)
    bundle["effects"]["base"] = "ready"
    assert restored.resume("operation-1")["ready"]
    assert bundle["calls"] == ["base", "base", "client"]
    history = coordinator(bundle).attempts("operation-1", "base")
    assert [item["attempt"] for item in history] == [1, 2]
    assert history[0]["receipt"]["reconciled"] == "not_applied"
    assert history[1]["receipt"]["outcome"] == "VERIFY"
    assert history[0]["receipt"]["artifact_pins"] == history[1]["receipt"]["artifact_pins"]


def test_competing_operation_and_os_controller_lock_block_before_effect(bundle):
    first = coordinator(bundle)
    plan = first.plan()
    first.start("one", plan, consents=plan["required_consents"])
    second = coordinator(bundle)
    with pytest.raises(InstallerError) as caught: second.start("two", plan, consents=plan["required_consents"])
    assert caught.value.code == "OPERATION_CONFLICT"
    with first.store.lock():
        with pytest.raises(InstallerError) as caught: second.resume("one")
    assert caught.value.code == "OPERATION_CONFLICT" and not bundle["calls"]
    assert first.cancel("one")["status"] == "CANCELED"
    assert second.start("two", plan, consents=plan["required_consents"])["status"] == "AWAITING_USER"


def test_owner_material_context_and_replacement_consent(bundle):
    first = coordinator(bundle)
    plan = first.plan()
    first.start("owner", plan, consents=plan["required_consents"])
    with pytest.raises(InstallerError) as caught: coordinator(bundle, owner_id="another-user").status("owner")
    assert caught.value.code == "PERMISSION_REQUIRED"
    bundle["inventory"]["resources"]["cpu_count"] = 4
    state = first.resume("owner")
    assert state["error"]["code"] == "PLAN_STALE" and state["required_action"] == "replan"
    new_plan = first.plan({"base": "auto", "client": "auto"})
    with pytest.raises(InstallerError) as caught: first.replace_plan("owner", new_plan, consents=plan["required_consents"])
    assert caught.value.code == "CONSENT_REQUIRED"
    assert first.replace_plan("owner", new_plan, consents=new_plan["required_consents"])["status"] == "AWAITING_USER"
    assert not bundle["calls"]
    assert first.resume("owner")["ready"]


def test_offline_and_user_artifact_paths_use_exact_signed_bytes(bundle, tmp_path):
    subject = coordinator(bundle)
    state = start_auto(subject, offline=True)
    assert state["status"] == "AWAITING_USER" and state["error"]["code"] == "PREREQUISITE_MISSING"
    assert not bundle["transport"].calls and not bundle["calls"]
    subject.cancel("operation-1")
    local = {}
    for entry in bundle["descriptor"]["entries"][:2]:
        artifact = entry["artifacts"][0]
        path = tmp_path / artifact["artifact_id"]
        path.write_bytes(bundle["transport"].files[artifact["url"]])
        local[artifact["artifact_id"]] = str(path)
    state = start_auto(subject, "offline-local", offline=True, local_artifacts=local)
    assert state["ready"] and not bundle["transport"].calls
    assert bundle["calls"] == ["base", "client"]


def test_corrupt_download_and_untrusted_host_never_execute(bundle):
    raw = bundle["descriptor"]["entries"][0]["artifacts"][0]
    bundle["transport"].files[raw["url"]] = b"x" * raw["size"]
    state = start_auto(coordinator(bundle))
    assert state["error"]["code"] == "INTEGRITY_FAILED" and not bundle["calls"]
    coordinator(bundle).cancel("operation-1")
    raw["url"] = "https://unknown.example.org/base"
    resign(bundle)
    bundle["transport"].calls.clear()
    state = start_auto(coordinator(bundle), "untrusted")
    assert state["error"]["code"] == "INTEGRITY_FAILED"
    assert not bundle["transport"].calls and not bundle["calls"]


def test_blocked_license_does_not_become_ready_through_manual_install(bundle):
    bundle["descriptor"]["entries"][0]["license"]["decision"] = "unknown"
    resign(bundle)
    subject = coordinator(bundle)
    with pytest.raises(InstallerError): subject.plan({"base": "auto"})
    present(bundle, "base")
    present(bundle, "client")
    plan = subject.plan()
    state = subject.start("blocked-license", plan, consents=plan["required_consents"])
    assert not state["ready"] and state["blocking_components"] == ["base"]
    assert not bundle["calls"] and not bundle["transport"].calls


def test_optional_dependency_selection_is_explicit_and_selected_optional_must_verify(bundle):
    subject = coordinator(bundle)
    with pytest.raises(InstallerError): subject.plan({"base": "skip"})
    with pytest.raises(InstallerError): subject.plan({"typo": "auto"})
    bundle["effects"]["metrics"] = "not_ready"
    plan = subject.plan({"base": "auto", "client": "auto", "metrics": "auto"})
    state = subject.start("optional", plan, consents=plan["required_consents"])
    assert not state["ready"] and "metrics" in state["blocking_components"]
    assert bundle["calls"] == ["base", "metrics"]


def test_recheck_and_cancel_preserve_shared_installed_component(bundle):
    bundle["effects"]["base"] = "not_ready"
    subject = coordinator(bundle)
    start_auto(subject)
    present(bundle, "base")
    state = subject.cancel("operation-1")
    assert state["status"] == "CANCELED"
    assert bundle["inventory"]["components"]["base"]["status"] == "present"
    assert subject.resume("operation-1")["status"] == "CANCELED" and bundle["calls"] == ["base"]


def test_diagnostic_observer_exception_cannot_repeat_an_installer(bundle):
    def broken(event):
        raise RuntimeError("private diagnostic failure")
    state = start_auto(coordinator(bundle, event_callback=broken))
    assert state["ready"] and bundle["calls"] == ["base", "client"]
    assert "private diagnostic failure" not in json.dumps(state)


def test_capabilities_are_verified_and_status_is_bound_to_product(bundle):
    subject = coordinator(bundle)
    identity = subject.identity()
    assert identity["product_id"] == "prerequisite-test" and identity["target"] == bundle["target"]
    assert set(subject.capabilities()["automatic_component_ids"]) == {"base", "client", "metrics"}
    assert subject.capabilities()["platform_acceptance"] == "not_verified"
    plan = subject.plan()
    subject.start("identity", plan, consents=plan["required_consents"])
    bundle["manifest"]["product_id"] = "another-product"
    bundle["descriptor"]["product_id"] = "another-product"
    resign(bundle)
    with pytest.raises(InstallerError) as caught: coordinator(bundle).status("identity")
    assert caught.value.code == "PERMISSION_REQUIRED"
    with pytest.raises(InstallerError): subject.capabilities()
    # Read-only historic status remains available through its original registration.
    assert subject.status("identity")["operation_id"] == "identity"


def test_state_hardlink_is_not_opened_as_database(bundle, tmp_path):
    directory = bundle["state_dir"]
    directory.mkdir(mode=0o700)
    victim = tmp_path / "unrelated-state"
    victim.write_bytes(b"must-not-change")
    os.link(victim, directory / "prerequisites.sqlite3")
    with pytest.raises(InstallerError) as caught: coordinator(bundle)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert victim.read_bytes() == b"must-not-change"


def test_real_fixed_runner_is_reached_only_after_signed_pins_and_consent(bundle, tmp_path, monkeypatch):
    from homensai_install.prerequisite_runner import FixedCommandRunner
    marker = tmp_path / "synthetic-result.json"
    content = ("import json,os,pathlib\npathlib.Path(" + repr(str(marker)) + ").write_text(json.dumps(sorted(os.environ)))\n").encode()
    raw = bundle["descriptor"]["entries"][0]["artifacts"][0]
    raw.update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    bundle["transport"].files[raw["url"]] = content
    resign(bundle)
    adapters = registry(bundle)
    old = adapters["install-base"]
    adapters["install-base"] = FixedCommandAdapter("install-base", os_name="linux", argv=old.argv,
        artifact_suffixes=old.artifact_suffixes, executable_sha256=old.executable_sha256,
        runner_factory=FixedCommandRunner)
    def discover():
        if marker.exists(): present(bundle, "base")
        return deepcopy(bundle["inventory"])
    monkeypatch.setenv("UNDECLARED_PARENT_SECRET", "private-parent-value")
    subject = coordinator(bundle, adapters=adapters, discover=discover)
    plan = subject.plan({"base": "auto", "client": "manual"})
    with pytest.raises(InstallerError): subject.start("real", plan, consents=[])
    assert not marker.exists()
    state = subject.start("real", plan, consents=plan["required_consents"])
    assert state["status"] == "AWAITING_USER" and state["steps"]["base"]["state"] == "READY"
    assert state["steps"]["base"]["receipt"]["returncode"] == 0
    assert set(json.loads(marker.read_text())) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
    assert not bundle["calls"]  # The fake installer runner was not used.


@pytest.mark.parametrize("phase", ["prerequisite_downloading", "prerequisite_execution_intent", "inside_installer"])
def test_concurrent_cancel_is_durable_and_stops_at_safe_boundary(bundle, phase):
    observations = []
    def cancel():
        controller = coordinator(bundle)
        state = controller.cancel("operation-1")
        observations.append(state)
        assert state["cancel_requested"] and state["status"] == "RUNNING"
        assert state["required_action"] == "wait_for_safe_boundary"
    def event(value):
        if value.get("message_code") == phase:
            cancel()
    if phase == "inside_installer":
        def effect():
            cancel()
            present(bundle, "base")
        bundle["effects"]["base"] = effect
    state = start_auto(coordinator(bundle, event_callback=event))
    assert observations
    assert all(item["status"] == "RUNNING" and item["required_action"] == "wait_for_safe_boundary" for item in observations)
    assert state["status"] == "CANCELED" and state["cancel_requested"]
    assert bundle["calls"] == (["base"] if phase == "inside_installer" else [])
    assert not bundle["transport"].calls if phase == "prerequisite_downloading" else bundle["transport"].calls
    events = coordinator(bundle).events("operation-1", limit=1000)
    assert [event["sequence"] for event in events] == list(range(1, state["revision"] + 1))
    assert coordinator(bundle).resume("operation-1")["status"] == "CANCELED"


def test_already_present_auto_choice_needs_no_install_adapter(bundle):
    present(bundle, "base")
    present(bundle, "client")
    subject = coordinator(bundle, adapters={})
    state = start_auto(subject)
    assert state["ready"] and not bundle["calls"] and not bundle["transport"].calls
    assert state["steps"]["base"]["ownership"] == "external"
