"""Настоящие собственные Windows children; никаких сигналов/чужих PID/продуктов."""
import os
import subprocess
import sys

import pytest

from homensai_install import adapters
from homensai_install.api import v1
from homensai_install.process_liveness import process_liveness

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Read-only Windows process handles")


@pytest.mark.parametrize("exit_code", [0, 259])
def test_owned_windows_child_can_be_observed_alive_and_exited_without_ctrl_c(monkeypatch, exit_code):
    def forbidden(*args):
        raise AssertionError("Windows liveness must not call os.kill")
    monkeypatch.setattr(os, "kill", forbidden)
    assert process_liveness(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c",
        "import os,sys;print('ready',flush=True);sys.stdin.buffer.read(1);os._exit(" + str(exit_code) + ")"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    try:
        assert child.stdout.readline() == b"ready\r\n"
        owner = {"pid": child.pid, "process_start": None}
        assert process_liveness(child.pid) is True
        assert v1._owner_alive(owner) is True
        assert adapters._process_alive(child.pid) is True
        assert child.poll() is None
        child.stdin.close()
        assert child.wait(timeout=5) == exit_code
        assert process_liveness(child.pid) is False
        assert v1._owner_alive(owner) is False
        assert adapters._process_alive(child.pid) is False
    finally:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.terminate()  # Только созданный этим тестом Popen child.
        child.wait(timeout=5)
        child.stdout.close()
