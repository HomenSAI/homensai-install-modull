"""Typed runner contracts и настоящие child processes, без live user manager."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

if sys.platform != "linux":
    pytest.skip("Linux systemd runner source tests; not Windows service acceptance", allow_module_level=True)

from homensai_install.errors import InstallerError
from homensai_install.service_systemd import SystemdUserRunner, check_environment, bus_value


def isolated_runner():
    # Bootstrap bypass только внутри fixture: production constructor требует
    # уже работающий непривилегированный user bus и trusted system binaries.
    runner = SystemdUserRunner.__new__(SystemdUserRunner)
    runner.uid = os.getuid()
    runner.home, runner.user, runner.shell = "/nonexistent", "fixture", "/bin/sh"
    runner._endpoint = lambda: None
    binary = Path(sys.executable).resolve()
    runner._binary = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    runner._tools = {"test-child": (binary, runner._binary(binary))}
    runner._environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}
    return runner


def test_bounded_client_timeout_does_not_leave_own_child(tmp_path):
    runner = isolated_runner()
    pidfile = tmp_path / "client.pid"
    code = "import os,time; from pathlib import Path; Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); time.sleep(20)"
    with pytest.raises(InstallerError) as error:
        runner._run("test-child", ["-c", code], 0.3)
    assert error.value.context["details"]["reason"] == "manager_response_timeout"
    pid = int(pidfile.read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_bounded_client_output_and_private_environment(tmp_path):
    runner = isolated_runner()
    with pytest.raises(InstallerError) as error:
        runner._run("test-child", ["-c", "import sys; sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024 + 1))"], 5)
    assert error.value.context["details"]["reason"] == "manager_response_too_large"
    raw = runner._run("test-child", ["-c", "import os,json; print(json.dumps(dict(os.environ)))"], 5)
    actual = json.loads(raw)
    assert set(actual) <= set(runner._environment) | {"LC_CTYPE"}


def test_fixed_manager_action_rejects_arbitrary_units_methods_and_shell():
    runner = isolated_runner()
    calls = []
    runner._run = lambda *args: calls.append(args)
    with pytest.raises(InstallerError):
        runner.command("kill", "hsai" + "1" * 48 + ".service", timeout=10)
    with pytest.raises(InstallerError):
        runner.command("start", "--host=foreign", timeout=10)
    with pytest.raises(InstallerError):
        runner.command("start", "x.service; touch /tmp/anything", timeout=10)
    assert not calls
    name = "hsai" + "1" * 48 + ".service"
    runner.command("stop", name, timeout=10)
    assert calls == [("systemctl", ["--user", "--no-pager", "--no-ask-password", "--job-mode=fail", "stop", name], 10)]


def test_typed_property_batch_preserves_argument_boundaries():
    runner = isolated_runner()
    runner._socket = Path("/run/user/12345/bus")
    fields = {"Id": "s", "DropInPaths": "as", "NeedDaemonReload": "b"}
    values = ["value with spaces", ["/one path/override.conf"], False]
    captured = []
    def run(tool, args, timeout):
        captured.append((tool, args, timeout))
        return b"\n".join(json.dumps({"type": kind, "data": value}).encode() for kind, value in zip(fields.values(), values))
    runner._run = run
    result = runner._properties("/org/freedesktop/systemd1/unit/example", "Unit", fields)
    assert result == dict(zip(fields, values))
    assert captured[0][1][-3:] == list(fields)
    assert captured[0][1][0] == "--address=unix:path=/run/user/12345/bus"


@pytest.mark.skipif(os.readlink("/proc/self") != str(os.getpid()), reason="PID/proc namespaces differ; rerun outside isolated PID sandbox")
def test_kernel_process_identity_is_verified_against_real_child(tmp_path):
    runner = isolated_runner()
    executable = str(Path(sys.executable).resolve())
    command = [executable, "-c", "import sys; sys.stdin.read()"]
    expected = {"executable": executable, "working_directory": str(tmp_path), "start_argv": command,
                "environment": {"PYTHONDONTWRITEBYTECODE": "1", "HOMENSAI_INSTALLATION_ID": "a" * 32}}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=tmp_path, env=expected["environment"])
    try:
        # Popen returns only after successful exec; proc identity is available.
        group = next(line[3:] for line in Path("/proc", str(process.pid), "cgroup").read_text().splitlines() if line.startswith("0::"))
        snapshot = {"MainPID": process.pid, "ControlGroup": group}
        runner.verify_process(snapshot, expected)
        with pytest.raises(InstallerError) as error:
            runner.verify_process(snapshot, {**expected, "executable": "/foreign/python"})
        assert error.value.context["details"]["reason"] == "service_process_identity_mismatch"
    finally:
        process.stdin.close()
        process.wait(timeout=5)


def test_environment_filter_never_accepts_extra_secrets_or_shell_hooks():
    for assignment in ["TOKEN=not-logged", "BASH_ENV=/foreign", "HOME=/different"]:
        with pytest.raises(InstallerError):
            check_environment([assignment], uid=1000, home="/home/fixture", user="fixture")
    check_environment(["PATH=/untrusted", "LD_PRELOAD=/untrusted", "LANG=C.UTF-8"], uid=1000)
    # Unit overrides PATH and unsets LD_PRELOAD; observed child may not retain it.
    with pytest.raises(InstallerError):
        check_environment(["LD_PRELOAD=/untrusted"], uid=1000, process_pid=123)


@pytest.mark.skipif(os.readlink("/proc/self") != str(os.getpid()) or not shutil.which("dbus-daemon") or not Path("/usr/bin/busctl").exists(),
                    reason="private bus regression needs matching namespaces and system busctl/dbus-daemon")
def test_real_busctl_property_and_method_have_distinct_json_shapes(tmp_path):
    # Это отдельный синтетический bus. User/system bus и systemd не трогаем.
    daemon = subprocess.Popen([shutil.which("dbus-daemon"), "--session", "--nofork", "--print-address=1",
        "--address=unix:path=" + str(tmp_path / "bus")], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        address = daemon.stdout.readline().decode().strip()
        common = ["/usr/bin/busctl", "--address=" + address, "--json=short", "--no-pager"]
        property_output = subprocess.run([*common, "get-property", "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "Features", "Interfaces"], capture_output=True, check=True, timeout=5).stdout
        lines = property_output.splitlines()
        assert len(lines) == 2
        assert all(type(bus_value(line, "as", property_value=True)) is list for line in lines)
        method_output = subprocess.run([*common, "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "ListNames"], capture_output=True, check=True, timeout=5).stdout
        assert "org.freedesktop.DBus" in bus_value(method_output, "as")
        assert json.loads(method_output)["data"] != bus_value(method_output, "as")
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)
        daemon.stdout.close()
