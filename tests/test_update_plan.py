"""Транзакция на фейках с durable ledger внутри tmp_path; служб/БД нет."""

import json
from pathlib import Path

import pytest

import shared.update_plan as module
from shared.manifest import load_manifest
from shared.update_plan import LockBusy, UpdateError, UpdatePlan, operation_lock


SECRET = "SYNTHETIC-SECRET-DO-NOT-EMIT"
FORWARD = ["stop", "archive", "stage", "backup:0", "migrate:0", "backup:1", "migrate:1",
           "switch", "start", "health", "post_update"]
ROLLBACK = ["rollback_stop", "before_rollback", "restore", "pointer_back", "rollback_start", "rollback_health"]


class Crash(BaseException):
    pass


class FakePorts:
    """Эффекты и дедупликация переживают создание нового экземпляра порта."""

    def __init__(self, root, *, fail=None, crash=None, after=False):
        self.root, self.fail, self.crash, self.after = root, fail, crash, after
        self.path = root / "fake-ledger.json"
        if not self.path.exists():
            self.path.write_text(json.dumps({"done": [], "events": [], "attempts": [], "snapshots": {},
                                              "pointer": "old", "db": 0}), encoding="utf-8")

    @property
    def data(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def current_release(self):
        return self.data["pointer"]

    def action(self, key, effect=None, details=None):
        phase = key.split("/", 1)[1]
        data = self.data
        data["attempts"].append(phase)
        self.save(data)
        if key in data["done"]:
            return
        trigger = self.fail == phase or self.crash == phase
        if trigger and not self.after:
            raise Crash() if self.crash == phase else RuntimeError(SECRET)
        if effect:
            effect(data)
        data["done"].append(key)
        data["events"].append([phase, details])
        self.save(data)
        if trigger and self.after:
            raise Crash() if self.crash == phase else RuntimeError(SECRET)

    def stop(self, key, components):
        self.action(key, details=components)

    def backup(self, key, purpose, command):
        self.action(key, lambda data: data["snapshots"].update({key: data["db"]}), [purpose, command])

    def stage_release(self, key, release):
        def effect(data):
            (self.root / (release + ".release")).write_text("verified synthetic payload")
        self.action(key, effect, release)

    def migrate(self, key, migration, command):
        self.action(key, lambda data: data.update(db=data["db"] + 1), [migration, command])

    def switch_pointer(self, key, release):
        self.action(key, lambda data: data.update(pointer=release), release)

    def start(self, key, components, pre_start):
        self.action(key, details=[components, pre_start])

    def health(self, key, url, timeout, checks):
        self.action(key, details=[url, timeout, checks])

    def restore(self, key, archive_key, command):
        def effect(data):
            assert key.split("/")[0] + "/before_rollback" in data["snapshots"]
            assert archive_key in data["snapshots"]
            data["db"] = data["snapshots"][archive_key]
        self.action(key, effect, [archive_key, command])

    def post_update(self, key, hooks):
        self.action(key, details=hooks)


@pytest.fixture
def manifest():
    return load_manifest(Path(__file__).resolve().parents[1] / "examples/rent.manifest.json")


def plan(root, manifest, ports):
    return UpdatePlan(manifest, root, ports, release="new", modes=("autonomous",),
                      migration_ids=("schema-1", "schema-2"))


def phases(ports):
    return [event[0] for event in ports.data["events"]]


def test_success_order_backups_hooks_and_retention(tmp_path, manifest):
    old = tmp_path / "old.release"
    old.write_bytes(b"old payload retained")
    ports = FakePorts(tmp_path)
    update = plan(tmp_path, manifest, ports)
    result = update.run()
    assert result["outcome"] == "updated"
    assert phases(ports) == FORWARD
    assert ports.data["pointer"] == "new" and ports.data["db"] == 2
    assert old.read_bytes() == b"old payload retained"
    assert (tmp_path / "new.release").exists()
    events = dict(ports.data["events"])
    assert events["stop"] == ["scheduler", "bot", "web"]
    assert events["start"][0] == ["web", "bot", "scheduler"]
    assert events["start"][1] == manifest["hooks"]["pre_start"]
    assert events["health"] == ["http://127.0.0.1:8820/healthz", 60, manifest["hooks"]["health_checks"]]
    transaction = result["transaction"]
    assert ports.data["snapshots"][transaction + "/backup:0"] == 0
    assert ports.data["snapshots"][transaction + "/backup:1"] == 1
    assert update.run() == result
    assert phases(ports) == FORWARD


@pytest.mark.parametrize("phase", FORWARD)
@pytest.mark.parametrize("after", [False, True])
def test_every_update_failure_rolls_back(tmp_path, manifest, phase, after):
    ports = FakePorts(tmp_path, fail=phase, after=after)
    result = plan(tmp_path, manifest, ports).run()
    assert result["outcome"] == "rolled_back" and result["failed_phase"] == phase
    assert ports.data["pointer"] == "old" and ports.data["db"] == 0
    events = phases(ports)
    assert "before_rollback" in events
    if FORWARD.index(phase) > 1:
        assert events.index("before_rollback") < events.index("restore") < events.index("pointer_back")
    else:
        assert "restore" not in events  # До checkpoint archive данные ещё не менялись.
    assert events[-1] == "rollback_health"


@pytest.mark.parametrize("phase", FORWARD)
@pytest.mark.parametrize("after", [False, True])
def test_crash_resume_each_update_phase(tmp_path, manifest, phase, after):
    with pytest.raises(Crash):
        plan(tmp_path, manifest, FakePorts(tmp_path, crash=phase, after=after)).run()
    ports = FakePorts(tmp_path)
    result = plan(tmp_path, manifest, ports).run()
    assert result["outcome"] == "updated"
    assert phases(ports) == FORWARD
    assert ports.data["pointer"] == "new" and ports.data["db"] == 2
    assert ports.data["attempts"].count(phase) == 2


@pytest.mark.parametrize("phase", ROLLBACK)
@pytest.mark.parametrize("after", [False, True])
def test_crash_resume_each_rollback_phase(tmp_path, manifest, phase, after):
    with pytest.raises(Crash):
        plan(tmp_path, manifest, FakePorts(tmp_path, fail="health", crash=phase, after=after)).run()
    ports = FakePorts(tmp_path)
    result = plan(tmp_path, manifest, ports).run()
    assert result["outcome"] == "rolled_back"
    assert phases(ports)[-6:] == ROLLBACK
    assert ports.data["pointer"] == "old" and ports.data["db"] == 0


@pytest.mark.parametrize("phase", ROLLBACK)
def test_rollback_failure_pauses_and_resumes(tmp_path, manifest, phase):
    # Начальная ошибка health, затем отдельная ошибка выбранного шага отката.
    class TwoFailures(FakePorts):
        def action(self, key, effect=None, details=None):
            if key.endswith("/health"):
                raise RuntimeError(SECRET)
            return super().action(key, effect, details)
    ports = TwoFailures(tmp_path, fail=phase)
    update = plan(tmp_path, manifest, ports)
    with pytest.raises(UpdateError) as error:
        update.run()
    assert SECRET not in str(error.value)
    saved = json.loads(update.state_path.read_text())
    assert saved["direction"] == "rollback" and saved["index"] == ROLLBACK.index(phase)
    if phase == "before_rollback":
        assert not {"restore", "pointer_back", "rollback_start"} & set(phases(ports))
    fresh = FakePorts(tmp_path)
    assert plan(tmp_path, manifest, fresh).run()["outcome"] == "rolled_back"
    assert fresh.data["db"] == 0


def test_operation_lock_contention_and_release(tmp_path, manifest):
    ports = FakePorts(tmp_path)
    update = plan(tmp_path, manifest, ports)
    update.lock_path.parent.mkdir(parents=True)
    with operation_lock(update.lock_path):
        with pytest.raises(LockBusy):
            plan(tmp_path, manifest, ports).run()
        assert ports.data["events"] == []
        assert not update.state_path.exists()
    assert update.run()["outcome"] == "updated"
    assert update.lock_path.exists()


@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("checkpoint", range(len(FORWARD) + 1))
def test_atomic_checkpoint_crash_resume(tmp_path, manifest, monkeypatch, boundary, checkpoint):
    update = plan(tmp_path, manifest, FakePorts(tmp_path))
    original = module._atomic_write
    count = 0

    def interrupted(path, value):
        nonlocal count
        current = count
        count += 1
        if current == checkpoint and boundary == "before":
            raise Crash()
        original(path, value)
        if current == checkpoint and boundary == "after":
            raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(module, "_atomic_write", interrupted)
        with pytest.raises(Crash):
            update.run()
    ports = FakePorts(tmp_path)
    assert plan(tmp_path, manifest, ports).run()["outcome"] == "updated"
    assert phases(ports) == FORWARD
    assert ports.data["db"] == 2


def test_replace_error_keeps_valid_checkpoint_and_same_operation(tmp_path, manifest, monkeypatch):
    ports = FakePorts(tmp_path)
    update = plan(tmp_path, manifest, ports)
    original = module.os.replace

    def fail_replace(source, target):
        if "migrate:0" in phases(ports):
            raise OSError(SECRET)
        return original(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail_replace)
        with pytest.raises(UpdateError):
            update.run()
    saved = json.loads(update.state_path.read_text())
    assert saved["direction"] == "forward" and update.forward[saved["index"]] == "migrate:0"
    assert not list(update.state_path.parent.glob(".update-*.tmp"))
    assert plan(tmp_path, manifest, FakePorts(tmp_path)).run()["outcome"] == "updated"
    assert ports.data["db"] == 2


@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("checkpoint", range(len(ROLLBACK) + 1))
def test_rollback_checkpoint_crash_resume(tmp_path, manifest, monkeypatch, boundary, checkpoint):
    update = plan(tmp_path, manifest, FakePorts(tmp_path, fail="health"))
    original = module._atomic_write
    count = 0

    def interrupted(path, value):
        nonlocal count
        hit = False
        if value["direction"] == "rollback":
            hit = count == checkpoint
            count += 1
        if hit and boundary == "before":
            raise Crash()
        original(path, value)
        if hit and boundary == "after":
            raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(module, "_atomic_write", interrupted)
        with pytest.raises(Crash):
            update.run()
    # При crash до checkpoint направления исходная ошибка воспроизводится
    # тем же key: durable порт обязан помнить и терминальный сбой шага.
    ports = FakePorts(tmp_path, fail="health")
    result = plan(tmp_path, manifest, ports).run()
    assert result["outcome"] == "rolled_back" and ports.data["db"] == 0


@pytest.mark.parametrize("corruption", ["invalid-json", "extra", "no-backup", "negative", "plan", "duplicate"])
def test_corrupt_state_refused_without_effects(tmp_path, manifest, corruption):
    update = plan(tmp_path, manifest, FakePorts(tmp_path, crash="restore", fail="health"))
    with pytest.raises(Crash):
        update.run()
    saved = json.loads(update.state_path.read_text())
    if corruption == "invalid-json":
        text = "{"
    elif corruption == "duplicate":
        text = '{"version":1,"version":1}'
    else:
        if corruption == "extra":
            saved["secret"] = SECRET
        elif corruption == "no-backup":
            saved["archive_ready"] = False
        elif corruption == "negative":
            saved["index"] = -1
        else:
            saved["plan"] = "a" * 64
        text = json.dumps(saved)
    update.state_path.write_text(text)
    ports = FakePorts(tmp_path)
    before = ports.data
    with pytest.raises(UpdateError) as error:
        plan(tmp_path, manifest, ports).run()
    assert SECRET not in str(error.value)
    assert ports.data == before


def test_changed_manifest_refused_on_resume(tmp_path, manifest):
    with pytest.raises(Crash):
        plan(tmp_path, manifest, FakePorts(tmp_path, crash="stage")).run()
    manifest["update"]["health_wait"] = 30
    with pytest.raises(UpdateError):
        plan(tmp_path, manifest, FakePorts(tmp_path)).run()


def test_no_secret_values_in_state_errors_or_output(tmp_path, manifest, monkeypatch, capsys):
    for secret in manifest["secrets"]:
        monkeypatch.setenv(secret["name"], SECRET)
    ports = FakePorts(tmp_path, fail="migrate:1")
    update = plan(tmp_path, manifest, ports)
    result = update.run()
    assert SECRET not in json.dumps(result)
    assert SECRET not in update.state_path.read_text()
    assert SECRET not in capsys.readouterr().out
    assert set(result) == {"version", "transaction", "plan", "old_release", "release", "direction",
                           "index", "failed_phase", "archive_ready", "outcome"}


@pytest.mark.parametrize("field", ["state_file", "pointer_file"])
def test_lock_path_collision_rejected(tmp_path, manifest, field):
    manifest["update"][field] = "operation.lock"
    with pytest.raises(UpdateError):
        plan(tmp_path, manifest, FakePorts(tmp_path))


PRESERVE_FORWARD = 'preserve_forward_journal_no_automatic_restore'


def forward_plan(root, manifest, ports):
    return UpdatePlan(manifest, root, ports, release='new', modes=('autonomous',),
                      migration_ids=('schema-1', 'schema-2'), failure_policy=PRESERVE_FORWARD)


@pytest.mark.parametrize('phase', FORWARD)
@pytest.mark.parametrize('after', [False, True])
def test_preserve_forward_failure_never_restores_or_rewinds_data(tmp_path, manifest, phase, after):
    ports = FakePorts(tmp_path, fail=phase, after=after)
    update = forward_plan(tmp_path, manifest, ports)
    with pytest.raises(RuntimeError):
        update.run()
    state = update.status()
    assert state['direction'] == 'forward' and state['index'] == FORWARD.index(phase)
    assert state['outcome'] == 'running' and state['failed_phase'] is None
    assert not set(ROLLBACK).intersection(phases(ports))
    before = list(phases(ports))
    ports = FakePorts(tmp_path)
    assert forward_plan(tmp_path, manifest, ports).run()['outcome'] == 'updated'
    assert phases(ports) == FORWARD
    assert phases(ports)[:len(before)] == before


@pytest.mark.parametrize('first_preserves', [False, True])
def test_failure_policy_is_pinned_in_journal_before_next_effect(tmp_path, manifest, first_preserves):
    constructor = forward_plan if first_preserves else plan
    with pytest.raises(Crash):
        constructor(tmp_path, manifest, FakePorts(tmp_path, crash='migrate:0')).run()
    ports = FakePorts(tmp_path)
    before = ports.data
    other = plan if first_preserves else forward_plan
    with pytest.raises(UpdateError):
        other(tmp_path, manifest, ports).run()
    assert ports.data == before


def test_default_failure_policy_keeps_legacy_plan_digest(tmp_path, manifest):
    import hashlib
    update = plan(tmp_path, manifest, FakePorts(tmp_path))
    legacy = {'manifest': update.manifest, 'release': 'new', 'modes': ['autonomous'],
              'migrations': ('schema-1', 'schema-2')}
    assert update.digest == hashlib.sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def test_unknown_failure_policy_is_rejected_before_effect(tmp_path, manifest):
    ports = FakePorts(tmp_path)
    with pytest.raises(UpdateError):
        UpdatePlan(manifest, tmp_path, ports, release='new', modes=('autonomous',), failure_policy='unknown')
    assert ports.data['events'] == []


def test_preserve_policy_rejects_well_formed_rollback_journal_before_effect(tmp_path, manifest):
    ports = FakePorts(tmp_path)
    legacy = plan(tmp_path, manifest, ports)
    with pytest.raises(Crash):
        plan(tmp_path, manifest, FakePorts(tmp_path, fail='health', crash='rollback_stop')).run()
    saved = json.loads(legacy.state_path.read_text())
    preserve = forward_plan(tmp_path, manifest, ports)
    saved['plan'] = preserve.digest
    legacy.state_path.write_text(json.dumps(saved))
    before = ports.data
    with pytest.raises(UpdateError):
        preserve.run()
    assert ports.data == before
