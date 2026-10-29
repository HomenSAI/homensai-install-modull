"""Read-only Windows PID contract; fake handles, никаких настоящих сигналов."""
import pytest

from homensai_install import adapters
from homensai_install.api import v1
import homensai_install.process_liveness as liveness


class Port:
    def __init__(self, *, handle=123, error=0, wait=258, close=True):
        self.handle, self.error, self.observed, self.closed = handle, error, wait, close
        self.calls = []

    def open(self, pid):
        self.calls.append(("open", pid))
        return self.handle

    def last_error(self):
        self.calls.append(("error",))
        return self.error

    def wait(self, handle):
        self.calls.append(("wait", handle))
        if isinstance(self.observed, Exception):
            raise self.observed
        return self.observed

    def close(self, handle):
        self.calls.append(("close", handle))
        if isinstance(self.closed, Exception):
            raise self.closed
        return self.closed


@pytest.mark.parametrize("observed, expected", [(0, False), (258, True), (0xFFFFFFFF, None), (42, None)])
def test_windows_wait_is_nonblocking_and_handle_is_closed(observed, expected):
    port = Port(wait=observed)
    assert liveness._windows_process_liveness(456, port=port) is expected
    assert port.calls == [("open", 456), ("wait", 123), ("close", 123)]


@pytest.mark.parametrize("error, expected", [(87, False), (5, None), (0, None), (299, None)])
def test_only_invalid_parameter_proves_missing_windows_pid(error, expected):
    port = Port(handle=None, error=error)
    assert liveness._windows_process_liveness(456, port=port) is expected
    assert port.calls == [("open", 456), ("error",)]


@pytest.mark.parametrize("failure", [OSError("query denied"), RuntimeError("broken port")])
def test_failed_wait_stays_unknown_and_closes_handle(failure):
    port = Port(wait=failure)
    assert liveness._windows_process_liveness(456, port=port) is None
    assert port.calls[-1] == ("close", 123)


@pytest.mark.parametrize("failure", [False, OSError("close failed")])
def test_failed_close_does_not_claim_safe_completion(failure):
    port = Port(wait=0, close=failure)
    assert liveness._windows_process_liveness(456, port=port) is None
    assert port.calls[-1] == ("close", 123)


def test_windows_pid_is_never_truncated_to_a_different_dword():
    port = Port()
    assert liveness._windows_process_liveness(2**32 + 5, port=port) is None
    assert not port.calls


@pytest.mark.parametrize("pid", [None, True, 0, -1, "12", 1.5])
def test_invalid_pid_is_not_a_live_owner(pid):
    assert liveness.process_liveness(pid) is False
    assert adapters._process_alive(pid) is False
    assert v1._owner_alive({"pid": pid}) is False


@pytest.mark.parametrize("observed, expected", [(0, False), (258, True), (0xFFFFFFFF, True)])
def test_both_windows_consumers_never_send_signal_zero(monkeypatch, observed, expected):
    port = Port(wait=observed)
    monkeypatch.setattr(liveness, "_WINDOWS", True)
    monkeypatch.setattr(liveness, "_WindowsProcessPort", lambda: port)
    def forbidden(*args):
        raise AssertionError("Windows liveness must not send CTRL_C_EVENT")
    monkeypatch.setattr(liveness.os, "kill", forbidden)
    monkeypatch.setattr(v1, "_process_identity", lambda pid: None)
    assert adapters._process_alive(456) is expected
    assert v1._owner_alive({"pid": 456, "process_start": None}) is expected


def test_unavailable_windows_api_keeps_unknown_owner_alive(monkeypatch):
    monkeypatch.setattr(liveness, "_WINDOWS", True)
    def denied():
        raise OSError("unavailable kernel API")
    monkeypatch.setattr(liveness, "_WindowsProcessPort", denied)
    monkeypatch.setattr(v1, "_process_identity", lambda pid: (_ for _ in ()).throw(AssertionError("unknown query must stop here")))
    assert liveness.process_liveness(456) is None
    assert adapters._process_alive(456) is True
    assert v1._owner_alive({"pid": 456}) is True


@pytest.mark.parametrize("failure, expected", [(ProcessLookupError(), False), (PermissionError(), None), (OSError(), None)])
def test_posix_signal_probe_preserves_conservative_access_policy(monkeypatch, failure, expected):
    monkeypatch.setattr(liveness, "_WINDOWS", False)
    def denied(*args):
        raise failure
    monkeypatch.setattr(liveness.os, "kill", denied)
    assert liveness.process_liveness(456) is expected


def test_api_keeps_posix_birth_token_and_zombie_checks(monkeypatch):
    monkeypatch.setattr(liveness, "_WINDOWS", False)
    monkeypatch.setattr(liveness.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(v1, "_process_identity", lambda pid: ("R", "birth", 1))
    assert v1._owner_alive({"pid": 456, "process_start": "birth"}) is True
    assert v1._owner_alive({"pid": 456, "process_start": "old-birth"}) is False
    monkeypatch.setattr(v1, "_process_identity", lambda pid: ("Z", "birth", 1))
    assert v1._owner_alive({"pid": 456, "process_start": "birth"}) is False
    monkeypatch.setattr(v1, "_process_identity", lambda pid: ("Z", "birth", 2))
    assert v1._owner_alive({"pid": 456, "process_start": "birth"}) is True
