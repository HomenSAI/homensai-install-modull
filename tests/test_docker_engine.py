"""Source tests: synthetic daemon responses, real signed bytes and AES archives.

No Docker executable/daemon, host server or network is invoked. These tests are
NOT installation acceptance for Docker, Windows or container images.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from homensai_install.docker_contract import (CONTRACT_FILE, LABEL_PREFIX,
    load_verified_docker_contract, resource_plan)
from homensai_install.docker_engine import (CONTAINER_FORMAT, FOREIGN_FORMAT, IMAGE_FORMAT,
    INFO_FORMAT, NETWORK_FORMAT, DockerEngine)
from homensai_install.engine import EngineError
from homensai_install.errors import InstallerError
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest
from shared import archive_crypto
from shared.datahome import resolve_datahome
from shared.update_plan import operation_lock, LockBusy


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class SyntheticDaemon:
    """Deliberately limited protocol model, no subprocess or network access."""
    endpoint = "unix:///var/run/fixture-docker.sock"

    def __init__(self, manifest, contract, spec, owner):
        self.manifest, self.contract, self.spec, self.owner = manifest, contract, spec, owner
        self.calls, self.effects, self.foreign = [], [], {}
        self.info = {"ID": "fixture-daemon-id", "OSType": "linux", "Architecture": "x86_64",
            "MemoryLimit": True, "PidsLimit": True, "SecurityOptions": ["name=seccomp,profile=builtin"]}
        self.network = {"Id": owner["network"]["id"], "Name": spec["network"]["name"], "Driver": "bridge",
            "Internal": True, "Scope": "local", "Options": {}, "Labels": spec["network"]["labels"], "Containers": {}}
        self.images, self.containers = {}, {}
        self.ignore_term, self.exit_code, self.after_effect, self.probe_override = False, 0, None, None
        for name, item in spec["components"].items():
            image_config = {"Env": ["PATH=/usr/bin:/bin", "LANG=C.UTF-8"], "WorkingDir": "/app"}
            self.images[item["image_id"]] = {"Id": item["image_id"], "RepoDigests": [item["image"]],
                "Os": "linux", "Architecture": "amd64", "Config": image_config}
            self.containers[owner["containers"][name]] = {
                "Id": owner["containers"][name], "Name": "/" + item["name"], "Image": item["image_id"],
                "Config": {"Image": item["image"], "User": item["user"], "Labels": item["labels"],
                    "Entrypoint": item["argv"][:1], "Cmd": item["argv"][1:],
                    "Env": image_config["Env"] + [k + "=" + v for k, v in item["environment"].items()],
                    "WorkingDir": "/app", "Healthcheck": None, "OpenStdin": False, "Tty": False},
                "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [],
                    "SecurityOpt": ["no-new-privileges"], "PidMode": "", "IpcMode": "private", "UTSMode": "",
                    "UsernsMode": "", "CgroupnsMode": "private", "CgroupParent": "", "Devices": [],
                    "DeviceRequests": None, "DeviceCgroupRules": None, "VolumesFrom": None, "Binds": None,
                    "NetworkMode": spec["network"]["name"], "Memory": item["memory_bytes"], "PidsLimit": item["pids_limit"],
                    "PublishAllPorts": False, "AutoRemove": False, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    "LogConfig": {"Type": "none", "Config": {}}, "Tmpfs": item["tmpfs"], "PortBindings": item["ports"]},
                "Mounts": item["mounts"], "Networks": {spec["network"]["name"]: {"NetworkID": owner["network"]["id"]}},
                "Ports": {}, "Running": False, "Status": "created", "ExitCode": 0, "OOMKilled": False, "Error": "",
                "Paused": False, "Restarting": False, "Dead": False, "StartedAt": "", "FinishedAt": "", "RestartCount": 0, "Health": None}
        self.images = copy.deepcopy(self.images)
        self.containers = copy.deepcopy(self.containers)

    def state(self, name, running, *, exit_code=0):
        cid = self.owner["containers"][name]
        item = self.containers[cid]
        item.update(Running=running, Status="running" if running else "exited", ExitCode=exit_code)
        item["Ports"] = copy.deepcopy(self.spec["components"][name]["ports"]) if running else {}
        if running:
            item["StartedAt"] = str(uuid.uuid4())
            self.network["Containers"][cid] = {"Name": self.spec["components"][name]["name"]}
        else:
            item["FinishedAt"] = str(uuid.uuid4())
            self.network["Containers"].pop(cid, None)

    def proof(self, challenge):
        return {"product_id": self.manifest["product_id"], "installation_id": self.owner["installation_id"],
            "release": self.owner["release"], "ready": True, "challenge": challenge,
            "proof": hashlib.sha256(("synthetic-private-instance-key:" + challenge).encode()).hexdigest()}

    def fetcher(self, url, challenge, timeout):
        assert url == "http://127.0.0.1:8820/healthz"
        return self.proof(challenge)

    def run(self, args, *, timeout=30):
        args = tuple(args)
        self.calls.append(args)
        assert 0 < timeout <= 600
        if args == ("info", "--format", INFO_FORMAT):
            value = self.info
        elif args[:3] == ("image", "inspect", "--format"):
            assert args[3] == IMAGE_FORMAT and args[4] == "--"
            value = self.images[args[5]]
        elif args[:3] == ("network", "inspect", "--format"):
            assert args[3] == NETWORK_FORMAT and args[4:] == ("--", self.owner["network"]["id"])
            value = self.network
        elif args[:3] == ("container", "inspect", "--format"):
            assert args[4] == "--"
            if args[3] == FOREIGN_FORMAT:
                value = self.foreign[args[5]]
            else:
                assert args[3] == CONTAINER_FORMAT
                value = self.containers[args[5]]
        elif args == ("container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"):
            return SimpleNamespace(returncode=0, stdout=("\n".join([*self.containers, *self.foreign]) + "\n").encode())
        elif args[:2] == ("container", "exec"):
            assert args[2:4] == ("--", self.owner["containers"]["web"])
            assert list(args[4:-2]) == self.contract.document["web"]["probe_cmd"]
            assert args[-2] == "--challenge"
            value = self.probe_override or self.proof(args[-1])
        elif args[:2] in (("container", "start"), ("container", "kill")):
            cid = args[-1]
            name = next(name for name, own in self.owner["containers"].items() if own == cid)
            self.effects.append((args[1], name))
            if args[1] == "start":
                assert args[2] == "--"
                self.state(name, True)
            else:
                assert args[2:4] == ("--signal=SIGTERM", "--")
                if not self.ignore_term:
                    self.state(name, False, exit_code=self.exit_code)
            if self.after_effect:
                self.after_effect(args)
            return SimpleNamespace(returncode=0, stdout=cid.encode())
        else:
            raise AssertionError("Undeclared synthetic Docker command")
        return SimpleNamespace(returncode=0, stdout=canonical_bytes(value))


@pytest.fixture
def docker_bundle(tmp_path):
    manifest = json.loads((Path(__file__).parents[1] / "examples/rent.manifest.json").read_text())
    manifest["paths"] = {name: name for name in ("app", "releases", "data", "backup", "services", "config", "runtime")}
    base = copy.deepcopy(manifest["components"][0])
    db, web = copy.deepcopy(base), copy.deepcopy(base)
    db.update(name="db", cmd=["/app/bin/database", "--foreground"], after=[])
    web.update(name="web", cmd=["/app/bin/web", "--foreground"], after=["db"])
    manifest["components"] = [db, web]
    manifest["modes"], manifest["mutual_exclusion"] = [{"name": "local", "components": ["db", "web"]}], []
    manifest["secrets"] = []
    manifest["hooks"] = {"pre_start": [], "health_checks": [], "post_update": []}
    manifest["update"]["health_wait"] = 1
    document = {"schema_version": 1, "backend": "docker-managed", "platform": "linux/amd64",
        "network": {"internal": True}, "storage": {"strategy": "filesystem_quiesced", "all_writers_managed": True},
        "web": {"component": "web", "container_port": 8080, "probe_cmd": ["/app/bin/probe", "--private-listener"]},
        "license_review": "legal/images.json", "components": []}
    reviews = {}
    for name, digit in (("db", "b"), ("web", "c")):
        entry = {"name": name, "image": "registry.example.invalid/fixture/" + name + "@sha256:" + digit * 64,
            "image_id": "sha256:" + digit * 64, "user": "1000:1000", "memory_bytes": 268435456, "pids_limit": 128,
            "stop_timeout_seconds": 1, "require_docker_health": False,
            "mounts": [{"root": "data", "target": "/homensai/data", "read_only": False},
                       {"root": "config", "target": "/homensai/config", "read_only": True}]}
        document["components"].append(entry)
        reviews[name] = {"image": entry["image"], "image_id": entry["image_id"], "commercial_use": True,
            "redistribution": True, "sbom_file": "legal/fixture-sbom.json", "notices_file": "legal/fixture-notices.txt"}
    payload = tmp_path / "payload"
    payload.mkdir()
    files = {CONTRACT_FILE: canonical_bytes(document),
        "legal/images.json": canonical_bytes({"schema_version": 1, "status": "approved", "images": reviews}),
        "legal/fixture-sbom.json": b'{"fixture_only":true,"not_a_real_image_sbom":true}',
        "legal/fixture-notices.txt": b"Synthetic fixture images do not exist and are not distributed.",
        "locks/windows.lock": b"fixture", "locks/linux.lock": b"fixture",
        "vendor/fixture-1-py3-none-any.whl": b"fixture, not a real distribution wheel"}
    manifest["runtime"]["lock_files"] = {os_name: {"file": "locks/" + os_name + ".lock",
        "sha256": hashlib.sha256(files["locks/" + os_name + ".lock"]).hexdigest()} for os_name in ("windows", "linux")}
    manifest["runtime"]["vendor_wheels"] = [{"file": "vendor/fixture-1-py3-none-any.whl",
        "sha256": hashlib.sha256(files["vendor/fixture-1-py3-none-any.whl"]).hexdigest()}]
    target = {"os": "linux", "arch": "x64", "mode": "docker", "scope": "user"}
    root, work = tmp_path / "installation", tmp_path / "backups"
    root.mkdir(mode=0o700)
    for name in manifest["paths"]:
        (root / name).mkdir(mode=0o700)
    control = root / ".hinstall"
    control.mkdir(mode=0o700)
    (root / "data" / "important.txt").write_text("User data survives stop/start/backup.")
    options = {"install_root": str(root), "payload_dir": str(payload), "release": "1.0.0",
        "runtime_executable": CONTRACT_FILE, "inventory_file": str(tmp_path / "inventory.json"),
        "signature_file": str(tmp_path / "signature.json"), "work_dir": str(work), "backup_password_ref": "vault:fixture"}
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = SignedPayloadVerifier({"test": public}, allow_test_keys=True)

    def sign():
        files[CONTRACT_FILE] = canonical_bytes(document)
        for name, raw in files.items():
            path = payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        inv = {"schema_version": 1, "product_id": manifest["product_id"], "release": "1.0.0", "development": True,
            "target": {k: target[k] for k in ("os", "arch", "mode")}, "manifest_sha256": hash_manifest(manifest),
            "runtime_executable": CONTRACT_FILE, "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
        Path(options["inventory_file"]).write_bytes(canonical_bytes(inv))
        Path(options["signature_file"]).write_bytes(canonical_bytes({"algorithm": "ed25519", "key_id": "test",
            "signature": base64.b64encode(private.sign(canonical_bytes(inv))).decode()}))

    sign()
    contract = load_verified_docker_contract(manifest, target, options, verifier=verifier)
    spec = resource_plan(contract, install_root=str(root), installation_id="1" * 32, modes=["local"])
    owner = {"schema_version": 1, "mode": "docker", "product_id": manifest["product_id"], "installation_id": "1" * 32,
        "owner": "uid:" + str(os.getuid()), "daemon_id": "fixture-daemon-id", "endpoint_sha256": hashlib.sha256(SyntheticDaemon.endpoint.encode()).hexdigest(),
        "release": "1.0.0", "manifest_sha256": hash_manifest(manifest), "contract_sha256": contract.contract_sha256,
        "modes": ["local"], "containers": {"db": "2" * 64, "web": "3" * 64}, "network": {"id": "a" * 64, "name": spec["network"]["name"]}}
    receipt = control / "docker-installation.json"
    receipt.write_bytes(canonical_bytes(owner))
    receipt.chmod(0o600)
    daemon = SyntheticDaemon(manifest, contract, spec, owner)
    engine = DockerEngine(verifier=verifier, runner=daemon, secret_provider=lambda ref: "fixture-long-password", fetcher=daemon.fetcher,
                          host_probe=lambda endpoint: {"mnt": "1:100", "net": "1:200"})
    return SimpleNamespace(manifest=manifest, target=target, options=options, document=document, files=files,
        private=private, verifier=verifier, sign=sign, contract=contract, spec=spec, owner=owner,
        daemon=daemon, engine=engine, root=root, work=work, payload=payload, receipt=receipt)


def make_plan(bundle, action):
    inventory = bundle.engine.discover(bundle.manifest, bundle.target, bundle.options)
    detail = bundle.engine.plan(action, bundle.manifest, bundle.target, inventory, bundle.options)
    return {"plan_id": str(uuid.uuid4()), "action": action, "source_manifest": copy.deepcopy(bundle.manifest),
            "target": copy.deepcopy(bundle.target), "options": copy.deepcopy(bundle.options), "engine_plan": detail}


def check_error(function, code):
    with pytest.raises((EngineError, InstallerError)) as caught:
        function()
    assert caught.value.code == code
    return caught.value


def test_no_runner_means_no_docker_or_host_python_execution():
    engine = DockerEngine()
    caps = engine.capabilities({"os": "linux", "arch": "x64", "mode": "docker"})
    assert not any(a["supported"] for a in caps["actions"].values())
    assert caps["production_ready"] is False and caps["docker_was_probed"] is False


def test_signed_contract_and_static_resource_review_do_not_provision(docker_bundle):
    b = docker_bundle
    assert b.daemon.calls == []
    assert b.spec["provisioning_implemented"] is False and b.spec["execution_supported"] is False
    assert b.spec["components"]["web"]["ports"] == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8820"}]}
    assert b.spec["components"]["web"]["argv"] == ["/app/bin/web", "--foreground"]
    assert b.spec["components"]["web"]["read_only_root"] is True
    assert not (b.root / ".hinstall" / "docker-operations").exists()


@pytest.mark.parametrize("change", ["bytes", "manifest", "target", "selector", "unknown_key"])
def test_signature_and_backend_selector_mismatch_block_execution(docker_bundle, change):
    b = docker_bundle
    if change == "bytes":
        (b.payload / CONTRACT_FILE).write_bytes(b"{}")
    elif change == "manifest":
        b.manifest["components"][0]["cmd"].append("--injected")
    elif change == "target":
        b.target["mode"] = "native"
    elif change == "selector":
        b.options["runtime_executable"] = "/usr/bin/python"
    else:
        b.document["unreviewed"] = "--privileged"
        b.sign()
    with pytest.raises((EngineError, InstallerError)):
        b.engine.discover(b.manifest, b.target, b.options)
    assert b.daemon.effects == []


@pytest.mark.parametrize("change", ["tag", "root_user", "socket", "external_network", "license_denied", "unsigned_notice", "hidden_hooks", "unmanaged_storage"])
def test_unsafe_signed_contract_is_rejected_too(docker_bundle, change):
    b = docker_bundle
    if change == "tag":
        b.document["components"][0]["image"] = "registry.example.invalid/image:latest"
    elif change == "root_user":
        b.document["components"][0]["user"] = "0:0"
    elif change == "socket":
        b.document["components"][0]["mounts"][0]["target"] = "/var/run/docker.sock"
    elif change == "external_network":
        b.document["network"]["internal"] = False
    elif change in {"license_denied", "unsigned_notice"}:
        value = json.loads(b.files["legal/images.json"])
        value["images"]["db"]["commercial_use" if change == "license_denied" else "notices_file"] = False if change == "license_denied" else "unsigned.txt"
        b.files["legal/images.json"] = canonical_bytes(value)
    elif change == "hidden_hooks":
        b.manifest["hooks"]["pre_start"] = [["/bin/sh", "-c", "echo unsafe-assumption"]]
    else:
        b.document["storage"]["all_writers_managed"] = False
    b.sign()
    with pytest.raises((EngineError, InstallerError)):
        load_verified_docker_contract(b.manifest, b.target, b.options, verifier=b.verifier)
    assert b.daemon.calls == []


def test_capabilities_are_development_only_and_do_not_claim_missing_lifecycle(docker_bundle):
    b = docker_bundle
    caps = b.engine.capabilities(b.target)
    assert {name for name, item in caps["actions"].items() if item["supported"]} == {"start", "stop", "backup"}
    assert caps["production_ready"] is False and caps["live_acceptance"] == "REQUIRED"
    b.daemon.calls.clear()
    for target in (b.target | {"os": "windows"}, b.target | {"arch": "arm64"}, b.target | {"mode": "native"}):
        assert not any(a["supported"] for a in b.engine.capabilities(target)["actions"].values())
    assert not b.daemon.calls


def test_status_is_readonly_and_does_not_expose_environment(docker_bundle):
    b = docker_bundle
    inventory = b.engine.discover(b.manifest, b.target, b.options)
    assert inventory["read_only"] is True and inventory["integrity"]["state"] == "VERIFIED"
    assert not b.daemon.effects and not (b.root / ".hinstall" / "docker-operations").exists()
    assert "PATH=" not in json.dumps(inventory) and "HostConfig" not in json.dumps(inventory)


def test_forwarded_or_vm_namespace_is_rejected_before_docker_cli(docker_bundle):
    b = docker_bundle
    def incompatible(endpoint):
        raise EngineError("CAPABILITY_UNSUPPORTED", details={"reason": "daemon_host_namespace_differs"})
    b.engine.host_probe = incompatible
    assert not any(item["supported"] for item in b.engine.capabilities(b.target)["actions"].values())
    check_error(lambda: b.engine.discover(b.manifest, b.target, b.options), "CAPABILITY_UNSUPPORTED")
    assert b.daemon.calls == []


def test_start_stop_are_ordered_and_only_exact_owned_container_ids(docker_bundle):
    b = docker_bundle
    events = []
    plan = make_plan(b, "start")
    result = b.engine.execute(plan, events.append)
    assert result["status"] == "SUCCEEDED" and result["health"]["status"] == "HEALTHY"
    assert b.daemon.effects == [("start", "db"), ("start", "web")]
    assert b.engine.execute(plan) == result  # durable idempotency, no re-start
    assert len(b.daemon.effects) == 2
    assert any(event["total"] is None for event in events)
    assert b.engine.execute(make_plan(b, "stop"))["status"] == "SUCCEEDED"
    assert b.daemon.effects[-2:] == [("kill", "web"), ("kill", "db")]
    assert all("SIGKILL" not in str(call) and "prune" not in call and "rm" not in call for call in b.daemon.calls)


@pytest.mark.parametrize("alter", ["daemon", "id", "label", "image", "rw_mount", "extra_socket", "privileged", "wildcard_port", "extra_network", "env", "foreign_member", "restart"])
def test_identity_and_container_configuration_drift_prevent_effects(docker_bundle, alter):
    b = docker_bundle
    plan = make_plan(b, "start")
    value = b.daemon.containers[b.owner["containers"]["web"]]
    if alter == "daemon":
        b.daemon.info["ID"] = "other-daemon"
    elif alter == "id":
        value["Id"] = "f" * 64
    elif alter == "label":
        value["Config"]["Labels"][LABEL_PREFIX + "installation"] = "f" * 32
    elif alter == "image":
        value["Image"] = "sha256:" + "f" * 64
    elif alter == "rw_mount":
        value["Mounts"][1]["RW"] = True
    elif alter == "extra_socket":
        value["Mounts"].append({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/sock", "RW": True})
    elif alter == "privileged":
        value["HostConfig"]["Privileged"] = True
    elif alter == "wildcard_port":
        value["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostIp"] = "0.0.0.0"
    elif alter == "extra_network":
        value["Networks"]["bridge"] = {"NetworkID": "f" * 64}
    elif alter == "env":
        value["Config"]["Env"].append("PYTHONPATH=/untrusted")
    elif alter == "foreign_member":
        b.daemon.network["Containers"]["f" * 64] = {"Name": "foreign"}
    else:
        value["HostConfig"]["RestartPolicy"] = {"Name": "always", "MaximumRetryCount": 0}
    with pytest.raises((EngineError, InstallerError)):
        b.engine.execute(plan)
    assert b.daemon.effects == []


def test_refuses_adoption_from_labels_without_private_receipt(docker_bundle):
    b = docker_bundle
    b.receipt.unlink()
    check_error(lambda: b.engine.discover(b.manifest, b.target, b.options), "RECOVERY_REQUIRED")
    assert b.daemon.calls == []


def test_unprivate_receipt_is_rejected_without_chmod_or_adoption(docker_bundle):
    b = docker_bundle
    b.receipt.chmod(0o644)
    check_error(lambda: b.engine.discover(b.manifest, b.target, b.options), "PERMISSION_REQUIRED")
    assert b.receipt.stat().st_mode & 0o777 == 0o644
    assert b.daemon.calls == []


def test_plan_detects_container_started_between_review_and_apply(docker_bundle):
    b = docker_bundle
    plan = make_plan(b, "start")
    b.daemon.state("db", True)
    check_error(lambda: b.engine.execute(plan), "PLAN_STALE")
    assert b.daemon.effects == []


def test_health_requires_direct_container_and_published_port_same_private_proof(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    b.engine.fetcher = lambda url, nonce, timeout: b.daemon.proof(nonce) | {"proof": "f" * 64}
    check_error(lambda: b.engine.health(b.manifest, b.target, b.options), "HEALTH_CHECK_FAILED")
    assert b.daemon.effects == []


def test_health_rechecks_identity_after_response(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    def fetch(url, nonce, timeout):
        b.daemon.state("web", True)  # same ID, new process instance
        return b.daemon.proof(nonce)
    b.engine.fetcher = fetch
    check_error(lambda: b.engine.health(b.manifest, b.target, b.options), "HEALTH_CHECK_FAILED")


def test_backup_stops_all_writers_encrypts_exact_data_and_restores_running_set(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    plan = make_plan(b, "backup")
    result = b.engine.execute(plan)
    assert b.daemon.effects == [("kill", "web"), ("kill", "db"), ("start", "db"), ("start", "web")]
    archive = Path(result["backup"]["archive_path"])
    assert archive_crypto.is_encrypted(archive)
    verified = archive_crypto.verify_archive(archive, password_provider=lambda: "fixture-long-password", expected_product_id=b.manifest["product_id"])
    item = next(file for file in verified["files"] if file["path"] == "roots/data/important.txt")
    assert item["sha256"] == hashlib.sha256((b.root / "data" / "important.txt").read_bytes()).hexdigest()
    assert result["backup"]["contains_images"] is False and result["backup"]["restoration_supported"] is False
    assert b.engine.execute(plan) == result and len(b.daemon.effects) == 4
    assert "fixture-long-password" not in "".join(file.read_text() for file in (b.root / ".hinstall" / "docker-operations").glob("*.json"))


def test_backup_does_not_start_components_that_were_stopped(docker_bundle):
    b = docker_bundle
    result = b.engine.execute(make_plan(b, "backup"))
    assert result["status"] == "SUCCEEDED" and result["health"]["status"] == "NOT_RUNNING"
    assert b.daemon.effects == []


def test_backup_plan_does_not_fetch_secret_and_apply_checks_it_before_stopping(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    calls = []
    def unavailable(ref):
        calls.append(ref)
        raise RuntimeError("fixture-sensitive-provider-error")
    b.engine.secret_provider = unavailable
    plan = make_plan(b, "backup")
    assert calls == []
    error = check_error(lambda: b.engine.execute(plan), "PREREQUISITE_MISSING")
    assert calls == ["vault:fixture"] and b.daemon.effects == []
    assert "fixture-sensitive-provider-error" not in str(error)


def test_foreign_backup_directory_rejected_before_any_component_stop(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    b.work.mkdir(mode=0o700)
    (b.work / "foreign.txt").write_text("preserve me")
    check_error(lambda: b.engine.execute(make_plan(b, "backup")), "OPERATION_CONFLICT")
    assert b.daemon.effects == [] and (b.work / "foreign.txt").read_text() == "preserve me"


def test_crash_after_archive_publish_reuses_verified_snapshot_without_rebuild(docker_bundle, monkeypatch):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    class Crash(BaseException):
        pass
    original = archive_crypto.build_encrypted_archive
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise Crash()
    monkeypatch.setattr(archive_crypto, "build_encrypted_archive", interrupted)
    plan = make_plan(b, "backup")
    with pytest.raises(Crash):
        b.engine.execute(plan)
    assert b.daemon.effects == [("kill", "web"), ("kill", "db")]
    result = b.engine.resume(plan, {})
    assert result["status"] == "SUCCEEDED" and calls == [1]
    assert b.daemon.effects[-2:] == [("start", "db"), ("start", "web")]


def test_completed_archive_tampering_blocks_resume_before_any_restart(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    class Crash(BaseException):
        pass
    def interrupt(event):
        if event["phase"] == "backup" and event["status"] == "SUCCEEDED":
            raise Crash()
    plan = make_plan(b, "backup")
    with pytest.raises(Crash):
        b.engine.execute(plan, interrupt)
    archive = next(b.work.glob("*.haes"))
    archive.write_bytes(b"untrusted replacement")
    check_error(lambda: b.engine.resume(plan, {}), "INTEGRITY_FAILED")
    assert b.daemon.effects == [("kill", "web"), ("kill", "db")]


def test_terminal_backup_receipt_is_not_claimed_when_archive_was_deleted(docker_bundle):
    b = docker_bundle
    plan = make_plan(b, "backup")
    result = b.engine.execute(plan)
    Path(result["backup"]["archive_path"]).unlink()
    check_error(lambda: b.engine.execute(plan), "INTEGRITY_FAILED")
    assert b.daemon.effects == []


def test_cancel_after_quiesce_resumes_same_backup_and_original_running_set(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    cancel = [False]
    def events(event):
        if event["phase"] == "stop" and event["status"] == "SUCCEEDED" and "component_id" not in event:
            cancel[0] = True
    plan = make_plan(b, "backup")
    result = b.engine.execute(plan, events, lambda: cancel[0])
    assert result["status"] == "CANCELED"
    assert b.daemon.effects == [("kill", "web"), ("kill", "db")]
    assert not list(b.work.glob("*.haes"))
    assert b.engine.resume(plan, {})["status"] == "SUCCEEDED"
    assert b.daemon.effects == [("kill", "web"), ("kill", "db"), ("start", "db"), ("start", "web")]


@pytest.mark.parametrize("source", ["root", "data", "ancestor"])
def test_foreign_rw_bind_prevents_backup_even_if_foreign_container_is_stopped(docker_bundle, source):
    b = docker_bundle
    path = b.root if source == "root" else b.root / "data" if source == "data" else b.root.parent
    cid = "f" * 64
    b.daemon.foreign[cid] = {"Id": cid, "Mounts": [{"Type": "bind", "Source": str(path), "RW": True}]}
    inventory = b.engine.discover(b.manifest, b.target, b.options)
    assert inventory["foreign_writers"]
    check_error(lambda: b.engine.plan("backup", b.manifest, b.target, inventory, b.options), "OPERATION_CONFLICT")
    assert b.daemon.effects == [] and not b.work.exists()


def test_foreign_bind_alias_is_detected_through_kernel_mount_map(docker_bundle, monkeypatch):
    b = docker_bundle
    alias = b.root.parent / "separate-alias"
    alias.mkdir()
    monkeypatch.setattr("homensai_install.docker_engine._mount_table", lambda: [
        ("8:1", Path("/"), Path("/")), ("8:1", b.root / "data", alias)])
    cid = "f" * 64
    b.daemon.foreign[cid] = {"Id": cid, "Mounts": [{"Type": "bind", "Source": str(alias), "RW": True}]}
    inventory = b.engine.discover(b.manifest, b.target, b.options)
    assert inventory["foreign_writers"] == [{"container_id": cid, "root": "data"}]
    check_error(lambda: b.engine.plan("backup", b.manifest, b.target, inventory, b.options), "OPERATION_CONFLICT")


def test_foreign_rw_volume_without_host_alias_proof_is_conservatively_blocked(docker_bundle):
    b = docker_bundle
    cid = "f" * 64
    b.daemon.foreign[cid] = {"Id": cid, "Mounts": [{"Type": "volume", "Source": "/var/lib/docker/volumes/unknown/_data", "RW": True}]}
    inventory = b.engine.discover(b.manifest, b.target, b.options)
    assert inventory["foreign_writers"] == [{"container_id": cid, "root": "unresolved_volume_alias"}]
    check_error(lambda: b.engine.plan("backup", b.manifest, b.target, inventory, b.options), "OPERATION_CONFLICT")


def test_hardlink_alias_outside_data_home_prevents_backup(docker_bundle):
    b = docker_bundle
    os.link(b.root / "data" / "important.txt", b.root.parent / "external-hardlink.txt")
    check_error(lambda: b.engine.execute(make_plan(b, "backup")), "RECOVERY_REQUIRED")
    assert not list(b.work.glob("*.haes"))


def test_work_directory_bind_alias_to_data_is_rejected_before_effects(docker_bundle, monkeypatch):
    b = docker_bundle
    monkeypatch.setattr("homensai_install.docker_engine._mount_table", lambda: [
        ("8:1", Path("/"), Path("/")), ("8:1", b.root / "data", b.work)])
    check_error(lambda: make_plan(b, "backup"), "INVALID_MANIFEST")
    assert b.daemon.effects == []


def test_nonzero_container_exit_blocks_snapshot_and_never_force_kills(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    b.daemon.exit_code = 143
    check_error(lambda: b.engine.execute(make_plan(b, "backup")), "RECOVERY_REQUIRED")
    assert b.daemon.effects == [("kill", "web")]
    assert not b.work.exists()


def test_ignored_sigterm_never_escalates_or_replays_on_resume(docker_bundle, monkeypatch):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    b.daemon.ignore_term = True
    clock = iter(range(100))
    monkeypatch.setattr("homensai_install.docker_engine.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("homensai_install.docker_engine.time.sleep", lambda amount: None)
    plan = make_plan(b, "stop")
    check_error(lambda: b.engine.execute(plan), "RECOVERY_REQUIRED")
    check_error(lambda: b.engine.resume(plan, {}), "RECOVERY_REQUIRED")
    assert b.daemon.effects == [("kill", "web")]
    assert all("SIGKILL" not in str(call) for call in b.daemon.calls)


def test_crash_after_start_adopts_exact_running_id_without_duplicate_start(docker_bundle):
    b = docker_bundle
    class Crash(BaseException):
        pass
    b.daemon.after_effect = lambda args: (_ for _ in ()).throw(Crash())
    plan = make_plan(b, "start")
    with pytest.raises(Crash):
        b.engine.execute(plan)
    assert b.daemon.effects == [("start", "db")]
    b.daemon.after_effect = None
    result = b.engine.resume(plan, {})
    assert result["status"] == "SUCCEEDED"
    assert b.daemon.effects == [("start", "db"), ("start", "web")]


def test_ambiguous_start_without_running_state_is_not_replayed(docker_bundle):
    b = docker_bundle
    class Crash(BaseException):
        pass
    def effect(args):
        b.daemon.state("db", False)
        raise Crash()
    b.daemon.after_effect = effect
    plan = make_plan(b, "start")
    with pytest.raises(Crash):
        b.engine.execute(plan)
    b.daemon.after_effect = None
    check_error(lambda: b.engine.resume(plan, {}), "RECOVERY_REQUIRED")
    assert b.daemon.effects == [("start", "db")]


def test_resume_after_health_checkpoint_rechecks_observed_container_state(docker_bundle):
    b = docker_bundle
    class Crash(BaseException):
        pass
    def interrupt(event):
        if event["phase"] == "health" and event["status"] == "SUCCEEDED":
            raise Crash()
    plan = make_plan(b, "start")
    with pytest.raises(Crash):
        b.engine.execute(plan, interrupt)
    b.daemon.state("web", False)
    check_error(lambda: b.engine.resume(plan, {}), "HEALTH_CHECK_FAILED")
    assert b.daemon.effects == [("start", "db"), ("start", "web")]


def test_resume_after_stop_checkpoint_cannot_report_stopped_for_restarted_container(docker_bundle):
    b = docker_bundle
    b.daemon.state("db", True)
    b.daemon.state("web", True)
    class Crash(BaseException):
        pass
    def interrupt(event):
        if event["phase"] == "stop" and event["status"] == "SUCCEEDED" and "component_id" not in event:
            raise Crash()
    plan = make_plan(b, "stop")
    with pytest.raises(Crash):
        b.engine.execute(plan, interrupt)
    b.daemon.state("web", True)
    check_error(lambda: b.engine.resume(plan, {}), "RECOVERY_REQUIRED")
    assert b.daemon.effects == [("kill", "web"), ("kill", "db")]


def test_cancel_resume_and_cross_plan_interlock(docker_bundle):
    b = docker_bundle
    plan = make_plan(b, "start")
    result = b.engine.execute(plan, cancel_requested=lambda: True)
    assert result["status"] == "CANCELED" and b.daemon.effects == []
    check_error(lambda: b.engine.execute(make_plan(b, "start")), "OPERATION_CONFLICT")
    assert b.engine.resume(plan, {})["status"] == "SUCCEEDED"


def test_install_root_lock_covers_independent_api_instances(docker_bundle):
    b = docker_bundle
    plan = make_plan(b, "start")
    with operation_lock(b.root / ".hinstall" / "docker.lock"):
        with pytest.raises(LockBusy):
            b.engine.execute(plan)
    assert b.daemon.effects == []


@pytest.mark.parametrize("action", ["install", "update", "restore", "rollback", "repair", "reinstall", "uninstall"])
def test_missing_provisioning_lifecycle_is_explicitly_unsupported(docker_bundle, action):
    b = docker_bundle
    check_error(lambda: b.engine.execute({"action": action}), "CAPABILITY_UNSUPPORTED")
    assert b.daemon.calls == []
