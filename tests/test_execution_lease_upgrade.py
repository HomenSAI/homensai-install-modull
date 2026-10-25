"""Existing v1 journals cannot become unprotected when a domain is introduced."""
import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from test_prerequisites_execution import bundle, coordinator
from test_public_api import Engine, submit


@pytest.mark.parametrize("unknown_effect", [False, True])
def test_registered_product_refuses_prelease_unfinished_prerequisite(bundle, unknown_effect):
    port = coordinator(bundle)
    plan = port.plan()
    # Exact v1 operation representation used before ExecutionDomain existed;
    # publishing it through the store intentionally does not create a lease.
    old = port._prepare_start("before-upgrade", plan, plan["required_consents"])
    if unknown_effect:
        old["status"] = "RECOVERY_REQUIRED"
        old["steps"]["base"]["state"] = "INSTALLING"
        old["system_changes_started"] = None
        old["required_action"] = "reconcile_installer"
    port.store.save(old, "legacy_operation_created", create=True)
    assert not port.execution_domain.directory.exists()
    root = bundle["tmp_path"]
    engine = Engine(root)
    api = InstallerAPI(root / "new-api-state", engine=engine, prerequisites=port)
    product = api.plan("install", bundle["manifest"], bundle["target"],
                       options={"install_root": str(root / "product-root")})
    with pytest.raises(InstallerError) as caught:
        submit(api, product, background=False)
    assert caught.value.code == "OPERATION_CONFLICT"
    assert engine.calls == 0 and not (root / "executed").exists()
    assert port.store.get("before-upgrade")["status"] == old["status"]
    if not unknown_effect:
        assert api.prerequisites_cancel("before-upgrade")["status"] == "CANCELED"
        assert submit(api, product, background=False)["status"] == "SUCCEEDED"
        assert engine.calls == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_registered_prerequisites_refuse_prelease_unknown_product(bundle, cancel):
    class InterruptedEngine(Engine):
        def execute(self, plan, emit, cancel_requested):
            self.calls += 1
            raise InstallerError("RECOVERY_REQUIRED")

        def resume(self, plan, previous, emit, cancel_requested):
            self.calls += 1
            return {"status": "SUCCEEDED"}

    root = bundle["tmp_path"]
    old_engine = InterruptedEngine(root)
    legacy = InstallerAPI(root / "existing-api", engine=old_engine)
    product = legacy.plan("install", bundle["manifest"], bundle["target"],
                          options={"install_root": str(root / "product-root")})
    interrupted = submit(legacy, product, background=False)
    assert interrupted["status"] == "RECOVERY_REQUIRED"
    if cancel:
        canceled = legacy.cancel(interrupted["operation_id"])
        assert canceled["status"] == "CANCELED"
        assert canceled["result"]["resume_required"] is True
    port = coordinator(bundle)
    api = InstallerAPI(root / "existing-api", engine=old_engine, prerequisites=port)
    assert not port.execution_domain.directory.exists()
    plan = api.prerequisites_plan()
    with pytest.raises(InstallerError) as caught:
        api.prerequisites_start("after-upgrade", plan, plan["required_consents"])
    assert caught.value.code == "OPERATION_CONFLICT"
    assert port.store.get("after-upgrade") is None
    assert old_engine.calls == 1 and bundle["calls"] == []
    recovered = api.resume(interrupted["operation_id"], background=False)
    assert recovered["status"] == "SUCCEEDED" and old_engine.calls == 2
    assert api.prerequisites_start("after-upgrade", plan, plan["required_consents"])["status"] == "AWAITING_USER"
    assert bundle["calls"] == []


@pytest.mark.parametrize("boundary", ["WAITING_REBOOT", "WAITING_RELOGIN"])
def test_prelease_canceled_boundary_cannot_be_mistaken_for_quiescence(bundle, boundary):
    port = coordinator(bundle)
    plan = port.plan()
    old = port._prepare_start("old-boundary", plan, plan["required_consents"])
    old["status"], old["cancel_requested"] = "CANCELED", True
    old["steps"]["base"]["state"] = boundary
    port.store.save(old, "legacy_canceled_boundary", create=True)
    assert not port.store.has_claim()
    root = bundle["tmp_path"]
    engine = Engine(root)
    api = InstallerAPI(root / "new-product-api", engine=engine, prerequisites=port)
    product = api.plan("install", bundle["manifest"], bundle["target"],
                       options={"install_root": str(root / "product-root")})
    with pytest.raises(InstallerError) as caught:
        submit(api, product, background=False)
    assert caught.value.code == "OPERATION_CONFLICT"
    assert engine.calls == 0
