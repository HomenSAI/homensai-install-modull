"""Только фейки runner/fetcher и публичные временные файлы; Docker не вызывается."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from shared import adapters_docker as docker
from shared import ctl
from shared.datahome import DataHomeError
from shared.manifest import ManifestError, load_manifest
from shared.render_docker import render_docker


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "jobpilot.manifest.json"
SENTINEL = "SYNTHETIC-ERROR-DO-NOT-EMIT"


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Реальные процессы запрещены")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


@pytest.fixture
def manifest():
    return load_manifest(EXAMPLE)


@pytest.fixture
def compose(tmp_path, manifest):
    directory = tmp_path / "проект с пробелами"
    directory.mkdir()
    path = directory / "compose.yml"
    path.write_text(render_docker(manifest)["compose.yml"], encoding="utf-8")
    return path


@pytest.mark.parametrize("method,tail", [
    ("start", ["up", "-d"]), ("stop", ["stop"]), ("ps", ["ps"]), ("logs", ["logs"])])
def test_every_command_dry_and_executed(manifest, compose, method, tail, monkeypatch):
    calls = []
    result = subprocess.CompletedProcess([], 0, stdout="synthetic output", stderr="")
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return result
    monkeypatch.setenv("SYNTHETIC_DOCKER_SECRET", SENTINEL)
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.invalid:1234")
    monkeypatch.setenv("COMPOSE_FILE", SENTINEL)
    adapter = docker.DockerComposeAdapter(manifest, compose, runner=runner, timeout=17)
    expected = ["docker", "compose", "-p", manifest["product_id"], "-f", str(compose), *tail]
    assert getattr(adapter, method)() == expected
    assert getattr(adapter, method)(execute=False) == expected
    assert calls == []
    assert getattr(adapter, method)(execute=True) is result
    assert calls == [(expected, dict(shell=False, timeout=17, check=False,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=str(compose.parent),
        env={"COMPOSE_DISABLE_ENV_FILE": "1"}))]
    assert SENTINEL not in repr(calls)


def test_default_runner_is_subprocess_run_but_never_called_by_default(manifest, compose, monkeypatch):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, "run", runner)
    adapter = docker.DockerComposeAdapter(manifest, compose)
    for method in (adapter.start, adapter.stop, adapter.ps, adapter.logs):
        assert isinstance(method(), list)
    assert adapter.health() == {"url": manifest["healthcheck_url"], "timeout": 3}
    assert not calls
    adapter.start(execute=True)
    assert len(calls) == 1
    adapter.stop()
    assert len(calls) == 1  # Разрешение не сохраняется между вызовами.


@pytest.mark.parametrize("method", ["start", "stop", "ps", "logs"])
@pytest.mark.parametrize("failure", ["exit", "timeout", "os", "called-process", "custom"])
def test_command_failures_are_sanitized(manifest, compose, method, failure):
    def runner(argv, **kwargs):
        if failure == "exit":
            return subprocess.CompletedProcess(argv, 9, SENTINEL, SENTINEL)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(SENTINEL, 17, output=SENTINEL, stderr=SENTINEL)
        if failure == "called-process":
            raise subprocess.CalledProcessError(9, SENTINEL, output=SENTINEL)
        if failure == "custom":
            raise RuntimeError(SENTINEL)
        raise OSError(SENTINEL)
    adapter = docker.DockerComposeAdapter(manifest, compose, runner=runner)
    with pytest.raises(docker.DockerAdapterError) as error:
        getattr(adapter, method)(execute=True)
    assert SENTINEL not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("product", ["-evil", "--help", "x --help", "x;evil", "x\nevil",
    "x$(evil)", "x/evil", "x\\evil", "x\x00", "X", "a" * 64, ""])
def test_product_injection_rejected_before_runner(manifest, compose, product):
    manifest["product_id"] = product
    with pytest.raises((ManifestError, docker.DockerAdapterError)):
        docker.DockerComposeAdapter(manifest, compose, runner=lambda *a, **k: pytest.fail("runner"))


@pytest.mark.parametrize("path", ["--help", "-", "relative.yml", "../compose.yml",
    "//server/share/compose.yml", "C:compose.yml", "bad:stream.yml", "NUL.yml"])
def test_ambiguous_paths_rejected(manifest, path):
    with pytest.raises((DataHomeError, docker.DockerAdapterError)):
        docker.DockerComposeAdapter(manifest, path)


@pytest.mark.parametrize("name", ["../escape.yml", "file.yml\n", "file.yml\x00", "file.yml --help",
    "file;evil.yml", "$(evil).yml", "`evil`.yml", "x&evil.yml", "x|evil.yml",
    "%VAR%.yml", 'x"evil.yml', "x'evil.yml", "NUL.yml", "file.yml.", "file.env"])
def test_injected_or_non_compose_paths_rejected(manifest, tmp_path, name):
    with pytest.raises((DataHomeError, docker.DockerAdapterError)):
        docker.DockerComposeAdapter(manifest, str(tmp_path) + "/" + name)


def test_nonexistent_file_can_be_planned_but_not_executed(manifest, tmp_path):
    path = tmp_path / "not-created" / "compose.yml"
    adapter = docker.DockerComposeAdapter(manifest, path,
        runner=lambda *a, **k: pytest.fail("runner не должен вызываться"))
    assert adapter.start()[-2:] == ["up", "-d"]
    assert not path.parent.exists()
    with pytest.raises(docker.DockerAdapterError):
        adapter.start(execute=True)
    assert not path.parent.exists()


def test_directory_rejected(manifest, tmp_path):
    path = tmp_path / "compose.yml"
    path.mkdir()
    with pytest.raises(docker.DockerAdapterError):
        docker.DockerComposeAdapter(manifest, path)


def test_reparse_path_rechecked_before_runner(manifest, compose, monkeypatch):
    adapter = docker.DockerComposeAdapter(manifest, compose,
        runner=lambda *a, **k: pytest.fail("runner"))
    original = Path.lstat
    def lstat(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self == compose:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(DataHomeError):
        adapter.start(execute=True)


@pytest.mark.parametrize("action", ["--help", "up -d", "build", "down", "ps;evil", "logs\n", None, []])
def test_no_arbitrary_action(manifest, compose, action):
    with pytest.raises(docker.DockerAdapterError):
        docker.DockerComposeAdapter(manifest, compose).command(action, execute=True)


@pytest.mark.parametrize("value", [1, "True", None, [], {}])
def test_explicit_boolean_execution_required(manifest, compose, value):
    adapter = docker.DockerComposeAdapter(manifest, compose)
    for method in (adapter.start, adapter.stop, adapter.ps, adapter.logs, adapter.health):
        with pytest.raises(docker.DockerAdapterError):
            method(execute=value)


@pytest.mark.parametrize("field", ["timeout", "health_timeout"])
@pytest.mark.parametrize("value", [0, -1, 601, True, "3", None, float("inf"), float("nan")])
def test_invalid_timeouts(manifest, compose, field, value):
    with pytest.raises(docker.DockerAdapterError):
        docker.DockerComposeAdapter(manifest, compose, **{field: value})


@pytest.mark.parametrize("url", ["http://example.com:8765/healthz", "http://localhost:8765/healthz",
    "http://0.0.0.0:8765/healthz", "http://127.0.0.2:8765/healthz", "http://[::1]:8765/healthz",
    "https://127.0.0.1:8765/healthz", "http://127.0.0.1:9000/healthz",
    "http://user:pass@127.0.0.1:8765/healthz", "http://127.0.0.1:8765/healthz?x=1",
    "http://127.0.0.1:8765/healthz#x", "http://127.0.0.1:8765/healthz\n",
    "http://127.0.0.1:8765@evil.invalid/healthz"])
def test_health_url_injection_rejected(manifest, compose, url):
    manifest["healthcheck_url"] = url
    with pytest.raises((ManifestError, docker.DockerAdapterError)):
        docker.DockerComposeAdapter(manifest, compose)


@pytest.mark.parametrize("outcome", [200, 204, 301, 302, 503, True, "200", "timeout", "url-timeout", "error"])
def test_health_fetcher_success_failure_timeout(manifest, compose, outcome):
    calls = []
    def fetcher(url, **kwargs):
        calls.append((url, kwargs))
        if outcome == "timeout":
            raise TimeoutError(SENTINEL)
        if outcome == "url-timeout":
            raise urllib.error.URLError(TimeoutError(SENTINEL))
        if outcome == "error":
            raise OSError(SENTINEL)
        return outcome
    adapter = docker.DockerComposeAdapter(manifest, compose, fetcher=fetcher, health_timeout=2.5,
        runner=lambda *a, **k: pytest.fail("health не вызывает runner"))
    assert adapter.health() == {"url": manifest["healthcheck_url"], "timeout": 2.5}
    assert not calls
    if type(outcome) is int and outcome == 200:
        assert adapter.health(execute=True) is True
    else:
        with pytest.raises(docker.DockerAdapterError) as error:
            adapter.health(execute=True)
        assert SENTINEL not in str(error.value)
    assert calls == [(manifest["healthcheck_url"], {"timeout": 2.5})]


def test_default_fetcher_disables_proxy_redirect_and_closes_response(manifest, compose, monkeypatch):
    calls = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append("closed")
    class Opener:
        def open(self, url, timeout):
            calls.append((url, timeout))
            return Response()
    def build_opener(proxy, redirects):
        assert isinstance(proxy, urllib.request.ProxyHandler) and proxy.proxies == {}
        assert redirects.redirect_request(None, None, 302, "", {}, "http://evil.invalid") is None
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    adapter = docker.DockerComposeAdapter(manifest, compose)
    assert adapter.health(execute=True) is True
    assert calls == [(manifest["healthcheck_url"], 3), "closed"]


def test_another_rendered_product_and_manifest_unchanged(manifest, compose):
    manifest.update(product_id="another-product", port=9001,
                    healthcheck_url="http://127.0.0.1:9001/healthz")
    original = deepcopy(manifest)
    adapter = docker.DockerComposeAdapter(manifest, compose)
    assert adapter.start()[3] == "another-product"
    assert adapter.health()["url"] == manifest["healthcheck_url"]
    assert manifest == original


def test_v2_refused(tmp_path):
    manifest = load_manifest(EXAMPLE.with_name("rent.manifest.json"))
    with pytest.raises(ManifestError, match="v1"):
        docker.DockerComposeAdapter(manifest, tmp_path / "compose.yml")


@pytest.mark.parametrize("action,tail", [("up", ["up", "-d"]), ("stop", ["stop"]),
                                         ("ps", ["ps"]), ("logs", ["logs"])])
@pytest.mark.parametrize("as_json", [True, False])
def test_ctl_docker_plan_only(manifest, tmp_path, capsys, monkeypatch, action, tail, as_json):
    def forbidden(*a, **k):
        raise AssertionError("План не вызывает runner/fetcher")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(docker, "fetch_health", forbidden)
    monkeypatch.chdir(tmp_path)
    args = ["--manifest", str(EXAMPLE), "docker-plan", "--compose-file", "новый/compose.yml",
            "--action", action]
    if as_json:
        args.append("--json")
    assert ctl.main(args) == 0
    output = capsys.readouterr()
    assert not output.err
    expected = ["docker", "compose", "-p", manifest["product_id"], "-f",
                str(tmp_path / "новый" / "compose.yml"), *tail]
    if as_json:
        assert json.loads(output.out)["data"] == {"dry_run": True, "argv": expected}
    else:
        assert "исполнение не выполнялось" in output.out
        assert json.loads(output.out.split("argv: ")[1]) == expected
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("extra", [["--execute"], ["--action", "build"],
    ["--action", "up;evil"], ["--compose-file", "../escape.yml"],
    ["--compose-file", "$(evil).yml"], ["--compose-file", "config.env"]])
def test_ctl_refuses_execution_and_injection(tmp_path, capsys, extra):
    assert ctl.main(["--manifest", str(EXAMPLE), "--json", "docker-plan",
        "--compose-file", str(tmp_path / "compose.yml"), "--action", "up", *extra]) == 2
    assert json.loads(capsys.readouterr().out)["data"] == {}
    assert not list(tmp_path.iterdir())


def test_ctl_v2_refused_without_writes(tmp_path, capsys):
    assert ctl.main(["--manifest", str(EXAMPLE.with_name("rent.manifest.json")), "docker-plan",
        "--compose-file", str(tmp_path / "compose.yml"), "--action", "up", "--json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert not list(tmp_path.iterdir())
