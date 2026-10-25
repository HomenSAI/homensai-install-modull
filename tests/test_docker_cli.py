"""Runner проверяется synthetic ELF, без Docker executable/daemon/network."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

import pytest

from homensai_install.docker_cli import DockerCLI, DockerCommandResult, MAX_OUTPUT_BYTES
from homensai_install.engine import EngineError


CID = "a" * 64
FORMAT = '{"ID":{{json .ID}}}'
FAKE_SOURCE = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *mode = argc == 8 ? argv[7] : "default";
    if (!strcmp(mode, "sleep")) { usleep(5000000); return 0; }
    if (!strcmp(mode, "nonzero")) { fputs("SECRET_STDERR_VALUE", stderr); fputs("bounded-result", stdout); return 17; }
    if (!strcmp(mode, "stderr")) { fputs("SECRET_STDERR_VALUE", stderr); fputs("ok", stdout); return 0; }
    if (!strcmp(mode, "stdout_overflow") || !strcmp(mode, "stderr_overflow") || !strcmp(mode, "combined_overflow")) {
        char buffer[8192]; memset(buffer, 'x', sizeof(buffer));
        if (!strcmp(mode, "combined_overflow")) {
            for (int i = 0; i < 70; ++i) { fwrite(buffer, 1, sizeof(buffer), stdout); fwrite(buffer, 1, sizeof(buffer), stderr); }
        } else {
            FILE *target = !strcmp(mode, "stderr_overflow") ? stderr : stdout;
            for (int i = 0; i < 140; ++i) fwrite(buffer, 1, sizeof(buffer), target);
        }
        fflush(stdout); fflush(stderr); usleep(5000000); return 0;
    }
    if (!strcmp(mode, "boundary")) {
        char buffer[8192]; memset(buffer, 'x', sizeof(buffer));
        for (int i = 0; i < 128; ++i) fwrite(buffer, 1, sizeof(buffer), stdout);
        return 0;
    }
    if (!strcmp(mode, "environment")) {
        const char *names[] = {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_API_VERSION", "DOCKER_TLS_VERIFY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "CUSTOM_SECRET", NULL};
        for (int i = 0; names[i]; ++i) if (getenv(names[i])) return 42;
        printf("HOME=%s\nPATH=%s\nSTDIN=%s\n", getenv("HOME"), getenv("PATH"), getchar() == EOF ? "EOF" : "DATA");
        return 0;
    }
    for (int i = 0; i < argc; ++i) printf("%s\n", argv[i]);
    return 0;
}
'''


@pytest.fixture(scope="module")
def compiled_cli(tmp_path_factory):
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux descriptor execution backend")
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("Synthetic ELF test needs local C compiler; Docker is never used")
    directory = tmp_path_factory.mktemp("synthetic-docker-cli")
    source = directory / "synthetic_cli.c"
    source.write_text(FAKE_SOURCE)
    executable = directory / "synthetic_cli"
    built = subprocess.run([compiler, "-O2", "-o", str(executable), str(source)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
    assert built.returncode == 0, "Synthetic ELF compiler failed"
    executable.chmod(0o700)
    return executable


@pytest.fixture
def setup_cli(tmp_path, compiled_cli):
    executable = tmp_path / "synthetic_cli"
    shutil.copyfile(compiled_cli, executable)
    executable.chmod(0o700)
    config = tmp_path / "private-config"
    config.mkdir(mode=0o700)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    endpoint = "unix://" + str(tmp_path / "not-a-real-daemon.sock")
    return executable, digest, endpoint, config


def runner(setup_cli):
    return DockerCLI(*setup_cli)


def reject(call, code=None, reason=None):
    with pytest.raises(EngineError) as caught:
        call()
    error = caught.value
    if code is not None:
        assert error.code == code
    if reason is not None:
        assert error.details["reason"] == reason
    assert "SECRET" not in str(error)
    assert "SECRET" not in json.dumps(error.details)
    return error


def test_constructor_does_not_run_process_or_probe_endpoint(setup_cli, monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("Construction must not execute")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    cli = runner(setup_cli)
    assert cli.endpoint == setup_cli[2]
    assert not Path(setup_cli[2][len("unix://"):]).exists()


def test_real_synthetic_process_has_explicit_flags_and_arguments(setup_cli):
    cli = runner(setup_cli)
    args = ("container", "exec", "--", CID, "/app/probe", "--challenge", "not a shell; $(touch forbidden)")
    result = cli.run(args)
    assert result.returncode == 0
    assert result.stdout.decode().splitlines() == [str(setup_cli[0]), "--host", setup_cli[2], "--config", str(setup_cli[3]), *args]
    assert not (setup_cli[3] / "forbidden").exists()
    assert not hasattr(result, "stderr")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.returncode = 1


@pytest.mark.parametrize("args", [
    ("info",), ("info", "--format", FORMAT),
    ("image", "inspect", "--format", FORMAT, "--", "sha256:" + CID),
    ("container", "inspect", "--format", FORMAT, "--", CID),
    ("network", "inspect", "--format", FORMAT, "--", CID),
    ("container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"),
    ("container", "start", "--", CID),
    ("container", "kill", "--signal=SIGTERM", "--", CID),
    ("container", "exec", "--", CID, "/app/probe", "--host", "ordinary-product-argument"),
])
def test_controller_command_grammar_is_supported(setup_cli, args):
    assert runner(setup_cli).run(args).returncode == 0


@pytest.mark.parametrize("args", [
    "info", [], ["pull", "image:latest"], ["container", "create", "image"],
    ["container", "rm", "--force", CID], ["build", "."],
    ["info", "--host", "tcp://remote:2375"], ["info", "--format", FORMAT, "--context", "other"],
    ["container", "start", "--host", "tcp://remote:2375", CID],
    ["container", "start", "--", "container-name"], ["container", "kill", "--signal=SIGKILL", "--", CID],
    ["container", "exec", "--privileged", CID, "/bin/sh"],
    ["container", "exec", "-e", "SECRET=value", "--", CID, "/app/probe"],
    ["container", "exec", "--", CID, "--privileged"],
    ["image", "inspect", "--format", FORMAT, "--", "image:latest"],
    ["info", "--format", "x\nSECRET"], ["info", "--format", "x\0y"],
    ["info", "--format", "x" * 8193], ["info", "--format", 1],
])
def test_unsafe_or_unknown_command_is_rejected_before_spawn(setup_cli, args, monkeypatch):
    cli = runner(setup_cli)
    def forbidden(*_, **__):
        raise AssertionError("Invalid argv must never execute")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    reject(lambda: cli.run(args), "INVALID_REQUEST")


def test_no_inherited_docker_loader_proxy_or_secret_environment(setup_cli, monkeypatch):
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_API_VERSION",
                 "DOCKER_TLS_VERIFY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "CUSTOM_SECRET"):
        monkeypatch.setenv(name, "SECRET_PARENT_VALUE")
    result = runner(setup_cli).run(("info", "--format", "environment"))
    assert result.returncode == 0
    assert result.stdout.decode() == f"HOME={setup_cli[3]}\nPATH=\nSTDIN=EOF\n"


def test_stderr_not_public_and_nonzero_returncode_preserved(setup_cli):
    cli = runner(setup_cli)
    assert cli.run(("info", "--format", "stderr")) == DockerCommandResult(0, b"ok")
    assert cli.run(("info", "--format", "nonzero")) == DockerCommandResult(17, b"bounded-result")


@pytest.mark.parametrize("mode", ["stdout_overflow", "stderr_overflow", "combined_overflow"])
def test_combined_output_limit_terminates_only_own_cli(setup_cli, mode, monkeypatch):
    cli = runner(setup_cli)
    real_popen = subprocess.Popen
    children = []
    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", capture)
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("Must not signal process group"))
    started = time.monotonic()
    error = reject(lambda: cli.run(("info", "--format", mode), timeout=2), "RECOVERY_REQUIRED", "docker_cli_output_limit")
    assert error.details["outcome"] == "unknown"
    assert time.monotonic() - started < 3
    assert len(children) == 1 and children[0].poll() is not None


def test_output_exact_limit_is_accepted(setup_cli):
    result = runner(setup_cli).run(("info", "--format", "boundary"))
    assert result.returncode == 0 and result.stdout == b"x" * MAX_OUTPUT_BYTES


def test_timeout_reaps_only_own_cli_and_does_not_echo_errors(setup_cli, monkeypatch):
    cli = runner(setup_cli)
    real_popen = subprocess.Popen
    children = []
    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", capture)
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("Must not signal process group"))
    started = time.monotonic()
    reject(lambda: cli.run(("info", "--format", "sleep"), timeout=0.05), "RECOVERY_REQUIRED", "docker_cli_deadline_exceeded")
    assert time.monotonic() - started < 2.5
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize("timeout", [0, -1, 301, 10**1000, float("nan"), float("inf"), True, "30"])
def test_invalid_timeout_rejected(setup_cli, timeout):
    reject(lambda: runner(setup_cli).run(("info",), timeout=timeout), "INVALID_REQUEST")


def test_rehash_before_every_run_detects_modified_executable(setup_cli, monkeypatch):
    cli = runner(setup_cli)
    with setup_cli[0].open("ab") as stream:
        stream.write(b"changed")
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("Changed executable must not run"))
    reject(lambda: cli.run(("info",)), "INTEGRITY_FAILED", "docker_cli_digest_mismatch")


def test_descriptor_pin_executes_verified_inode_when_path_is_replaced(setup_cli, monkeypatch):
    cli = runner(setup_cli)
    real_popen = subprocess.Popen
    def replace_then_spawn(*args, **kwargs):
        original = setup_cli[0]
        replacement = original.with_name("replacement")
        replacement.write_bytes(b"not an executable")
        replacement.chmod(0o700)
        os.replace(replacement, original)
        assert kwargs["executable"].startswith("/proc/self/fd/")
        assert kwargs["pass_fds"] and kwargs["shell"] is False
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", replace_then_spawn)
    assert cli.run(("info",)).returncode == 0
    reject(lambda: cli.run(("info",)), "CAPABILITY_UNSUPPORTED", "docker_cli_native_elf_required")


def test_spawn_error_is_sanitized(setup_cli, monkeypatch):
    cli = runner(setup_cli)
    def denied(*_, **__):
        raise OSError("SECRET_RAW_EXCEPTION")
    monkeypatch.setattr(subprocess, "Popen", denied)
    reject(lambda: cli.run(("info",)), "PREREQUISITE_MISSING", "docker_cli_process_start_failed")


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://host", "unix://relative.sock", "unix:///tmp/../other.sock",
    "unix:///tmp/x?token=SECRET", "unix:///tmp/x%2fsock", "unix:////tmp/socket", "unix:///tmp/x#fragment", "npipe:////./pipe/other"])
def test_nonlocal_or_ambiguous_endpoint_rejected(setup_cli, endpoint):
    executable, digest, _, config = setup_cli
    reject(lambda: DockerCLI(executable, digest, endpoint, config), "INVALID_REQUEST")


def test_named_pipe_recognized_but_never_executed_on_linux(setup_cli, monkeypatch):
    executable, digest, _, config = setup_cli
    cli = DockerCLI(executable, digest, "npipe:////./pipe/docker_engine", config)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("No Windows fallback"))
    reject(lambda: cli.run(("info",)), "CAPABILITY_UNSUPPORTED", "docker_cli_named_pipe_execution_unavailable")


def test_private_empty_json_config_is_allowed_but_helpers_are_not(setup_cli):
    path = setup_cli[3] / "config.json"
    path.write_text("{}\n")
    path.chmod(0o600)
    cli = runner(setup_cli)
    path.write_text('{"credsStore":"SECRET-untrusted-helper"}')
    reject(lambda: cli.run(("info",)), "INVALID_REQUEST", "docker_config_features_not_permitted")


@pytest.mark.parametrize("change", ["public_dir", "context_dir", "config_symlink", "public_config"])
def test_config_must_remain_private_and_without_context_plugins(setup_cli, change):
    cli = runner(setup_cli)
    directory = setup_cli[3]
    if change == "public_dir":
        directory.chmod(0o755)
    elif change == "context_dir":
        (directory / "contexts").mkdir()
    elif change == "config_symlink":
        target = directory.parent / "other.json"
        target.write_text("{}")
        (directory / "config.json").symlink_to(target)
    else:
        (directory / "config.json").write_text("{}")
        (directory / "config.json").chmod(0o644)
    reject(lambda: cli.run(("info",)))


@pytest.mark.parametrize("change", ["symlink", "hardlink", "writable", "setuid", "non_executable", "script", "relative", "fifo"])
def test_untrusted_executable_form_rejected(setup_cli, change):
    executable, digest, endpoint, config = setup_cli
    if change == "symlink":
        link = executable.with_name("link")
        link.symlink_to(executable)
        executable = link
    elif change == "hardlink":
        os.link(executable, executable.with_name("alias"))
    elif change == "writable":
        executable.chmod(0o777)
    elif change == "setuid":
        executable.chmod(0o700 | stat.S_ISUID)
    elif change == "non_executable":
        executable.chmod(0o600)
    elif change == "script":
        executable.write_bytes(b"#!/bin/sh\necho SECRET\n")
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    elif change == "fifo":
        executable.unlink()
        os.mkfifo(executable, mode=0o600)
    else:
        executable = Path("relative")
    reject(lambda: DockerCLI(executable, digest, endpoint, config))


def test_missing_or_wrong_pin_is_rejected(setup_cli):
    executable, digest, endpoint, config = setup_cli
    reject(lambda: DockerCLI(executable, "", endpoint, config), "INVALID_REQUEST")
    reject(lambda: DockerCLI(executable, "0" * 64, endpoint, config), "INTEGRITY_FAILED")


def test_result_has_no_stderr_or_command_metadata():
    assert dataclasses.asdict(DockerCommandResult(0, b"{}")) == {"returncode": 0, "stdout": b"{}"}
