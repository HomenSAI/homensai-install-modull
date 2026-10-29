"""Реальные файловые effects + injected manager; НЕ systemd/VM acceptance."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

if sys.platform != "linux":
    pytest.skip("Linux systemd user source tests; not Windows service acceptance", allow_module_level=True)

from homensai_install.errors import InstallerError
from homensai_install.engine import _digest as native_digest
from homensai_install.service_manager import SystemdUserManager, ServicePlan
from homensai_install.service_systemd import bus_value
from homensai_install.trust import canonical_bytes
from test_service_contracts import bundle_factory


class Interruption(BaseException):
    pass


class FakeManager:
    """Model protocol; не запускает shell, product, systemctl или systemd."""
    live = False

    def __init__(self, unit_dir):
        self.unit_dir = Path(unit_dir)
        self.expected = {}
        self.units = {}
        self.calls = []
        self.env = []
        self.populated = set()
        self.crash_after = None
        self.stop_timeout = False
        self.keep_writer = False
        self.checked_processes = []
        self.wrong_process = False

    def bind(self, plan):
        self.expected.update({item["configuration_file"]: item for item in plan.as_dict()["rendered"]["services"]})

    def _new(self, expected):
        name = expected["configuration_file"]
        def command(args):
            return [[args[0], args, False, 0, 0, 0, 0, 0, 0, 0]] if args else []
        return {"Id": name, "Names": [name], "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "FragmentPath": str(self.unit_dir / name), "SourcePath": "", "DropInPaths": [], "Transient": False,
            "NeedDaemonReload": False, "Job": [0, "/"], "UnitFileState": "disabled", "Type": "exec",
            "ExecStart": command(expected["start_argv"]), "ExecStop": command(expected["stop_argv"]),
            "ExecStartPre": [], "ExecStartPost": [], "ExecStopPost": [], "ExecCondition": [], "ExecReload": [],
            "WorkingDirectory": expected["working_directory"],
            "Environment": [key + "=" + value for key, value in expected["environment"].items()],
            "EnvironmentFiles": [], "PassEnvironment": [],
            "UnsetEnvironment": ["PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"],
            "User": "", "Group": "", "DynamicUser": False, "RootDirectory": "", "RootImage": "",
            "KillMode": "control-group", "KillSignal": 15, "SendSIGKILL": False, "NoNewPrivileges": True, "UMask": 0o077,
            "TimeoutStopUSec": expected["stop_timeout_seconds"] * 1000000,
            "TimeoutStartUSec": expected["stop_timeout_seconds"] * 1000000,
            "TimeoutStopFailureMode": "terminate", "TimeoutStartFailureMode": "terminate",
            "Restart": "on-failure", "RestartUSec": expected["restart"]["delay"] * 1000000,
            "StandardOutput": "null", "StandardError": "null", "MainPID": 0, "ControlPID": 0, "ControlGroup": "", "Result": "success"}

    def snapshot(self, name):
        return copy.deepcopy(self.units.get(name))

    def environment(self):
        return list(self.env)

    def command(self, action, name=None, *, timeout):
        self.calls.append((action, name, timeout))
        if action == "daemon-reload":
            for identity, expected in self.expected.items():
                if (self.unit_dir / identity).exists():
                    previous = self.units.get(identity)
                    unit = self._new(expected)
                    if previous:
                        for field in ("MainPID", "ControlPID", "ControlGroup", "ActiveState", "SubState", "Result"):
                            unit[field] = previous[field]
                    unit["UnitFileState"] = "enabled" if (self.unit_dir / "default.target.wants" / identity).is_symlink() else "disabled"
                    self.units[identity] = unit
                else:
                    self.units.pop(identity, None)
        elif action == "start":
            value = self.units[name]
            value.update(ActiveState="active", SubState="running", MainPID=4321,
                         ControlGroup="/user.slice/user-1000.slice/user@1000.service/app.slice/" + name)
            self.populated.add(value["ControlGroup"])
        elif action == "stop":
            value = self.units[name]
            group = value["ControlGroup"]
            value.update(ActiveState="failed" if self.stop_timeout else "inactive", SubState="failed" if self.stop_timeout else "dead",
                         MainPID=0, ControlPID=0, Result="timeout" if self.stop_timeout else "success", ControlGroup="")
            if not self.keep_writer and not self.stop_timeout:
                self.populated.discard(group)
        else:
            raise AssertionError(action)
        if self.crash_after == action:
            self.crash_after = None
            raise Interruption()

    def quiescent(self, name, group):
        return group not in self.populated

    def verify_process(self, snapshot, expected):
        self.checked_processes.append(snapshot["MainPID"])
        if self.wrong_process:
            raise InstallerError("OPERATION_CONFLICT", details={"reason": "service_process_identity_mismatch"})


@pytest.fixture
def rig(bundle_factory, tmp_path):
    manifest, target, options, verifier = bundle_factory()
    root = tmp_path / "installation пробел"
    control = root / ".hinstall"
    control.mkdir(parents=True, mode=0o700)
    (control / "installation.json").write_bytes(canonical_bytes({"schema_version": 1,
        "product_id": manifest["product_id"], "installation_id": "a" * 32,
        "owner": "uid:" + str(os.getuid()), "mode": "native", "installed": False}))
    shutil.copytree(options["payload_dir"], root / "releases/app-1.0.0")
    data = root / "data"
    data.mkdir()
    (data / "precious.sqlite").write_bytes(b"persistent content")
    units = tmp_path / "user-units"
    fake = FakeManager(units)
    manager = SystemdUserManager(verifier=verifier, runner=fake, unit_dir=units,
                                state_dir=control / "service-manager")

    class Rig:
        def plan(self, action, **kwargs):
            value = manager.plan(manifest, target, options, install_root=str(root),
                installation_id="a" * 32, modes=["local"], action=action, **kwargs)
            fake.bind(value)
            return value

        def apply(self, action, **kwargs):
            value = self.plan(action)
            return manager.apply(value, consent=value.digest, **kwargs)

    result = Rig()
    result.root, result.control, result.units = root, control, units
    result.manager, result.fake = manager, fake
    result.manifest, result.target, result.options, result.verifier = manifest, target, options, verifier
    return result


def reason(error):
    return error.value.context.get("details", {}).get("reason")


def test_full_owned_lifecycle_preserves_data_and_receipts(rig):
    initial = rig.plan("register")
    assert rig.manager.status(initial)["services"][0]["registered"] is False
    assert not rig.units.exists() and not (rig.control / "service-manager").exists()
    result = rig.manager.apply(initial, consent=initial.digest)
    assert result["evidence_scope"] == "injected-runner" and not result["production_ready"]
    name = initial.as_dict()["rendered"]["services"][0]["configuration_file"]
    assert (rig.units / name).is_file() and (rig.units / name).stat().st_mode & 0o777 == 0o600
    rig.apply("enable")
    assert (rig.units / "default.target.wants" / name).is_symlink()
    rig.apply("start")
    assert rig.fake.checked_processes == [4321]
    assert rig.manager.status(rig.plan("stop"))["services"][0]["active_state"] == "active"
    assert rig.apply("stop")["quiescent"]
    rig.apply("disable")
    rig.apply("uninstall")
    assert rig.manager.status(rig.plan("start"))["services"][0]["registered"] is False
    assert not (rig.units / name).exists()
    assert (rig.root / "data/precious.sqlite").read_bytes() == b"persistent content"
    assert (rig.root / "releases/app-1.0.0/runtime/python").is_file()
    assert all(call[0] in {"start", "stop", "daemon-reload"} for call in rig.fake.calls)
    state = json.loads((rig.control / "service-manager/state.json").read_bytes())
    assert len(state["operations"]) == 6
    assert all(item["status"] == "done" for item in state["operations"].values())


def test_exact_consent_before_any_effect_and_body_cannot_change(rig):
    plan = rig.plan("register")
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent="yes")
    assert reason(error) == "exact_service_plan_consent_required"
    assert not rig.units.exists() and not (rig.control / "service-manager").exists()
    rig.manager.apply(plan, consent=plan.digest)
    calls = list(rig.fake.calls)
    assert rig.manager.apply(plan, consent=plan.digest)["replayed"]
    assert rig.fake.calls == calls
    changed = plan.as_dict()
    changed["action"] = "start"
    edited = ServicePlan(canonical_bytes(changed))
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(edited, consent=edited.digest)
    assert reason(error) == "service_operation_body_changed"


@pytest.mark.parametrize("filename", ["runtime/python", "legal/new-notice.txt"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_entire_resigned_payload_is_bound_to_review_and_resume(rig, filename, interrupted):
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from homensai_install.trust import SignedPayloadVerifier
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    rig.manager.verifier = SignedPayloadVerifier({"review-test": public}, allow_test_keys=True)
    release = rig.root / "releases/app-1.0.0"
    def resign():
        inventory_path = Path(rig.options["inventory_file"])
        inventory = json.loads(inventory_path.read_bytes())
        inventory["files"] = {p.relative_to(release).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in release.rglob("*") if p.is_file()}
        inventory_path.write_bytes(canonical_bytes(inventory))
        Path(rig.options["signature_file"]).write_bytes(canonical_bytes({"algorithm":"ed25519",
            "key_id":"review-test", "signature":base64.b64encode(private.sign(canonical_bytes(inventory))).decode()}))
    resign()
    plan = rig.plan("register")
    if interrupted:
        rig.fake.crash_after = "daemon-reload"
        with pytest.raises(Interruption):
            rig.manager.apply(plan, consent=plan.digest)
    calls = list(rig.fake.calls)
    changed = release / filename
    changed.parent.mkdir(exist_ok=True)
    changed.write_bytes(b"new signed bytes under the same release and service descriptor")
    resign()
    with pytest.raises(InstallerError) as caught:
        rig.manager.apply(plan, consent=plan.digest, resume=interrupted)
    assert caught.value.code == "PLAN_STALE"
    assert rig.fake.calls == calls
    if not interrupted:
        assert not rig.units.exists()
        assert not (rig.control / "service-manager").exists()
        from homensai_install.engine import NativeEngine
        owner = NativeEngine()._owner({"control": rig.control, "manifest": rig.manifest})
        assert owner["installation_id"] == "a" * 32
    else:
        assert (rig.control / "service-manager/state.json").is_file()


def test_plan_revision_cas_prevents_stale_effect(rig):
    first = rig.plan("register")
    second = rig.plan("register")
    rig.manager.apply(first, consent=first.digest)
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(second, consent=second.digest)
    assert reason(error) == "service_plan_revision_changed"


def test_native_lifecycle_lock_blocks_service_apply_before_journal_and_unit_effects(rig):
    from shared.update_plan import operation_lock
    plan = rig.plan("register")
    path = rig.root.parent / (".hinstall-" + native_digest(str(rig.root))[:24] + ".lock")
    with operation_lock(path):
        held = path.read_bytes()
        inode = path.stat().st_ino
        with pytest.raises(InstallerError) as error:
            rig.manager.apply(plan, consent=plan.digest)
        assert error.value.code == "LOCK_BUSY"
        assert reason(error) == "installation_lifecycle_already_running"
        assert not (rig.control / "service-manager").exists()
        assert not rig.units.exists() and not rig.fake.calls
        assert path.read_bytes() == held
    assert rig.manager.apply(plan, consent=plan.digest)["status"] == "applied"
    assert path.stat().st_ino == inode  # Постоянный общий inode не заменяется.


def test_service_apply_holds_shared_lifecycle_lock_through_manager_effect(rig, monkeypatch):
    from shared.update_plan import operation_lock, LockBusy
    path = rig.root.parent / (".hinstall-" + native_digest(str(rig.root))[:24] + ".lock")
    original = rig.fake.command
    observed = []
    def command(*args, **kwargs):
        with pytest.raises(LockBusy):
            with operation_lock(path):
                raise AssertionError("Другой lifecycle не должен войти во время service effect")
        observed.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(rig.fake, "command", command)
    rig.apply("register")
    assert observed == ["daemon-reload"]


@pytest.mark.parametrize("action", ["register", "start", "stop", "enable", "disable", "uninstall"])
def test_interruption_after_manager_effect_requires_explicit_resume(rig, action):
    if action != "register":
        rig.apply("register")
    if action in {"stop", "uninstall"}:
        rig.apply("start")
    if action in {"disable", "uninstall"}:
        rig.apply("enable")
    plan = rig.plan(action)
    rig.fake.crash_after = action if action in {"start", "stop"} else "daemon-reload"
    with pytest.raises(Interruption):
        rig.manager.apply(plan, consent=plan.digest)
    count = len(rig.fake.calls)
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent=plan.digest)
    assert reason(error) == "explicit_service_resume_required"
    fresh = SystemdUserManager(verifier=rig.verifier, runner=rig.fake, unit_dir=rig.units,
                              state_dir=rig.control / "service-manager")
    assert fresh.apply(plan, consent=plan.digest, resume=True)["status"] == "applied"
    assert len(rig.fake.calls) == count


@pytest.mark.parametrize("field,value", [
    ("DropInPaths", ["/foreign/override.conf"]), ("FragmentPath", "/foreign.service"),
    ("NeedDaemonReload", True), ("User", "root"), ("SendSIGKILL", True),
    ("KillMode", "process"), ("ExecStartPre", [["/bin/false"]]), ("Transient", True),
    ("EnvironmentFiles", [["/secret", False]]), ("PassEnvironment", ["TOKEN"]),
    ("Names", ["unexpected.service"]), ("RootDirectory", "/foreign"),
])
def test_effective_manager_tamper_blocks_start_before_mutation(rig, field, value):
    rig.apply("register")
    name = next(iter(rig.fake.units))
    rig.fake.units[name][field] = value
    previous = list(rig.fake.calls)
    with pytest.raises(InstallerError):
        rig.apply("start")
    assert rig.fake.calls == previous


def test_effective_argv_and_unapproved_environment_are_rejected(rig):
    rig.apply("register")
    name = next(iter(rig.fake.units))
    rig.fake.units[name]["ExecStart"][0][1].append("injected")
    before = list(rig.fake.calls)
    with pytest.raises(InstallerError) as error:
        rig.apply("start")
    assert reason(error) == "effective_service_argv_mismatch"
    assert rig.fake.calls == before


def test_signed_runtime_is_reverified_from_installed_release(rig):
    rig.apply("register")
    plan = rig.plan("start")
    (rig.root / "releases/app-1.0.0/runtime/python").write_bytes(b"changed")
    before = list(rig.fake.calls)
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent=plan.digest)
    assert error.value.code == "INTEGRITY_FAILED" and rig.fake.calls == before


def test_existing_unit_with_identical_text_is_never_adopted(rig):
    plan = rig.plan("register")
    rig.units.mkdir(mode=0o700)
    for name, text in plan.as_dict()["rendered"]["files"].items():
        (rig.units / name).write_text(text)
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent=plan.digest)
    assert reason(error) == "existing_service_not_owned"
    assert not rig.fake.calls


def test_unit_file_hardlink_and_symlink_are_not_overwritten(rig, tmp_path):
    plan = rig.plan("register")
    rig.units.mkdir(mode=0o700)
    name = next(iter(plan.as_dict()["rendered"]["files"]))
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"keep")
    os.link(foreign, rig.units / name)
    with pytest.raises(InstallerError):
        rig.manager.apply(plan, consent=plan.digest)
    assert foreign.read_bytes() == b"keep"
    (rig.units / name).unlink()
    (rig.units / name).symlink_to(foreign)
    with pytest.raises(InstallerError):
        rig.manager.apply(plan, consent=plan.digest, resume=True)
    assert foreign.read_bytes() == b"keep" and not rig.fake.calls


def test_stopping_timeout_or_remaining_child_cannot_authorize_snapshot(rig):
    rig.apply("register")
    rig.apply("start")
    plan = rig.plan("stop")
    rig.fake.stop_timeout = True
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent=plan.digest)
    assert error.value.code == "RECOVERY_REQUIRED"
    count = len(rig.fake.calls)
    rig.fake.stop_timeout = False
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent=plan.digest, resume=True)
    assert reason(error) == "ungraceful_service_result_requires_recovery"
    assert len(rig.fake.calls) == count
    assert not any(call[0] in {"kill", "reset-failed"} for call in rig.fake.calls)


def test_main_pid_zero_does_not_hide_descendant_writer(rig):
    rig.apply("register")
    rig.apply("start")
    plan = rig.plan("stop")
    rig.fake.keep_writer = True
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent=plan.digest)
    assert error.value.code == "RECOVERY_REQUIRED"
    name = next(iter(rig.fake.units))
    assert rig.fake.units[name]["MainPID"] == 0
    assert rig.fake.populated
    rig.fake.populated.clear()
    count = len(rig.fake.calls)
    assert rig.manager.apply(plan, consent=plan.digest, resume=True)["quiescent"]
    assert len(rig.fake.calls) == count


def test_manager_environment_secret_never_reaches_start_or_error(rig):
    rig.apply("register")
    rig.fake.env = ["TOKEN=must-not-leak"]
    before = list(rig.fake.calls)
    with pytest.raises(InstallerError) as error:
        rig.apply("start")
    assert reason(error) == "manager_environment_not_allowlisted"
    assert "must-not-leak" not in json.dumps(error.value.to_dict())
    assert rig.fake.calls == before


def test_wrong_kernel_process_blocks_success_receipt(rig):
    rig.apply("register")
    rig.fake.wrong_process = True
    with pytest.raises(InstallerError) as error:
        rig.apply("start")
    assert reason(error) == "service_process_identity_mismatch"
    state = json.loads((rig.control / "service-manager/state.json").read_bytes())
    assert any(value["status"] == "running" for value in state["operations"].values())


def test_foreign_enable_link_cannot_be_deleted(rig, tmp_path):
    rig.apply("register")
    name = next(iter(rig.fake.units))
    wants = rig.units / "default.target.wants"
    wants.mkdir()
    outside = tmp_path / "outside.service"
    outside.write_text("keep")
    (wants / name).symlink_to(outside)
    before = list(rig.fake.calls)
    with pytest.raises(InstallerError) as error:
        rig.apply("disable")
    assert reason(error) == "foreign_enable_link"
    assert (wants / name).is_symlink() and outside.read_text() == "keep"
    assert rig.fake.calls == before


def test_file_write_crash_is_reconciled_without_adoption(rig, monkeypatch):
    plan = rig.plan("register")
    original = rig.manager._save
    def save(state):
        if any(value == "done" for operation in state["operations"].values() for value in operation["steps"].values()):
            raise Interruption()
        original(state)
    monkeypatch.setattr(rig.manager, "_save", save)
    with pytest.raises(Interruption):
        rig.manager.apply(plan, consent=plan.digest)
    assert list(rig.units.glob("*.service"))
    monkeypatch.setattr(rig.manager, "_save", original)
    result = rig.manager.apply(plan, consent=plan.digest, resume=True)
    assert result["status"] == "applied"


def test_process_exit_after_atomic_unit_publish_resumes_in_new_manager(rig):
    plan = rig.plan("register")
    pid = os.fork()
    if pid == 0:
        original = rig.manager._save
        def save(state):
            if any(value == "done" for operation in state["operations"].values() for value in operation["steps"].values()):
                os._exit(23)
            original(state)
        rig.manager._save = save
        rig.manager.apply(plan, consent=plan.digest)
        os._exit(24)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 23
    fresh = SystemdUserManager(verifier=rig.verifier, runner=rig.fake, unit_dir=rig.units,
                              state_dir=rig.control / "service-manager")
    assert fresh.apply(plan, consent=plan.digest, resume=True)["status"] == "applied"
    assert len(rig.fake.calls) == 1


def test_process_interruption_after_unlink_before_reload_resumes(rig, monkeypatch):
    rig.apply("register")
    rig.apply("start")
    rig.apply("stop")
    plan = rig.plan("uninstall")
    original = rig.manager._save
    def save(state):
        if any(key.endswith(":remove") and value == "done"
               for operation in state["operations"].values() for key, value in operation["steps"].items()):
            raise Interruption()
        original(state)
    monkeypatch.setattr(rig.manager, "_save", save)
    with pytest.raises(Interruption):
        rig.manager.apply(plan, consent=plan.digest)
    assert not list(rig.units.glob("*.service"))
    name = next(iter(rig.fake.units))
    rig.fake.units[name]["NeedDaemonReload"] = True
    monkeypatch.setattr(rig.manager, "_save", original)
    assert rig.manager.apply(plan, consent=plan.digest, resume=True)["quiescent"]
    assert name not in rig.fake.units


def test_service_lock_and_state_symlink_fail_without_following(rig, tmp_path):
    plan = rig.plan("register")
    destination = tmp_path / "outside"
    destination.write_text("keep")
    directory = rig.control / "service-manager"
    directory.mkdir()
    (directory / "lock").symlink_to(destination)
    with pytest.raises((InstallerError, OSError)):
        rig.manager.apply(plan, consent=plan.digest)
    assert destination.read_text() == "keep" and not rig.fake.calls


def test_writable_parent_and_identity_swap_are_rejected(rig, tmp_path):
    plan = rig.plan("register")
    rig.control.chmod(0o777)
    with pytest.raises(InstallerError):
        rig.manager.apply(plan, consent=plan.digest)
    rig.control.chmod(0o700)
    owner = json.loads((rig.control / "installation.json").read_bytes())
    owner["installation_id"] = "b" * 32
    (rig.control / "installation.json").write_bytes(canonical_bytes(owner))
    with pytest.raises(InstallerError) as error:
        rig.manager.apply(plan, consent=plan.digest)
    assert reason(error) == "installation_ownership_mismatch" and not rig.fake.calls


def test_owned_unit_replacement_requires_complete_stop(rig):
    rig.apply("register")
    rig.apply("start")
    before = list(rig.fake.calls)
    with pytest.raises(InstallerError) as error:
        rig.apply("register")
    assert reason(error) == "graceful_service_stop_not_confirmed"
    assert rig.fake.calls == before


@pytest.mark.parametrize("raw,signature", [
    (b'{"type":"s","data":["correct"]}', "s"),
    (b'{"type":"as","data":[["a b", "\\\"quote"]]}', "as"),
    (b'{"type":"b","data":[false]}', "b"),
])
def test_typed_busctl_body_is_decoded_without_shell_splitting(raw, signature):
    assert bus_value(raw, signature) == json.loads(raw)["data"][0]


@pytest.mark.parametrize("raw", [b'{"type":"s","data":[]}', b'{"type":"u","data":["wrong"]}',
    b'{"type":"s","type":"u","data":[1]}', b'{"type":"s","data":[true]}', b'not-json'])
def test_malformed_busctl_inventory_fails_closed(raw):
    with pytest.raises(InstallerError):
        bus_value(raw, "s")
