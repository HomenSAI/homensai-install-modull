"""Trusted registration cannot be replaced by an untrusted request field."""

import base64
from copy import deepcopy
import json

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from homensai_install.trust import canonical_bytes
from test_prerequisites_execution import bundle, coordinator, resign
from test_public_api import Engine, submit


def test_api_inherits_coordinator_domain_and_rejects_conflicting_registration(bundle):
    from homensai_install.execution_lease import ExecutionDomain

    root = bundle["tmp_path"]
    port = coordinator(bundle)
    api = InstallerAPI(root / "api-one", prerequisites=port)
    assert api.execution_domain is port.execution_domain
    same = ExecutionDomain(port.execution_domain.directory, owner_id=port.owner_id)
    assert InstallerAPI(root / "api-two", prerequisites=port, execution_domain=same).execution_domain is port.execution_domain
    different = ExecutionDomain(root / "different-domain", owner_id=port.owner_id)
    with pytest.raises(InstallerError) as caught:
        InstallerAPI(root / "api-three", prerequisites=port, execution_domain=different)
    assert caught.value.code == "INVALID_REQUEST"
    wrong_owner = ExecutionDomain(root / "owner-domain", owner_id="different-owner")
    with pytest.raises(InstallerError) as caught:
        coordinator(bundle, execution_domain=wrong_owner)
    assert caught.value.code == "INVALID_REQUEST"
    assert not different.directory.exists() and not wrong_owner.directory.exists()


@pytest.mark.parametrize("field", ["execution_domain", "execution_domain_dir", "owner_id"])
def test_manifest_and_options_cannot_select_a_trusted_domain(bundle, field):
    root = bundle["tmp_path"]
    port = coordinator(bundle)
    api = InstallerAPI(root / "api-state", engine=Engine(root), prerequisites=port)
    original = api.execution_domain
    manifest = deepcopy(bundle["manifest"])
    manifest[field] = str(root / "untrusted-domain")
    with pytest.raises(InstallerError) as caught:
        api.validate(manifest)
    assert caught.value.code == "INVALID_MANIFEST"
    with pytest.raises(InstallerError) as caught:
        api.plan("install", bundle["manifest"], bundle["target"], options={field: str(root / "untrusted-domain")})
    assert caught.value.code == "INVALID_REQUEST"
    assert api.execution_domain is original
    assert not original.directory.exists() and not (root / "untrusted-domain").exists()


@pytest.mark.parametrize("change", ["catalog", "release", "mode"])
def test_default_domain_survives_catalog_release_mode_and_api_directory_change(bundle, change):
    root = bundle["tmp_path"]
    old = coordinator(bundle)
    plan = old.plan()
    waiting = old.start("old-manual", plan, consents=plan["required_consents"])
    assert waiting["status"] == "AWAITING_USER" and old.execution_domain.busy() is True
    if change == "catalog":
        bundle["descriptor"]["catalog_version"] = "2.0.0"
    elif change == "release":
        bundle["options"]["release"] = "2.0.0"
    else:
        bundle["target"]["mode"] = "docker"
        bundle["descriptor"]["target"]["mode"] = "docker"
    resign(bundle)
    if change == "release":
        from pathlib import Path
        inventory_path = Path(bundle["options"]["inventory_file"])
        inventory = json.loads(inventory_path.read_text())
        inventory["release"] = "2.0.0"
        inventory_path.write_bytes(canonical_bytes(inventory))
        signature_path = Path(bundle["options"]["signature_file"])
        signature = json.loads(signature_path.read_text())
        signature["signature"] = base64.b64encode(bundle["private"].sign(canonical_bytes(inventory))).decode()
        signature_path.write_bytes(canonical_bytes(signature))
    new = coordinator(bundle)
    assert new.execution_domain.same_domain(old.execution_domain)
    engine = Engine(root)
    api = InstallerAPI(root / "another-product-api-state", engine=engine, prerequisites=new)
    product = api.plan("install", bundle["manifest"], bundle["target"],
                       options={"install_root": str(root / "another-root")})
    with pytest.raises(InstallerError) as caught:
        submit(api, product, background=False)
    assert caught.value.code == "OPERATION_CONFLICT"
    assert engine.calls == 0 and not (root / "executed").exists()
    assert new.execution_domain.busy() is True and bundle["calls"] == []
