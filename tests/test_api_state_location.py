"""Журнал API остаётся вне заменяемых payload и постоянных roots продукта."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from shared.datahome import resolve_datahome


ROOT = Path(__file__).resolve().parents[1]


class Engine:
    def __init__(self):
        self.effects = []

    def capabilities(self, target):
        return {"actions": {"install": {"supported": True}}}

    def discover(self, manifest, target, options):
        return {"fingerprint_data": {"stable": True}}

    def plan(self, *args):
        return {"steps": [], "required_consents": []}

    def execute(self, *args):
        self.effects.append("execute")
        return {"status": "RECOVERY_REQUIRED"}

    def resume(self, *args):
        self.effects.append("resume")
        return {"status": "SUCCEEDED"}


@pytest.fixture
def subject(tmp_path):
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text())
    options = {"install_root": str(tmp_path / "install"),
               "payload_dir": str(tmp_path / "signed-payload")}
    home = resolve_datahome(manifest, options["install_root"])
    roots = {"app": home.app, "releases": home.releases, **home.roots,
             "payload": Path(options["payload_dir"])}
    return manifest, options, roots, Engine()


def rejected(function):
    with pytest.raises(InstallerError) as error:
        function()
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.to_dict()["details"] == {"field_path": "state_dir",
                                              "reason": "state_directory_overlaps_managed_files"}


@pytest.mark.parametrize("name", ["app", "releases", "data", "backup", "services", "config", "runtime", "payload"])
@pytest.mark.parametrize("nested", [False, True])
def test_plan_refuses_state_inside_every_managed_root_before_creating_journal(subject, name, nested):
    manifest, options, roots, engine = subject
    directory = roots[name] / "private-state" if nested else roots[name]
    api = InstallerAPI(directory, engine=engine)
    rejected(lambda: api.plan("install", manifest, options=options))
    assert not directory.exists()
    assert not Path(options["install_root"]).exists()
    assert not Path(options["payload_dir"]).exists()
    assert engine.effects == []


@pytest.mark.parametrize("location", ["outside", "control", "similar-sibling", "ancestor"])
def test_unrelated_private_state_locations_remain_supported(subject, tmp_path, location):
    manifest, options, roots, engine = subject
    directory = {"outside": tmp_path / "state",
                 "control": Path(options["install_root"]) / ".hinstall" / "api",
                 "similar-sibling": roots["data"].with_name(roots["data"].name + "-api"),
                 "ancestor": tmp_path}[location]
    api = InstallerAPI(directory, engine=engine)
    plan = api.plan("install", manifest, options=options)
    assert api.get_plan(plan["plan_id"]) == plan
    assert engine.effects == []
    assert (directory / "operations.sqlite3").is_file()


def test_custom_nested_manifest_data_root_is_protected(subject, tmp_path):
    manifest, options, roots, engine = subject
    manifest = deepcopy(manifest)
    manifest["paths"]["data"] = "persistent/user-data"
    directory = Path(options["install_root"]) / "persistent/user-data/api-state"
    api = InstallerAPI(directory, engine=engine)
    rejected(lambda: api.plan("install", manifest, options=options))
    assert not directory.parent.exists()


def test_case_variant_cannot_bypass_conservative_path_comparison(subject):
    manifest, options, roots, engine = subject
    directory = roots["data"].with_name(roots["data"].name.upper()) / "api-state"
    api = InstallerAPI(directory, engine=engine)
    rejected(lambda: api.plan("install", manifest, options=options))
    assert not directory.exists()


def test_legacy_manifest_keeps_explicit_payload_boundary_without_invented_roots(subject, tmp_path):
    _, options, roots, engine = subject
    manifest = json.loads((ROOT / "examples/jobpilot.manifest.json").read_text())
    rejected(lambda: InstallerAPI(roots["payload"] / "api-state", engine=engine).plan(
        "install", manifest, options=options))
    assert not roots["payload"].exists()
    outside = InstallerAPI(tmp_path / "legacy-api", engine=engine)
    plan = outside.plan("install", manifest)
    assert outside.get_plan(plan["plan_id"]) == plan


def foreign_options():
    # Pure transport fixture; no foreign drive, process or target OS is accessed.
    return ({"install_root": "C:\\Users\\Synthetic User\\Application",
             "payload_dir": "D:\\Signed Payload"} if os.name != "nt" else
            {"install_root": "/home/synthetic-user/application", "payload_dir": "/opt/signed-payload"})


def test_foreign_absolute_locators_roundtrip_without_host_resolution(subject, tmp_path):
    manifest, _, _, engine = subject
    options = foreign_options()
    api = InstallerAPI(tmp_path / "transport-api", engine=engine)
    plan = api.plan("install", manifest, options=options)
    assert api.get_plan(plan["plan_id"])["options"] == options
    operation = api.submit(plan["plan_id"], plan["plan_digest"], [plan["review_consent_ref"]],
                           idempotency_key="foreign-transport", background=False)
    assert operation["status"] == "RECOVERY_REQUIRED"
    assert api.resume(operation["operation_id"], background=False)["status"] == "SUCCEEDED"
    assert engine.effects == ["execute", "resume"]


def test_foreign_install_root_does_not_disable_local_payload_boundary(subject, tmp_path):
    manifest, _, _, engine = subject
    options = {**foreign_options(), "payload_dir": str(tmp_path / "local-payload")}
    state = tmp_path / "local-payload" / "api-state"
    rejected(lambda: InstallerAPI(state, engine=engine).plan("install", manifest, options=options))
    assert not state.parent.exists()


def test_foreign_install_root_does_not_disable_absolute_local_manifest_roots(subject, tmp_path):
    manifest, _, roots, engine = subject
    manifest = deepcopy(manifest)
    manifest["paths"] = {name: str(roots[name]) for name in manifest["paths"]}
    state = roots["data"] / "api-state"
    rejected(lambda: InstallerAPI(state, engine=engine).plan(
        "install", manifest, options=foreign_options()))
    assert not state.parent.exists()


@pytest.mark.parametrize("root", ["C:relative", "C:\\Users\\..\\Application", "\\\\server\\share"])
def test_unsafe_foreign_paths_are_not_silently_exempted(subject, tmp_path, root):
    manifest, _, _, engine = subject
    state = tmp_path / "api-state"
    with pytest.raises(InstallerError) as error:
        InstallerAPI(state, engine=engine).plan("install", manifest, options={"install_root": root})
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.to_dict()["details"] == {"reason": "unsafe_managed_path"}
    assert not state.exists()


def database_snapshot(directory):
    return {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_ino,
                        path.stat().st_mode, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
            for path in directory.iterdir() if path.is_file()}


def relocated_state(subject, tmp_path, *, operation=False):
    manifest, options, roots, engine = subject
    original = tmp_path / "original-api"
    api = InstallerAPI(original, engine=engine)
    plan = api.plan("install", manifest, options=options)
    result = None
    if operation:
        result = api.submit(plan["plan_id"], plan["plan_digest"], [plan["review_consent_ref"]],
                            idempotency_key="original", background=False)
        assert result["status"] == "RECOVERY_REQUIRED"
        # Reproduce a persisted request whose controller died before returning.
        api._journal().update(result["operation_id"],
            {"status": "RUNNING", "owner": {"pid": 2147483647, "process_start": "never"}},
            {"type": "fixture.original_controller_lost"})
    destination = roots["data"] / "api-state"
    destination.parent.mkdir(parents=True, mode=0o700)
    shutil.copytree(original, destination)
    engine.effects.clear()
    return InstallerAPI(destination, engine=engine), plan, result, engine


def test_submit_rechecks_legacy_plan_before_journal_or_product_writes(subject, tmp_path):
    api, plan, _, engine = relocated_state(subject, tmp_path)
    before = database_snapshot(api.state_dir)
    rejected(lambda: api.submit(plan["plan_id"], plan["plan_digest"], [plan["review_consent_ref"]],
                                idempotency_key="new", background=False))
    assert database_snapshot(api.state_dir) == before
    assert engine.effects == []


def test_resume_refuses_legacy_location_before_interruption_status_write(subject, tmp_path):
    api, _, operation, engine = relocated_state(subject, tmp_path, operation=True)
    before = database_snapshot(api.state_dir)
    rejected(lambda: api.resume(operation["operation_id"], background=False))
    assert database_snapshot(api.state_dir) == before
    assert engine.effects == []
