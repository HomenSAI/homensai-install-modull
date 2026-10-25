"""Known pre-domain journals remain guarded after the registering SDK exits."""

import json
import sqlite3

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from test_prerequisites_execution import bundle, coordinator
from test_public_api import Engine, submit


def register_refused_legacy_request(bundle):
    root = bundle["tmp_path"]
    legacy_engine = Engine(root)
    legacy_engine.execute = lambda *_: {"status": "RECOVERY_REQUIRED"}
    legacy = InstallerAPI(root / "legacy-api", engine=legacy_engine)
    plan = legacy.plan("install", bundle["manifest"], bundle["target"],
                       options={"install_root": str(root / "legacy-root")})
    interrupted = submit(legacy, plan, background=False)
    assert interrupted["status"] == "RECOVERY_REQUIRED"

    registered = coordinator(bundle)
    upgraded = InstallerAPI(root / "legacy-api", engine=legacy_engine, prerequisites=registered)
    prerequisite_plan = upgraded.prerequisites_plan()
    with pytest.raises(InstallerError) as caught:
        upgraded.prerequisites_start("refused-registration", prerequisite_plan,
                                     prerequisite_plan["required_consents"])
    assert caught.value.code == "OPERATION_CONFLICT"
    assert registered.store.get("refused-registration") is None
    return registered


@pytest.mark.parametrize("next_kind", ["product", "prerequisite"])
def test_rejected_upgrade_remembers_legacy_journal_in_new_domain_instance(bundle, next_kind):
    """A refused first mutation must not forget the old journal on restart.

    Both controllers explicitly participate in the same trusted domain. The
    legacy journal has already been presented by a mutating API request, not
    merely mentioned by a read-only constructor.
    """
    root = bundle["tmp_path"]
    registered = register_refused_legacy_request(bundle)

    # New coordinator and API objects cannot inherit an in-memory callback
    # registration from the old domain object. Only durable knowledge survives.
    restored = coordinator(bundle)
    assert restored.execution_domain is not registered.execution_domain
    assert restored.execution_domain.same_domain(registered.execution_domain)
    fresh_engine = Engine(root)
    fresh = InstallerAPI(root / "different-api", engine=fresh_engine, prerequisites=restored)
    assert restored.execution_domain.busy() is True
    with pytest.raises(InstallerError) as caught:
        if next_kind == "product":
            product = fresh.plan("install", bundle["manifest"], bundle["target"],
                                  options={"install_root": str(root / "different-root")})
            submit(fresh, product, background=False)
        else:
            plan = fresh.prerequisites_plan()
            fresh.prerequisites_start("after-controller-restart", plan, plan["required_consents"])
    assert caught.value.code == "OPERATION_CONFLICT"
    assert fresh_engine.calls == 0 and not (root / "executed").exists()
    assert restored.store.get("after-controller-restart") is None
    assert bundle["calls"] == []


@pytest.mark.parametrize("change", ["missing", "replacement"])
def test_removing_or_replacing_known_legacy_journal_does_not_clear_guard(bundle, change):
    from homensai_install.api.journal import Journal

    root = bundle["tmp_path"]
    register_refused_legacy_request(bundle)
    journal = root / "legacy-api" / "operations.sqlite3"
    before = journal.read_bytes()
    preserved = root / "preserved-legacy.sqlite3"
    journal.rename(preserved)
    if change == "replacement":
        Journal(root / "legacy-api", create=True)
        assert journal.stat().st_ino != preserved.stat().st_ino
    restored = coordinator(bundle)
    api = InstallerAPI(root / "different-api", engine=Engine(root), prerequisites=restored)
    assert restored.execution_domain.busy() is None
    plan = api.prerequisites_plan()
    with pytest.raises(InstallerError) as caught:
        api.prerequisites_start("must-not-bypass-missing-history", plan, plan["required_consents"])
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert restored.store.get("must-not-bypass-missing-history") is None
    assert preserved.read_bytes() == before
    assert bundle["calls"] == [] and not (root / "executed").exists()


@pytest.mark.parametrize("damage", ["missing_product_status", "missing_prerequisite_status", "invalid_prerequisite_steps"])
def test_valid_json_with_incomplete_legacy_state_is_not_quiescence(bundle, damage):
    root = bundle["tmp_path"]
    port = coordinator(bundle)
    engine = Engine(root)
    if damage == "missing_product_status":
        legacy = InstallerAPI(root / "legacy-api", engine=engine)
        engine.execute = lambda *_: {"status": "SUCCEEDED"}
        plan = legacy.plan("install", bundle["manifest"], bundle["target"],
                           options={"install_root": str(root / "legacy-root")})
        terminal = submit(legacy, plan, background=False)
        assert terminal["status"] == "SUCCEEDED"
        with sqlite3.connect(legacy._journal().path) as db:
            operation = json.loads(db.execute("SELECT body FROM operations").fetchone()[0])
            del operation["status"]
            db.execute("UPDATE operations SET body=?", (json.dumps(operation),))
        api = InstallerAPI(root / "legacy-api", engine=engine, prerequisites=port)
        plan = api.prerequisites_plan()
        with pytest.raises(InstallerError) as caught:
            api.prerequisites_start("must-not-classify-corruption-as-idle", plan, plan["required_consents"])
        assert port.store.get("must-not-classify-corruption-as-idle") is None
    else:
        plan = port.plan()
        old = port._prepare_start("damaged-legacy", plan, plan["required_consents"])
        old["status"], old["cancel_requested"] = "CANCELED", True
        port.store.save(old, "legacy_canceled", create=True)
        assert not port.store.has_claim()
        with sqlite3.connect(port.store.path) as db:
            operation = json.loads(db.execute("SELECT body FROM operations").fetchone()[0])
            if damage == "missing_prerequisite_status":
                del operation["status"]
            else:
                operation["steps"] = None
            db.execute("UPDATE operations SET body=?", (json.dumps(operation),))
        api = InstallerAPI(root / "new-api", engine=engine, prerequisites=port)
        product = api.plan("install", bundle["manifest"], bundle["target"],
                           options={"install_root": str(root / "product-root")})
        with pytest.raises(InstallerError) as caught:
            submit(api, product, background=False)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert engine.calls == 0 and not (root / "executed").exists()
    assert bundle["calls"] == []


def test_legacy_journal_replacement_between_observation_and_read_is_refused(bundle, monkeypatch):
    from homensai_install.prerequisite_state import PrerequisiteStore

    root = bundle["tmp_path"]
    port = coordinator(bundle)
    plan = port.plan()
    old = port._prepare_start("interrupted-before-upgrade", plan, plan["required_consents"])
    old["status"], old["system_changes_started"] = "RECOVERY_REQUIRED", None
    old["steps"]["base"]["state"] = "INSTALLING"
    port.store.save(old, "legacy_unknown_effect", create=True)
    engine = Engine(root)
    api = InstallerAPI(root / "new-api", engine=engine, prerequisites=port)
    product = api.plan("install", bundle["manifest"], bundle["target"],
                       options={"install_root": str(root / "product-root")})
    original = port.execution_domain._guard_registry
    preserved = root / "preserved-interrupted.sqlite3"
    original_bytes = port.store.path.read_bytes()

    def replace_then_read(records, **kwargs):
        if not preserved.exists():
            expected = next(record for record in records if record["kind"] == "prerequisite")
            assert expected["identity"] == [port.store.path.stat().st_dev, port.store.path.stat().st_ino]
            port.store.path.rename(preserved)
            PrerequisiteStore(port.store.directory)
            assert port.store.path.stat().st_ino != preserved.stat().st_ino
        return original(records, **kwargs)

    monkeypatch.setattr(port.execution_domain, "_guard_registry", replace_then_read)
    with pytest.raises(InstallerError) as caught:
        submit(api, product, background=False)
    assert caught.value.code == "STATE_STORAGE_UNAVAILABLE"
    assert engine.calls == 0 and not (root / "executed").exists()
    assert preserved.read_bytes() == original_bytes
