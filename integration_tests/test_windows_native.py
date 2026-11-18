"""Подписанный Windows payload и реальный процесс через публичный SDK."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from homensai_install.api.v1 import InstallerAPI
from homensai_install.trust import canonical_bytes, hash_manifest
from homensai_install.windows_user import private_directory

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native lifecycle")
TARGET = {"os": "windows", "arch": "x64", "mode": "native", "scope": "user"}

_SERVER = """\
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = {"product_id": os.environ["HOMENSAI_PRODUCT_ID"],
                "installation_id": os.environ["HOMENSAI_INSTALLATION_ID"],
                "release": os.environ["HOMENSAI_RELEASE"], "ready": True}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

with HTTPServer(("127.0.0.1", int(os.environ["HOMENSAI_WEB_PORT"])), Handler) as server:
    server.serve_forever()
"""

_MIGRATE = """\
import os
from pathlib import Path
assert Path(__file__).resolve().parent == Path.cwd()
with (Path(os.environ["HOMENSAI_DATA_HOME"]) / "runtime.txt").open("a", encoding="utf-8") as stream:
    stream.write(sys.executable + "\\n")
"""


def copy_test_runtime(runtime):
    """App-local interpreter from the locked test environment, without its venv."""
    base = Path(sys.base_prefix)
    version = f"python{sys.version_info.major}{sys.version_info.minor}"
    runtime.mkdir()
    for source in (base / "python.exe", *base.glob("python*.dll"), *base.glob("vcruntime*.dll")):
        shutil.copyfile(source, runtime / source.name)
    shutil.copytree(base / "DLLs", runtime / "DLLs", ignore=shutil.ignore_patterns("*.pdb"))
    with zipfile.ZipFile(runtime / (version + ".zip"), "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for source in (base / "Lib").rglob("*.py"):
            relative = source.relative_to(base / "Lib")
            if not {"site-packages", "test", "tests", "__pycache__"}.intersection(relative.parts):
                zipped.write(source, relative.as_posix())
    (runtime / (version + "._pth")).write_text(version + ".zip\nDLLs\n.\n", encoding="ascii")


@pytest.fixture
def windows_installation(tmp_path):
    root = private_directory(tmp_path / "Тест Windows")
    payload = root / "source"
    payload.mkdir()
    copy_test_runtime(payload / "runtime")
    (payload / "app.py").write_text(_SERVER, encoding="utf-8")
    (payload / "migrate.py").write_text("import sys\n" + _MIGRATE, encoding="utf-8")
    (payload / "legal").mkdir()
    (payload / "legal/en.txt").write_text("Windows DEV fixture", encoding="utf-8")
    (payload / "locks").mkdir()
    for name in ("linux", "windows"):
        (payload / "locks" / (name + ".lock")).write_text("fixture\n", encoding="utf-8")
    (payload / "vendor").mkdir()
    (payload / "vendor/reference_fixture-1-py3-none-any.whl").write_bytes(b"fixture: no pip execution")

    manifest = json.loads((Path(__file__).parents[1] / "examples/rent.manifest.json").read_bytes())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        manifest["web"]["port"] = probe.getsockname()[1]
    manifest.update(product_id="windows-sdk-reference", product_name="Windows SDK reference")
    manifest["paths"] = {name: name for name in ("app", "releases", "data", "backup", "services", "config", "runtime")}
    manifest["components"] = [{**manifest["components"][0], "name": "web", "cmd": ["python", "app.py"], "after": []}]
    manifest["modes"] = [{"name": "local", "components": ["web"]}]
    manifest["mutual_exclusion"] = []
    manifest["migrations"]["cmd"] = ["python", "migrate.py"]
    manifest["hooks"] = {"pre_start": [], "health_checks": [], "post_update": []}
    manifest["secrets"] = []
    manifest["update"]["health_wait"] = 10
    def artifact(name):
        return {"file": name, "sha256": hashlib.sha256((payload / name).read_bytes()).hexdigest()}
    manifest["runtime"]["lock_files"] = {name: artifact("locks/" + name + ".lock")
                                          for name in ("linux", "windows")}
    manifest["runtime"]["vendor_wheels"] = [artifact("vendor/reference_fixture-1-py3-none-any.whl")]
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    trust = root / "trust.json"
    trust.write_bytes(canonical_bytes({"schema_version": 1, "keys": {"fixture": base64.b64encode(public).decode()}}))
    inventory = {
        "schema_version": 1, "product_id": manifest["product_id"], "release": "reference-1",
        "target": {name: TARGET[name] for name in ("os", "arch", "mode")}, "development": True,
        "manifest_sha256": hash_manifest(manifest), "runtime_executable": "runtime/python.exe",
        "files": {path.relative_to(payload).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in payload.rglob("*") if path.is_file()},
    }
    signature = {"algorithm": "ed25519", "key_id": "fixture",
                 "signature": base64.b64encode(key.sign(canonical_bytes(inventory))).decode()}
    (root / "inventory.json").write_bytes(canonical_bytes(inventory))
    (root / "signature.json").write_bytes(canonical_bytes(signature))
    options = {"payload_dir": str(payload), "inventory_file": str(root / "inventory.json"),
               "signature_file": str(root / "signature.json"), "release": inventory["release"],
               "runtime_executable": inventory["runtime_executable"], "install_root": str(root / "Приложение с пробелом"),
               "eula_locale": "en", "eula_digest": inventory["files"]["legal/en.txt"]}
    api = InstallerAPI.from_bootstrap(root / "api", trust_config=trust, allow_development=True)
    try:
        yield api, manifest, options, trust
    finally:
        # Only Popen handles returned by the fixture's own engine.
        for child in api._engine()._backends[("native", "user")]._children.values():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


def test_signed_native_install_via_public_sdk(windows_installation):
    api, manifest, options, trust = windows_installation
    root = Path(options["install_root"])
    assert api.capabilities(TARGET)["actions"]["install"]["supported"]
    inventory = api.discover(manifest, TARGET, options)
    assert not root.exists()
    plan = api.plan("install", manifest, TARGET, inventory=inventory, options=options)
    assert plan["target"] == TARGET and plan["engine_plan"]["backend_routing"]["target"] == TARGET
    assert not root.exists()
    consents = [*plan["required_consents"], plan["review_consent_ref"]]
    installed = api.submit(plan["plan_id"], plan["plan_digest"], consents,
                           idempotency_key="windows-signed-install", background=False)
    assert installed["status"] == "SUCCEEDED", installed.get("error")
    assert installed["result"]["health"]["ready"] is True
    other = InstallerAPI.from_bootstrap(api.state_dir, trust_config=trust, allow_development=True)
    assert other.status(installed["operation_id"])["status"] == "SUCCEEDED"
    replay = other.submit(plan["plan_id"], plan["plan_digest"], consents,
                          idempotency_key="windows-signed-install", background=False)
    assert replay["operation_id"] == installed["operation_id"]
    ran = (root / "data/runtime.txt").read_text(encoding="utf-8").splitlines()
    assert len(ran) == 1 and Path(ran[0]).is_relative_to(root / "releases")


def test_windows_signed_payload_tamper_rejected_before_installation(windows_installation):
    api, manifest, options, _ = windows_installation
    plan = api.plan("install", manifest, TARGET, options=options)
    (Path(options["payload_dir"]) / "app.py").write_text("raise SystemExit", encoding="utf-8")
    result = api.submit(plan["plan_id"], plan["plan_digest"],
                        [*plan["required_consents"], plan["review_consent_ref"]],
                        idempotency_key="tampered-windows-payload", background=False)
    assert result["status"] == "RECOVERY_REQUIRED" and result["error"]["code"] == "INTEGRITY_FAILED"
    assert not Path(options["install_root"]).exists()
