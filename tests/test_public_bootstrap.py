"""Публичный bootstrap сохраняет trust boundary и не создаёт состояние."""

import base64
import json
import ssl  # Define SSLSocket before conftest replaces socket.socket for offline tests.

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError


def test_bootstrap_reads_pinned_keys_without_product_or_journal_effects(tmp_path):
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"schema_version": 1, "keys": {
        "fixture-key": base64.b64encode(bytes(range(32))).decode()}}))
    api = InstallerAPI.from_bootstrap(tmp_path / "state", trust_config=trust)
    assert api.describe()["backends"]["bootstrap_sdk"].endswith("InstallerAPI.from_bootstrap")
    assert not api.state_dir.exists()
    with pytest.raises(InstallerError) as caught:
        InstallerAPI.from_bootstrap(tmp_path / "state", trust_config=tmp_path / "missing")
    assert caught.value.code == "INTEGRITY_FAILED"
    assert not api.state_dir.exists()


@pytest.mark.parametrize("options,code", [
    ({"development_runtime": "/untrusted/python"}, "CONSENT_REQUIRED"),
    ({"allow_development": "yes"}, "INVALID_REQUEST"),
    ({"docker_cli_config": "/missing/docker-bootstrap.json"}, "INVALID_REQUEST"),
])
def test_bootstrap_failures_use_public_errors_before_state(tmp_path, options, code):
    with pytest.raises(InstallerError) as caught:
        InstallerAPI.from_bootstrap(tmp_path / "state", **options)
    assert caught.value.code == code
    assert not (tmp_path / "state").exists()
