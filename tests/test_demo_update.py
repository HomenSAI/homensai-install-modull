"""Файловая транзакция с настоящей синтетической миграцией SQLite.

Службы и health подменены; это не приёмка платформенного установщика.
"""

from hashlib import sha256
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from shared.adapters_fs import FilesystemPorts, ReleaseSource
from shared import archive_crypto
from shared.datahome import resolve_datahome
from shared.manifest import load_manifest, validate_manifest
from shared.update_plan import UpdateError, UpdatePlan, operation_lock

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples/demo_product"


def invoke(executable, data, *args):
    return subprocess.run([sys.executable, "-B", str(executable), "migrate",
                           "--data-dir", str(data), *args],
                          capture_output=True, text=True, timeout=15, check=True)


def entries(db):
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT text FROM entries ORDER BY id")]


class Effects:
    def __init__(self, home, *, fail_health, corrupt_archive=False):
        self.home = home
        self.fail_health = fail_health
        self.corrupt_archive = corrupt_archive
        self.ports = None
        self.calls = []

    def stop(self, key, components):
        self.calls.append(key.split("/")[-1])

    def migrate(self, key, migration, command):
        self.calls.append("migrate")
        invoke(self.home.releases / "app-2" / "main.py", self.home.roots["data"],
               "--migration", migration)

    def start(self, key, components, pre_start):
        self.calls.append(key.split("/")[-1])

    def health(self, key, url, timeout, checks):
        phase = key.split("/")[-1]
        self.calls.append(phase)
        if phase == "health" and self.fail_health:
            if self.corrupt_archive:
                path = self.ports.archive_path(key.split('/')[0] + '/archive')
                ciphertext = bytearray(path.read_bytes())
                ciphertext[-1] ^= 1
                path.write_bytes(ciphertext)
            raise RuntimeError("synthetic health failure")

    def post_update(self, key, hooks):
        self.calls.append("post_update")


@pytest.mark.parametrize("signed_release,encrypted,fail_health,corrupt", [
    (False, False, False, False), (False, False, True, False),
    (True, False, False, False), (True, False, True, False),
    (True, True, False, False), (True, True, True, False), (True, True, True, True),
])
def test_real_demo_migration_update_and_rollback(tmp_path, signed_release, encrypted, fail_health, corrupt):
    source = tmp_path / "source"
    subprocess.run([sys.executable, "-B", str(DEMO / "build_releases.py"), str(source)],
                   capture_output=True, text=True, timeout=15, check=True)
    synthetic = {"locks/windows.lock": b"synthetic windows lock",
                 "locks/linux.lock": b"synthetic linux lock",
                 "vendor/demo-0-py3-none-any.whl": b"synthetic wheel placeholder"}
    payloads = {}
    for version in ("1", "2"):
        path = source / ("app-" + version)
        for name, data in synthetic.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        hashes = {p.relative_to(path).as_posix(): sha256(p.read_bytes()).hexdigest()
                  for p in path.rglob("*") if p.is_file()}
        payloads[version] = ReleaseSource(path, hashes)

    manifest = load_manifest(ROOT / "examples/rent.manifest.json")
    manifest["product_name"] = "Синтетический тест обновления"
    manifest["product_id"] = "homensai-test-demo"
    manifest["runtime"]["lock_files"] = {
        name: {"file": "locks/" + name + ".lock", "sha256": sha256(synthetic["locks/" + name + ".lock"]).hexdigest()}
        for name in ("windows", "linux")}
    wheel = "vendor/demo-0-py3-none-any.whl"
    manifest["runtime"]["vendor_wheels"] = [{"file": wheel, "sha256": sha256(synthetic[wheel]).hexdigest()}]
    if signed_release:
        from shared.host_target import HostTargetError, native_target
        try:
            manifest["compatibility"] = {"targets": [native_target()]}
        except HostTargetError:
            pytest.skip("Среда процесса не входит в нативный профиль")
    validate_manifest(manifest)
    if signed_release:
        ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
        ser = pytest.importorskip("cryptography.hazmat.primitives.serialization")
        from shared.package import build_package
        from shared.package_prepare import prepare_native_signed_package
        from shared.package_signing import sign_package
        private = ed.Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
        package = tmp_path / "release-2.zip"
        build_package(manifest, "2", source / "app-2", package)
        envelope = sign_package(package, private_key=private,
                                expected_product_id=manifest["product_id"], expected_version="2")
        prepared = tmp_path / "verified-release-2"
        receipt = prepare_native_signed_package(package, envelope, prepared, trusted_public_key=public,
                                                expected_product_id=manifest["product_id"], expected_version="2")
        assert receipt["process_target"] == manifest["compatibility"]["targets"][0]
        payloads["2"] = ReleaseSource(prepared, receipt["files"])
    installation = tmp_path / "installation"
    home = resolve_datahome(manifest, installation)
    for directory in (home.app, home.releases, *home.roots.values()):
        directory.mkdir(parents=True, exist_ok=True)
    effects = Effects(home, fail_health=fail_health, corrupt_archive=corrupt)
    password = "synthetic-test-password-not-a-secret"
    password_provider = (lambda: password) if encrypted else None
    ports = FilesystemPorts(manifest, installation, work_dir=tmp_path / "work",
                            payloads=payloads, effects=effects, allow_unencrypted=not encrypted,
                            password_provider=password_provider)
    effects.ports = ports
    with operation_lock(home.roots["services"] / "operation.lock"):
        ports.stage_release("0" * 32 + "/stage", "1")
        ports.switch_pointer("0" * 32 + "/switch", "1")
    invoke(ports.release_path("1") / "main.py", home.roots["data"])
    settings = home.roots["data"] / "settings.json"
    settings.write_text('{"display_name":"Example","locale":"de"}\n', encoding="utf-8")

    update = UpdatePlan(manifest, installation, ports, release="2", modes=("platform",),
                        migration_ids=("schema-2",))
    if corrupt:
        with pytest.raises(UpdateError):
            update.run()
        # Повреждённый архив не должен частично восстановить или удалить данные.
        assert ports.current_release() == "2"
        assert entries(home.roots["data"] / "demo.sqlite3") == ["synthetic-schema-1", "synthetic-schema-2"]
        assert json.loads(settings.read_text(encoding="utf-8"))["locale"] == "de"
        assert 'rollback_health' not in effects.calls
        return
    result = update.run()
    assert result["outcome"] == ("rolled_back" if fail_health else "updated")
    assert ports.current_release() == ("1" if fail_health else "2")
    expected = ["synthetic-schema-1"] + ([] if fail_health else ["synthetic-schema-2"])
    assert entries(home.roots["data"] / "demo.sqlite3") == expected
    assert json.loads(settings.read_text(encoding="utf-8"))["locale"] == "de"

    @contextmanager
    def read_snapshot(path):
        if encrypted:
            assert archive_crypto.is_encrypted(path)
            assert password.encode() not in path.read_bytes()
            assert b'synthetic-schema-1' not in path.read_bytes()
            with archive_crypto.decrypted_archive(path, password_provider=password_provider,
                                                   expected_product_id=manifest['product_id']) as (plain, _):
                with zipfile.ZipFile(plain) as archive:
                    yield archive
        else:
            with zipfile.ZipFile(path) as archive:
                yield archive

    before_migration = ports.archive_path(result["transaction"] + "/backup:0")
    with read_snapshot(before_migration) as archive:
        original_db = tmp_path / "before-migration.sqlite3"
        original_db.write_bytes(archive.read("roots/data/demo.sqlite3"))
    assert entries(original_db) == ["synthetic-schema-1"]
    if fail_health:
        snapshot = ports.archive_path(result["transaction"] + "/before_rollback")
        with read_snapshot(snapshot) as archive:
            migrated_db = tmp_path / "before-rollback.sqlite3"
            migrated_db.write_bytes(archive.read("roots/data/demo.sqlite3"))
        assert entries(migrated_db) == ["synthetic-schema-1", "synthetic-schema-2"]
        assert "rollback_health" in effects.calls
    if encrypted:
        assert archive_crypto.is_encrypted(ports.archive_path(result["transaction"] + "/archive"))
        assert not list((tmp_path / 'work').glob('*.zip'))
        assert not list((tmp_path / 'work').glob('.archive-crypto-*'))
        assert not list((tmp_path / 'work').glob('.archive-build-*'))
        assert not list((tmp_path / 'work').glob('.restore-*'))
