"""Source/contract tests. Daemon моделируется; ни один тест не является live Docker gate."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from test_docker_engine import SyntheticDaemon, check_error, docker_bundle, make_plan
from homensai_install.docker_cli import _arguments
from homensai_install.docker_commands import container_create, network_create
from homensai_install.docker_contract import load_verified_docker_contract, resource_plan
from homensai_install.docker_engine import DockerEngine, FOREIGN_FORMAT
from homensai_install.docker_lifecycle import CONTAINER_LIST_FORMAT, NETWORK_LIST_FORMAT, NETWORK_CONSUMER_FORMAT
from homensai_install.engine import EngineError
from homensai_install.api.v1 import InstallerAPI
from homensai_install.trust import canonical_bytes
from shared.update_plan import operation_lock, LockBusy


class PowerLoss(BaseException):
    pass


class ProvisioningDaemon(SyntheticDaemon):
    """Ограниченная stateful модель для эффектов и crash windows, не Docker."""

    def __init__(self, bundle):
        super().__init__(bundle.manifest, bundle.contract, bundle.spec, bundle.owner)
        self.container_templates = copy.deepcopy(self.containers)
        self.network_template = copy.deepcopy(self.network)
        self.containers, self.network = {}, None
        self.foreign_networks = {}
        self.before_effect = None

    def select_plan(self, bundle, plan):
        self.spec = plan["engine_plan"]["provisioning_spec"]
        self.owner = copy.deepcopy(bundle.owner)
        self.owner.update(installation_id=plan["engine_plan"]["installation_id"],
                          network={"id": "a" * 64, "name": self.spec["network"]["name"]})
        template = SyntheticDaemon(bundle.manifest, bundle.contract, self.spec, self.owner)
        self.container_templates = template.containers
        self.network_template = template.network
        self.contract = bundle.contract

    def _effect(self, args, name, function):
        _arguments(args)  # Реальный независимый ограничитель argv.
        self.calls.append(args)
        self.effects.append((args[0] + ":" + args[1], name))
        if self.before_effect:
            self.before_effect(args)
        result = function()
        if self.after_effect:
            self.after_effect(args)
        return SimpleNamespace(returncode=0, stdout=(result + "\n").encode())

    def run(self, args, *, timeout=30):
        args = tuple(args)
        if args == ("container", "ls", "--all", "--no-trunc", "--format", CONTAINER_LIST_FORMAT):
            self.calls.append(args)
            rows = [{"Id": cid, "Name": value["Name"].lstrip("/")} for cid, value in self.containers.items()]
            rows += [{"Id": cid, "Name": value.get("Name", "foreign-" + cid[:12])} for cid, value in self.foreign.items()]
            return SimpleNamespace(returncode=0, stdout=b"\n".join(canonical_bytes(row) for row in rows))
        if args == ("network", "ls", "--no-trunc", "--format", NETWORK_LIST_FORMAT):
            self.calls.append(args)
            rows = ([{"Id": self.network["Id"], "Name": self.network["Name"]}] if self.network else [])
            rows += [{"Id": nid, "Name": value["Name"]} for nid, value in self.foreign_networks.items()]
            return SimpleNamespace(returncode=0, stdout=b"\n".join(canonical_bytes(row) for row in rows))
        if args == ("container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}") and not self.containers and not self.foreign:
            self.calls.append(args)
            return SimpleNamespace(returncode=0, stdout=b"")
        if args[:3] == ("container", "inspect", "--format") and args[3] == FOREIGN_FORMAT and args[-1] in self.containers:
            self.calls.append(args)
            value = self.containers[args[-1]]
            return SimpleNamespace(returncode=0, stdout=canonical_bytes({"Id": value["Id"], "Mounts": value["Mounts"]}))
        if args[:3] == ("container", "inspect", "--format") and args[3] == NETWORK_CONSUMER_FORMAT:
            self.calls.append(args)
            value = self.foreign[args[-1]]
            return SimpleNamespace(returncode=0, stdout=canonical_bytes({"Id": args[-1],
                "NetworkMode": value.get("NetworkMode", "bridge"), "Networks": value.get("Networks", {})}))
        if args[:2] == ("network", "create"):
            def create():
                assert self.network is None
                self.network = copy.deepcopy(self.network_template)
                self.network["Name"] = args[-1]
                self.network["Labels"] = dict(args[index + 1].split("=", 1) for index, flag in enumerate(args) if flag == "--label")
                return self.network["Id"]
            return self._effect(args, "network", create)
        if args[:2] == ("container", "create"):
            name = next(name for name, item in self.spec["components"].items() if item["name"] == args[args.index("--name") + 1])
            cid = self.owner["containers"][name]
            def create():
                assert cid not in self.containers
                value = copy.deepcopy(self.container_templates[cid])
                value["Config"]["Labels"] = dict(args[index + 1].split("=", 1) for index, flag in enumerate(args) if flag == "--label")
                value["Config"]["Env"] = self.images[value["Image"]]["Config"]["Env"] + [args[index + 1] for index, flag in enumerate(args) if flag == "--env"]
                marker = args.index("--")
                value["Config"]["Image"], value["Config"]["Cmd"] = args[marker + 1], list(args[marker + 2:])
                value["Config"]["Entrypoint"] = [args[args.index("--entrypoint") + 1]]
                value["Config"]["User"] = args[args.index("--user") + 1]
                value["HostConfig"]["ReadonlyRootfs"] = "--read-only" in args
                # Docker может ещё не назначить endpoint до start.
                value["Networks"][self.spec["network"]["name"]]["NetworkID"] = ""
                self.containers[cid] = value
                return cid
            return self._effect(args, name, create)
        if len(args) == 4 and args[1:3] == ("rm", "--"):
            cid = args[-1]
            if args[0] == "container":
                name = next(name for name, own in self.owner["containers"].items() if own == cid)
                def remove():
                    assert self.containers[cid]["Running"] is False
                    del self.containers[cid]
                    return cid
            else:
                name = "network"
                def remove():
                    assert self.network["Id"] == cid and self.network["Containers"] == {}
                    self.network = None
                    return cid
            return self._effect(args, name, remove)
        return super().run(args, timeout=timeout)

    def state(self, name, running, *, exit_code=0):
        super().state(name, running, exit_code=exit_code)
        if running:
            self.containers[self.owner["containers"][name]]["Networks"][self.spec["network"]["name"]]["NetworkID"] = self.owner["network"]["id"]


@pytest.fixture
def fresh_bundle(docker_bundle, monkeypatch):
    b = docker_bundle
    b.files["legal/ru.txt"] = "Синтетические условия; этот fixture не является продуктом.".encode()
    b.options.update(eula_locale="ru", eula_digest=hashlib.sha256(b.files["legal/ru.txt"]).hexdigest())
    b.sign()
    b.contract = load_verified_docker_contract(b.manifest, b.target, b.options, verifier=b.verifier)
    b.spec = resource_plan(b.contract, install_root=str(b.root), installation_id=b.owner["installation_id"], modes=["local"])
    b.owner["contract_sha256"] = b.contract.contract_sha256
    shutil.rmtree(b.root)  # Только искусственный fixture, не действие installer.
    b.daemon = ProvisioningDaemon(b)
    b.engine = DockerEngine(verifier=b.verifier, runner=b.daemon, secret_provider=lambda ref: "fixture-long-password",
        fetcher=b.daemon.fetcher, host_probe=lambda endpoint: {"mnt": "1:100", "net": "1:200"})
    # CaaS запускает pytest как root. Модель не исполняет container user 1000;
    # отдельный тест ниже проверяет настоящий отказ UID/GID preflight.
    monkeypatch.setattr(b.engine, "_install_user", lambda spec: None)
    return b


def install_plan(b):
    plan = make_plan(b, "install")
    b.daemon.select_plan(b, plan)
    return plan


def installed(b):
    plan = install_plan(b)
    assert b.engine.execute(plan)["status"] == "SUCCEEDED"
    return b


def journal(b, plan):
    token = hashlib.sha256(plan["plan_id"].encode()).hexdigest()
    return b.root / ".hinstall" / "docker-operations" / (token + ".json")


def test_fresh_discover_and_review_do_not_create_directories_or_resources(fresh_bundle):
    b = fresh_bundle
    b.options.pop("eula_digest")
    inventory = b.engine.discover(b.manifest, b.target, b.options)
    assert inventory["installation"] is None and inventory["read_only"] is True
    assert not b.root.exists() and b.daemon.effects == []
    check_error(lambda: b.engine.plan("install", b.manifest, b.target, inventory, b.options), "CONSENT_REQUIRED")
    assert not b.root.exists() and b.daemon.effects == []


def test_actual_nonroot_bind_identity_guard_is_not_bypassed_by_root(fresh_bundle):
    b = fresh_bundle
    spec = next(iter(b.spec["components"].values())) | {"user": "999999:999998"}
    error = check_error(lambda: DockerEngine._install_user(b.engine, spec), "CAPABILITY_UNSUPPORTED")
    assert error.details["reason"] == "private_bind_mount_uid_gid_must_match_installer"
    assert not b.root.exists()


def test_install_provisions_digest_pinned_resources_and_retains_verified_release(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    result = b.engine.execute(plan)
    assert result["status"] == "SUCCEEDED" and result["health"]["status"] == "HEALTHY"
    assert result["production_ready"] is False
    assert b.daemon.effects == [("network:create", "network"), ("container:create", "db"), ("container:create", "web"), ("start", "db"), ("start", "web")]
    owner = json.loads(b.receipt.read_text())
    assert owner["installation_id"] == plan["engine_plan"]["installation_id"]
    assert owner["containers"] == b.daemon.owner["containers"] and owner["network"] == b.daemon.owner["network"]
    assert b.receipt.stat().st_mode & 0o777 == 0o600
    metadata = json.loads(Path(result["recovery"]["metadata_path"]).read_text())
    retained = load_verified_docker_contract(metadata["manifest"], metadata["target"], metadata["options"], verifier=b.verifier)
    assert retained == b.contract
    creates = [args for args in b.daemon.calls if args[:2] == ("container", "create")]
    assert len(creates) == 2 and all("--pull=never" in args for args in creates)
    assert all("--read-only" in args and "--cap-drop" in args and "ALL" in args and "--log-driver" in args for args in creates)
    assert "--publish" not in creates[0]
    assert creates[1][creates[1].index("--publish") + 1] == "127.0.0.1:8820:8080/tcp"
    assert not any(args[0] in {"pull", "build", "compose"} for args in b.daemon.calls)
    assert b.engine.discover(b.manifest, b.target, b.options)["installation"]["state"] == "INSTALLED"


def test_uninstall_removes_only_owned_containers_and_network_preserves_data_images(fresh_bundle):
    b = installed(fresh_bundle)
    data = b.root / "data" / "user.db"
    data.write_bytes(b"real-user-data-fixture\0unchanged")
    original = {path: path.read_bytes() for path in b.root.rglob("*") if path.is_file() and ".hinstall" not in path.parts}
    images = copy.deepcopy(b.daemon.images)
    b.options.pop("eula_digest")  # Удаление не требует повторного согласия EULA.
    b.daemon.effects.clear()
    plan = make_plan(b, "uninstall")
    result = b.engine.execute(plan)
    assert result["status"] == "SUCCEEDED" and result["health"]["status"] == "UNINSTALLED"
    assert result["preserves_user_data"] is True and result["images_retained"] is True
    assert b.daemon.effects == [("kill", "web"), ("kill", "db"), ("container:rm", "web"), ("container:rm", "db"), ("network:rm", "network")]
    assert b.daemon.images == images and b.daemon.network is None and b.daemon.containers == {}
    assert all(path.read_bytes() == raw for path, raw in original.items())
    assert b.receipt.exists() and Path(result["recovery"]["metadata_path"]).exists()
    assert b.engine.discover(b.manifest, b.target, b.options)["installation"]["state"] == "UNINSTALLED"
    assert not any("--force" in args or "--volumes" in args or "prune" in args for args in b.daemon.calls)


@pytest.mark.parametrize("effect", [("network", "create"), ("container", "create"), ("container", "start")])
def test_crash_after_daemon_create_or_start_is_reconciled_without_duplicate_effect(fresh_bundle, effect):
    b = fresh_bundle
    plan = install_plan(b)
    def interrupt(args):
        if args[:2] == effect:
            raise PowerLoss()
    b.daemon.after_effect = interrupt
    with pytest.raises(PowerLoss):
        b.engine.execute(plan)
    record = json.loads(journal(b, plan).read_text())
    assert record["status"] == "RUNNING"
    b.daemon.after_effect = None
    result = b.engine.resume(plan, {})
    assert result["status"] == "SUCCEEDED"
    assert b.daemon.effects.count(("network:create", "network")) == 1
    assert b.daemon.effects.count(("container:create", "db")) == 1
    assert b.daemon.effects.count(("container:create", "web")) == 1
    assert b.daemon.effects.count(("start", "db")) == 1
    assert b.daemon.effects.count(("start", "web")) == 1


@pytest.mark.parametrize("kind", ["container", "network"])
def test_crash_after_remove_resumes_only_remaining_owned_resources(fresh_bundle, kind):
    b = installed(fresh_bundle)
    plan = make_plan(b, "uninstall")
    def interrupt(args):
        if args[:2] == (kind, "rm"):
            raise PowerLoss()
    b.daemon.after_effect = interrupt
    with pytest.raises(PowerLoss):
        b.engine.execute(plan)
    b.daemon.after_effect = None
    assert b.engine.discover(b.manifest, b.target, b.options)["installation"]["state"] == "UNINSTALLING"
    result = b.engine.resume(plan, {})
    assert result["status"] == "SUCCEEDED"
    assert b.daemon.effects.count(("container:rm", "web")) == 1
    assert b.daemon.effects.count(("container:rm", "db")) == 1
    assert b.daemon.effects.count(("network:rm", "network")) == 1


def test_private_intent_is_durable_before_every_external_create_and_remove(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    def assert_before(args):
        record = json.loads(journal(b, plan).read_text())
        name = "network" if args[0] == "network" else next(name for name, spec in b.daemon.spec["components"].items()
            if spec["name"] == args[args.index("--name") + 1])
        assert record["effects"]["create:" + args[0] + ":" + name] == "INTENT"
        assert record["effects"]["retain:release"] == "CONFIRMED"
        assert all((b.root / root).stat().st_mode & 0o777 == 0o700 for root in b.manifest["paths"])
        assert not b.receipt.exists()
    b.daemon.before_effect = assert_before
    assert b.engine.execute(plan)["status"] == "SUCCEEDED"
    plan = make_plan(b, "uninstall")
    def assert_remove(args):
        record = json.loads(journal(b, plan).read_text())
        name = "network" if args[0] == "network" else next(name for name, cid in b.daemon.owner["containers"].items() if cid == args[-1])
        assert record["effects"]["remove:" + args[0] + ":" + name] == "INTENT"
        assert record["effects"]["retain:release"] == "CONFIRMED"
        assert "stop" in record["completed"]
    b.daemon.before_effect = assert_remove
    assert b.engine.execute(plan)["status"] == "SUCCEEDED"


@pytest.mark.parametrize("phase", ["prepare", "retain_release", "provision", "publish_receipt", "start", "health"])
def test_install_resume_revalidates_each_completed_checkpoint(fresh_bundle, phase):
    b = fresh_bundle
    plan = install_plan(b)
    def interrupt(event):
        if event.get("phase") == phase and event.get("status") == "SUCCEEDED" and "component_id" not in event:
            raise PowerLoss()
    with pytest.raises(PowerLoss):
        b.engine.execute(plan, emit=interrupt)
    result = b.engine.resume(plan, {})
    assert result["status"] == "SUCCEEDED"
    assert b.daemon.effects.count(("network:create", "network")) == 1
    assert all(b.daemon.effects.count((operation, name)) == 1 for operation in ("container:create", "start") for name in ("db", "web"))


def test_unknown_create_with_no_observed_resource_is_not_replayed(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    b.daemon.before_effect = lambda args: (_ for _ in ()).throw(PowerLoss())
    with pytest.raises(PowerLoss):
        b.engine.execute(plan)
    b.daemon.before_effect = None
    before = list(b.daemon.effects)
    error = check_error(lambda: b.engine.resume(plan, {}), "RECOVERY_REQUIRED")
    assert error.details["reason"] == "uncertain_create_not_replayed_without_exact_resource"
    assert b.daemon.effects == before and b.daemon.network is None
    inventory = b.engine.discover(b.manifest, b.target, b.options)
    assert inventory["unfinished_operations"]
    check_error(lambda: b.engine.plan("install", b.manifest, b.target, inventory, b.options), "OPERATION_CONFLICT")


def test_unknown_remove_with_resource_still_present_is_not_replayed(fresh_bundle):
    b = installed(fresh_bundle)
    plan = make_plan(b, "uninstall")
    b.daemon.before_effect = lambda args: (_ for _ in ()).throw(PowerLoss())
    with pytest.raises(PowerLoss):
        b.engine.execute(plan)
    b.daemon.before_effect = None
    effects = list(b.daemon.effects)
    error = check_error(lambda: b.engine.resume(plan, {}), "RECOVERY_REQUIRED")
    assert error.details["reason"] == "uncertain_remove_not_replayed"
    assert b.daemon.effects == effects and len(b.daemon.containers) == 2


def test_cancel_after_provision_keeps_one_identity_and_explicit_resume(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    canceled = False
    def events(event):
        nonlocal canceled
        if event.get("phase") == "provision" and event.get("status") == "SUCCEEDED":
            canceled = True
    result = b.engine.execute(plan, events, lambda: canceled)
    assert result["status"] == "CANCELED" and result["resume_required"] is True
    assert not b.receipt.exists() and len(b.daemon.containers) == 2
    assert b.engine.resume(plan, {})["status"] == "SUCCEEDED"
    assert b.daemon.effects.count(("network:create", "network")) == 1
    assert json.loads(b.receipt.read_text())["installation_id"] == plan["engine_plan"]["installation_id"]


def test_replaced_data_directory_blocks_resume_before_any_daemon_create(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    def interrupt(event):
        if event.get("phase") == "prepare" and event.get("status") == "SUCCEEDED":
            raise PowerLoss()
    with pytest.raises(PowerLoss):
        b.engine.execute(plan, interrupt)
    old = b.root / "data"
    old.rename(b.root / "old-data")
    old.mkdir(mode=0o700)
    check_error(lambda: b.engine.resume(plan, {}), "PLAN_STALE")
    assert b.daemon.effects == []


@pytest.mark.parametrize("change", ["image_id", "repo_digest", "arch", "volumes", "onbuild"])
def test_unavailable_or_unsafe_image_fails_before_creating_install_root(fresh_bundle, change):
    b = fresh_bundle
    image = next(iter(b.daemon.images.values()))
    if change == "image_id":
        image["Id"] = "sha256:" + "f" * 64
    elif change == "repo_digest":
        image["RepoDigests"] = []
    elif change == "arch":
        image["Architecture"] = "arm64"
    elif change == "volumes":
        image["Config"]["Volumes"] = {"/secret-volume": {}}
    else:
        image["Config"]["OnBuild"] = ["RUN unreviewed"]
    with pytest.raises(EngineError):
        install_plan(b)
    assert not b.root.exists() and b.daemon.effects == []


def test_new_resource_configuration_drift_is_not_adopted_or_removed(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    def corrupt(args):
        if args[:2] == ("container", "create"):
            next(iter(b.daemon.containers.values()))["HostConfig"]["Privileged"] = True
    b.daemon.after_effect = corrupt
    check_error(lambda: b.engine.execute(plan), "INTEGRITY_FAILED")
    assert not b.receipt.exists()
    assert not any(operation in {"start", "container:rm", "network:rm"} for operation, _ in b.daemon.effects)
    record = json.loads(journal(b, plan).read_text())
    assert record["effects"]["create:container:db"] == "INTENT" and record["owner"]["containers"] == {}


def test_duplicate_network_name_after_uncertain_create_is_not_adopted(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    def interrupt(args):
        if args[:2] == ("network", "create"):
            raise PowerLoss()
    b.daemon.after_effect = interrupt
    with pytest.raises(PowerLoss):
        b.engine.execute(plan)
    b.daemon.after_effect = None
    b.daemon.foreign_networks["f" * 64] = {"Name": b.daemon.network["Name"]}
    check_error(lambda: b.engine.resume(plan, {}), "RECOVERY_REQUIRED")
    assert b.daemon.effects == [("network:create", "network")]


def test_uninstall_works_when_image_store_and_data_root_are_unavailable(fresh_bundle):
    b = installed(fresh_bundle)
    b.daemon.images.clear()
    (b.root / "data").rmdir()
    inventory = b.engine.discover(b.manifest, b.target, b.options)
    assert inventory["integrity"]["state"] == "OWNERSHIP_ONLY"
    check_error(lambda: b.engine.plan("start", b.manifest, b.target, inventory, b.options), "RECOVERY_REQUIRED")
    result = b.engine.execute(make_plan(b, "uninstall"))
    assert result["status"] == "SUCCEEDED" and result["data_available"] is False
    assert not (b.root / "data").exists()


def test_uninstall_preserves_network_used_by_foreign_stopped_container(fresh_bundle):
    b = installed(fresh_bundle)
    b.daemon.foreign["f" * 64] = {"Id": "f" * 64, "Mounts": [], "NetworkMode": b.daemon.network["Name"], "Networks": {}}
    error = check_error(lambda: b.engine.execute(make_plan(b, "uninstall")), "OPERATION_CONFLICT")
    assert error.details["reason"] == "foreign_container_depends_on_owned_network"
    assert b.daemon.network is not None and "f" * 64 in b.daemon.foreign
    assert ("network:rm", "network") not in b.daemon.effects


def test_preserved_release_can_manage_uninstall_after_original_payload_is_gone(fresh_bundle):
    b = fresh_bundle
    result = b.engine.execute(install_plan(b))
    metadata = json.loads(Path(result["recovery"]["metadata_path"]).read_text())
    shutil.rmtree(b.payload)
    Path(b.options["inventory_file"]).unlink()
    Path(b.options["signature_file"]).unlink()
    b.options = metadata["options"]
    result = b.engine.execute(make_plan(b, "uninstall"))
    assert result["status"] == "SUCCEEDED" and result["recovery"]["contains_images"] is False


def test_same_install_plan_resumes_from_retained_signed_bytes_after_source_cache_loss(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    def interrupt(args):
        if args[:2] == ("container", "create"):
            raise PowerLoss()
    b.daemon.after_effect = interrupt
    with pytest.raises(PowerLoss):
        b.engine.execute(plan)
    original_binding = json.loads(journal(b, plan).read_text())["binding"]
    shutil.rmtree(b.payload)
    Path(b.options["inventory_file"]).unlink()
    Path(b.options["signature_file"]).unlink()
    b.daemon.after_effect = None
    assert b.engine.resume(plan, {})["status"] == "SUCCEEDED"
    assert json.loads(journal(b, plan).read_text())["binding"] == original_binding
    assert b.daemon.effects.count(("container:create", "db")) == 1
    assert b.engine.execute(make_plan(b, "uninstall"))["status"] == "SUCCEEDED"


def test_existing_corrupt_source_is_not_hidden_by_good_retained_copy(fresh_bundle):
    b = installed(fresh_bundle)
    (b.payload / "legal/ru.txt").write_text("tampered original still present")
    effects = list(b.daemon.effects)
    check_error(lambda: b.engine.discover(b.manifest, b.target, b.options), "INTEGRITY_FAILED")
    assert b.daemon.effects == effects


def test_missing_source_never_trusts_tampered_retained_signature(fresh_bundle):
    b = fresh_bundle
    result = b.engine.execute(install_plan(b))
    metadata = json.loads(Path(result["recovery"]["metadata_path"]).read_text())
    signature = Path(metadata["options"]["signature_file"])
    signature.write_text('{"algorithm":"ed25519","key_id":"test","signature":"changed"}')
    shutil.rmtree(b.payload)
    effects = list(b.daemon.effects)
    check_error(lambda: b.engine.discover(b.manifest, b.target, b.options), "INTEGRITY_FAILED")
    assert b.daemon.effects == effects


def test_completed_uninstall_receipt_does_not_hide_tampered_recovery_bytes(fresh_bundle):
    b = installed(fresh_bundle)
    plan = make_plan(b, "uninstall")
    result = b.engine.execute(plan)
    path = Path(result["recovery"]["payload_dir"]) / "legal/ru.txt"
    path.write_text("changed owned recovery bytes")
    effects = list(b.daemon.effects)
    check_error(lambda: b.engine.resume(plan, {}), "INTEGRITY_FAILED")
    assert b.daemon.effects == effects


def test_partial_install_root_lock_blocks_other_api_process(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    canceled = False
    def event(value):
        nonlocal canceled
        if value.get("phase") == "prepare" and value.get("status") == "SUCCEEDED":
            canceled = True
    assert b.engine.execute(plan, event, lambda: canceled)["status"] == "CANCELED"
    with operation_lock(b.root / ".hinstall" / "docker.lock"):
        with pytest.raises(LockBusy):
            b.engine.resume(plan, {})
    assert b.daemon.effects == []


def test_retained_copy_intent_can_resume_after_partial_temporary_file(fresh_bundle, monkeypatch):
    b = fresh_bundle
    plan = install_plan(b)
    original = b.engine._retain_file
    interrupted = False
    def copy_with_crash(ctx, state, source, destination, expected):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            b.engine._private_recovery_directory(destination.parent)
            temporary = ".copy-" + "a" * 32 + ".tmp"
            relative = destination.relative_to(ctx["control"]).as_posix()
            state.setdefault("copies", {})[relative] = {"temporary": temporary, "sha256": expected}
            b.engine._save(ctx, state)
            path = destination.with_name(temporary)
            path.write_bytes(b"incomplete durable copy")
            path.chmod(0o600)
            raise PowerLoss()
        return original(ctx, state, source, destination, expected)
    monkeypatch.setattr(b.engine, "_retain_file", copy_with_crash)
    with pytest.raises(PowerLoss):
        b.engine.execute(plan)
    assert b.engine.resume(plan, {})["status"] == "SUCCEEDED"
    assert not list((b.root / ".hinstall" / "docker-recovery").rglob(".copy-*.tmp"))


@pytest.mark.parametrize("mutation", ["pull", "root_user", "host_network", "capability", "security", "tmpfs", "readonly", "secret_env", "duplicate_label", "mutable_image", "mount_csv", "entrypoint_flag"])
def test_provision_argv_rejects_privilege_and_parser_expansion(fresh_bundle, mutation):
    b = fresh_bundle
    spec = next(iter(b.spec["components"].values()))
    valid = container_create(spec)
    assert _arguments(valid) == valid
    args = list(valid)
    if mutation == "pull":
        args[args.index("--pull=never")] = "--pull=always"
    elif mutation == "root_user":
        args[args.index("--user") + 1] = "0:0"
    elif mutation == "host_network":
        args[args.index("--network") + 1] = "host"
    elif mutation == "capability":
        args[args.index("--cap-drop") + 1] = "NET_ADMIN"
    elif mutation == "security":
        args[args.index("--security-opt") + 1] = "seccomp=unconfined"
    elif mutation == "tmpfs":
        args[args.index("--tmpfs") + 1] = "/tmp:rw,exec"
    elif mutation == "readonly":
        args.remove("--read-only")
    elif mutation == "secret_env":
        pos = args.index("--env")
        args[pos:pos] = ["--env", "LD_PRELOAD=/unreviewed/library"]
    elif mutation == "duplicate_label":
        pos = args.index("--label")
        args[pos:pos] = args[pos:pos + 2]
    elif mutation == "mutable_image":
        args[args.index("--") + 1] = "some-image:latest"
    elif mutation == "mount_csv":
        args[args.index("--mount") + 1] += ",src=/var/run/docker.sock"
    else:
        args[args.index("--entrypoint") + 1] = "--privileged"
    check_error(lambda: _arguments(tuple(args)), "INVALID_REQUEST")
    assert b.daemon.effects == []


def test_network_command_cannot_disable_isolation_or_inject_driver_options(fresh_bundle):
    args = network_create(fresh_bundle.spec["network"])
    assert _arguments(args) == args
    for changed in (tuple(value for value in args if value != "--internal"),
            args[:3] + ("host",) + args[4:], args[:-2] + ("--opt", "com.docker.network.bridge.enable_icc=true") + args[-2:]):
        check_error(lambda: _arguments(changed), "INVALID_REQUEST")


@pytest.mark.parametrize("version", ["27.5.1", "20.10.24", "unknown", None])
def test_old_or_unverifiable_daemon_cannot_publish_loopback_but_allows_removal(fresh_bundle, version):
    b = installed(fresh_bundle)
    b.daemon.info["ServerVersion"] = version
    caps = b.engine.capabilities(b.target)
    assert caps["actions"]["install"]["supported"] is False
    assert caps["actions"]["start"]["supported"] is False
    assert caps["actions"]["uninstall"]["supported"] is True
    check_error(lambda: make_plan(b, "start"), "CAPABILITY_UNSUPPORTED")
    assert b.engine.execute(make_plan(b, "uninstall"))["status"] == "SUCCEEDED"


def test_json_is_private_before_atomic_publication_even_with_permissive_umask(fresh_bundle, monkeypatch):
    b = fresh_bundle
    plan = install_plan(b)
    original = os.replace
    observed = []
    def inspect_mode(source, destination):
        if Path(source).name.startswith(".docker-write-"):
            observed.append(Path(source).stat().st_mode & 0o777)
        return original(source, destination)
    monkeypatch.setattr(os, "replace", inspect_mode)
    previous = os.umask(0)
    try:
        assert b.engine.execute(plan)["status"] == "SUCCEEDED"
    finally:
        os.umask(previous)
    assert observed and set(observed) == {0o600}


def test_fresh_state_is_rechecked_under_lock_before_creating_new_journal(fresh_bundle, monkeypatch):
    b = fresh_bundle
    plan = install_plan(b)
    original = b.engine._bootstrap_install
    def concurrent_completion(ctx):
        original(ctx)
        (b.root / "data").mkdir(mode=0o700)
        (b.root / "data" / "someone-else.db").write_bytes(b"must remain unchanged")
    monkeypatch.setattr(b.engine, "_bootstrap_install", concurrent_completion)
    check_error(lambda: b.engine.execute(plan), "OPERATION_CONFLICT")
    assert not journal(b, plan).exists() and b.daemon.effects == []
    assert (b.root / "data" / "someone-else.db").read_bytes() == b"must remain unchanged"


def test_unpublished_first_journal_temporary_is_not_an_ownership_receipt(fresh_bundle):
    b = fresh_bundle
    plan = install_plan(b)
    operations = b.root / ".hinstall" / "docker-operations"
    operations.mkdir(parents=True, mode=0o700)
    b.root.chmod(0o700)
    operations.parent.chmod(0o700)
    abandoned = operations / (".docker-write-" + "a" * 32 + ".tmp")
    abandoned.write_bytes(b'{"incomplete')
    abandoned.chmod(0o600)
    assert b.engine.execute(plan)["status"] == "SUCCEEDED"
    assert b.daemon.effects.count(("network:create", "network")) == 1
    assert abandoned.read_bytes() == b'{"incomplete'


def test_public_api_review_consent_submit_and_uninstall_use_same_docker_executor(fresh_bundle):
    b = fresh_bundle
    api = InstallerAPI(b.root.parent / "api-state", engine=b.engine)
    plan = api.plan("install", b.manifest, b.target, options=b.options)
    b.daemon.select_plan(b, plan)
    check_error(lambda: api.submit(plan["plan_id"], plan["plan_digest"], [], idempotency_key="install", background=False), "CONSENT_REQUIRED")
    assert b.daemon.effects == [] and not b.root.exists()
    operation = api.submit(plan["plan_id"], plan["plan_digest"], plan["required_consents"] + [plan["review_consent_ref"]],
        idempotency_key="install", background=False)
    assert operation["status"] == "SUCCEEDED", operation
    assert operation["result"]["installation_id"] == plan["engine_plan"]["installation_id"]
    uninstall = api.plan("uninstall", b.manifest, b.target, options=b.options)
    assert uninstall["required_consents"] == ["action:uninstall"]
    removed = api.submit(uninstall["plan_id"], uninstall["plan_digest"], ["action:uninstall", uninstall["review_consent_ref"]],
        idempotency_key="uninstall", background=False)
    assert removed["status"] == "SUCCEEDED", removed
    assert removed["result"]["preserves_user_data"] is True


def test_unwritable_confirmation_journal_leaves_create_intent_for_resume(fresh_bundle, monkeypatch):
    b = fresh_bundle
    plan = install_plan(b)
    original = b.engine._save
    def disk_full(ctx, state):
        if state["effects"].get("create:container:db") == "CONFIRMED":
            raise OSError("simulated full filesystem")
        return original(ctx, state)
    monkeypatch.setattr(b.engine, "_save", disk_full)
    with pytest.raises(OSError):
        b.engine.execute(plan)
    assert json.loads(journal(b, plan).read_text())["effects"]["create:container:db"] == "INTENT"
    assert b.daemon.effects.count(("container:create", "db")) == 1
    monkeypatch.setattr(b.engine, "_save", original)
    assert b.engine.resume(plan, {})["status"] == "SUCCEEDED"
    assert b.daemon.effects.count(("container:create", "db")) == 1


@pytest.mark.parametrize("checkpoint", ["before_execute", "after_prepare"])
def test_reviewed_plan_rejects_resigned_payload_change(fresh_bundle, checkpoint):
    b = fresh_bundle
    plan = install_plan(b)
    if checkpoint == "after_prepare":
        def stop(event):
            if event.get("phase") == "prepare" and event.get("status") == "SUCCEEDED" and "component_id" not in event:
                raise PowerLoss()
        with pytest.raises(PowerLoss):
            b.engine.execute(plan, emit=stop)
    b.files["legal/fixture-notices.txt"] = b"Changed signed notices after plan review."
    b.sign()
    effects = list(b.daemon.effects)
    check_error(lambda: b.engine.resume(plan, {}), "PLAN_STALE")
    assert b.daemon.effects == effects


def test_new_install_receipt_pins_all_release_bytes_across_later_plans(fresh_bundle):
    b = installed(fresh_bundle)
    receipt = json.loads(b.receipt.read_text())
    assert receipt["schema_version"] == 2 and receipt["payload_sha256"] == b.contract.payload_sha256
    b.files["legal/fixture-notices.txt"] = b"A different publisher-approved release under the same version."
    b.sign()
    effects = list(b.daemon.effects)
    error = check_error(lambda: b.engine.discover(b.manifest, b.target, b.options), "INTEGRITY_FAILED")
    assert error.details["reason"] == "installed_full_payload_pin_mismatch"
    assert b.daemon.effects == effects


def test_legacy_receipt_still_requires_full_payload_pin_for_new_operation(docker_bundle):
    b = docker_bundle
    plan = make_plan(b, "start")
    b.files["legal/fixture-notices.txt"] = b"Changed signed notices after legacy start review."
    b.sign()
    check_error(lambda: b.engine.execute(plan), "PLAN_STALE")
    assert b.daemon.effects == []


def test_component_named_network_has_separate_create_and_remove_intents(fresh_bundle, monkeypatch):
    b = fresh_bundle
    b.manifest["components"][0]["name"] = "network"
    b.manifest["components"][1]["after"] = ["network"]
    b.manifest["modes"][0]["components"] = ["network", "web"]
    b.document["components"][0]["name"] = "network"
    review = json.loads(b.files["legal/images.json"])
    review["images"]["network"] = review["images"].pop("db")
    b.files["legal/images.json"] = canonical_bytes(review)
    b.owner["containers"]["network"] = b.owner["containers"].pop("db")
    b.sign()
    b.contract = load_verified_docker_contract(b.manifest, b.target, b.options, verifier=b.verifier)
    b.spec = resource_plan(b.contract, install_root=str(b.root), installation_id=b.owner["installation_id"], modes=["local"])
    b.owner["contract_sha256"] = b.contract.contract_sha256
    b.daemon = ProvisioningDaemon(b)
    b.engine = DockerEngine(verifier=b.verifier, runner=b.daemon, fetcher=b.daemon.fetcher,
        host_probe=lambda endpoint: {"mnt": "1:100", "net": "1:200"})
    monkeypatch.setattr(b.engine, "_install_user", lambda spec: None)
    plan = install_plan(b)
    assert b.engine.execute(plan)["status"] == "SUCCEEDED"
    state = json.loads(journal(b, plan).read_text())
    assert state["effects"]["create:network:network"] == "CONFIRMED"
    assert state["effects"]["create:container:network"] == "CONFIRMED"
    assert "create:network:network" in state["before_create"] and "create:container:network" in state["before_create"]
    remove = make_plan(b, "uninstall")
    assert b.engine.execute(remove)["status"] == "SUCCEEDED"
    state = json.loads(journal(b, remove).read_text())
    assert state["effects"]["remove:network:network"] == "CONFIRMED"
    assert state["effects"]["remove:container:network"] == "CONFIRMED"


@pytest.mark.parametrize("reference", ["full_id", "short_id", "very_short_id", "name", "slash_name"])
def test_foreign_namespace_reference_is_resolved_before_stopping_donor(fresh_bundle, reference):
    b = installed(fresh_bundle)
    cid = b.daemon.owner["containers"]["db"]
    name = b.daemon.spec["components"]["db"]["name"]
    selected = {"full_id": cid, "short_id": cid[:12], "very_short_id": cid[:4], "name": name, "slash_name": "/" + name}[reference]
    b.daemon.foreign["f" * 64] = {"Id": "f" * 64, "Mounts": [], "NetworkMode": "container:" + selected, "Networks": {}}
    effects = list(b.daemon.effects)
    error = check_error(lambda: b.engine.execute(make_plan(b, "uninstall")), "OPERATION_CONFLICT")
    assert error.details["reason"] == "foreign_container_depends_on_owned_namespace"
    assert b.daemon.effects == effects
    assert all(value["Running"] for value in b.daemon.containers.values())


def test_unresolved_foreign_namespace_is_not_guessed_before_removal(fresh_bundle):
    b = installed(fresh_bundle)
    b.daemon.foreign["f" * 64] = {"Id": "f" * 64, "Mounts": [], "NetworkMode": "container:missing-donor", "Networks": {}}
    effects = list(b.daemon.effects)
    check_error(lambda: b.engine.execute(make_plan(b, "uninstall")), "RECOVERY_REQUIRED")
    assert b.daemon.effects == effects
