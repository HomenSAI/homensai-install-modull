"""Контракт Rent: только фиктивные данные, tmp_path и локальная проверка."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from shared.manifest import ManifestError, load_manifest, main, validate_manifest
from shared.render_docker import render_docker


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rent.manifest.json"
V2_FIELDS = (
    "runtime", "paths", "components", "web", "secrets", "modes",
    "mutual_exclusion", "migrations", "backup", "restore", "update", "hooks",
)


@pytest.fixture
def manifest():
    return load_manifest(EXAMPLE)


def test_example_copy_roundtrip_and_cli(tmp_path, manifest, capsys):
    original = deepcopy(manifest)
    result = validate_manifest(manifest)
    result["components"][0]["cmd"].append("--changed")
    result["runtime"]["lock_files"]["windows"]["file"] = "changed.lock"
    assert manifest == original
    path = tmp_path / "rent.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert load_manifest(path) == manifest
    assert main([str(path)]) == 0
    assert "homensai-rent-demo" in capsys.readouterr().out


@pytest.mark.parametrize("field", V2_FIELDS)
def test_required_v2_sections(manifest, field):
    del manifest[field]
    with pytest.raises(ManifestError, match="обязательные"):
        validate_manifest(manifest)


@pytest.mark.parametrize("version", [0, 3, True, 2.0, "2"])
def test_invalid_version(manifest, version):
    manifest["schema_version"] = version
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_v1_remains_valid_but_version_change_requires_migration(manifest):
    legacy = load_manifest(EXAMPLE.with_name("jobpilot.manifest.json"))
    assert legacy["schema_version"] == 1
    for field in V2_FIELDS:
        extended = deepcopy(legacy)
        extended[field] = manifest[field]
        with pytest.raises(ManifestError):
            validate_manifest(extended)
    legacy["schema_version"] = 2
    with pytest.raises(ManifestError):
        validate_manifest(legacy)
    manifest["port"] = 8820
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "127.0.0.2", "127.0.0.1\n", "https://127.0.0.1"])
def test_only_ipv4_loopback(manifest, host):
    manifest["web"]["host"] = host
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("field,value", [
    ("port", 1023), ("port", 65536), ("port", True), ("port", 8820.0),
    ("healthz", "http://example.invalid/"), ("healthz", "/healthz?token=x"),
    ("healthz", "/healthz#x"), ("healthz", "/healthz\n"),
])
def test_web_fields(manifest, field, value):
    manifest["web"][field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("directory", ["data", "backup", "services", "config", "runtime"])
@pytest.mark.parametrize("parent", ["app", "releases"])
@pytest.mark.parametrize("suffix", ["", "/nested", "/./nested", "\\nested"])
def test_persistent_dirs_outside_code(manifest, directory, parent, suffix):
    manifest["paths"][directory] = manifest["paths"][parent] + suffix
    with pytest.raises(ManifestError, match="постоянный каталог"):
        validate_manifest(manifest)


@pytest.mark.parametrize("layout", ["relative", "posix", "windows"])
def test_paths_are_lexical_and_sibling_prefix_is_allowed(manifest, layout):
    roots = {"relative": "demo/", "posix": "/fictitious-demo/", "windows": "Z:\\fictitious-demo\\"}
    manifest["paths"] = {key: roots[layout] + key for key in manifest["paths"]}
    manifest["paths"]["data"] = roots[layout] + "app-data"
    assert validate_manifest(manifest) == manifest


def test_windows_case_and_slashes_cannot_bypass_containment(manifest):
    manifest["paths"] = {key: "Z:/DEMO/" + key for key in manifest["paths"]}
    manifest["paths"]["data"] = "z:\\demo\\APP\\data"
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_windows_persistent_directories_on_another_drive_are_outside_code(manifest):
    manifest["paths"] = {key: "Z:/DEMO/" + key for key in manifest["paths"]}
    for key in ("data", "backup", "services"):
        manifest["paths"][key] = "Y:/DEMO/app/" + key
    assert validate_manifest(manifest) == manifest


def test_relative_case_cannot_bypass_containment_on_windows(manifest):
    manifest["paths"]["data"] = "DEMO-LAYOUT/APP/data"
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("path", ["demo/app/../data", "demo/app./data", "demo/app /data", "Z:relative", "//host/share", "\\demo\\data", "demo/data:stream", "demo\x00data", "/other-root/data"])
def test_ambiguous_or_mixed_paths_rejected(manifest, path):
    manifest["paths"]["data"] = path
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("dependencies", [["missing"], ["web"], ["bot"], ["scheduler"], ["bot", "bot"]])
def test_invalid_component_dependencies(manifest, dependencies):
    manifest["components"][0]["after"] = dependencies
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_dependency_order_is_not_array_order(manifest):
    manifest["components"].reverse()
    assert validate_manifest(manifest) == manifest


@pytest.mark.parametrize("field,value", [
    ("name", "web"), ("cmd", "python web.py"), ("cmd", []),
    ("cmd", ["python", "\n"]), ("after", "web"),
    ("heartbeat", {"file": "h", "interval": 60, "timeout": 59}),
    ("heartbeat", {"file": "../heartbeat", "interval": 60, "timeout": 180}),
    ("restart", {"delay": 601, "max": 3, "window": 600}),
    ("restart", {"delay": -1, "max": 3, "window": 600}),
    ("restart", {"delay": 60, "max": 0, "window": 600}),
    ("restart", {"delay": True, "max": 3, "window": 600}),
])
def test_component_contract(manifest, field, value):
    manifest["components"][1][field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("field", ["value", "default", "secret", "password", "token", "api_key", "private_key", "env", "values", "secret_value"])
@pytest.mark.parametrize("section", ["root", "secret", "component", "runtime", "web", "update", "hooks"])
def test_value_and_secret_looking_fields_rejected_without_echo(manifest, section, field):
    targets = {"root": manifest, "secret": manifest["secrets"][0], "component": manifest["components"][0],
               "runtime": manifest["runtime"], "web": manifest["web"], "update": manifest["update"], "hooks": manifest["hooks"]}
    targets[section][field] = "FICTIONAL-SENTINEL-DO-NOT-ECHO"
    with pytest.raises(ManifestError) as error:
        validate_manifest(manifest)
    assert "FICTIONAL-SENTINEL-DO-NOT-ECHO" not in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("name", "BACKUP_PASSWORD=fake"), ("name", "not a name"),
    ("required", "true"), ("contexts", ["other"]), ("contexts", ["service", "service"]),
    ("contexts", ["service"]), ("same_value", 1),
])
def test_secret_metadata(manifest, field, value):
    manifest["secrets"][0][field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_duplicate_secret_names(manifest):
    manifest["secrets"].append(deepcopy(manifest["secrets"][0]))
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_no_secrets_is_valid(manifest):
    manifest["secrets"] = []
    assert validate_manifest(manifest) == manifest


@pytest.mark.parametrize("value", [[], [["autonomous"]], [["autonomous", "missing"]], [["autonomous", "autonomous"]], [["autonomous", "platform"], ["platform", "autonomous"]]])
def test_exclusion_references_and_uniqueness(manifest, value):
    manifest["mutual_exclusion"] = value
    if not value:
        assert validate_manifest(manifest) == manifest
    else:
        with pytest.raises(ManifestError):
            validate_manifest(manifest)


@pytest.mark.parametrize("change", ["duplicate-mode", "unknown-component", "missing-dependency", "uncovered"])
def test_mode_membership(manifest, change):
    if change == "duplicate-mode":
        manifest["modes"][1]["name"] = "autonomous"
    elif change == "unknown-component":
        manifest["modes"][0]["components"].append("missing")
    elif change == "missing-dependency":
        manifest["modes"][0]["components"].remove("web")
    else:
        manifest["modes"][0]["components"].remove("scheduler")
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("section,field", [
    ("runtime", "offline"), ("migrations", "backup_before_each"), ("backup", "encrypted"),
    ("update", "staged_releases"), ("update", "backup_before_rollback"),
    ("update", "retain_previous_release"), ("update", "retain_runtime"),
])
@pytest.mark.parametrize("value", [False, 1, "true"])
def test_mandatory_safety_declarations(manifest, section, field, value):
    manifest[section][field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("value", ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 64 + "\n"])
@pytest.mark.parametrize("artifact", ["windows", "linux", "wheel"])
def test_artifacts_require_sha256(manifest, artifact, value):
    target = manifest["runtime"]["vendor_wheels"][0] if artifact == "wheel" else manifest["runtime"]["lock_files"][artifact]
    target["sha256"] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_missing_artifact_hash_and_os_lock(manifest):
    for artifact in [*manifest["runtime"]["lock_files"].values(), *manifest["runtime"]["vendor_wheels"]]:
        digest = artifact.pop("sha256")
        with pytest.raises(ManifestError):
            validate_manifest(manifest)
        artifact["sha256"] = digest
    del manifest["runtime"]["lock_files"]["linux"]
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("field,value", [("vendor_wheels", []), ("python", "3.13")])
def test_offline_runtime_requirements(manifest, field, value):
    manifest["runtime"][field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("file", ["../demo.whl", "/demo.whl", "Z:/demo.whl", "https://example.invalid/demo.whl", "vendor/demo.tar.gz", ".", "vendor/"])
def test_local_relative_wheel_files(manifest, file):
    manifest["runtime"]["vendor_wheels"][0]["file"] = file
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_artifact_paths_unique(manifest):
    manifest["runtime"]["vendor_wheels"].append(deepcopy(manifest["runtime"]["vendor_wheels"][0]))
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("field,value", [("pointer_file", "../active.json"), ("pointer_file", "update.json"), ("pointer_file", "active.json/."), ("state_file", "/update.json"), ("health_wait", 0), ("health_wait", True), ("health_wait", 601)])
def test_update_fields(manifest, field, value):
    manifest["update"][field] = value
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


@pytest.mark.parametrize("hook", ["pre_start", "health_checks", "post_update"])
def test_hooks_require_argv_lists(manifest, hook):
    manifest["hooks"][hook] = ["python -m demo"]
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_validation_does_not_execute_commands_or_inspect_layout(manifest, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Исполнение/доступ к каталогам продукта запрещены")

    monkeypatch.setattr("os.system", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    assert validate_manifest(manifest) == manifest


def test_v2_is_not_silently_rendered_as_single_process_v1(manifest):
    with pytest.raises(ManifestError, match="только v1"):
        render_docker(manifest)
