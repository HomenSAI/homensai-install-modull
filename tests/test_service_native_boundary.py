"""User-session не обходит зарегистрированные service writers по одному PID."""
import json
import os

import pytest

from homensai_install.engine import EngineError, NativeEngine, _owner_id


@pytest.fixture
def boundary(tmp_path):
    control = tmp_path / ".hinstall"
    directory = control / "service-manager"
    directory.mkdir(parents=True, mode=0o700)
    owner = {"product_id": "reference", "installation_id": "a" * 32,
             "owner": _owner_id(), "mode": "native"}
    (control / "installation.json").write_text(json.dumps(owner))
    context = {"control": control, "manifest": {"product_id": "reference"}}
    state = {"schema_version": 1, "binding": dict(owner), "revision": 1,
             "services": {"owned.service": {"removed": True, "enabled": False, "start_uncertain": False}},
             "operations": {"op": {"status": "done"}}}
    path = directory / "state.json"
    def write():
        path.write_text(json.dumps(state))
        if os.name != "nt":
            path.chmod(0o600)
    write()
    return NativeEngine(), context, state, path, write


@pytest.mark.parametrize("change", [
    lambda state: state["services"]["owned.service"].update(removed=False),
    lambda state: state["services"]["owned.service"].update(enabled=True),
    lambda state: state["services"]["owned.service"].update(start_uncertain=True),
    lambda state: state["operations"]["op"].update(status="running"),
    lambda state: state.update(services={}, operations={"registration": {"status": "running"}}),
])
def test_registered_stopped_or_pending_service_fences_native(boundary, change):
    engine, context, state, path, write = boundary
    change(state)
    write()
    before = path.read_bytes()
    with pytest.raises(EngineError) as caught:
        engine._owner(context)
    assert caught.value.code == "OPERATION_CONFLICT"
    assert path.read_bytes() == before


def test_completed_service_removal_allows_user_session_again(boundary):
    engine, context, state, _, _ = boundary
    assert engine._owner(context) == state["binding"]


@pytest.mark.parametrize("change", [
    lambda state: state["binding"].update(installation_id="b" * 32),
    lambda state: state.update(schema_version=True),
    lambda state: state.update(services=[]),
    lambda state: state.update(operations={"x": False}),
])
def test_foreign_or_malformed_service_journal_is_not_absence(boundary, change):
    engine, context, state, _, write = boundary
    change(state)
    write()
    with pytest.raises(EngineError) as caught:
        engine._owner(context)
    assert caught.value.code == "RECOVERY_REQUIRED"


@pytest.mark.parametrize("kind", ["missing", "corrupt", "symlink"])
def test_unreadable_service_journal_fails_closed(boundary, kind, tmp_path):
    engine, context, _, path, _ = boundary
    path.unlink()
    if kind == "corrupt":
        path.write_text("{")
    elif kind == "symlink":
        target = tmp_path / "outside.json"
        target.write_text("{}")
        try:
            path.symlink_to(target)
        except OSError:
            if os.name != "nt":
                raise
            pytest.skip("Windows symlink fixture requires Developer Mode or matching privileges")
    with pytest.raises(EngineError) as caught:
        engine._owner(context)
    assert caught.value.code == "RECOVERY_REQUIRED"
