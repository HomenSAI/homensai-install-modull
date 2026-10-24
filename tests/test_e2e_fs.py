# HomenS.AI · © 2026 Serhii Khomenko
"""Настоящая локальная ФС и ZIP; только вымышленные данные внутри tmp_path."""

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

import shared.adapters_fs as fs
import shared.update_plan as updates
from shared.archive import ArchiveError, verify_archive
from shared.datahome import DataHomeError, resolve_datahome
from shared.manifest import load_manifest, validate_manifest
from shared.update_plan import LockBusy, UpdateError, UpdatePlan, operation_lock


SECRET = "SYNTHETIC-FS-SECRET-NEVER-WRITTEN"
FORWARD = ["stop", "archive", "stage", "backup:0", "migrate:0", "switch",
           "start", "health", "post_update"]
ROLLBACK = ["rollback_stop", "before_rollback", "restore", "pointer_back",
            "rollback_start", "rollback_health"]


class Crash(BaseException):
    pass


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Процессы запрещены")
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setenv("E2E_TOKEN", SECRET)
    monkeypatch.setenv("BACKUP_PASSWORD", SECRET)


class FakeEffects:
    """Фейк хранит только ключи/результаты, команды и env не журналируются."""

    def __init__(self, root, home, *, fail_health=False):
        self.path = root / "fake-effects.json"
        self.home = home
        self.fail_health = fail_health

    def action(self, key, effect=None):
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        if key in data:
            if data[key] == "failed":
                raise RuntimeError(SECRET)
            return
        phase = key.split("/", 1)[1]
        if effect:
            effect()
        failed = phase == "health" and self.fail_health
        data[key] = "failed" if failed else "done"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        updates._atomic_write(self.path, data)
        if failed:
            raise RuntimeError(SECRET)

    def stop(self, key, components):
        self.action(key)

    def migrate(self, key, migration, command):
        def change():
            (self.home.roots["data"] / "database.bin").write_bytes(b"schema-2 + new records")
            (self.home.roots["data"] / "new-only.txt").write_bytes(b"created by fake migration")
            (self.home.roots["data"] / "obsolete.txt").unlink(missing_ok=True)
        self.action(key, change)

    def start(self, key, components, pre_start):
        self.action(key)

    def health(self, key, url, timeout, checks):
        self.action(key)

    def post_update(self, key, hooks):
        self.action(key)


@pytest.fixture
def layout(tmp_path):
    original = load_manifest(Path(__file__).resolve().parents[1] / "examples/jobpilot.manifest.json")
    # Явное тестовое дополнение v1 до v2: продуктовые команды не исполняются.
    payload = tmp_path / "payload"
    payload.mkdir()
    contents = {"main.py": b"# fake JobPilot release\n", "locks/windows.lock": b"fake windows lock",
                "locks/linux.lock": b"fake linux lock", "vendor/demo-0-py3-none-any.whl": b"fake wheel"}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
    for name, content in contents.items():
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (payload / "empty").mkdir()
    artifact = lambda name: {"file": name, "sha256": hashes[name]}
    manifest = validate_manifest({
        "schema_version": 2, "product_name": original["product_name"],
        "product_id": original["product_id"], "archive_before_replace": original["archive_before_replace"],
        "runtime": {"python": "3.14", "offline": True,
                    "lock_files": {"windows": artifact("locks/windows.lock"),
                                   "linux": artifact("locks/linux.lock")},
                    "vendor_wheels": [artifact("vendor/demo-0-py3-none-any.whl")]},
        "paths": {name: name for name in ("app", "releases", "data", "backup", "services", "config", "runtime")},
        "components": [{"name": "web", "cmd": original["docker"]["start_command"], "after": [],
                        "heartbeat": {"file": "heartbeat.json", "interval": 1, "timeout": 2},
                        "restart": {"delay": 1, "max": 3, "window": 10}}],
        "web": {"host": "127.0.0.1", "port": original["port"],
                "healthz": original["healthcheck_url"].split(str(original["port"]), 1)[1]},
        "secrets": [{"name": "E2E_TOKEN", "required": True, "contexts": ["service"], "same_value": False}],
        "modes": [{"name": "local", "components": ["web"]}], "mutual_exclusion": [],
        "migrations": {"cmd": original["migration_command"], "backup_before_each": True, "new_db_no_backup": False},
        "backup": {"cmd": ["fake-backup"], "encrypted": True}, "restore": {"cmd": ["fake-restore"]},
        "update": {"staged_releases": True, "pointer_file": "active.json", "state_file": "update.json",
                   "health_wait": 2, "backup_before_rollback": True, "retain_previous_release": True,
                   "retain_runtime": True},
        "hooks": {"pre_start": [], "health_checks": [], "post_update": []},
    })
    root = tmp_path / "installation"
    home = resolve_datahome(manifest, root)
    for path in (home.app, home.releases, *home.roots.values()):
        path.mkdir(parents=True)
    for name, directory in home.roots.items():
        (directory / "original.txt").write_bytes(("original " + name).encode())
        (directory / "empty").mkdir()
    (home.roots["data"] / "database.bin").write_bytes(b"schema-1 original records")
    (home.roots["data"] / "obsolete.txt").write_bytes(b"restore me")
    work = tmp_path / "operations"
    source = fs.ReleaseSource(payload, hashes)
    new_payload = tmp_path / "payload-new"
    new_payload.mkdir()
    new_contents = dict(contents, **{"main.py": b"# fake JobPilot release 2\n"})
    for name, content in new_contents.items():
        target = new_payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (new_payload / "empty").mkdir()
    new_source = fs.ReleaseSource(new_payload, {
        name: hashlib.sha256(content).hexdigest() for name, content in new_contents.items()})

    def ports(*, fail_health=False):
        return fs.FilesystemPorts(manifest, root, work_dir=work,
                                  payloads={"1.0": source, "2.0": new_source},
                                  effects=FakeEffects(work, home, fail_health=fail_health),
                                  allow_unencrypted=True)

    # Fresh layout: полный staged выпуск и атомарный указатель, не реальная служба.
    fresh = ports()
    with operation_lock(home.roots["services"] / "operation.lock"):
        fresh.stage_release("0" * 32 + "/stage", "1.0")
        fresh.switch_pointer("0" * 32 + "/switch", "1.0")
    return manifest, root, home, work, ports


def plan(layout, *, fail_health=False):
    manifest, root, _, _, ports = layout
    return UpdatePlan(manifest, root, ports(fail_health=fail_health), release="2.0", modes=("local",))


def verify_result(layout, result, *, rollback):
    _, root, home, work, ports = layout
    current = ports()
    assert result["outcome"] == ("rolled_back" if rollback else "updated")
    assert current.current_release() == ("1.0" if rollback else "2.0")
    assert current.release_path("1.0").is_dir() and current.release_path("2.0").is_dir()
    assert (current.release_path("1.0") / "main.py").read_bytes() == b"# fake JobPilot release\n"
    assert (current.release_path("2.0") / "main.py").read_bytes() == b"# fake JobPilot release 2\n"
    data = home.roots["data"]
    assert (data / "database.bin").read_bytes() == (
        b"schema-1 original records" if rollback else b"schema-2 + new records")
    assert (data / "obsolete.txt").exists() == rollback
    assert (data / "new-only.txt").exists() != rollback
    assert (home.roots["runtime"] / "original.txt").read_bytes() == b"original runtime"
    if rollback:
        before = current.archive_path(result["transaction"] + "/before_rollback")
        verify_archive(before, expected_product_id=home.product_id)
        with zipfile.ZipFile(before) as archive:
            assert archive.read("roots/data/database.bin") == b"schema-2 + new records"
        assert before != current.archive_path(result["transaction"] + "/archive")
    for path in root.parent.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
            if path.suffix == ".zip":
                with zipfile.ZipFile(path) as archive:
                    assert all(SECRET.encode() not in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize("rollback", [False, True])
def test_fresh_archive_update_health_rollback(layout, rollback):
    update = plan(layout, fail_health=rollback)
    result = update.run()
    verify_result(layout, result, rollback=rollback)
    before = {p: p.read_bytes() for p in layout[3].glob("*.zip")}
    assert plan(layout).run() == result
    assert before == {p: p.read_bytes() for p in layout[3].glob("*.zip")}


@pytest.mark.parametrize("phase", FORWARD + ROLLBACK)
@pytest.mark.parametrize("after", [False, True])
def test_crash_resume_every_phase_with_fresh_adapters(layout, phase, after):
    rollback = phase in ROLLBACK
    update = plan(layout, fail_health=rollback)
    execute = update._execute

    def interrupted(state, current):
        if current == phase and not after:
            raise Crash()
        try:
            return execute(state, current)
        finally:
            if current == phase and after:
                raise Crash()
    update._execute = interrupted
    with pytest.raises(Crash):
        update.run()
    result = plan(layout, fail_health=rollback).run()
    verify_result(layout, result, rollback=rollback)


@pytest.mark.parametrize("rollback,checkpoint", [
    *[(False, index) for index in range(len(FORWARD) + 1)],
    *[(True, index) for index in range(len(ROLLBACK) + 1)],
])
@pytest.mark.parametrize("after", [False, True])
def test_real_state_crash_at_every_checkpoint(layout, rollback, checkpoint, after, monkeypatch):
    # Отдельная свежая установка на каждый checkpoint, все записи через FS-порты.
    update = plan(layout, fail_health=rollback)
    original = updates._atomic_write
    hit = False
    count = 0

    def interrupted(path, value):
        nonlocal hit, count
        selected = False
        if path == update.state_path and value["direction"] == ("rollback" if rollback else "forward"):
            selected = count == checkpoint
            count += 1
        if selected:
            hit = True
            if not after:
                raise Crash()
        original(path, value)
        if selected and hit and after:
            raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(updates, "_atomic_write", interrupted)
        with pytest.raises(Crash):
            update.run()
    assert hit
    verify_result(layout, plan(layout, fail_health=rollback).run(), rollback=rollback)


@pytest.mark.parametrize("boundary", ["stage_publish", "archive_publish", "pointer", "restore_copy", "restore_receipt"])
def test_crash_inside_filesystem_effects(layout, boundary, monkeypatch):
    update = plan(layout, fail_health=True)
    hit = False
    original_rename, original_link = fs.os.rename, fs.os.link
    original_atomic, original_copy, original_record = fs._atomic_write, fs._copy_file, fs.FilesystemPorts._record

    def crash_once():
        nonlocal hit
        if not hit:
            hit = True
            raise Crash()

    def rename(source, destination):
        original_rename(source, destination)
        if boundary == "stage_publish":
            crash_once()

    def link(source, destination):
        original_link(source, destination)
        if boundary == "archive_publish":
            crash_once()

    def atomic(path, value):
        original_atomic(path, value)
        if boundary == "pointer" and path == update.ports.pointer:
            crash_once()

    def copy(source, destination):
        original_copy(source, destination)
        if boundary == "restore_copy" and "restore-" in str(source):
            crash_once()

    def record(self, key, value):
        if boundary == "restore_receipt" and key.endswith("/restore"):
            crash_once()
        original_record(self, key, value)

    with monkeypatch.context() as patch:
        patch.setattr(fs.os, "rename", rename)
        patch.setattr(fs.os, "link", link)
        patch.setattr(fs, "_atomic_write", atomic)
        patch.setattr(fs, "_copy_file", copy)
        patch.setattr(fs.FilesystemPorts, "_record", record)
        with pytest.raises(Crash):
            update.run()
    assert hit
    verify_result(layout, plan(layout, fail_health=True).run(), rollback=True)


def test_failed_before_rollback_blocks_restore_and_pointer(layout, monkeypatch):
    update = plan(layout, fail_health=True)
    original = fs.build_archive

    def failed(home, destination):
        state = json.loads(update.state_path.read_text())
        if state["direction"] == "rollback":
            raise ArchiveError(SECRET)
        return original(home, destination)

    with monkeypatch.context() as patch:
        patch.setattr(fs, "build_archive", failed)
        with pytest.raises(UpdateError) as error:
            update.run()
    assert SECRET not in str(error.value)
    assert update.ports.current_release() == "2.0"
    assert (layout[2].roots["data"] / "database.bin").read_bytes() == b"schema-2 + new records"
    assert update.status()["index"] == 1
    with pytest.raises(ArchiveError):
        update.ports.restore(update.status()["transaction"] + "/restore",
                             update.status()["transaction"] + "/archive", ())
    verify_result(layout, plan(layout).run(), rollback=True)


def test_stale_lock_detection_never_steals_live_lock(tmp_path):
    path = tmp_path / "operation.lock"
    # Остаток после смерти процесса: файл held, но OS lock уже отсутствует.
    path.write_bytes(b'\0{"version":1,"status":"held","pid":1}')
    inode = path.stat().st_ino
    with operation_lock(path) as stale:
        assert stale is True
        with pytest.raises(LockBusy):
            with operation_lock(path):
                pytest.fail("Живая блокировка отобрана")
    with operation_lock(path) as stale:
        assert stale is False
    assert path.stat().st_ino == inode
    path.write_bytes(b"\0broken metadata")
    with operation_lock(path) as stale:
        assert stale is True


def test_plaintext_opt_in_and_path_overlap_rejected(layout):
    manifest, root, home, work, ports = layout
    with pytest.raises(UpdateError, match="allow_unencrypted"):
        fs.FilesystemPorts(manifest, root, work_dir=work, payloads={}, effects=FakeEffects(work, home))
    with pytest.raises(UpdateError):
        fs.FilesystemPorts(manifest, root, work_dir=home.roots["backup"], payloads={},
                           effects=FakeEffects(work, home), allow_unencrypted=True)
    with pytest.raises((UpdateError, DataHomeError)):
        ports().release_path("../escape")


def test_tampered_or_extra_payload_never_published(layout):
    ports = layout[4]()
    source = ports.payloads["2.0"]
    (source.directory / "extra.py").write_bytes(b"unexpected")
    with pytest.raises(UpdateError):
        ports.stage_release("a" * 32 + "/stage", "2.0")
    assert not ports.release_path("2.0").exists()
    assert ports.release_path("1.0").exists()


def test_wrong_runtime_pin_and_existing_release_are_not_overwritten(layout):
    ports = layout[4]()
    ports.manifest["runtime"]["lock_files"]["windows"]["sha256"] = "0" * 64
    with pytest.raises(UpdateError):
        ports.stage_release("a" * 32 + "/stage", "2.0")
    assert not ports.release_path("2.0").exists()
    ports = layout[4]()
    ports.release_path("2.0").mkdir()
    sentinel = ports.release_path("2.0") / "keep.txt"
    sentinel.write_bytes(b"existing unrelated release")
    with pytest.raises(UpdateError):
        ports.stage_release("a" * 32 + "/stage", "2.0")
    assert sentinel.read_bytes() == b"existing unrelated release"


def test_reparse_payload_is_rejected_without_reading_bytes(layout, monkeypatch):
    ports = layout[4]()
    forbidden = ports.payloads["2.0"].directory / "main.py"
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == forbidden:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(UpdateError):
        ports.stage_release("a" * 32 + "/stage", "2.0")
    assert not ports.release_path("2.0").exists()


def test_nested_control_paths_survive_restore(layout):
    manifest, root, home, _, ports = layout
    manifest["update"]["pointer_file"] = "control/pointer/active.json"
    manifest["update"]["state_file"] = "control/state/update.json"
    current = ports()
    current.switch_pointer("b" * 32 + "/switch", "1.0")
    result = plan(layout, fail_health=True).run()
    verify_result(layout, result, rollback=True)
    assert (home.roots["services"] / "control/state/update.json").is_file()


def test_restore_keeps_current_checkpoint_pointer_and_lock_inode(layout):
    update = plan(layout, fail_health=True)
    inode = update.lock_path.stat().st_ino
    execute = update._execute

    def interrupted(state, phase):
        if phase == "pointer_back":
            assert update.ports.current_release() == "2.0"
            assert update.status()["direction"] == "rollback"
            assert update.status()["index"] == ROLLBACK.index("pointer_back")
            assert update.lock_path.stat().st_ino == inode
            assert (layout[2].roots["data"] / "database.bin").read_bytes() == b"schema-1 original records"
            raise Crash()
        return execute(state, phase)

    update._execute = interrupted
    with pytest.raises(Crash):
        update.run()
    verify_result(layout, plan(layout).run(), rollback=True)
    assert update.lock_path.stat().st_ino == inode


def test_pointer_replace_error_keeps_previous_json(layout, monkeypatch):
    ports = layout[4]()
    ports.stage_release("a" * 32 + "/stage", "2.0")
    original = fs.os.replace

    def fail(source, destination):
        if destination == ports.pointer:
            raise OSError(SECRET)
        return original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(fs.os, "replace", fail)
        with pytest.raises(OSError):
            ports.switch_pointer("a" * 32 + "/switch", "2.0")
    assert ports.current_release() == "1.0"
    assert not list(ports.pointer.parent.glob(".update-*.tmp"))
