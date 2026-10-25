# HomenS.AI · © 2026 Serhii Khomenko
"""Source/integration проверки Linux user-session, не Windows/Docker acceptance."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from homensai_install.engine import NativeEngine, EngineError, _birth
from homensai_install import engine as engine_module
from shared import archive_crypto
from shared.archive import build_archive


SERVER = '''import os,json,signal,threading,subprocess,sys
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/healthz':
   ready=not Path('fail_health').exists()
   if Path('expected-secret.txt').exists(): ready=ready and os.environ.get('PRODUCT_TOKEN')==Path('expected-secret.txt').read_text()
   value={"product_id":os.environ['HOMENSAI_PRODUCT_ID'],"installation_id":os.environ['HOMENSAI_INSTALLATION_ID'],"release":os.environ['HOMENSAI_RELEASE'],"ready":ready}
   body=json.dumps(value).encode()
  else: body=b'<html><body>Reference</body></html>'
  self.send_response(200);self.end_headers();self.wfile.write(body)
 def log_message(self,*args): pass
class Server(HTTPServer): allow_reuse_address=True
server=Server(('127.0.0.1',int(os.environ['HOMENSAI_WEB_PORT'])),Handler)
if Path('spawn-child').exists(): subprocess.Popen([sys.executable,'child.py'])
def stop(*args): threading.Thread(target=server.shutdown,daemon=True).start()
signal.signal(signal.SIGTERM,stop)
server.serve_forever(poll_interval=.05)
server.server_close()
'''

CHILD_WRITER = '''import os,time
from pathlib import Path
home=Path(os.environ['HOMENSAI_DATA_HOME'])
(home/'child-pid.txt').write_text(str(os.getpid()))
while True:
 with (home/'child-writes.txt').open('a') as output: output.write('write\\n')
 time.sleep(.05)
'''

MIGRATE = '''import os,sqlite3
from pathlib import Path
if Path('expected-secret.txt').exists(): assert os.environ.get('PRODUCT_TOKEN')==Path('expected-secret.txt').read_text()
p=Path(os.environ['HOMENSAI_DATA_HOME'])/'product.sqlite'
db=sqlite3.connect(p)
db.execute('create table if not exists app_version (v text)')
db.execute('delete from app_version')
db.execute('insert into app_version values (?)',(os.environ['HOMENSAI_RELEASE'],))
db.commit();db.close()
with (Path(os.environ['HOMENSAI_DATA_HOME'])/'migration-runs.txt').open('a') as log:
 log.write(os.environ['HOMENSAI_RELEASE']+'\\n')
'''


class SimulatedCrash(BaseException):
    pass


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux pidfd integration")
class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hinstall-engine-")
        self.base = Path(self.tmp.name)
        self.root = self.base / "installed"
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.manifest = json.loads((Path(__file__).parents[1] / "examples/rent.manifest.json").read_text())
        self.manifest.update(product_id="native-engine-test", product_name="Native engine test")
        self.manifest["paths"] = {name: name for name in ("app", "releases", "data", "backup", "services", "config", "runtime")}
        self.manifest["components"] = [{"name": "web", "cmd": ["python", "server.py"], "after": [],
            "heartbeat": {"file": "health.json", "interval": 1, "timeout": 3},
            "restart": {"delay": 1, "max": 1, "window": 3}}]
        self.manifest["modes"] = [{"name": "local", "components": ["web"]}]
        self.manifest["mutual_exclusion"] = []
        self.manifest["web"].update(port=self.port)
        self.manifest["migrations"]["cmd"] = ["python", "migrate.py"]
        self.manifest["hooks"] = {"pre_start": [], "health_checks": [], "post_update": []}
        self.manifest["secrets"] = []
        self.manifest["update"]["health_wait"] = 2
        self.target = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
        self.trusted = {}
        def verifier(manifest, target, options):
            # Test-only injected trust provider. Real Ed25519 verification has
            # its own tests; engine must not accept options.verified instead.
            supplied = json.loads(Path(options["inventory_file"]).read_text())
            release = options["release"]
            expected = self.trusted[release]
            if supplied != expected or supplied["manifest"] != manifest or supplied["target"] != target:
                raise EngineError("INTEGRITY_FAILED")
            if Path(options["signature_file"]).read_text() != "test-signature:" + release:
                raise EngineError("INTEGRITY_FAILED")
            return supplied["files"]
        self.engine = NativeEngine(verifier, lambda ref: "test-only-strong-archive-password",
                                   development_runtime=sys.executable)
        self.current = None

    def tearDown(self):
        if self.current:
            try:
                self.run_action("stop", self.current)
            except Exception:
                # Only our live Popen handles, never arbitrary recorded PIDs.
                for process in self.engine._children.values():
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
        self.tmp.cleanup()

    def bundle(self, release, *, bad_health=False, child_writer=False, synthetic_secret=None):
        folder = self.base / ("source-" + release)
        folder.mkdir()
        files = {"server.py": SERVER, "migrate.py": MIGRATE,
                 "runtime/python": "development marker; external interpreter is explicitly injected\n",
                 "locks/windows.lock": "test-lock\n", "locks/linux.lock": "test-lock\n",
                 "vendor/test_fixture-1.0-py3-none-any.whl": "test fixture, never production wheel\n"}
        files.update({"legal/" + lang + ".txt": "Test terms " + lang for lang in ("ru", "en", "de")})
        if bad_health:
            files["fail_health"] = "intentional readiness failure\n"
        if child_writer:
            files.update({"spawn-child": "test child writer\n", "child.py": CHILD_WRITER})
        if synthetic_secret is not None:
            files["expected-secret.txt"] = synthetic_secret  # Test fixture only, never a product secret.
        for path, content in files.items():
            dest = folder / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        inventory = {path: hashlib.sha256((folder / path).read_bytes()).hexdigest() for path in files}
        manifest = copy.deepcopy(self.manifest)
        if synthetic_secret is not None:
            manifest["secrets"] = [{"name": "PRODUCT_TOKEN", "required": True,
                                   "contexts": ["service", "admin"], "same_value": True}]
        manifest["runtime"]["lock_files"] = {os_name: {"file": "locks/" + os_name + ".lock", "sha256": inventory["locks/" + os_name + ".lock"]} for os_name in ("windows", "linux")}
        wheel = "vendor/test_fixture-1.0-py3-none-any.whl"
        manifest["runtime"]["vendor_wheels"] = [{"file": wheel, "sha256": inventory[wheel]}]
        self.trusted[release] = {"manifest": manifest, "target": self.target, "files": inventory}
        inv = self.base / (release + ".inventory.json")
        inv.write_text(json.dumps(self.trusted[release]))
        signature = self.base / (release + ".signature.json")
        signature.write_text("test-signature:" + release)
        return {"manifest": manifest, "options": {"install_root": str(self.root), "release": release,
            "payload_dir": str(folder), "runtime_executable": "runtime/python", "modes": ["local"],
            "inventory_file": str(inv), "signature_file": str(signature),
            "eula_digest": inventory["legal/en.txt"], "eula_locale": "en", "backup_password_ref": "test:archive"}}

    def run_action(self, action, bundle, **overrides):
        opts = {**bundle["options"], **overrides}
        plan = {"action": action, "source_manifest": bundle["manifest"], "target": self.target,
                "options": opts, "plan_id": uuid.uuid4().hex}
        result = self.engine.execute(plan)
        if action in ("install", "update", "restore", "repair", "reinstall"):
            self.current = bundle
        return result

    def operation_plan(self, action, bundle, **overrides):
        return {"action": action, "source_manifest": bundle["manifest"], "target": self.target,
                "options": {**bundle["options"], **overrides}, "plan_id": uuid.uuid4().hex}

    def new_engine(self):
        self.engine = NativeEngine(self.engine.verifier, self.engine.secret_provider,
                                   development_runtime=sys.executable)
        return self.engine

    def test_real_fresh_start_backup_uninstall_restore(self):
        bundle = self.bundle("v1")
        result = self.run_action("install", bundle)
        self.assertTrue(result["health"]["ready"])
        user_file = self.root / "data" / "личные документы.txt"
        user_file.write_text("preserve this data", encoding="utf-8")
        record_path = self.root / ".hinstall" / "processes.json"
        pid = json.loads(record_path.read_text())["web"]["pid"]
        self.run_action("start", bundle)
        self.assertEqual(pid, json.loads(record_path.read_text())["web"]["pid"])
        backup = self.run_action("backup", bundle)["archive"]
        self.assertTrue(archive_crypto.is_encrypted(backup["path"]))
        result = self.run_action("uninstall", bundle)
        self.assertTrue(result["data_preserved"])
        self.assertEqual(user_file.read_text(), "preserve this data")
        self.assertFalse((self.root / "releases" / "app-v1").exists())
        result = self.run_action("restore", bundle, archive_path=backup["path"])
        self.assertTrue(result["health"]["ready"])
        self.assertEqual(user_file.read_text(), "preserve this data")

    def test_sequential_updates_and_automatic_rollback(self):
        v1, v2, v3, bad = (self.bundle(name, bad_health=name == "bad") for name in ("v1", "v2", "v3", "bad"))
        self.run_action("install", v1)
        data = self.root / "data" / "user.txt"
        data.write_text("retained")
        self.assertEqual(self.run_action("update", v2)["status"], "SUCCEEDED")
        self.assertEqual(self.run_action("update", v3)["status"], "SUCCEEDED")
        self.assertEqual(self.run_action("update", bad)["status"], "FAILED_ROLLED_BACK")
        self.current = v3
        self.assertEqual(json.loads((self.root / "services" / "active.json").read_text())["release"], "v3")
        self.assertEqual(data.read_text(), "retained")
        self.assertTrue(list((self.base / ".installed-recovery").glob("*.hsac")))

    def test_tampered_payload_rejected_before_claim(self):
        bundle = self.bundle("v1")
        (Path(bundle["options"]["payload_dir"]) / "server.py").write_text("raise SystemExit(0)")
        with self.assertRaises(EngineError) as context:
            self.run_action("install", bundle)
        self.assertEqual(context.exception.code, "INTEGRITY_FAILED")
        self.assertFalse(self.root.exists())

    def test_existing_foreign_directory_never_adopted(self):
        bundle = self.bundle("v1")
        self.root.mkdir()
        foreign = self.root / "foreign.txt"
        foreign.write_text("untouched")
        with self.assertRaises(EngineError):
            self.run_action("install", bundle)
        self.assertEqual(foreign.read_text(), "untouched")

    def test_foreign_process_receipt_not_terminated(self):
        bundle = self.bundle("v1")
        self.run_action("install", bundle)
        record = {"pid": os.getpid(), "birth": _birth(os.getpid()),
                  "installation_id": "1" * 32, "release": "v1"}
        self.assertFalse(self.engine._running(record))
        with self.assertRaises(EngineError) as error:
            self.engine._terminate(record)
        self.assertEqual(error.exception.code, "OPERATION_CONFLICT")

    def test_unknown_verifier_fails_closed(self):
        bundle = self.bundle("v1")
        engine = NativeEngine(development_runtime=sys.executable)
        with self.assertRaises(EngineError) as context:
            engine.execute({"action": "install", "source_manifest": bundle["manifest"],
                            "target": self.target, "options": bundle["options"]})
        self.assertEqual(context.exception.code, "INTEGRITY_FAILED")
        self.assertFalse(self.root.exists())

    def test_repair_same_version_keeps_user_data(self):
        bundle = self.bundle("v1")
        self.run_action("install", bundle)
        self.run_action("stop", bundle)
        user_file = self.root / "data" / "user.txt"
        user_file.write_text("valuable")
        (self.root / "releases" / "app-v1" / "server.py").write_text("corrupt")
        result = self.run_action("repair", bundle)
        self.assertTrue(result["health"]["ready"])
        self.assertEqual(user_file.read_text(), "valuable")

    def test_resume_fresh_after_migration_does_not_repeat_it(self):
        bundle = self.bundle("v1")
        plan = self.operation_plan("install", bundle)
        def crash(event):
            if event["phase"] == "migrate" and event["status"] == "SUCCEEDED":
                raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.engine.execute(plan, emit=crash)
        self.assertFalse((self.root / "services" / "active.json").exists())
        result = self.new_engine().resume(plan, {})
        self.current = bundle
        self.assertTrue(result["health"]["ready"])
        self.assertEqual((self.root / "data" / "migration-runs.txt").read_text(), "v1\n")

    def test_resume_update_after_pointer_still_starts_and_checks_health(self):
        first, second = self.bundle("v1"), self.bundle("v2")
        self.run_action("install", first)
        plan = self.operation_plan("update", second)
        def crash(event):
            if event["phase"] == "activate" and event["status"] == "SUCCEEDED":
                raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.engine.execute(plan, emit=crash)
        self.assertEqual(json.loads((self.root / "services" / "active.json").read_text())["release"], "v2")
        result = self.new_engine().resume(plan, {})
        self.current = second
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["release"], "v2")
        self.assertTrue(self.engine._health(self.engine._context(second["manifest"], self.target, second["options"]))["ready"])
        self.assertEqual((self.root / "data" / "migration-runs.txt").read_text(), "v1\nv2\n")

    def test_cancel_and_resume_same_operation_after_migration(self):
        bundle = self.bundle("v1")
        plan = self.operation_plan("install", bundle)
        cancel = [False]
        def request_cancel(event):
            if event["phase"] == "migrate" and event["status"] == "SUCCEEDED":
                cancel[0] = True
        result = self.engine.execute(plan, emit=request_cancel, cancel_requested=lambda: cancel[0])
        self.assertEqual(result["status"], "CANCELED")
        with self.assertRaises(EngineError) as error:
            self.run_action("install", bundle)
        self.assertEqual(error.exception.code, "RECOVERY_REQUIRED")
        result = self.new_engine().resume(plan, {})
        self.current = bundle
        self.assertTrue(result["health"]["ready"])
        self.assertEqual((self.root / "data" / "migration-runs.txt").read_text(), "v1\n")

    def test_ambiguous_migration_effect_is_never_repeated(self):
        bundle = self.bundle("v1")
        plan = self.operation_plan("install", bundle)
        actual_run = subprocess.run
        def crash_after_effect(argv, **kwargs):
            result = actual_run(argv, **kwargs)
            if "migrate.py" in argv:
                raise SimulatedCrash()
            return result
        with patch("homensai_install.engine.subprocess.run", side_effect=crash_after_effect):
            with self.assertRaises(SimulatedCrash):
                self.engine.execute(plan)
        with self.assertRaises(EngineError) as error:
            self.new_engine().resume(plan, {})
        self.assertEqual(error.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual((self.root / "data" / "migration-runs.txt").read_text(), "v1\n")

    def test_backup_resume_restores_pre_crash_running_state(self):
        bundle = self.bundle("v1")
        self.run_action("install", bundle)
        plan = self.operation_plan("backup", bundle)
        def crash(event):
            if event["phase"] == "stop" and event["status"] == "SUCCEEDED":
                raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.engine.execute(plan, emit=crash)
        result = self.new_engine().resume(plan, {})
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertTrue(self.engine._health(self.engine._context(bundle["manifest"], self.target, bundle["options"]))["ready"])

    def test_late_rollback_requires_acknowledgement_and_keeps_before_restore(self):
        first, second = self.bundle("v1"), self.bundle("v2")
        self.run_action("install", first)
        user_file = self.root / "data" / "user.txt"
        user_file.write_text("snapshot data")
        snapshot = self.run_action("backup", first)["archive"]["path"]
        self.run_action("update", second)
        user_file.write_text("new data")
        with self.assertRaises(EngineError) as error:
            self.run_action("rollback", second, archive_path=snapshot)
        self.assertEqual(error.exception.code, "CONSENT_REQUIRED")
        self.assertEqual(user_file.read_text(), "new data")
        result = self.run_action("rollback", second, archive_path=snapshot, acknowledge_data_loss=True)
        self.current = first
        self.assertTrue(result["health"]["ready"])
        self.assertEqual(user_file.read_text(), "snapshot data")
        self.assertTrue(list((self.base / ".installed-recovery").glob("*-before-restore.hsac")))

    def test_eula_hash_is_bound_to_signed_selected_language(self):
        bundle = self.bundle("v1")
        with self.assertRaises(EngineError) as error:
            self.run_action("install", bundle, eula_digest="0" * 64)
        self.assertEqual(error.exception.code, "CONSENT_REQUIRED")
        self.assertFalse(self.root.exists())

    def test_required_product_secret_is_checked_before_claim(self):
        bundle = self.bundle("v1")
        bundle["manifest"]["secrets"] = [{"name": "PRODUCT_TOKEN", "required": True,
                                            "contexts": ["service", "admin"], "same_value": True}]
        Path(bundle["options"]["inventory_file"]).write_text(json.dumps(self.trusted["v1"]))
        self.engine.secret_provider = None
        with self.assertRaises(EngineError) as error:
            self.run_action("install", bundle)
        self.assertEqual(error.exception.code, "PREREQUISITE_MISSING")
        self.assertFalse(self.root.exists())

    def test_orphan_after_spawn_is_adopted_by_exact_launch_identity(self):
        bundle = self.bundle("v1")
        plan = self.operation_plan("install", bundle)
        actual_write = engine_module._write
        def crash_receipt(path, value, **kwargs):
            if Path(path).name == "processes.json" and any(item.get("status") == "running" for item in value.values()):
                raise SimulatedCrash()
            return actual_write(path, value, **kwargs)
        with patch("homensai_install.engine._write", side_effect=crash_receipt):
            with self.assertRaises(SimulatedCrash):
                self.engine.execute(plan)
        self.current = bundle
        child = next(iter(self.engine._children.values()))
        result = self.new_engine().resume(plan, {})
        self.assertTrue(result["health"]["ready"])
        receipt = json.loads((self.root / ".hinstall" / "processes.json").read_text())["web"]
        self.assertEqual(receipt["pid"], child.pid)
        self.assertEqual(receipt["status"], "running")

    def test_unknown_launch_is_not_blindly_repeated(self):
        bundle = self.bundle("v1")
        plan = self.operation_plan("install", bundle)
        real_popen = subprocess.Popen
        def fail_before_spawn(argv, **kwargs):
            if "server.py" in argv:
                raise SimulatedCrash()
            return real_popen(argv, **kwargs)
        with patch("homensai_install.engine.subprocess.Popen", side_effect=fail_before_spawn):
            with self.assertRaises(SimulatedCrash):
                self.engine.execute(plan)
        with self.assertRaises(EngineError) as error:
            self.new_engine().resume(plan, {})
        self.assertEqual(error.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(self.engine._children, {})

    def test_plaintext_archive_cannot_enter_encrypted_restore(self):
        bundle = self.bundle("v1")
        self.run_action("install", bundle)
        self.run_action("stop", bundle)
        ctx = self.engine._context(bundle["manifest"], self.target, bundle["options"])
        plain = self.base / "plain.zip"
        build_archive(ctx["home"], plain)
        self.run_action("start", bundle)
        pid = json.loads((self.root / ".hinstall" / "processes.json").read_text())["web"]["pid"]
        with self.assertRaises(EngineError) as error:
            self.run_action("restore", bundle, archive_path=str(plain))
        self.assertEqual(error.exception.code, "INTEGRITY_FAILED")
        self.assertEqual(json.loads((self.root / ".hinstall" / "processes.json").read_text())["web"]["pid"], pid)

    def test_tampered_restore_candidate_is_rejected_before_live_changes(self):
        bundle = self.bundle("v1")
        self.run_action("install", bundle)
        user_file = self.root / "data" / "user.txt"
        user_file.write_text("snapshot")
        archive = self.run_action("backup", bundle)["archive"]["path"]
        user_file.write_text("new current data")
        plan = self.operation_plan("restore", bundle, archive_path=archive)
        def crash(event):
            if event["phase"] == "verify_archive" and event["status"] == "SUCCEEDED":
                raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.engine.execute(plan, emit=crash)
        candidate = next((self.base / ".installed-recovery").glob("*-restore-candidate"))
        (candidate / "roots" / "data" / "user.txt").write_text("unexpected substituted data")
        with self.assertRaises(EngineError) as error:
            self.new_engine().resume(plan, {})
        self.assertEqual(error.exception.code, "INTEGRITY_FAILED")
        self.assertEqual(user_file.read_text(), "new current data")

    def test_export_directory_permissions_and_foreign_archive_are_preserved(self):
        bundle = self.bundle("v1")
        self.run_action("install", bundle)
        export = self.base / "exports"
        export.mkdir(mode=0o750)
        existing = export / "existing.hsac"
        existing.write_text("foreign bytes")
        with self.assertRaises(EngineError) as error:
            self.run_action("backup", bundle, archive_path=str(existing))
        self.assertEqual(error.exception.code, "OPERATION_CONFLICT")
        self.assertEqual(existing.read_text(), "foreign bytes")
        result = self.run_action("backup", bundle, archive_path=str(export / "created.hsac"))
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(export.stat().st_mode & 0o777, 0o750)
        self.assertEqual((export / "created.hsac").stat().st_mode & 0o777, 0o600)

    def test_remaining_component_child_blocks_backup_without_killing_it(self):
        bundle = self.bundle("v1", child_writer=True)
        self.run_action("install", bundle)
        child_file = self.root / "data" / "child-pid.txt"
        deadline = time.monotonic() + 3
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        child_pid = int(child_file.read_text())
        parent = json.loads((self.root / ".hinstall" / "processes.json").read_text())["web"]
        child = {**parent, "pid": child_pid, "birth": _birth(child_pid)}
        try:
            with self.assertRaises(EngineError) as error:
                self.run_action("backup", bundle)
            self.assertEqual(error.exception.code, "RECOVERY_REQUIRED")
            self.assertTrue(self.engine._running(child))
            self.assertFalse(list((self.base / ".installed-recovery").glob("*.hsac")))
        finally:
            # Только обнаруженный собственный launch token + kernel birth.
            self.engine._terminate(child)
        self.run_action("stop", bundle)

    def test_stored_secret_refs_survive_restart_and_failed_update_rollback(self):
        first = self.bundle("v1", synthetic_secret="synthetic-first")
        second = self.bundle("bad", bad_health=True, synthetic_secret="synthetic-second")
        first["options"]["secret_refs"] = {"PRODUCT_TOKEN": "product:first"}
        second["options"]["secret_refs"] = {"PRODUCT_TOKEN": "product:second"}
        secrets = {"product:first": "synthetic-first", "product:second": "synthetic-second",
                   "test:archive": "test-only-strong-archive-password"}
        calls = []
        def provider(reference):
            calls.append(reference)
            return secrets.get(reference)
        self.engine.secret_provider = provider
        self.run_action("install", first)
        self.assertEqual(calls.count("product:first"), 1)
        self.run_action("stop", first)
        restarted = copy.deepcopy(first)
        restarted["options"].pop("secret_refs")
        self.assertTrue(self.run_action("start", restarted)["health"]["ready"])
        self.assertEqual(self.run_action("update", second)["status"], "FAILED_ROLLED_BACK")
        self.current = first
        ctx = self.engine._context(first["manifest"], self.target, first["options"])
        self.assertTrue(self.engine._health(ctx)["ready"])


if __name__ == "__main__":
    unittest.main()
