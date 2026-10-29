"""SDK preparation stays explicit and never turns ordinary planning into execution."""
from copy import deepcopy

import pytest

from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI
from homensai_install.backends import BackendRouter
from homensai_install.errors import InstallerError
from test_public_api import Engine, manifest


class PreparationEngine(Engine):
    def __init__(self, root):
        super().__init__(root)
        self.prepared = []
        self.product_plan = {"from_version": "1", "to_version": "2", "steps": [
            {"id": "schema-v2", "backup_required": True, "reversible": False}]}
        self.product_plan["plan_digest"] = digest(self.product_plan)
        self.result = {"migration_review_id": "a" * 64, "action": "update",
                       "product_plan": self.product_plan, "from_release": "v1", "to_release": "v2",
                       "installation_id": "installation", "binding_sha256": "b" * 64}

    def prepare_migration(self, action, source, target, options):
        self.prepared.append((action, deepcopy(source), deepcopy(target), deepcopy(options)))
        return deepcopy(self.result)


def test_prepare_is_an_explicit_sdk_call_and_plan_never_runs_it(tmp_path, manifest):
    engine = PreparationEngine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=BackendRouter(native=engine))
    options = {"install_root": str(tmp_path / "install"), "release": "v2"}
    api.validate(manifest)
    api.discover(manifest, options=options)
    plan = api.plan("update", manifest, options=options)
    assert engine.prepared == [] and engine.calls == 0
    result = api.prepare_migration("update", manifest, options=options)
    assert result == engine.result and len(engine.prepared) == 1 and engine.calls == 0
    api.plan("update", manifest, options={**options, "migration_review_id": result["migration_review_id"]})
    assert len(engine.prepared) == 1
    assert plan["options"] == options


@pytest.mark.parametrize("action", [None, [], "backup", "arbitrary_shell"])
def test_preparation_rejects_other_actions_before_backend(tmp_path, manifest, action):
    engine = PreparationEngine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=engine)
    with pytest.raises(InstallerError) as error:
        api.prepare_migration(action, manifest)
    assert error.value.code == "INVALID_REQUEST" and engine.prepared == []
    assert not api.state_dir.exists()


def test_unconfigured_preparation_never_falls_back_to_other_backend(tmp_path, manifest):
    engine = PreparationEngine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=BackendRouter(native=engine))
    with pytest.raises(InstallerError) as error:
        api.prepare_migration("update", manifest,
            target={"os": "linux", "arch": "x64", "mode": "docker", "scope": "user"})
    assert error.value.code == "CAPABILITY_UNSUPPORTED" and engine.prepared == []
    legacy = InstallerAPI(tmp_path / "legacy-state", engine=Engine(tmp_path))
    with pytest.raises(InstallerError) as error:
        legacy.prepare_migration("update", manifest)
    assert error.value.code == "CAPABILITY_UNSUPPORTED"
    assert not api.state_dir.exists() and not legacy.state_dir.exists()


@pytest.mark.parametrize("identifier", [None, True, {}, "../review", "0" * 63, "F" * 64])
def test_review_identifier_is_opaque_digest_and_cannot_select_a_path(tmp_path, manifest, identifier):
    engine = PreparationEngine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=engine)
    with pytest.raises(InstallerError) as error:
        api.plan("update", manifest, options={"migration_review_id": identifier})
    assert error.value.code == "INVALID_REQUEST" and engine.prepared == []


@pytest.mark.parametrize("change", ["extra", "action", "review", "binding", "release", "plan", "missing_source"])
def test_preparation_rejects_mismatched_or_tampered_backend_result(tmp_path, manifest, change):
    engine = PreparationEngine(tmp_path)
    if change == "extra": engine.result["archive_path"] = "/not-public"
    elif change == "action": engine.result["action"] = "install"
    elif change == "review": engine.result["migration_review_id"] = "../review"
    elif change == "binding": engine.result["binding_sha256"] = "invalid"
    elif change == "release": engine.result["to_release"] = "other"
    elif change == "missing_source": engine.result["from_release"] = None
    else: engine.result["product_plan"]["from_version"] = "other"
    api = InstallerAPI(tmp_path / "state", engine=engine)
    with pytest.raises(InstallerError) as error:
        api.prepare_migration("update", manifest, options={"release": "v2"})
    assert error.value.code == "UNEXPECTED_ERROR" and engine.calls == 0
