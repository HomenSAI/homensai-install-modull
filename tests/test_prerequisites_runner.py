"""Real bounded synthetic processes; no package/system installation."""
import json
import os
from pathlib import Path
import sys

import pytest

from homensai_install.errors import InstallerError
from homensai_install.prerequisite_runner import (
    CommandResult, FixedCommandAdapter, FixedCommandRunner,
    docker_desktop_user_adapter, docker_ubuntu_deb_adapter, system_markers, verify_regular_file,
)
from shared.datahome import DataHomeError


@pytest.fixture
def python_executable(private_test_python):
    if sys.platform != "linux":
        pytest.skip("POSIX synthetic subprocess acceptance; not a Windows package claim")
    return private_test_python


def test_real_exact_argv_has_no_shell_or_parent_secrets(tmp_path, python_executable, monkeypatch):
    executable, sha = python_executable
    output, shell_file = tmp_path / "output.json", tmp_path / "shell-was-used"
    script = tmp_path / "observe.py"
    script.write_text("import json, os, pathlib, sys\npathlib.Path(" + repr(str(output)) + ").write_text(json.dumps({'argv':sys.argv[1:], 'env':dict(os.environ)}))\n")
    literal = "$(touch " + str(shell_file) + "); secret-token=$UNDECLARED_PARENT_SECRET"
    argv = (str(executable), "-I", "-B", str(script), literal)
    monkeypatch.setenv("UNDECLARED_PARENT_SECRET", "must-not-be-inherited")
    monkeypatch.setenv("LD_PRELOAD", "/fake/untrusted.so")
    monkeypatch.setenv("PYTHONPATH", "/fake/untrusted-python")
    runner = FixedCommandRunner([argv], executable_hashes={str(executable): sha})
    result = runner.run(argv, timeout=5)
    assert result.returncode == 0 and result.started and result.pid > 0
    data = json.loads(output.read_text())
    assert data["argv"] == [literal] and not shell_file.exists()
    assert set(data["env"]) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
    assert "must-not-be-inherited" not in output.read_text()
    with pytest.raises(InstallerError) as caught: runner.run((*argv[:-1], "changed"))
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"


def test_timeout_returns_unknown_without_claiming_or_killing_system_installer(tmp_path, python_executable):
    executable, sha = python_executable
    marker = tmp_path / "completed-after-timeout"
    script = tmp_path / "slow.py"
    script.write_text("import pathlib,time\ntime.sleep(0.15)\npathlib.Path(" + repr(str(marker)) + ").write_text('finished')\n")
    argv = (str(executable), "-I", "-B", str(script))
    runner = FixedCommandRunner([argv], executable_hashes={str(executable): sha})
    result = runner.run(argv, timeout=0.02)
    assert result.started and result.timed_out and result.returncode is None and result.pid > 0
    assert runner._children[0].wait(timeout=5) == 0
    assert marker.read_text() == "finished"


def test_changed_executable_pin_blocks_process_launch(tmp_path, python_executable, monkeypatch):
    executable, _ = python_executable
    def forbidden(*args, **kwargs):
        pytest.fail("bad executable pin must fail before Popen")
    monkeypatch.setattr("homensai_install.prerequisite_runner.subprocess.Popen", forbidden)
    argv = (str(executable), "--version")
    runner = FixedCommandRunner([argv], executable_hashes={str(executable): "0" * 64})
    with pytest.raises(InstallerError) as caught: runner.run(argv)
    assert caught.value.code == "INTEGRITY_FAILED"


@pytest.mark.parametrize("unsafe", ["writable", "hardlink", "symlink"])
def test_unsafe_executable_is_rejected_before_use(tmp_path, private_test_python, unsafe):
    if sys.platform != "linux":
        pytest.skip("POSIX file safety checks")
    path, digest = private_test_python
    if unsafe == "writable":
        path.chmod(0o777)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "second-link")
    else:
        link = tmp_path / "python-link"
        link.symlink_to(path)
        path = link
    with pytest.raises((InstallerError, DataHomeError)):
        verify_regular_file(str(path), digest)


@pytest.mark.parametrize("executable", ["python", "/bin/sh", "/bin/bash", "/fake/cmd.exe", "/fake/powershell.exe", "/fake/install.cmd"])
def test_shell_or_path_lookup_cannot_be_allowlisted(executable):
    with pytest.raises(ValueError): FixedCommandRunner([(executable, "argument")], executable_hashes={executable: "a" * 64})


def test_owner_environment_is_explicit_and_does_not_accept_loader_or_credentials(python_executable, tmp_path):
    executable, sha = python_executable
    argv = (str(executable), "--version")
    for environment in ({"PYTHONPATH": "/untrusted"}, {"LD_PRELOAD": "/untrusted"}, {"TOKEN": "secret"}, {"HOME": "bad\nvalue"}):
        with pytest.raises(ValueError): FixedCommandRunner([argv], executable_hashes={str(executable): sha}, environment=environment)
    runner = FixedCommandRunner([argv], executable_hashes={str(executable): sha}, environment={"HOME": str(tmp_path)})
    assert runner.environment["HOME"] == str(tmp_path)


def test_elevation_refusal_precedes_popen(python_executable, monkeypatch):
    executable, sha = python_executable
    argv = (str(executable), "--version")
    monkeypatch.setattr("homensai_install.prerequisite_runner._admin", lambda: False)
    runner = FixedCommandRunner([argv], executable_hashes={str(executable): sha})
    with pytest.raises(InstallerError) as caught: runner.run(argv, elevated=True)
    assert caught.value.code == "PERMISSION_REQUIRED"


def test_adapter_requires_whole_pinned_artifacts_and_disjoint_exit_codes(python_executable):
    executable, sha = python_executable
    kwargs = dict(os_name="linux", artifact_suffixes=(".py",), executable_sha256=sha)
    for args in ((str(executable), "--payload={artifact_0}"), (str(executable), "{artifact_1}"),
                 (str(executable), "{artifact_0}", "{artifact_0}"), (str(executable), "--version")):
        with pytest.raises(ValueError): FixedCommandAdapter("fixed", argv=args, **kwargs)
    with pytest.raises(ValueError): FixedCommandAdapter("fixed", argv=(str(executable), "{artifact_0}"), reboot_codes=(0,), **kwargs)
    adapter = FixedCommandAdapter("fixed", argv=(str(executable), "{artifact_0}"), reboot_codes=(3010,), **kwargs)
    assert adapter.outcome(CommandResult(3010, True)) == "REBOOT_REQUIRED"
    assert adapter.outcome(CommandResult(None, False)) == "NOT_STARTED"
    assert adapter.outcome(CommandResult(None, True, timed_out=True)) == "UNKNOWN"


def test_known_docker_factories_require_real_platform_preflight_and_publisher(python_executable):
    executable, sha = python_executable
    with pytest.raises(ValueError): docker_desktop_user_adapter(environment={}, publisher_check=None, preflight=lambda e, i: True)
    windows = docker_desktop_user_adapter(environment={}, publisher_check=lambda p, s: True, preflight=lambda e, i: True)
    assert windows.argv == ("{artifact_0}", "install", "--user", "--quiet", "--accept-license", "--backend=wsl-2", "--no-windows-containers")
    assert not windows.elevated and windows.artifact_suffixes == (".exe",)
    with pytest.raises(ValueError): docker_ubuntu_deb_adapter(dpkg_executable=executable, dpkg_sha256=sha, preflight=None)
    linux = docker_ubuntu_deb_adapter(dpkg_executable=executable, dpkg_sha256=sha, preflight=lambda e, i: True)
    assert linux.elevated and linux.argv[1] == "--install" and linux.artifact_suffixes == (".deb",) * 5
    assert "sudo" not in linux.argv and "apt" not in linux.argv


def test_marker_provider_does_not_turn_controller_pid_into_a_login(monkeypatch):
    monkeypatch.setattr("homensai_install.prerequisite_runner.platform.system", lambda: "Windows")
    assert system_markers() == {"boot_id": None, "session_id": None}
