"""Контракт и отказ при неверных/неоднозначных данных."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from shared.manifest import ManifestError, load_manifest, main, validate_manifest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "jobpilot.manifest.json"


@pytest.fixture
def manifest():
    return load_manifest(EXAMPLE)


def test_example_and_independent_copy(manifest):
    original = deepcopy(manifest)
    result = validate_manifest(manifest)
    result["docker"]["start_command"].append("--changed")
    assert manifest == original
    assert manifest["archive_before_replace"] is True


@pytest.mark.parametrize("field", ["schema_version", "product_name", "product_id", "port", "data_home_env", "migration_command", "healthcheck_url", "archive_before_replace", "docker"])
def test_required_fields(manifest, field):
    del manifest[field]
    with pytest.raises(ManifestError, match="обязательные"):
        validate_manifest(manifest)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("schema_version", 1.0),
    ("product_name", ""), ("product_name", " name "), ("product_name", "a\nb"),
    ("product_name", "a" * 129), ("product_name", "a\x7fb"),
    ("product_id", "../escape"), ("product_id", "UPPER"), ("product_id", "a\n"),
    ("product_id", "a" * 64), ("product_id", "x${HOME}"),
    ("port", True), ("port", "8765"), ("port", 8765.0), ("port", 0),
    ("port", 1023), ("port", 65536),
    ("data_home_env", "A=B"), ("data_home_env", "data_home"),
    ("data_home_env", "HOME"), ("data_home_env", "PYTHONDONTWRITEBYTECODE"),
    ("migration_command", "python -m migrate"), ("migration_command", []),
    ("migration_command", ["python", ""]), ("migration_command", ["python", 1]),
    ("migration_command", ["python", "x\nRUN touch /oops"]),
    ("migration_command", ["a"] * 65), ("migration_command", ["a" * 4097]),
    ("healthcheck_url", "http://example.com:8765/health"),
    ("healthcheck_url", "http://localhost:8765/health"),
    ("healthcheck_url", "http://127.0.0.1:8766/health"),
    ("healthcheck_url", "http://127.0.0.1:08765/health"),
    ("healthcheck_url", "http://user:pass@127.0.0.1:8765/health"),
    ("healthcheck_url", "http://127.0.0.1:8765/health?secret=x"),
    ("healthcheck_url", "http://127.0.0.1:8765/health#fragment"),
    ("healthcheck_url", "http://127.0.0.1:8765/health\n"),
    ("archive_before_replace", False), ("archive_before_replace", 1),
])
def test_invalid_fields(manifest, field, value):
    manifest[field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("value", [None, [], "manifest", 1, True])
def test_reject_non_object(value):
    with pytest.raises(ManifestError):
        validate_manifest(value)


@pytest.mark.parametrize("port", [1024, 65535])
def test_port_boundaries(manifest, port):
    manifest.update(port=port, healthcheck_url=f"http://127.0.0.1:{port}/health")
    assert validate_manifest(manifest)["port"] == port


@pytest.mark.parametrize("target", ["root", "docker"])
def test_unknown_fields(manifest, target):
    (manifest if target == "root" else manifest["docker"])["unexpected"] = "not displayed"
    with pytest.raises(ManifestError, match="неизвестные") as error:
        validate_manifest(manifest)
    assert "not displayed" not in str(error.value)


@pytest.mark.parametrize("field", ["base_image", "start_command"])
def test_required_docker_fields(manifest, field):
    del manifest["docker"][field]
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("field,value", [
    ("base_image", "python:latest"), ("base_image", "x@sha256:abc"),
    ("base_image", "python:5000:tag@sha256:" + "a" * 64),
    ("base_image", "python\nRUN echo bad@sha256:" + "a" * 64),
    ("start_command", "python app.py"), ("start_command", []),
    ("start_command", ["python", "x\x00y"]),
])
def test_invalid_docker_fields(manifest, field, value):
    manifest["docker"][field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("image", ["python:3.14-slim", "ghcr.io/homensai/runtime:v1", "localhost:5000/team/runtime"])
def test_digest_image_references(manifest, image):
    manifest["docker"]["base_image"] = image + "@sha256:" + "a" * 64
    assert validate_manifest(manifest)["docker"]["base_image"].startswith(image)


@pytest.mark.parametrize("body", ["{", '{"port":1,"port":2}', '{"docker":{"x":1,"x":2}}', '{"port":NaN}', '{"port":Infinity}', "[1,2]", '{"password":"do-not-echo"}'])
def test_bad_json(tmp_path, body):
    path = tmp_path / "manifest.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ManifestError) as error:
        load_manifest(path)
    assert "do-not-echo" not in str(error.value)


def test_load_bom_and_unicode(tmp_path, manifest):
    manifest["product_name"] = "Продукт HomenS.AI"
    path = tmp_path / "манифест.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8-sig")
    assert load_manifest(path) == manifest


def test_missing_and_invalid_encoding(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "missing.json")
    path = tmp_path / "invalid.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_cli_validation(tmp_path, capsys):
    assert main([str(EXAMPLE)]) == 0
    assert "homensai-jobpilot-pro" in capsys.readouterr().out
    assert main([str(tmp_path / "missing")]) == 2


def test_schema_contract_is_local():
    schema = json.loads((EXAMPLE.parents[1] / "shared" / "manifest.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["archive_before_replace"]["const"] is True
    assert schema["properties"]["migration_command"]["$ref"] == "#/$defs/command"
