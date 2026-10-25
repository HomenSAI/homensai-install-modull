"""Trusted configuration and failed-finalization recovery; synthetic effects."""
from contextlib import contextmanager

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from homensai_install.execution_lease import ExecutionDomain
from homensai_install.prerequisite_runner import CommandResult
from test_prerequisites_execution import bundle, coordinator, start_auto
from test_public_api import Engine, manifest, make_plan, submit


def test_domain_construction_is_lazy_and_registered_api_cannot_override(bundle):
    domain = ExecutionDomain(bundle["tmp_path"] / "shared-domain", owner_id="original-user-1000")
    subject = coordinator(bundle, execution_domain=domain)
    first = InstallerAPI(bundle["tmp_path"] / "product-a", prerequisites=subject)
    second = InstallerAPI(bundle["tmp_path"] / "product-b", prerequisites=subject,
                          execution_domain=ExecutionDomain(domain.directory, owner_id=domain.owner_id))
    assert first.execution_domain is subject.execution_domain
    assert second.execution_domain is subject.execution_domain
    assert domain.busy() is False and not domain.directory.exists()
    assert not first.state_dir.exists() and not second.state_dir.exists()
    for owner, directory in (("other-owner", domain.directory), (domain.owner_id, domain.directory / "other")):
        with pytest.raises(InstallerError) as caught:
            InstallerAPI(bundle["tmp_path"] / "forbidden", prerequisites=subject,
                         execution_domain=ExecutionDomain(directory, owner_id=owner))
        assert caught.value.code == "INVALID_REQUEST"
    assert not domain.directory.exists()


def test_failed_release_preserves_domain_and_explicit_resume_does_not_repeat_engine(tmp_path, manifest, monkeypatch):
    domain = ExecutionDomain(tmp_path / "domain", owner_id="test-owner")
    engine = Engine(tmp_path)
    api = InstallerAPI(tmp_path / "state", engine=engine, execution_domain=domain)
    original_db = domain._db
    refused = []

    @contextmanager
    def unavailable_final_release(**kwargs):
        if kwargs.get("write") and engine.calls and not refused:
            refused.append("release")
            raise InstallerError("STATE_STORAGE_UNAVAILABLE")
        with original_db(**kwargs) as db:
            yield db

    monkeypatch.setattr(domain, "_db", unavailable_final_release)
    plan = make_plan((api, engine, manifest, {"install_root": str(tmp_path / "product")}))
    result = submit(api, plan, background=False)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["execution_lease_recovery_required"] is True
    assert result["result"]["status"] == "SUCCEEDED"
    assert engine.calls == 1 and domain.busy() is True
    # Idempotent ACK recovery does not implicitly clear the failed lease.
    assert submit(api, plan, background=False)["operation_id"] == result["operation_id"]
    assert domain.busy() is True and engine.calls == 1
    final = api.resume(result["operation_id"], background=False)
    assert final["status"] == "SUCCEEDED" and not final["execution_lease_recovery_required"]
    assert domain.busy() is False and engine.calls == 1


@pytest.mark.parametrize("code,marker", [(3010, "boot_id"), (10, "session_id")])
def test_cancel_boundary_holds_common_domain_until_observed_change(bundle, code, marker):
    bundle["results"]["base"] = CommandResult(code, True, pid=123)
    subject = coordinator(bundle)
    result = start_auto(subject)
    assert result["status"] in {"REBOOT_REQUIRED", "RELOGIN_REQUIRED"}
    canceled = subject.cancel("operation-1")
    assert canceled["status"] == "CANCELED" and subject.execution_domain.busy() is True
    # A canceled receipt cannot hide the still unobserved host boundary.
    assert subject.recheck("operation-1")["status"] == "CANCELED"
    assert subject.execution_domain.busy() is True
    bundle["markers"][marker] = "changed-and-observed"
    assert subject.resume("operation-1")["status"] == "CANCELED"
    assert subject.execution_domain.busy() is False
    assert bundle["calls"] == ["base"]
