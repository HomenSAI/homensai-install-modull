"""Контракт раннего API v1: только согласование и чистая валидация."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from shared.api import MAX_BYTES, handle

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "examples/jobpilot.manifest.json").read_text(encoding="utf-8"))


def request(operation="capabilities", params=None, **overrides):
    result = {"api_major": 1, "request_id": "test-1", "operation": operation,
              "params": {} if params is None else params}
    result.update(overrides)
    return result


def cli(data):
    return subprocess.run([sys.executable, "-B", "-m", "shared.api"], cwd=ROOT,
                          input=data, capture_output=True, timeout=15)


def test_capabilities_only_advertises_read_only_operations():
    response = handle(request())
    assert response["ok"] is True
    assert response["result"]["operations"] == ["capabilities", "validate"]
    assert response["result"]["execution_modes"] == []
    assert response["result"]["manifest_versions"] == [1, 2]


@pytest.mark.parametrize("name,product,version", [
    ("jobpilot", "homensai-jobpilot-pro", 1),
    ("rent", "homensai-rent-demo", 2),
])
def test_validate_python_and_cli_agree(name, product, version):
    manifest = json.loads((ROOT / "examples" / (name + ".manifest.json")).read_text(encoding="utf-8"))
    value = request("validate", {"manifest": manifest})
    response = handle(value)
    assert response["result"] == {"product_id": product, "schema_version": version}
    result = cli(json.dumps(value).encode())
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout) == response


@pytest.mark.parametrize("change,code", [
    ({"api_major": 2}, "unsupported_version"),
    ({"api_major": True}, "invalid_request"),
    ({"operation": "install"}, "unsupported_operation"),
    ({"operation": "../secret-value"}, "unsupported_operation"),
    ({"params": {"unexpected": "secret-value"}}, "invalid_request"),
    ({"request_id": "invalid request secret-value"}, "invalid_request"),
    ({"unknown": "secret-value"}, "invalid_request"),
])
def test_reject_unsupported_and_malformed_requests_without_values(change, code):
    response = handle(request(**change))
    assert response["ok"] is False and response["error"]["code"] == code
    assert "secret-value" not in json.dumps(response)


def test_manifest_value_is_not_echoed_in_error():
    manifest = deepcopy(MANIFEST)
    manifest["password"] = "SECRET-SENTINEL"
    response = handle(request("validate", {"manifest": manifest}))
    assert response["error"]["code"] == "invalid_request"
    assert "SECRET-SENTINEL" not in json.dumps(response)


def test_reject_cycles_and_deep_python_requests():
    cycle = []
    cycle.append(cycle)
    assert handle(request(params={"cycle": cycle}))["error"]["code"] == "invalid_request"
    nested = None
    for _ in range(80):
        nested = [nested]
    assert handle(request(params={"nested": nested}))["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("payload", [
    b'{"api_major":1,"api_major":1}', b'{"api_major":NaN}',
    b'{"unexpected":"SECRET-SENTINEL"}', b'\xff',
    b" " * (MAX_BYTES + 1),
])
def test_cli_rejects_bad_json_and_oversize_without_echo(payload):
    result = cli(payload)
    assert result.returncode == 2 and result.stderr == b""
    response = json.loads(result.stdout)
    assert response["error"]["code"] == "invalid_request"
    assert b"SECRET-SENTINEL" not in result.stdout


def test_python_input_is_not_mutated():
    original = request("validate", {"manifest": deepcopy(MANIFEST)})
    before = deepcopy(original)
    handle(original)
    assert original == before


def test_cli_rejects_arguments_without_echo():
    result = subprocess.run([sys.executable, "-B", "-m", "shared.api", "SECRET-SENTINEL"],
                            cwd=ROOT, input=b"", capture_output=True, timeout=15)
    assert result.returncode == 2 and b"SECRET-SENTINEL" not in result.stdout + result.stderr
