"""Проверки синтетического продукта и локальных процессов, без внешней сети."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from examples.demo_product import main as demo_main

DEMO = Path(__file__).resolve().parents[1] / "examples/demo_product/main.py"


def command(data, *args):
    return [sys.executable, "-B", str(DEMO), *args, "--data-dir", str(data)]


def run(data, *args):
    return subprocess.run(command(data, *args), capture_output=True, text=True, timeout=10)


def entries(data):
    with sqlite3.connect(data / "demo.sqlite3") as conn:
        return conn.execute("SELECT text FROM entries ORDER BY id").fetchall()


@pytest.mark.parametrize("fail", ["before", "during"])
def test_failed_migration_does_not_commit(tmp_path, fail):
    assert run(tmp_path, "migrate", "--fail", fail).returncode != 0
    assert entries(tmp_path) == []
    assert run(tmp_path, "migrate").returncode == 0
    assert entries(tmp_path) == [("synthetic-schema-1",)]


def test_resume_after_commit_is_idempotent(tmp_path):
    assert run(tmp_path, "migrate", "--fail", "after").returncode != 0
    assert entries(tmp_path) == [("synthetic-schema-1",)]
    assert run(tmp_path, "migrate").returncode == 0
    assert run(tmp_path, "migrate", "--migration", "schema-2", "--release", "2").returncode == 0
    assert entries(tmp_path) == [("synthetic-schema-1",), ("synthetic-schema-2",)]


def test_settings_persist_across_migrations(tmp_path):
    assert run(tmp_path, "migrate").returncode == 0
    settings = tmp_path / "settings.json"
    assert json.loads(settings.read_text(encoding="utf-8"))["display_name"] == "Synthetic User"
    settings.write_text('{"locale":"de","display_name":"Test"}\n', encoding="utf-8")
    assert run(tmp_path, "migrate", "--release", "2", "--migration", "schema-2").returncode == 0
    assert json.loads(settings.read_text(encoding="utf-8"))["locale"] == "de"


@pytest.mark.parametrize('action', ['serve', 'worker'])
def test_start_failure(tmp_path, action):
    assert run(tmp_path, action, "--fail", "start").returncode != 0
    assert not (tmp_path / "demo.sqlite3").exists()


@pytest.mark.parametrize('address', ['127.0.0.1', '0.0.0.0'])
def test_server_uses_explicit_listen_address_without_opening_a_socket(tmp_path, monkeypatch, address):
    seen = []
    class FakeServer:
        server_port = 8080
        def __init__(self, binding, handler):
            seen.append(binding)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def serve_forever(self, **kwargs):
            pass
    monkeypatch.setattr(demo_main, 'ThreadingHTTPServer', FakeServer)
    demo_main.serve(tmp_path, '1', 8080, 'none', listen=address)
    assert seen == [(address, 8080)]


def test_server_rejects_unexpected_listen_address(tmp_path):
    assert run(tmp_path, 'serve', '--listen', 'example.com').returncode != 0
    assert not (tmp_path / 'demo.sqlite3').exists()


@pytest.mark.parametrize("failure,status", [("none", 200), ("health", 503)])
def test_server_health(tmp_path, failure, status):
    # Клиент находится в отдельном процессе и подключается только к loopback.
    output = tmp_path / "server.txt"
    with output.open("w+") as log:
        process = subprocess.Popen(command(tmp_path, "serve", "--release", "2", "--fail", failure),
                                   stdout=log, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                log.seek(0)
                line = log.readline()
                if line:
                    break
                if process.poll() is not None:
                    pytest.fail("Тестовый сервер не запустился")
                time.sleep(0.05)
            else:
                pytest.fail("Тестовый сервер не сообщил порт")
            port = json.loads(line)["port"]
            script = ("import sys, urllib.request, urllib.error; "
                      "u='http://127.0.0.1:'+sys.argv[1]+'/health'; "
                      "o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
                      "r=o.open(u,timeout=3) if sys.argv[2]=='200' else None; "
                      "print(r.status, r.read().decode()) if r else None")
            if status == 200:
                result = subprocess.run([sys.executable, "-B", "-c", script, str(port), str(status)],
                                        capture_output=True, text=True, timeout=10)
                assert result.returncode == 0
                assert '"release": "2"' in result.stdout
            else:
                script = ("import sys,urllib.request,urllib.error; "
                          "o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
                          "\ntry: o.open('http://127.0.0.1:'+sys.argv[1]+'/health',timeout=3)"
                          "\nexcept urllib.error.HTTPError as e: print(e.code)")
                result = subprocess.run([sys.executable, "-B", "-c", script, str(port)],
                                        capture_output=True, text=True, timeout=10)
                assert result.returncode == 0 and result.stdout.strip() == "503"
        finally:
            process.terminate()
            process.wait(timeout=10)
            if os.name == 'posix':
                assert process.returncode == 0


def test_worker_writes_heartbeat(tmp_path):
    process = subprocess.Popen(command(tmp_path, "worker", "--release", "2"),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        heartbeat = tmp_path / "worker-heartbeat.json"
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert json.loads(heartbeat.read_text(encoding="utf-8"))["release"] == "2"
    finally:
        process.terminate()
        process.wait(timeout=10)
        if os.name == 'posix':
            assert process.returncode == 0


@pytest.mark.skipif(os.name != 'posix', reason='Сигнал SIGTERM проверяется на POSIX')
@pytest.mark.parametrize('action', ['serve', 'worker'])
def test_controlled_stop_failure_keeps_process_running(tmp_path, action):
    output = tmp_path / 'process.log'
    with output.open('w+') as log:
        process = subprocess.Popen(command(tmp_path, action, '--fail', 'stop'), stdout=log,
                                   stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                log.seek(0)
                if (log.readline() if action == 'serve' else (tmp_path / 'worker-heartbeat.json').exists()):
                    break
                if process.poll() is not None:
                    pytest.fail('Тестовый процесс завершился до готовности')
                time.sleep(0.05)
            else:
                pytest.fail('Тестовый процесс не подтвердил готовность')
            process.terminate()
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_distinct_release_payloads_enforce_release_identity(tmp_path):
    releases = tmp_path / "releases"
    builder = DEMO.with_name("build_releases.py")
    built = subprocess.run([sys.executable, "-B", str(builder), str(releases)],
                           capture_output=True, text=True, timeout=10)
    assert built.returncode == 0
    data = tmp_path / "data"
    def invoke(version, *extra):
        return subprocess.run([sys.executable, "-B", str(releases / ("app-" + version) / "main.py"),
                               "migrate", "--data-dir", str(data), *extra],
                              capture_output=True, text=True, timeout=10)
    assert invoke("1").returncode == 0
    assert invoke("1", "--migration", "schema-2").returncode != 0
    assert invoke("1", "--release", "2").returncode != 0
    assert invoke("2", "--migration", "schema-2").returncode == 0
    assert entries(data) == [("synthetic-schema-1",), ("synthetic-schema-2",)]
    assert (releases / "app-1" / "release.json").read_bytes() != (releases / "app-2" / "release.json").read_bytes()
    assert subprocess.run([sys.executable, "-B", str(builder), str(releases)],
                          capture_output=True, text=True, timeout=10).returncode != 0
