"""Настоящий browser fixture: установленный SDK, signed synthetic catalog, loopback.

Запускать копию этого файла вне checkout в чистом venv с установленным wheel.
Команды установщиков и внешний transport запрещены; production readiness не заявляется.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--manifest-template", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    if work.exists():
        raise RuntimeError("Нужен новый каталог browser fixture")
    work.mkdir(mode=0o700, parents=True)
    os.chmod(work, 0o700)
    checkout = args.checkout.resolve()
    if work.is_relative_to(checkout):
        raise RuntimeError("Browser fixture должен быть вне checkout")
    # Прочитать external template до запрета checkout; ядро загружается только из wheel.
    manifest = json.loads(args.manifest_template.read_text(encoding="utf-8"))
    origins = {}
    denied = []
    original_connect = socket.socket.connect

    def guarded_connect(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1"):
            denied.append("external_connect")
            raise RuntimeError("Browser fixture разрешает только loopback")
        return original_connect(connection, address)
    socket.socket.connect = guarded_connect

    def audit(event, values):
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork"}:
            denied.append(event)
            raise RuntimeError("Browser fixture запрещает дочерние процессы")
        if event in {"open", "os.listdir", "os.scandir"} and values and isinstance(values[0], (str, bytes)):
            file = Path(os.fsdecode(values[0])).resolve()
            if file.is_relative_to(checkout):
                denied.append("checkout_read")
                raise RuntimeError("Browser fixture запрещает чтение checkout")
    sys.addaudithook(audit)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.api.model import digest
    from homensai_install.downloads import DownloadManager
    from homensai_install.gui import WizardSession, create_server
    from homensai_install.prerequisite_catalog import SignedPrerequisiteCatalog
    from homensai_install.prerequisite_execution import PrerequisiteCoordinator
    from homensai_install.prerequisite_runner import CommandResult, FixedCommandAdapter
    from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest

    for name, module in tuple(sys.modules.items()):
        if name.startswith(("homensai_install", "shared")) and getattr(module, "__file__", None):
            path = Path(module.__file__).resolve()
            if path.is_relative_to(checkout):
                raise RuntimeError("Source fallback в browser fixture")
            origins[name] = str(path)
    manifest.update(product_id="browser-prerequisite-upload", product_name="Synthetic browser upload")
    payload = work / "payload"
    for folder in ("installer", "licenses", "legal"):
        (payload / folder).mkdir(parents=True, mode=0o700)
    (payload / "runtime").write_bytes(b"Non-executable synthetic runtime; never executed\n")
    for locale in ("ru", "en", "de"):
        (payload / "legal" / (locale + ".txt")).write_text("Synthetic product agreement " + locale, encoding="utf-8")
    terms = "Synthetic prerequisite terms; no third party software.\n"
    (payload / "licenses/base.txt").write_text(terms, encoding="utf-8")
    content = b"# Synthetic local artifact; NEVER EXECUTED\n" + b"x" * (1024 * 1024)
    (work / "correct-installer.bin").write_bytes(content)
    (work / "wrong-installer.bin").write_bytes(b"z" * len(content))
    (work / "oversize-installer.bin").write_bytes(content + b"extra")
    target = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
    artifact = {"artifact_id": "base-package", "version": "1.0.0", "url": "https://browser-fixture.example.invalid/base?token=synthetic-never-public", "sha256": sha(content), "size": len(content), "license_ref": "base-terms"}
    entry = {"requirement": {"component_id": "base", "kind": "base", "required": True, "minimum_version": "1.0.0", "scope": "host"}, "depends_on": [], "ownership": "shared", "adapter_id": "install-base", "artifacts": [artifact], "license": {"license_id": "base-terms", "version": "2026-01", "text_path": "licenses/base.txt", "text_sha256": sha(terms.encode()), "locale": "en", "decision": "allowed", "acceptance_required": True, "conditions": []}}
    descriptor = {"schema_version": 1, "catalog_id": "browser-prerequisites", "catalog_version": "1.0.0", "product_id": manifest["product_id"], "target": target, "entries": [entry]}
    (payload / "installer/prerequisites.json").write_bytes(canonical_bytes(descriptor))
    inventory = {"schema_version": 1, "product_id": manifest["product_id"], "release": "1.0.0", "target": {key: target[key] for key in ("os", "arch", "mode")}, "files": {file.relative_to(payload).as_posix(): sha(file.read_bytes()) for file in sorted(payload.rglob("*")) if file.is_file()}, "development": True, "manifest_sha256": hash_manifest(manifest), "runtime_executable": "runtime"}
    private = Ed25519PrivateKey.generate()
    signature = {"algorithm": "ed25519", "key_id": "browser-fixture", "signature": base64.b64encode(private.sign(canonical_bytes(inventory))).decode()}
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    del private
    (work / "inventory.json").write_bytes(canonical_bytes(inventory))
    (work / "signature.json").write_bytes(canonical_bytes(signature))
    options = {"payload_dir": str(payload), "inventory_file": str(work / "inventory.json"), "signature_file": str(work / "signature.json"), "release": "1.0.0", "runtime_executable": "runtime", "install_root": str(work / "install-root")}
    verifier = SignedPayloadVerifier({"browser-fixture": public}, allow_test_keys=True)
    catalog = SignedPrerequisiteCatalog(manifest, target, options, verifier=verifier)
    ledger = {"installer_calls": [], "transport_calls": [], "product_calls": [], "denied": denied}

    def save_ledger():
        write_json(work / "ledger.json", ledger)

    class ForbiddenTransport:
        def open(self, url, *, headers, timeout):
            ledger["transport_calls"].append("unexpected")
            save_ledger()
            raise RuntimeError("Browser fixture запрещает внешний transport")

    inventory_observation = {"platform": {"os": "linux", "arch": "x64", "release": "synthetic"}, "identity": {"uid": os.getuid(), "scope": "current_process", "elevated": False}, "resources": {"cpu_count": 2}, "ports": [], "disks": [], "components": {"base": {"status": "missing", "version": None, "scope": "host"}}}
    executable = Path(sys.executable).resolve()
    def runner_factory(allowed_commands, *, executable_hashes, environment=None):
        class Runner:
            def run(self, argv, *, timeout, elevated):
                ledger["installer_calls"].append("base")
                save_ledger()
                raise RuntimeError("Выбор/передача файла не должны запускать installer")
        return Runner()
    adapter = FixedCommandAdapter("install-base", os_name="linux", argv=(str(executable), "-I", "-B", "{artifact_0}"), artifact_suffixes=(".py",), executable_sha256=sha(executable.read_bytes()), runner_factory=runner_factory)
    coordinator = PrerequisiteCoordinator(catalog, state_dir=work / "prerequisite-state", owner_id="browser-fixture-owner", downloads=DownloadManager(work / "cache", trusted_hosts=["browser-fixture.example.invalid"], transport=ForbiddenTransport(), retries=0), adapters={"install-base": adapter}, discover=lambda: deepcopy(inventory_observation), markers=lambda: {"boot_id": "browser-fixture-boot", "session_id": "browser-fixture-session"}, product_consent_refs=["product-eula:browser-fixture"])
    class ProductEngine:
        def capabilities(self, selected_target):
            return {"actions": {"install": {"supported": True}}}
        def discover(self, selected_manifest, selected_target, selected_options):
            return {"fingerprint_data": {"fixture": True}}
        def plan(self, *args):
            raise RuntimeError("Product execution is outside browser upload acceptance")
        def execute(self, *args):
            ledger["product_calls"].append("unexpected")
            save_ledger()
            raise RuntimeError("Product execution is outside browser upload acceptance")
    api = InstallerAPI(work / "api-state", engine=ProductEngine(), prerequisites=coordinator)
    session = WizardSession(api, manifest, options, verifier=verifier, development=True)
    server = create_server(session)
    url = "http://127.0.0.1:" + str(server.server_port) + "/#token=" + session.token
    (work / "url.txt").write_text(url, encoding="utf-8")
    os.chmod(work / "url.txt", 0o600)
    write_json(work / "fixture.json", {"scope": "Linux Docker synthetic browser upload; installed wheel; no real OS installer/product lifecycle", "module_origins": origins, "payload_files": inventory["files"], "artifact": {key: artifact[key] for key in ("artifact_id", "size", "sha256")}, "manifest_sha256": hash_manifest(manifest), "target": target, "production_ready": False})
    save_ledger()
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        save_ledger()


if __name__ == "__main__":
    main()
