"""Signed service contract tests: без manager calls, runtime запуска и сети."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from homensai_install.errors import InstallerError
from homensai_install.service_contracts import (load_verified_service_contract,
    render_service_plan, service_id, verify_service_ownership)
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest


@pytest.fixture
def bundle_factory(tmp_path):
    def make(backend="systemd-user", *, transform=None):
        payload = tmp_path / backend
        payload.mkdir()
        files = {"runtime/python": b"synthetic runtime, never executed",
                 "server.py": b"synthetic product, never executed",
                 "stop.py": b"synthetic synchronous shutdown helper",
                 "tools/winsw.exe": b"synthetic wrapper fixture, NOT a real WinSW executable",
                 "legal/winsw-license.txt": b"MIT fixture notice, never distributed as WinSW",
                 "locks/win.lock": b"fixture", "locks/linux.lock": b"fixture",
                 "vendor/fixture-1-py3-none-any.whl": b"synthetic fixture, not a distribution wheel"}
        manifest = json.loads((Path(__file__).parents[1] / "examples/rent.manifest.json").read_text())
        manifest["paths"] = {name: name for name in ("app", "releases", "data", "backup", "services", "config", "runtime")}
        manifest["secrets"] = []
        manifest["components"] = [copy.deepcopy(manifest["components"][0])]
        manifest["components"][0]["cmd"] = ["python", "server.py", "quoted argument", 'double"quote', "$PATH", "x%y", ";"]
        manifest["components"][0]["after"] = []
        manifest["modes"] = [{"name": "local", "components": ["web"]}]
        manifest["mutual_exclusion"] = []
        manifest["runtime"]["lock_files"] = {name: {"file": path, "sha256": hashlib.sha256(files[path]).hexdigest()}
                                                for name, path in (("windows", "locks/win.lock"), ("linux", "locks/linux.lock"))}
        wheel = "vendor/fixture-1-py3-none-any.whl"
        manifest["runtime"]["vendor_wheels"] = [{"file": wheel, "sha256": hashlib.sha256(files[wheel]).hexdigest()}]
        document = {"schema_version": 1, "backend": backend,
                    "components": [{"name": "web", "stop_cmd": ["python", "stop.py", "--wait"], "stop_timeout_seconds": 30}]}
        target = {"os": "windows" if backend == "windows-winsw" else "linux", "arch": "x64", "mode": "native",
                  "scope": "machine" if backend == "windows-winsw" else "user"}
        if backend == "windows-winsw":
            document["wrapper"] = {"file": "tools/winsw.exe", "version": "2.12.0", "license": "MIT", "license_file": "legal/winsw-license.txt"}
            manifest["winsw"] = {"version": "2.12.0", "sha256": hashlib.sha256(files["tools/winsw.exe"]).hexdigest()}
            manifest["components"][0]["cmd"].remove("x%y")
        if transform:
            transform(manifest, document, files, target)
        files["service-contract.json"] = canonical_bytes(document)
        for name, data in files.items():
            output = payload / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        inventory = {"schema_version": 1, "product_id": manifest["product_id"], "release": "1.0.0",
                     "target": {key: target[key] for key in ("os", "arch", "mode")},
                     "development": True, "manifest_sha256": hash_manifest(manifest), "runtime_executable": "runtime/python",
                     "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        signature = {"algorithm": "ed25519", "key_id": "test",
                     "signature": base64.b64encode(private.sign(canonical_bytes(inventory))).decode()}
        inv, sig = tmp_path / (backend + ".inventory.json"), tmp_path / (backend + ".signature.json")
        inv.write_bytes(canonical_bytes(inventory)); sig.write_bytes(canonical_bytes(signature))
        options = {"payload_dir": str(payload), "inventory_file": str(inv), "signature_file": str(sig),
                   "release": "1.0.0", "runtime_executable": "runtime/python"}
        verifier = SignedPayloadVerifier({"test": public}, allow_test_keys=True)
        return manifest, target, options, verifier
    return make


def load(bundle):
    manifest, target, options, verifier = bundle
    return load_verified_service_contract(manifest, target, options, verifier=verifier)


def test_signed_systemd_plan_uses_absolute_runtime_and_escaped_arguments(bundle_factory):
    contract = load(bundle_factory())
    result = render_service_plan(contract, install_root="/srv/HomenS AI/данные", installation_id="1" * 32, modes=["local"])
    text = next(iter(result["files"].values()))
    assert 'ExecStart="/srv/HomenS AI/данные/releases/app-1.0.0/runtime/python"' in text
    assert '"$$PATH"' in text and '"x%%y"' in text and '\\;' in text
    assert 'SendSIGKILL=no' in text and 'KillMode=control-group' in text
    assert 'WantedBy=default.target' in text and 'Type=exec' in text
    assert 'ExecStop="/srv/HomenS AI/данные/releases/app-1.0.0/runtime/python" "stop.py" "--wait"' in text
    assert result["execution_supported"] is False and result["production_ready"] is False
    assert "live_host_adapter" in result["required_gates"]


def test_signed_winsw_plan_has_explicit_stop_and_low_privilege_identity(bundle_factory):
    contract = load(bundle_factory("windows-winsw"))
    result = render_service_plan(contract, install_root="C:\\Program Files\\HomenS AI", installation_id="1" * 32, modes=["local"])
    xml = ET.fromstring(next(iter(result["files"].values())))
    assert xml.findtext("executable") == "C:\\Program Files\\HomenS AI\\releases\\app-1.0.0\\runtime\\python"
    assert xml.findtext("stopexecutable") == xml.findtext("executable")
    assert xml.findtext("stoparguments") == '"stop.py" "--wait"'
    assert xml.find("arguments") is None
    assert xml.findtext("serviceaccount/user") == "LocalService"
    assert xml.find("log").attrib["mode"] == "none"
    assert "reboot" not in {element.get("action") for element in xml.findall("onfailure")}
    assert result["services"][0]["wrapper_license"] == "MIT"
    assert result["services"][0]["wrapper_destination"].endswith(xml.findtext("id") + ".exe")


@pytest.mark.parametrize("tamper", ["contract", "wrapper", "manifest", "target"])
def test_tampering_is_rejected_before_render(bundle_factory, tamper):
    bundle = bundle_factory("windows-winsw")
    manifest, target, options, _ = bundle
    if tamper == "contract":
        (Path(options["payload_dir"]) / "service-contract.json").write_text('{}')
    elif tamper == "wrapper":
        (Path(options["payload_dir"]) / "tools/winsw.exe").write_text("modified wrapper")
    elif tamper == "manifest":
        manifest["components"][0]["cmd"] = ["python", "-c", "untrusted"]
    else:
        target["os"] = "linux"
    with pytest.raises(InstallerError) as error:
        load(bundle)
    assert error.value.code == "INTEGRITY_FAILED"


@pytest.mark.parametrize("change", ["outside_executable", "unknown_component", "extra_field", "missing_stopargs", "missing_notice", "pin_mismatch"])
def test_even_signed_contract_cannot_bypass_service_rules(bundle_factory, change):
    def transform(manifest, document, files, target):
        if change == "outside_executable": document["components"][0]["stop_cmd"] = ["../outside.exe", "stop"]
        elif change == "unknown_component": document["components"][0]["name"] = "foreign"
        elif change == "extra_field": document["sudo_shell"] = "anything"
        elif change == "missing_stopargs": document["components"][0]["stop_cmd"] = ["stop.py"]
        elif change == "missing_notice": files.pop("legal/winsw-license.txt")
        elif change == "pin_mismatch": manifest["winsw"]["sha256"] = "0" * 64
    with pytest.raises(InstallerError):
        load(bundle_factory("windows-winsw", transform=transform))


@pytest.mark.parametrize("root", ["/", "relative", "/srv/../other", "/srv/CON", "/srv/folder\n[Service]"])
def test_systemd_unsafe_roots_are_rejected(bundle_factory, root):
    with pytest.raises(InstallerError):
        render_service_plan(load(bundle_factory()), install_root=root, installation_id="1" * 32, modes=["local"])


def test_winsw_percent_expansion_is_rejected(bundle_factory):
    with pytest.raises(InstallerError):
        render_service_plan(load(bundle_factory("windows-winsw")), install_root="C:\\Apps\\%UNTRUSTED%", installation_id="1" * 32, modes=["local"])


def test_product_secrets_require_credential_adapter(bundle_factory):
    def transform(manifest, document, files, target):
        manifest["secrets"] = [{"name": "TOKEN", "contexts": ["service"], "required": True, "same_value": False}]
    with pytest.raises(InstallerError) as error:
        render_service_plan(load(bundle_factory(transform=transform)), install_root="/srv/app", installation_id="1" * 32, modes=["local"])
    assert error.value.code == "CAPABILITY_UNSUPPORTED"


def test_manager_ownership_requires_every_critical_field(bundle_factory):
    result = render_service_plan(load(bundle_factory()), install_root="/srv/app", installation_id="1" * 32, modes=["local"])
    receipt = result["services"][0]
    assert verify_service_ownership(receipt, receipt)["owned"] is True
    for field in ("configuration_sha256", "executable", "service_account", "working_directory", "service_id",
                  "dropin_paths", "extra_exec_hooks", "start_argv", "stop_argv", "environment_sha256"):
        observed = {**receipt, field: "foreign"}
        with pytest.raises(InstallerError) as error:
            verify_service_ownership(receipt, observed)
        assert error.value.code == "OPERATION_CONFLICT"


def test_installations_never_share_service_names():
    assert service_id("product", "1" * 32, "web") != service_id("product", "2" * 32, "web")
    assert service_id("product", "1" * 32, "web") != service_id("product", "1" * 32, "db")


def test_verifier_is_mandatory(bundle_factory):
    manifest, target, options, _ = bundle_factory()
    with pytest.raises(InstallerError) as error:
        load_verified_service_contract(manifest, target, options)
    assert error.value.code == "INTEGRITY_FAILED"
