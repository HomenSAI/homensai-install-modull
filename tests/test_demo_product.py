"""Проверки синтетического продукта и локальных процессов, без внешней сети."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

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


def test_start_failure(tmp_path):
    assert run(tmp_path, "serve", "--fail", "start").returncode != 0
    assert not (tmp_path / "demo.sqlite3").exists()


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
