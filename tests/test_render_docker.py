"""Проверка текста шаблонов без Docker, служб, сети и выполнения миграций."""

from copy import deepcopy
import json
from pathlib import Path
import urllib.request

import pytest

from shared.manifest import ManifestError, load_manifest
from shared.render_docker import main, render_docker


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "jobpilot.manifest.json"


@pytest.fixture
def manifest():
    return load_manifest(EXAMPLE)


def health_argv(text):
    return json.loads(next(line.strip()[4:] for line in text.splitlines() if line.strip().startswith("CMD [")))


def test_docker_security_and_healthcheck(manifest):
    result = render_docker(manifest)
    dockerfile = result["Dockerfile"]
    compose = result["compose.yml"]
    assert set(result) == {"Dockerfile", "compose.yml"}
    assert f"FROM {manifest['docker']['base_image']}\n" in dockerfile
    assert "COPY --chown=0:0 payload/ /app/" in dockerfile
    assert "chown 10001:10001 /data && chmod 0700 /data" in dockerfile
    assert dockerfile.rfind("USER 10001:10001") > dockerfile.rfind("USER 0:0")
    assert 'ENTRYPOINT []\nCMD ["python", "-m", "backend.main"]' in dockerfile
    assert dockerfile.count("VOLUME ") == 1
    assert 'VOLUME ["/data"]' in dockerfile
    assert "JOBPILOT_DATA_HOME=/data" in dockerfile
    for required in ['user: "10001:10001"', "read_only: true", "restart: unless-stopped",
                     "cap_drop: [ALL]", "no-new-privileges:true", '"127.0.0.1:8765:8765"',
                     'JOBPILOT_DATA_HOME: "/data"', "- data:/data", "data: {}"]:
        assert required in compose
    assert "0.0.0.0" not in compose
    assert "privileged:" not in compose
    assert "tmpfs:" not in compose
    assert compose.count("- data:/data") == 1
    assert "interval: 30s" in compose and "start_period: 60s" in compose
    health = health_argv(dockerfile)
    compose_health = json.loads(next(line.split("test: ", 1)[1] for line in compose.splitlines() if "test: " in line))
    assert compose_health == ["CMD", *health]
    assert manifest["healthcheck_url"] in health[2]


def test_second_product_has_no_jobpilot_constants(manifest):
    manifest.update(product_id="homensai-rent", product_name="HomenS.AI Rent", port=9001,
                    data_home_env="RENT_DATA_HOME", healthcheck_url="http://127.0.0.1:9001/healthz",
                    migration_command=["python", "-m", "rent.migrate"])
    manifest["docker"]["start_command"] = ["python", "-m", "rent.main"]
    text = "\n".join(render_docker(manifest).values())
    for value in ("homensai-rent", "RENT_DATA_HOME", "127.0.0.1:9001:9001", "rent.main", "/healthz"):
        assert value in text
    for value in ("JOBPILOT", "backend.main", "8765", "${"):
        assert value not in text


def test_literal_argv_no_shell_or_migration(manifest, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Команды не должны выполняться")

    monkeypatch.setattr("os.system", forbidden)
    manifest["docker"]["start_command"] = ["python", "script with spaces.py", 'x"y', "$HOME", "$(echo x)", "кириллица"]
    manifest["migration_command"] = ["UNIQUE_MIGRATION_SENTINEL", "never-execute"]
    original = deepcopy(manifest)
    text = render_docker(manifest)["Dockerfile"]
    command_line = text.splitlines()[-1]
    assert json.loads(command_line.removeprefix("CMD ")) == manifest["docker"]["start_command"]
    assert "UNIQUE_MIGRATION_SENTINEL" not in text
    assert manifest == original
    assert render_docker(manifest) == render_docker(original)


def test_invalid_manifest_is_not_rendered(manifest):
    manifest["archive_before_replace"] = False
    with pytest.raises(ManifestError):
        render_docker(manifest)


@pytest.mark.parametrize("status,expected", [(200, 0), (204, 1), (301, 1), (503, 1)])
def test_health_command_checks_status_without_network(manifest, monkeypatch, status, expected):
    observed = {}

    class Response:
        def __init__(self):
            self.status = status

        def close(self):
            observed["closed"] = True

    class Opener:
        def open(self, url, timeout):
            observed.update(url=url, timeout=timeout)
            return Response()

    def build_opener(proxy, redirects):
        assert proxy.proxies == {}
        assert redirects.redirect_request(None, None, 302, "", {}, "http://elsewhere") is None
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    code = health_argv(render_docker(manifest)["Dockerfile"])[2]
    with pytest.raises(SystemExit) as error:
        exec(compile(code, "<rendered-healthcheck>", "exec"), {})
    assert error.value.code == expected
    assert observed == {"url": manifest["healthcheck_url"], "timeout": 3, "closed": True}


def test_health_command_propagates_connection_failure(manifest, monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise OSError("Synthetic connection failure")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(OSError):
        exec(health_argv(render_docker(manifest)["Dockerfile"])[2], {})


def test_cli_writes_only_requested_files_and_refuses_overwrite(tmp_path, manifest):
    output = tmp_path / "результат с пробелами"
    args = [str(EXAMPLE), "--output-dir", str(output)]
    assert main(args) == 0
    assert {path.name for path in output.iterdir()} == {"Dockerfile", "compose.yml"}
    expected = render_docker(manifest)
    for name, content in expected.items():
        assert (output / name).read_bytes() == content.encode("utf-8")
    assert main(args) == 2
    assert (output / "Dockerfile").read_text(encoding="utf-8") == expected["Dockerfile"]


def test_invalid_cli_does_not_create_output(tmp_path):
    manifest = tmp_path / "bad.json"
    manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "absent"
    assert main([str(manifest), "--output-dir", str(output)]) == 2
    assert not output.exists()


def test_cli_preserves_partial_existing_result(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "compose.yml").write_text("existing-content", encoding="utf-8")
    assert main([str(EXAMPLE), "--output-dir", str(output)]) == 2
    assert not (output / "Dockerfile").exists()
    assert (output / "compose.yml").read_text(encoding="utf-8") == "existing-content"
