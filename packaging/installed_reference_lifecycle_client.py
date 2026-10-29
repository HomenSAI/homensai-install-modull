"""Public installed SDK reference preparation/fresh/update; без checkout imports."""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import sys
import time
from urllib.request import ProxyHandler, build_opener


PRE_APPLY_FAULTS = {"tampered_backup", "foreign_backup", "live_data_drift"}
HEALTH_FAULTS = {"candidate_health", "candidate_cleanup"}
MAINTENANCE_PROFILES = {"repair", "reinstall"}


def _own_source_lines(error):
    """Только номера собственных frames: без paths, source text или locals."""
    lines = []
    frame = error.__traceback__
    while frame is not None and len(lines) < 16:
        if frame.tb_frame.f_code.co_filename == __file__:
            number = frame.tb_lineno
            if type(number) is int and 1 <= number <= 10000:
                lines.append(number)
        frame = frame.tb_next
    return lines


def sha(value):
    return hashlib.sha256(value).hexdigest()


def tree(root):
    return {path.relative_to(root).as_posix(): sha(path.read_bytes()) for path in root.rglob("*") if path.is_file()}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def notes(database):
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return {"schema": connection.execute("PRAGMA user_version").fetchone()[0],
                "notes": [list(row) for row in connection.execute("SELECT id,text FROM notes ORDER BY id")]}
    finally:
        connection.close()


def private_recovery_files(paths):
    """Независимая POSIX-регрессия private modes на известных fixture paths."""
    if os.name != "posix":
        return
    for path in paths:
        info, parent = path.lstat(), path.parent.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, "Restored private file требует owned 0600"
        assert stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
        assert stat.S_IMODE(parent.st_mode) == 0o700, "Owned DataHome parent требует 0700"


def listener_stopped(port):
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0, "Owned listener остался после stop"


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    assert sys.flags.isolated and sys.flags.no_user_site and sys.prefix != sys.base_prefix
    checkout, work, site = Path(config["checkout"]), Path(config["work"]), Path(config["site_packages"])
    assert not Path.cwd().is_relative_to(checkout) and not Path(__file__).is_relative_to(checkout)
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.engine import NativeEngine
    from homensai_install.errors import InstallerError
    from homensai_install.trust import load_trust_config
    verifier = load_trust_config(config["trust"], allow_development=True)
    secret = lambda reference: config["archive_password"] if reference == "acceptance.archive" else config["product_secret"]
    target = config["target"]
    manifest = json.loads(Path(config["manifest"]).read_text())
    bundles = {version: json.loads(Path(filename).read_text()) for version, filename in config["bundle_options"].items()}
    root = Path(bundles["1"]["install_root"])
    observations, actions, legacy_root, active_update = [], [], None, None

    def api(state, *, registered=True):
        if registered and config["profile"] in {"fresh", "update-readiness", "update", "recovery", *MAINTENANCE_PROFILES}:
            return InstallerAPI.from_bootstrap(work / state, trust_config=config["trust"], allow_development=True,
                product_adapter_descriptor="installer/adapter.json", secret_provider=secret, locale="ru")
        engine = NativeEngine(verifier, secret, **({"product_adapter_descriptor": "installer/adapter.json"} if registered else {}))
        return InstallerAPI(work / state, engine=engine, locale="ru")

    def run_action(instance, name, options, key, *, reviewed_plan=None):
        plan = reviewed_plan if reviewed_plan is not None else instance.plan(name, manifest, target, options=options)
        operation = instance.submit(plan["plan_id"], plan["plan_digest"],
            [*plan["required_consents"], plan["review_consent_ref"]], idempotency_key=key, background=False)
        actions.append({"action": name, "status": operation["status"],
                        "error_code": (operation.get("error") or {}).get("code")})
        assert operation["status"] == "SUCCEEDED", operation.get("error")
        return plan, operation

    report = {"status": "FAIL", "profile": config["profile"], "observations": observations, "actions": actions}
    try:
        current = api("fresh-sdk-state")
        assert not root.exists()
        review = current.prepare_migration("install", manifest, target, options=bundles["1"])
        assert review["from_release"] is None and review["to_release"] == "reference-v1"
        assert review["product_plan"]["from_version"] == "empty" and review["product_plan"]["to_version"] == "1"
        assert len(review["product_plan"]["steps"]) == 1 and not review["product_plan"]["steps"][0]["backup_required"]
        for name in manifest["paths"]:
            assert (root / name).is_dir() and not list((root / name).iterdir())
        owner = json.loads((root / ".hinstall/installation.json").read_text())
        assert owner["installed"] is False and owner["installation_id"] == review["installation_id"]
        before = tree(root)
        prepared = {**bundles["1"], "migration_review_id": review["migration_review_id"]}
        supported = current.capabilities(target)["actions"]["install"]["supported"]
        if supported:
            planned = current.plan("install", manifest, target, options=prepared)
            assert planned["engine_plan"]["migration_binding"]["migration_review_id"] == review["migration_review_id"]
        else:
            try:
                current.plan("install", manifest, target, options=prepared)
            except InstallerError as error:
                assert error.code == "CAPABILITY_UNSUPPORTED"
            else:
                raise AssertionError("Preparation granted unsupported install")
        assert tree(root) == before, "Обычный plan запустил product hook"
        observations.append({"case": "public_fresh_preparation", "status": "PASS", "from_version": "empty",
            "to_version": "1", "product_roots_empty": True, "legacy_bootstrap": False,
            "normal_plan_has_no_product_effects": True, "adapter_install_capability": supported})
        before = tree(root)
        try:
            current.prepare_migration("install", manifest, target,
                options={**bundles["1"], "migration_review_id": "f" * 64})
        except InstallerError as error:
            assert error.code == "INVALID_REQUEST"
        else:
            raise AssertionError("Caller review ID passed preparation input validation")
        assert tree(root) == before
        observations.append({"case": "caller_review_id_rejected_before_hook", "status": "PASS"})
        if config["profile"] in {"fresh", "update-readiness", "update", "recovery", *MAINTENANCE_PROFILES}:
            assert supported, "Этот SDK revision ещё не поддерживает adapter fresh install"
            for changed, expected_error in (({**prepared, "migration_review_id": "e" * 64}, "PLAN_STALE"),
                    ({**prepared, "fresh_receipt_id": "a" * 64}, "INVALID_REQUEST")):
                before = tree(root)
                try:
                    current.plan("install", manifest, target, options=changed)
                except InstallerError as error:
                    assert error.code == expected_error
                else:
                    raise AssertionError("Caller-controlled proof/review принят")
                assert tree(root) == before
            plan, operation = run_action(current, "install", prepared, "true-adapter-fresh")
            result = operation["result"]
            assert result["health"]["ready"] and result["health"]["product_adapter"]["ready"]
            database = root / "data/reference.sqlite"
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                assert connection.execute("PRAGMA user_version").fetchone() == (1,)
                business_effects = connection.execute("SELECT request_digest,result FROM product_adapter_effects").fetchall()
                assert len(business_effects) == 1
            finally:
                connection.close()
            assert not (root / "data/FORBIDDEN_LEGACY_MIGRATION").exists()
            receipts = list((root / ".hinstall/fresh-receipts").glob("*.json"))
            assert len(receipts) == 1
            fresh_receipt = json.loads(receipts[0].read_text())
            assert receipts[0].stem == sha(canonical(fresh_receipt))
            assert fresh_receipt["kind"] == "fresh_empty" and fresh_receipt["schema_version"] == 1
            assert fresh_receipt["verification_scope"] == "owned_empty_roots_before_first_product_write"
            assert fresh_receipt["binding"]["installation_id"] == review["installation_id"]
            assert fresh_receipt["binding"]["migration_review_id"] == review["migration_review_id"]
            assert fresh_receipt["binding"]["from_release"] is None
            assert fresh_receipt["binding"]["from_version"] == "empty" and fresh_receipt["binding"]["to_version"] == "1"
            assert fresh_receipt["binding"]["plan_digest"] == review["product_plan"]["plan_digest"]
            journals = [json.loads(path.read_text()) for path in (root / ".hinstall/operations").glob("*.json")]
            installed = [state for state in journals if state["action"] == "install"]
            assert len(installed) == 1 and installed[0]["product_adapter_migration_complete"] is True
            assert installed[0]["fresh_receipt_id"] == receipts[0].stem
            assert installed[0]["migration_review_id"] == review["migration_review_id"]
            connection = sqlite3.connect((root / ".hinstall/product-adapter/reference-v1/adapter-invocations.sqlite3").as_uri() + "?mode=ro", uri=True)
            try:
                adapter_effects = connection.execute("SELECT request_digest,result,state FROM calls WHERE hook='migration_apply'").fetchall()
                assert len(adapter_effects) == 1 and adapter_effects[0][2] == "SUCCEEDED"
                assert business_effects[0][0] == adapter_effects[0][0]
                assert json.loads(business_effects[0][1]) == json.loads(adapter_effects[0][1]) == installed[0]["product_adapter_migration_result"]
            finally:
                connection.close()
            assert json.loads((root / "services" / manifest["update"]["pointer_file"]).read_text())["release"] == "reference-v1"
            assert json.loads((root / ".hinstall/installation.json").read_text())["installed"] is True
            run_action(current, "stop", bundles["1"], "fresh-stop-before-start")
            listener_stopped(config["port"])
            run_action(current, "start", bundles["1"], "fresh-start-after-stop")
            owned_pid = json.loads((root / ".hinstall/processes.json").read_text())["web"]["pid"]
            executable = Path("/proc/" + str(owned_pid) + "/exe").resolve()
            assert executable.is_relative_to(root / "releases") and sha(executable.read_bytes()) == config["runtime_sha256"]
            commandline = Path("/proc/" + str(owned_pid) + "/cmdline").read_bytes()
            assert config["product_secret"].encode() not in commandline and config["archive_password"].encode() not in commandline
            run_action(current, "start", bundles["1"], "fresh-duplicate-start")
            assert json.loads((root / ".hinstall/processes.json").read_text())["web"]["pid"] == owned_pid
            with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
                health = json.load(response)
            assert health["installation_id"] == review["installation_id"] and health["release"] == "reference-v1"
            assert health["ready"] and health["database_schema"] == 1 and health["api_key_configured"]
            document = root / "data/user-files/Личные документы.txt"
            document.write_text("Synthetic installed document — сохранить\n", encoding="utf-8")
            credential = root / "data/synthetic-secret.txt"
            credential.write_text(config["product_secret"], encoding="utf-8")
            credential.chmod(0o600)
            settings = root / "config/settings.json"
            value = json.loads(settings.read_text())
            value.update(site_title="Настройки installed lifecycle", language="de")
            settings.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            connection = sqlite3.connect(database)
            try:
                connection.execute("INSERT INTO notes(id,text) VALUES(2,?)", ("  Installed USER note  ",))
                connection.commit()
            finally:
                connection.close()
            data_before = notes(database)
            retained = {"document": sha(document.read_bytes()), "credential": sha(credential.read_bytes()), "settings": sha(settings.read_bytes())}
            _, backup = run_action(current, "backup", bundles["1"], "fresh-backup")
            archive = backup["result"]["archive"]
            assert archive["encrypted"] and archive["includes_recovery_payload"]
            assert sha(Path(archive["path"]).read_bytes()) == archive["sha256"]
            # Независимый installed archive reader — только oracle, не adapter restore.
            from shared import archive_crypto
            assert archive_crypto.is_encrypted(archive["path"])
            candidate = work / "fresh-archive-verification"
            candidate.mkdir(mode=0o700)
            archive_crypto.restore_archive(archive["path"], candidate, password_provider=lambda: config["archive_password"],
                                           expected_product_id=manifest["product_id"])
            restored = candidate / "roots"
            assert notes(restored / "data/reference.sqlite") == data_before
            assert sha((restored / "data/user-files/Личные документы.txt").read_bytes()) == retained["document"]
            assert sha((restored / "data/synthetic-secret.txt").read_bytes()) == retained["credential"]
            assert sha((restored / "config/settings.json").read_bytes()) == retained["settings"]
            assert (restored / "runtime/recovery/reference-v1/payload/runtime/bin/python").is_file()
            assert notes(database) == data_before
            run_action(current, "stop", bundles["1"], "fresh-stop")
            listener_stopped(config["port"])
            for directory in (root / ".hinstall", work / "fresh-sdk-state"):
                for path in directory.rglob("*"):
                    if path.is_file():
                        raw = path.read_bytes()
                        assert config["archive_password"].encode() not in raw and config["product_secret"].encode() not in raw
            observations.append({"case": "true_adapter_fresh_install", "status": "PASS", "schema": 1,
                "business_effect_rows": 1, "fresh_receipts": 1, "legacy_migration_forbidden_marker_absent": True,
                "fresh_receipt_matches_native_journal_and_application_result": True,
                "caller_proof_and_foreign_review_rejected": True,
                "constructor": "InstallerAPI.from_bootstrap trusted descriptor",
                "authorizer": "NativeEngine real fresh receipt verifier, no synthetic authorizer callback",
                "owned_pid_stable_on_start": True, "real_encrypted_backup_restored_as_oracle": True,
                "database_documents_settings_and_synthetic_credential_preserved": True,
                "actual_signed_runtime_sha256": sha(executable.read_bytes()), "secrets_absent_from_argv_and_control_logs": True,
                "owned_listener_stopped": True})
            if config["profile"] in MAINTENANCE_PROFILES:
                action = config["profile"]
                active_update = {"case": "installed_adapter_" + action, "status": "RUNNING"}
                observations.append(active_update)
                run_action(current, "start", bundles["1"], "maintenance-source-start")
                upgrade = current.prepare_migration("update", manifest, target, options=bundles["2"])
                run_action(current, "update", {**bundles["2"], "migration_review_id": upgrade["migration_review_id"]},
                           "maintenance-setup-update-v2")
                document.write_text("Latest maintenance document после backup v1", encoding="utf-8")
                credential.write_text(config["product_secret"] + " synthetic latest retained credential", encoding="utf-8")
                settings_value = json.loads(settings.read_text())
                settings_value.update(site_title="Latest maintenance settings", language="en")
                settings.write_text(json.dumps(settings_value, ensure_ascii=False), encoding="utf-8")
                with sqlite3.connect(database) as db:
                    text = "Latest maintenance note after v1 snapshot"
                    db.execute("INSERT INTO notes(id,text,normalized_text,text_length,category) VALUES(3,?,?,?,?)",
                               (text, text.casefold(), len(text), "general"))
                    migration_rows = db.execute("SELECT * FROM product_adapter_effects ORDER BY effect_key").fetchall()
                    schema_rows = db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
                latest_data = notes(database)
                assert latest_data["schema"] == 2 and latest_data != data_before
                product_ledger = root / "data/reference-adapter.sqlite3"
                latest_files = {"document": sha(document.read_bytes()), "credential": sha(credential.read_bytes()),
                                "settings": sha(settings.read_bytes())}
                meta_path = root / ".hinstall/releases/reference-v2.json"
                original_metadata = json.loads(meta_path.read_text())
                original_owner = json.loads((root / ".hinstall/installation.json").read_text())
                original_refs = original_metadata["secret_refs"]
                ledger = root / ".hinstall/product-adapter/reference-v2/adapter-invocations.sqlite3"
                ledger_info = ledger.lstat()
                ledger_identity = [ledger_info.st_dev, ledger_info.st_ino, ledger_info.st_uid]
                installed_candidates = [p for p in (root / "releases").iterdir() if p.name.endswith("reference-v2")]
                assert len(installed_candidates) == 1
                installed_payload = installed_candidates[0]
                candidate_payload = Path(bundles["2"]["payload_dir"])
                candidate_files = tree(candidate_payload)
                sentinels = [root / "data/FORBIDDEN_DAMAGED_ADAPTER", root / "data/FORBIDDEN_DAMAGED_SERVER"]
                retained_uninstall = None
                if action == "repair":
                    for module, sentinel in zip(("adapter.py", "server.py"), sentinels):
                        paths = list(installed_payload.rglob("homensai_reference/" + module))
                        assert len(paths) == 1
                        paths[0].write_text("from pathlib import Path\nPath(" + repr(str(sentinel)) +
                            ").write_text('synthetic corrupt installed code executed')\nraise SystemExit(89)\n", encoding="utf-8")
                    assert tree(installed_payload) != candidate_files
                else:
                    _, removed = run_action(current, "uninstall", bundles["2"], "maintenance-uninstall-preserve")
                    retained_uninstall = removed["result"]["retained_recovery"]
                    assert removed["result"]["data_preserved"] and removed["result"]["recovery_available"]
                    assert not any((root / "releases").iterdir())
                    listener_stopped(config["port"])
                assert notes(database) == latest_data
                for path, name in ((document, "document"), (credential, "credential"), (settings, "settings")):
                    assert sha(path.read_bytes()) == latest_files[name]
                provider_calls = []
                previous_secret = secret

                def maintenance_secret(reference):
                    assert reference in {"acceptance.archive", *original_refs.values()}, "Incoming credential ref was used"
                    provider_calls.append(reference)
                    return previous_secret(reference)

                secret = maintenance_secret
                current = api("fresh-sdk-state")
                maintenance_options = {**bundles["2"], "secret_refs": {"REFERENCE_API_KEY": "acceptance.forbidden-incoming"}}
                before_plan = tree(root)
                planned = current.plan(action, manifest, target, options=maintenance_options)
                assert tree(root) == before_plan, "Maintenance plan executed a product hook"
                detail = planned["engine_plan"]["code_maintenance"]
                assert detail["kind"] == "same_release_code_maintenance" and detail["action"] == action
                assert detail["release"] == "reference-v2" and detail["installation_id"] == original_owner["installation_id"]
                assert detail["candidate_binding_sha256"] == sha(canonical(detail["candidate_binding"]))
                assert detail["review_sha256"] == sha(canonical({k: v for k, v in detail.items() if k != "review_sha256"}))
                assert detail["ledger_identity"] == ledger_identity and detail["credential_policy"] == "installed_release_refs"
                token = sha(canonical([planned["plan_id"], planned["plan_digest"]]))[:32]
                container = root / ".hinstall/code-maintenance" / token
                closure = container / "candidate/payload"
                from homensai_install.adapters import ExecutableProductAdapter
                original_execute = ExecutableProductAdapter._execute
                executed = []

                def maintenance_observer(adapter, hook, request, request_digest):
                    payload_path = Path(adapter.options["payload_dir"])
                    assert not any(path.exists() for path in sentinels)
                    assert hook not in {"migration_apply", "restore", "post_restore"}, "Code-only maintenance ran a data mutation hook"
                    if hook in {"migration_plan", "quiesce"}:
                        assert payload_path == closure and tree(payload_path) == candidate_files
                        assert sha((payload_path / "runtime/bin/python").read_bytes()) == config["runtime_sha256"]
                        if hook == "quiesce":
                            assert Path(adapter.environment["HOMENSAI_DATA_HOME"]) == root / "data"
                            assert request["idempotency_key"] == "quiesce_" + token
                            assert request["operation_id"] == "op_" + token
                            assert adapter.state_dir == ledger.parent
                            assert json.loads((root / ".hinstall/processes.json").read_text()) == {}
                            listener_stopped(config["port"])
                        else:
                            assert Path(adapter.environment["HOMENSAI_DATA_HOME"]).is_relative_to(container)
                            assert Path(adapter.environment["HOMENSAI_DATA_HOME"]) != root / "data"
                    elif hook == "readiness":
                        assert payload_path == installed_payload and tree(payload_path) == candidate_files
                    executed.append({"hook": hook, "runtime_sha256": sha((payload_path / "runtime/bin/python").read_bytes())})
                    return original_execute(adapter, hook, request, request_digest)

                ExecutableProductAdapter._execute = maintenance_observer
                try:
                    _, maintained = run_action(current, action, maintenance_options, "installed-" + action, reviewed_plan=planned)
                finally:
                    ExecutableProductAdapter._execute = original_execute
                result = maintained["result"]
                assert result["code_only"] is True and "external_actions_on_hold" not in result
                assert not any(path.exists() for path in sentinels)
                assert tree(installed_payload) == candidate_files and tree(candidate_payload) == candidate_files
                assert notes(database) == latest_data
                for path, name in ((document, "document"), (credential, "credential"), (settings, "settings")):
                    assert sha(path.read_bytes()) == latest_files[name]
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    assert db.execute("SELECT * FROM product_adapter_effects ORDER BY effect_key").fetchall() == migration_rows
                    assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == schema_rows == [(1,), (2,)]
                assert json.loads(meta_path.read_text())["secret_refs"] == original_refs
                final_owner = json.loads((root / ".hinstall/installation.json").read_text())
                assert final_owner["installation_id"] == original_owner["installation_id"]
                assert final_owner["installed"] is True and "retained_uninstall" not in final_owner
                info = ledger.lstat()
                assert [info.st_dev, info.st_ino, info.st_uid] == ledger_identity
                proof, locator = result["before_maintenance"], result["before_maintenance_archive"]
                assert proof["kind"] == "before_code_maintenance_backup" and proof["maintenance_action"] == action
                assert proof["receipt_id"] == sha(canonical({k: v for k, v in proof.items() if k != "receipt_id"}))
                assert proof["review_sha256"] == detail["review_sha256"]
                assert proof["candidate_binding_sha256"] == detail["candidate_binding_sha256"]
                assert proof["operation_id"] == "op_" + token and proof["installation_id"] == original_owner["installation_id"]
                assert proof["product_id"] == manifest["product_id"] and proof["from_release"] == "reference-v2"
                assert proof["migration_eligible"] is False and proof["encrypted"] and proof["includes_recovery_payload"]
                assert proof["verification_scope"] == "full_snapshot_and_signed_recovery_payload_restored"
                assert proof["schema_plan"]["from_version"] == proof["schema_plan"]["to_version"] == "2"
                assert proof["schema_plan"]["steps"] == []
                witness = proof["quiesce_witness"]
                assert witness["ledger_identity"] == ledger_identity and witness["result"] == {"quiesced": True, "active_writers": 0}
                assert witness["idempotency_key"] == "quiesce_" + token and witness["operation_id"] == "op_" + token
                with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
                    row = db.execute("SELECT request_digest,operation_id,hook,state,result FROM calls WHERE call_id=?", (witness["call_id"],)).fetchone()
                    assert row[:4] == (witness["request_digest"], "op_" + token, "quiesce", "SUCCEEDED")
                    assert json.loads(row[4]) == witness["result"]
                with sqlite3.connect(product_ledger.as_uri() + "?mode=ro", uri=True) as db:
                    assert db.execute("SELECT request_digest,hook,result FROM effects WHERE effect_key=?", ("quiesce_" + token,)).fetchone()[:2] == (witness["request_digest"], "quiesce")
                assert archive_crypto.is_encrypted(locator["path"])
                assert sha(Path(locator["path"]).read_bytes()) == proof["archive_sha256"] == locator["sha256"]
                oracle = work / ("before-" + action + "-oracle")
                oracle.mkdir(mode=0o700)
                oracle_manifest = archive_crypto.restore_archive(locator["path"], oracle,
                    password_provider=lambda: config["archive_password"], expected_product_id=manifest["product_id"])
                assert sha(canonical(oracle_manifest)) == proof["archive_manifest_sha256"]
                assert set(oracle_manifest["roots"]) == {"data", "backup", "services", "config", "runtime"}
                assert notes(oracle / "roots/data/reference.sqlite") == latest_data
                for filename, name in (("data/user-files/Личные документы.txt", "document"),
                                       ("data/synthetic-secret.txt", "credential"), ("config/settings.json", "settings")):
                    assert sha((oracle / "roots" / filename).read_bytes()) == latest_files[name]
                assert json.loads((oracle / "roots/services" / manifest["update"]["pointer_file"]).read_text()) == {"release": "reference-v2"}
                assert sha((oracle / "roots/runtime/recovery/reference-v2/payload/runtime/bin/python").read_bytes()) == config["runtime_sha256"]
                private_recovery_files((product_ledger, credential))
                with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
                    live = json.load(response)
                assert live["ready"] and live["release"] == "reference-v2" and live["database_schema"] == 2
                assert live["installation_id"] == original_owner["installation_id"] and live["api_key_configured"]
                assert current.status(maintained["operation_id"])["result"] == result
                assert any(item["hook"] == "quiesce" for item in executed) and any(item["hook"] == "readiness" for item in executed)
                assert provider_calls and "acceptance.forbidden-incoming" not in provider_calls
                run_action(current, "stop", bundles["2"], "maintenance-final-stop")
                listener_stopped(config["port"])
                assert not (root / "data/FORBIDDEN_LEGACY_MIGRATION").exists()
                assert not (root / "data/FORBIDDEN_LEGACY_RESTORE").exists()
                active_update.update(status="PASS", public_operation_id=maintained["operation_id"],
                    before_maintenance_receipt=proof, all_five_root_encrypted_archive_restored_as_oracle=True,
                    real_public_factory_and_native_authorizer=True, actual_signed_candidate_hooks=executed,
                    corrupted_installed_adapter_and_server_never_executed=action == "repair",
                    completed_uninstall_then_explicit_valid_candidate=action == "reinstall",
                    latest_schema_documents_settings_credentials_unchanged=True, no_migration_or_reset=True,
                    installed_credential_refs_and_installation_identity_preserved=True,
                    authoritative_ledger_inode_preserved=True, private_modes_verified=os.name == "posix",
                    retained_uninstall_receipt=retained_uninstall, owned_listener_stopped=True)
                active_update = None
            if config["profile"] == "recovery":
                action, fault = config["recovery_action"], config.get("recovery_fault")
                active_update = {"case": "installed_adapter_" + action, "fault": fault, "status": "RUNNING"}
                observations.append(active_update)
                source_snapshot = Path(archive["path"])
                snapshot_digest = sha(source_snapshot.read_bytes())
                run_action(current, "start", bundles["1"], "recovery-source-v1-start")
                update_review = current.prepare_migration("update", manifest, target, options=bundles["2"])
                update_options = {**bundles["2"], "migration_review_id": update_review["migration_review_id"]}
                _, updated = run_action(current, "update", update_options, "recovery-setup-update-v2")
                assert updated["result"]["release"] == "reference-v2" and notes(database)["schema"] == 2
                document.write_text("Новые документы v2 перед recovery", encoding="utf-8")
                credential.write_text(config["product_secret"] + "-v2", encoding="utf-8")
                changed_settings = json.loads(settings.read_text())
                changed_settings.update(site_title="Новые настройки v2 перед recovery", language="en")
                settings.write_text(json.dumps(changed_settings, ensure_ascii=False), encoding="utf-8")
                with sqlite3.connect(database) as db:
                    text = "New v2 note before recovery"
                    db.execute("INSERT INTO notes(id,text,normalized_text,text_length,category) VALUES(3,?,?,?,?)",
                               (text, text.casefold(), len(text), "general"))
                latest_data = notes(database)
                assert latest_data["schema"] == 2 and latest_data != data_before
                latest_files = {"document": sha(document.read_bytes()), "credential": sha(credential.read_bytes()),
                                "settings": sha(settings.read_bytes())}
                assert all(latest_files[name] != retained[name] for name in retained)
                recovery_options = {**bundles["2"], "archive_path": str(source_snapshot), "acknowledge_data_loss": True}
                retained_uninstall = None
                if config.get("uninstall_before_restore"):
                    assert action == "restore" and fault is None
                    before_uninstall_plan = tree(root)
                    uninstall_plan = current.plan("uninstall", manifest, target, options=bundles["2"])
                    assert tree(root) == before_uninstall_plan
                    _, removed = run_action(current, "uninstall", bundles["2"], "recovery-uninstall-preserve",
                                            reviewed_plan=uninstall_plan)
                    listener_stopped(config["port"])
                    assert not list((root / "releases").iterdir())
                    retained_uninstall = removed["result"]["retained_recovery"]
                    assert removed["result"]["data_preserved"] is True and removed["result"]["recovery_available"] is True
                    assert retained_uninstall["source_adapter_binding"] == uninstall_plan["engine_plan"]["product_adapter_binding"]
                    assert retained_uninstall["source_adapter_binding"]["release"] == "reference-v2"
                    witness = retained_uninstall["quiesce_witness"]
                    assert witness["result"] == {"quiesced": True, "active_writers": 0}
                    assert retained_uninstall["receipt_id"] == sha(canonical({k: v for k, v in retained_uninstall.items() if k != "receipt_id"}))
                    owner_after_uninstall = json.loads((root / ".hinstall/installation.json").read_text())
                    assert owner_after_uninstall["installed"] is False
                    uninstall_token = sha(canonical([uninstall_plan["plan_id"], uninstall_plan["plan_digest"]]))[:32]
                    assert owner_after_uninstall["retained_uninstall"] == {
                        "operation_id": "op_" + uninstall_token, "receipt_id": retained_uninstall["receipt_id"]}
                    source_ledger = root / ".hinstall/product-adapter/reference-v2/adapter-invocations.sqlite3"
                    source_ledger_identity = (source_ledger.stat().st_dev, source_ledger.stat().st_ino)
                    assert witness["ledger_identity"][:2] == list(source_ledger_identity)
                    with sqlite3.connect(source_ledger.as_uri() + "?mode=ro", uri=True) as db:
                        witnessed = db.execute("SELECT request_digest,operation_id,hook,state,result FROM calls WHERE call_id=?",
                                               (witness["call_id"],)).fetchone()
                    assert witnessed[:4] == (witness["request_digest"], witness["operation_id"], "quiesce", "SUCCEEDED")
                    assert json.loads(witnessed[4]) == witness["result"]
                    assert notes(database) == latest_data
                    assert sha(document.read_bytes()) == latest_files["document"]
                    assert sha(credential.read_bytes()) == latest_files["credential"]
                    assert sha(settings.read_bytes()) == latest_files["settings"]
                    # The immutable signed fixture inputs remain available for
                    # final byte verification. This public request deliberately
                    # supplies no usable incoming source; observed hook roots
                    # below must be the independently verified private closure.
                    unavailable_source = work / "incoming-payload-intentionally-unavailable"
                    assert not unavailable_source.exists()
                    recovery_options["payload_dir"] = str(unavailable_source)
                    receipt_path = root / ".hinstall/uninstall-receipts" / (uninstall_token + ".json")
                    original_receipt = receipt_path.read_bytes()
                    bad_record = json.loads(original_receipt); bad_record["receipt_id"] = "0" * 64
                    receipt_path.write_text(json.dumps(bad_record))
                    before_bad_plan = tree(root)
                    before_archives = set(work.rglob("*.hsac"))
                    from homensai_install.adapters import ExecutableProductAdapter
                    actual_execute = ExecutableProductAdapter._execute
                    def forbid_unverified_hook(*args, **kwargs):
                        raise AssertionError("Tampered retained receipt executed a hook")
                    ExecutableProductAdapter._execute = forbid_unverified_hook
                    try:
                        try:
                            current.plan("restore", manifest, target, options=recovery_options)
                        except InstallerError as error:
                            assert error.code in {"INTEGRITY_FAILED", "RECOVERY_REQUIRED", "OPERATION_CONFLICT"}
                        else:
                            raise AssertionError("Tampered retained receipt accepted into restore plan")
                        assert tree(root) == before_bad_plan
                        assert set(work.rglob("*.hsac")) == before_archives
                    finally:
                        ExecutableProductAdapter._execute = actual_execute
                        # Explicitly restore only known synthetic fixture bytes.
                        # The successful path below obtains a NEW public review.
                        receipt_path.write_bytes(original_receipt)
                    assert receipt_path.read_bytes() == original_receipt
                    observations.append({"case": "installed_adapter_uninstall_preserve", "status": "PASS",
                        "retained_recovery": retained_uninstall, "owned_release_payloads_removed": True,
                        "latest_business_data_documents_settings_credentials_preserved": True,
                        "tampered_receipt_refused_before_hook_data_or_archive_effect": True,
                        "synthetic_receipt_restored_then_new_review_required": True,
                        "incoming_payload_unavailable": True})
                before_plan = tree(root)
                recovery_plan = current.plan(action, manifest, target, options=recovery_options)
                assert tree(root) == before_plan
                detail = recovery_plan["engine_plan"]
                assert detail["product_adapter_binding"]["release"] == "reference-v2"
                assert detail["recovery_source_sha256"] == snapshot_digest
                assert detail["recovery_failure_policy"] == "preserve_before_recovery_snapshot_no_automatic_retry"
                assert {"restore", "post_restore", "readiness", "quiesce", "operation_status", "migration_plan"} <= set(detail["recovery_target_hooks"])
                consent = [*recovery_plan["required_consents"], recovery_plan["review_consent_ref"]]
                native_token = sha(canonical([recovery_plan["plan_id"], recovery_plan["plan_digest"]]))[:32]
                native_path = root / ".hinstall/operations" / (native_token + ".json")
                from homensai_install.adapters import ExecutableProductAdapter
                original_execute = ExecutableProductAdapter._execute
                injected = []
                retained_source_calls = []
                stage_resume = config.get("recovery_stage_resume", False)
                root_resume = config.get("recovery_root_resume", False) or stage_resume
                resuming_roots = [False]
                root_data_inodes = {}
                stage_at_cut = {}
                stage_intent_at_cut = None
                stage_snapshot_path = work / "recovery-stage-at-cut.json"
                root_trace = work / "recovery-root-hook-trace.jsonl"
                root_before_image = work / "recovery-root-before-image.json"
                if root_resume:
                    assert fault is None and config.get("uninstall_before_restore")
                    assert os.name == "posix" and hasattr(os, "fork")
                    assert not root_trace.is_relative_to(root) and not root_before_image.is_relative_to(root)
                    assert not root_trace.exists() and not root_before_image.exists()

                def data_inodes():
                    return {p.relative_to(root / "data").as_posix(): [p.stat().st_dev, p.stat().st_ino]
                            for p in (root / "data").rglob("*") if p.is_file()}

                def root_image():
                    return {name: {"files": tree(root / name), "directories": sorted(
                        p.relative_to(root / name).as_posix() for p in (root / name).rglob("*") if p.is_dir())}
                            for name in ("data", "backup", "services", "config", "runtime")}

                def node_image(path):
                    info = path.lstat()
                    assert not stat.S_ISLNK(info.st_mode)
                    value = {"identity": [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]}
                    if stat.S_ISREG(info.st_mode):
                        value["sha256"] = sha(path.read_bytes())
                    else:
                        assert stat.S_ISDIR(info.st_mode)
                    return value

                def stage_publication_image():
                    target_code = root / "releases/app-reference-v1"
                    def namespace_image(path):
                        return {p.relative_to(path).as_posix(): node_image(p)
                                for p in [path, *sorted(path.rglob("*"))]}
                    ports_path = Path(stage_intent_at_cut["body"]["ports_receipt"]["path"])
                    metadata = root / ".hinstall/releases"
                    return {"roots": {name: namespace_image(root / name)
                            for name in ("data", "backup", "services", "config", "runtime")},
                        "target_code": namespace_image(target_code),
                        "target_metadata": {suffix: node_image(metadata / ("reference-v1" + suffix))
                            for suffix in (".json", ".inventory.json", ".signature.json")},
                        "target_metadata_directory": node_image(metadata),
                        "ports_receipt": node_image(ports_path)}

                def lose_recovery_ack(adapter, hook, request, request_digest):
                    if root_resume:
                        release = adapter.options["release"]
                        if resuming_roots[0] and release == "reference-v2":
                            raise AssertionError("Source hook attempted against partially restored DataHome")
                        if resuming_roots[0] and release == "reference-v1" and hook == "restore":
                            assert data_inodes() == root_data_inodes, "Completed first root was recopied"
                            if stage_resume:
                                assert stage_publication_image() == stage_at_cut, "Stage adoption rewrote roots, target code, metadata or Ports receipt"
                        entry = {"hook": hook, "release": release, "request_digest": request_digest,
                            "idempotency_key": request["idempotency_key"],
                            "payload_dir": str(adapter.options["payload_dir"]),
                            "phase": "resume" if resuming_roots[0] else "initial"}
                        if stage_resume and resuming_roots[0] and hook == "restore":
                            entry["stage_at_restore_sha256"] = sha(canonical(stage_publication_image()))
                        descriptor = os.open(root_trace, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                        with os.fdopen(descriptor, "a") as stream:
                            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                            stream.flush(); os.fsync(stream.fileno())
                    if config.get("uninstall_before_restore") and adapter.options["release"] == "reference-v2":
                        closure = Path(bundles["2"]["work_dir"]) / (native_token + "-retained-adapter-source")
                        assert Path(adapter.options["payload_dir"]) == closure / "payload"
                        assert sha((closure / "payload/runtime/bin/python").read_bytes()) == config["runtime_sha256"]
                        if hook == "quiesce":
                            assert request["operation_id"] == "op_" + native_token
                            assert request["idempotency_key"] == "quiesce_" + native_token
                            assert request["operation_id"] != retained_uninstall["quiesce_witness"]["operation_id"]
                        retained_source_calls.append({"hook": hook, "request_digest": request_digest})
                    result = original_execute(adapter, hook, request, request_digest)
                    if fault and hook == "restore" and adapter.options["release"] == "reference-v1":
                        assert not injected
                        injected.append(request_digest)
                        raise InstallerError("ADAPTER_TIMEOUT")
                    return result

                if fault or config.get("uninstall_before_restore"):
                    ExecutableProductAdapter._execute = lose_recovery_ack
                try:
                    if root_resume:
                        from shared.adapters_fs import FilesystemPorts
                        actual_reconcile = FilesystemPorts._reconcile
                        child = os.fork()
                        if child == 0:
                            try:
                                def after_real_first_root(source, destination, preserved):
                                    selected = Path(destination) == root / "data" and Path(source).parent.name == "roots"
                                    if selected:
                                        with root_before_image.open("x") as stream:
                                            os.chmod(root_before_image, 0o600)
                                            json.dump(root_image(), stream, ensure_ascii=False, sort_keys=True)
                                            stream.flush(); os.fsync(stream.fileno())
                                    actual_reconcile(source, destination, preserved)
                                    if selected and not stage_resume:
                                        os.kill(os.getpid(), signal.SIGKILL)
                                FilesystemPorts._reconcile = staticmethod(after_real_first_root)
                                if stage_resume:
                                    from homensai_install.recovery_replacement import RootReplacement
                                    def before_real_stage_completion(protocol):
                                        # The engine-wrapped Ports method has
                                        # published both code and metadata.
                                        assert protocol.state["recovery_roots_complete"] is True
                                        assert protocol.state["recovery_target_stage_started"] is True
                                        assert not protocol.state.get("recovery_target_stage_complete")
                                        os.kill(os.getpid(), signal.SIGKILL)
                                    RootReplacement.stage_complete = before_real_stage_completion
                                current.submit(recovery_plan["plan_id"], recovery_plan["plan_digest"],
                                    consent, idempotency_key="installed-recovery", background=False)
                            except BaseException:
                                os._exit(86)
                            os._exit(87)
                        deadline = time.monotonic() + 120
                        while True:
                            waited, child_status = os.waitpid(child, os.WNOHANG)
                            if waited:
                                break
                            if time.monotonic() > deadline:
                                os.kill(child, signal.SIGKILL); os.waitpid(child, 0)
                                raise AssertionError("Owned child did not reach the selected recovery boundary")
                            time.sleep(0.02)
                        assert os.waitstatus_to_exitcode(child_status) == -signal.SIGKILL
                        interrupted_native = json.loads(native_path.read_text())
                        assert interrupted_native["recovery_replace_started"] is True
                        if stage_resume:
                            assert interrupted_native["recovery_roots_complete"] is True
                            assert set(interrupted_native["recovery_root_progress"]) == {"data", "backup", "services", "config", "runtime"}
                            assert all(v == {"started": True, "complete": True}
                                for v in interrupted_native["recovery_root_progress"].values())
                            assert interrupted_native["recovery_target_stage_started"] is True
                            assert not interrupted_native.get("recovery_target_stage_complete")
                            assert "restore" not in interrupted_native["completed"]
                            stage_path = root / ".hinstall/recovery-stage-receipts" / (native_token + ".json")
                            stage_intent_at_cut = json.loads(stage_path.read_text())
                            assert stage_intent_at_cut["receipt_id"] == sha(canonical(stage_intent_at_cut["body"]))
                            assert stage_intent_at_cut["receipt_id"] == interrupted_native["recovery_target_stage_receipt_id"]
                            assert stage_intent_at_cut["body"]["root_receipt_id"] == interrupted_native["recovery_root_receipt_id"]
                            assert stage_intent_at_cut["body"]["operation_id"] == "op_" + native_token
                            assert json.loads(Path(stage_intent_at_cut["body"]["ports_receipt"]["path"]).read_text()) == stage_intent_at_cut["body"]["ports_receipt"]["value"]
                            stage_intent_before = stage_path.read_bytes(), node_image(stage_path)
                            stage_at_cut.update(stage_publication_image())
                            assert not stage_snapshot_path.exists() and not stage_snapshot_path.is_relative_to(root)
                            with stage_snapshot_path.open("x") as stream:
                                os.chmod(stage_snapshot_path, 0o600)
                                json.dump(stage_at_cut, stream, ensure_ascii=False, sort_keys=True)
                                stream.flush(); os.fsync(stream.fileno())
                        else:
                            assert interrupted_native["recovery_root_progress"]["data"] == {"started": True, "complete": False}
                            assert not interrupted_native.get("recovery_roots_complete")
                        marker = json.loads((root / ".hinstall/installation.json").read_text())["pending_recovery"]
                        assert marker == {"operation_id": "op_" + native_token,
                            "receipt_id": interrupted_native["recovery_root_receipt_id"]}
                        before_resume_safety = interrupted_native["step_results"]["before_" + action]
                        before_resume_cipher = Path(before_resume_safety["archive"]["path"])
                        before_resume_cipher_sha = sha(before_resume_cipher.read_bytes())
                        assert before_resume_cipher_sha == before_resume_safety["receipt"]["archive_sha256"]
                        assert notes(database) == data_before and sha(credential.read_bytes()) == retained["credential"]
                        root_data_inodes.update(data_inodes())
                        source_ledger_at_cut = [source_ledger.stat().st_dev, source_ledger.stat().st_ino]
                        assert source_ledger_at_cut == list(source_ledger_identity)
                        initial_trace = [json.loads(line) for line in root_trace.read_text().splitlines()]
                        assert sum(v["release"] == "reference-v2" and v["hook"] == "quiesce" for v in initial_trace) == 1
                        assert not any(v["hook"] in {"restore", "post_restore"} for v in initial_trace)
                        if stage_resume:
                            assert not any(v["release"] == "reference-v1" and v["hook"] in {"restore", "post_restore", "readiness"}
                                           for v in initial_trace)
                        current = api("fresh-sdk-state")
                        matches = [v for v in current.operations_list(unfinished_only=False)["operations"]
                                   if v["plan_id"] == recovery_plan["plan_id"]]
                        assert len(matches) == 1
                        interrupted_id = matches[0]["operation_id"]
                        reopened = current.status(interrupted_id)
                        assert reopened["status"] == "RECOVERY_REQUIRED"
                        resuming_roots[0] = True
                        recovered = current.resume(interrupted_id, {}, background=False)
                        assert recovered["operation_id"] == interrupted_id and recovered["status"] == "SUCCEEDED", recovered
                        resumed_native = json.loads(native_path.read_text())
                        assert resumed_native["step_results"]["before_" + action] == before_resume_safety
                        assert sha(before_resume_cipher.read_bytes()) == before_resume_cipher_sha
                        if stage_resume:
                            assert resumed_native["recovery_target_stage_receipt_id"] == interrupted_native["recovery_target_stage_receipt_id"]
                            assert (stage_path.read_bytes(), node_image(stage_path)) == stage_intent_before
                        assert not json.loads((root / ".hinstall/installation.json").read_text()).get("pending_recovery")
                        final_trace = [json.loads(line) for line in root_trace.read_text().splitlines()]
                        assert not any(v["phase"] == "resume" and v["release"] == "reference-v2" for v in final_trace)
                        assert sum(v["release"] == "reference-v2" and v["hook"] == "quiesce" for v in final_trace) == 1
                        for hook in ("restore", "post_restore"):
                            assert sum(v["release"] == "reference-v1" and v["hook"] == hook for v in final_trace) == 1
                        retained_source_calls[:] = [{"hook": v["hook"], "request_digest": v["request_digest"]}
                            for v in final_trace if v["release"] == "reference-v2"]
                        active_update.update(real_sigkill=True, child_kernel_exit_code=-signal.SIGKILL,
                            child_wait_status=child_status, owned_child_pid=child,
                            original_operation_id=interrupted_id, resumed_with_new_public_factory=True,
                            first_root_files_not_recopied=True, source_quiesce_once=True,
                            no_source_hooks_or_status_on_mixed_data=True, target_restore_and_post_restore_once=True,
                            same_safety_cipher_sha256=before_resume_cipher_sha,
                            durable_hook_trace_sha256=sha(root_trace.read_bytes()),
                            full_before_image_sha256=sha(root_before_image.read_bytes()))
                        if stage_resume:
                            target_restore = [v for v in final_trace if v["release"] == "reference-v1" and v["hook"] == "restore"]
                            assert len(target_restore) == 1 and target_restore[0]["stage_at_restore_sha256"] == sha(canonical(stage_at_cut))
                            active_update.update(recovery_stage_resume=True, cut="after_engine_wrapped_stage_release_before_native_stage_complete",
                                all_five_roots_complete_at_cut=True, no_target_recovery_or_readiness_hooks_before_cut=True,
                                readonly_candidate_schema_diagnostic_preserved=True,
                                all_roots_code_metadata_ports_unchanged_before_target_restore=True,
                                stage_receipt_id=stage_intent_at_cut["receipt_id"], stage_receipt_unchanged=True,
                                stage_at_cut_snapshot_file_sha256=sha(stage_snapshot_path.read_bytes()),
                                stage_at_restore_snapshot_sha256=target_restore[0]["stage_at_restore_sha256"])
                    else:
                        recovered = current.submit(recovery_plan["plan_id"], recovery_plan["plan_digest"],
                            consent, idempotency_key="installed-recovery", background=False)
                finally:
                    ExecutableProductAdapter._execute = original_execute
                operation_id = recovered["operation_id"]
                sdk_ledger = root / ".hinstall/product-adapter/reference-v1/adapter-invocations.sqlite3"
                product_ledger = root / "data/reference-adapter.sqlite3"
                private_recovery_files((product_ledger, credential))
                expected_document = retained["document"]
                if fault:
                    assert injected and recovered["status"] == "RECOVERY_REQUIRED", recovered
                    assert notes(database) == data_before
                    assert json.loads((root / "services" / manifest["update"]["pointer_file"]).read_text()) == {"release": "reference-v2"}
                    with sqlite3.connect(sdk_ledger.as_uri() + "?mode=ro", uri=True) as db:
                        assert db.execute("SELECT request_digest,state FROM calls WHERE hook='restore'").fetchall() == [(injected[0], "UNKNOWN")]
                    with sqlite3.connect(product_ledger.as_uri() + "?mode=ro", uri=True) as db:
                        effects_before = db.execute("SELECT * FROM recovery_effects ORDER BY effect_key").fetchall()
                        assert len(effects_before) == 1 and effects_before[0][2] == "restore" and effects_before[0][-1] == 1
                    listener_stopped(config["port"])
                    # New data written after the committed restore-hook must survive
                    # original-operation resume: never repeat root replacement.
                    document.write_text("Новые пользовательские данные после recovery hook", encoding="utf-8")
                    expected_document = sha(document.read_bytes())
                    before_resume = json.loads(native_path.read_text())
                    if fault == "missing_ledger":
                        removed_ledger_digest = sha(sdk_ledger.read_bytes())
                        sdk_ledger.unlink()
                    current = api("fresh-sdk-state")
                    reopened = current.status(operation_id)
                    assert reopened["status"] == "RECOVERY_REQUIRED"
                    duplicate = current.submit(recovery_plan["plan_id"], recovery_plan["plan_digest"],
                        consent, idempotency_key="installed-recovery", background=False)
                    assert duplicate["operation_id"] == operation_id and duplicate["status"] == "RECOVERY_REQUIRED"
                    recovered = current.resume(operation_id, {}, background=False)
                    assert recovered["operation_id"] == operation_id
                    after_resume = json.loads(native_path.read_text())
                    assert after_resume["step_results"]["before_" + action] == before_resume["step_results"]["before_" + action]
                    if fault == "missing_ledger":
                        assert recovered["status"] == "RECOVERY_REQUIRED" and not sdk_ledger.exists(), recovered
                        with sqlite3.connect(product_ledger.as_uri() + "?mode=ro", uri=True) as db:
                            assert db.execute("SELECT * FROM recovery_effects ORDER BY effect_key").fetchall() == effects_before
                        assert sha(document.read_bytes()) == expected_document and notes(database) == data_before
                        assert sha(credential.read_bytes()) == retained["credential"] and sha(settings.read_bytes()) == retained["settings"]
                        listener_stopped(config["port"])
                    else:
                        assert recovered["status"] == "SUCCEEDED", recovered
                        with sqlite3.connect(sdk_ledger.as_uri() + "?mode=ro", uri=True) as db:
                            assert db.execute("SELECT request_digest,state FROM calls WHERE hook='restore'").fetchall() == [(injected[0], "SUCCEEDED")]
                    active_update.update(original_operation_id=operation_id, reopened_status=reopened["status"],
                        duplicate_status=duplicate["status"], resume_status=recovered["status"],
                        injection="transport_response_lost_after_real_signed_restore_hook", request_digest=injected[0],
                        original_safety_proof_unchanged=True, new_post_hook_document_preserved=True)
                    if fault == "missing_ledger":
                        active_update.update(removed_sdk_ledger_sha256=removed_ledger_digest, ledger_recreated=False)
                else:
                    assert recovered["status"] == "SUCCEEDED", recovered
                if config.get("uninstall_before_restore"):
                    assert any(item["hook"] == "quiesce" for item in retained_source_calls)
                    assert (source_ledger.stat().st_dev, source_ledger.stat().st_ino) == source_ledger_identity
                    assert not (root / "runtime/recovery/reference-v2").exists()
                    final_owner = json.loads((root / ".hinstall/installation.json").read_text())
                    assert final_owner["installed"] is True and "retained_uninstall" not in final_owner
                    active_update.update(uninstall_before_restore=True,
                        retained_source_calls=retained_source_calls, retained_source_closure_verified=True,
                        authoritative_source_ledger_identity_preserved=True, fresh_restore_quiesce_distinct_from_uninstall=True)
                state = json.loads(native_path.read_text())
                safety = state["step_results"]["before_" + action]
                proof = safety["receipt"]
                assert proof["kind"] == "before_recovery_backup" and proof["migration_eligible"] is False
                assert proof["recovery_action"] == action and proof["source_archive_sha256"] == snapshot_digest
                assert proof["from_release"] == "reference-v2" and proof["operation_id"] == "op_" + native_token
                assert proof["installation_id"] == review["installation_id"] and proof["product_id"] == manifest["product_id"]
                source_metadata = json.loads((root / ".hinstall/releases/reference-v2.json").read_text())
                assert proof["from_metadata_sha256"] == sha(canonical(source_metadata))
                assert proof["from_manifest_sha256"] == sha(canonical(source_metadata["manifest"]))
                assert proof["from_inventory_sha256"] == sha(canonical(source_metadata["inventory"]))
                assert proof["consistency_scope"] == "owned_components_stopped_and_adapter_quiesced"
                assert proof["verification_scope"] == "full_snapshot_and_signed_recovery_payload_restored"
                record = json.loads((root / ".hinstall/recovery-receipts" / (proof["receipt_id"] + ".json")).read_text())
                assert proof["receipt_id"] == record["receipt_id"] == sha(canonical(record["body"]))
                assert proof == {"receipt_id": record["receipt_id"], **record["body"]}
                safety_archive = Path(record["archive_path"])
                assert archive_crypto.is_encrypted(safety_archive) and sha(safety_archive.read_bytes()) == proof["archive_sha256"]
                safety_oracle = work / ("before-" + action + "-oracle")
                safety_oracle.mkdir(mode=0o700)
                safety_manifest = archive_crypto.restore_archive(safety_archive, safety_oracle,
                    password_provider=lambda: config["archive_password"], expected_product_id=manifest["product_id"])
                assert sha(canonical(safety_manifest)) == proof["archive_manifest_sha256"]
                if root_resume:
                    assert {p.name for p in (safety_oracle / "roots").iterdir()} == {"data", "backup", "services", "config", "runtime"}
                    expected_roots = json.loads(root_before_image.read_text())
                    for name, expected in expected_roots.items():
                        restored_root = safety_oracle / "roots" / name
                        assert tree(restored_root) == expected["files"]
                        assert sorted(p.relative_to(restored_root).as_posix() for p in restored_root.rglob("*") if p.is_dir()) == expected["directories"]
                    active_update["all_five_safety_roots_match_exact_pre_replace_image"] = True
                assert notes(safety_oracle / "roots/data/reference.sqlite") == latest_data
                for filename, name in (("data/user-files/Личные документы.txt", "document"),
                                       ("data/synthetic-secret.txt", "credential"), ("config/settings.json", "settings")):
                    assert sha((safety_oracle / "roots" / filename).read_bytes()) == latest_files[name]
                assert json.loads((safety_oracle / "roots/services" / manifest["update"]["pointer_file"]).read_text()) == {"release": "reference-v2"}
                previous_runtime = safety_oracle / "roots/runtime/recovery/reference-v2/payload/runtime/bin/python"
                assert sha(previous_runtime.read_bytes()) == config["runtime_sha256"]
                assert sha(source_snapshot.read_bytes()) == snapshot_digest
                assert notes(database) == data_before and sha(document.read_bytes()) == expected_document
                assert sha(credential.read_bytes()) == retained["credential"] and sha(settings.read_bytes()) == retained["settings"]
                private_recovery_files((product_ledger, credential))
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    assert db.execute("SELECT count(*) FROM product_adapter_effects").fetchone() == (1,)
                    assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,)]
                with sqlite3.connect(product_ledger.as_uri() + "?mode=ro", uri=True) as db:
                    effects = db.execute("SELECT effect_key,request_digest,hook,operation_id,binding,result,external_actions_on_hold FROM recovery_effects ORDER BY effect_key").fetchall()
                assert len(effects) == (1 if fault == "missing_ledger" else 2)
                assert {row[2] for row in effects} == ({"restore"} if fault == "missing_ledger" else {"restore", "post_restore"})
                assert all(row[3] == "op_" + native_token and row[-1] == 1 for row in effects)
                for row in effects:
                    binding = json.loads(row[4])
                    assert binding["before_recovery_receipt_id"] == proof["receipt_id"]
                    assert binding["source_archive_sha256"] == snapshot_digest and binding["recovery_action"] == action
                    assert binding["installation_id"] == review["installation_id"] and binding["release"] == "reference-v1"
                assert not (root / "data/FORBIDDEN_LEGACY_MIGRATION").exists()
                assert not (root / "data/FORBIDDEN_LEGACY_RESTORE").exists()
                if fault != "missing_ledger":
                    assert recovered["result"]["health"]["release"] == "reference-v1"
                    assert recovered["result"]["external_actions_on_hold"] is True
                    assert state["status"] == "SUCCEEDED" and state.get("product_adapter_recovery_required") is not True
                    assert json.loads((root / "services" / manifest["update"]["pointer_file"]).read_text()) == {"release": "reference-v1"}
                    with sqlite3.connect(sdk_ledger.as_uri() + "?mode=ro", uri=True) as db:
                        calls = db.execute("SELECT request_digest,hook,result,state FROM calls WHERE hook IN ('restore','post_restore') ORDER BY hook").fetchall()
                    assert len(calls) == 2 and all(row[3] == "SUCCEEDED" for row in calls)
                    assert {(row[0], row[1], canonical(json.loads(row[2]))) for row in calls} == {
                        (row[1], row[2], canonical(json.loads(row[5]))) for row in effects}
                    process = json.loads((root / ".hinstall/processes.json").read_text())["web"]
                    executable = Path("/proc/" + str(process["pid"]) + "/exe").resolve()
                    assert executable.is_relative_to(root / "releases/app-reference-v1")
                    assert sha(executable.read_bytes()) == config["runtime_sha256"]
                    with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
                        health = json.load(response)
                    assert health["ready"] and health["release"] == "reference-v1" and health["database_schema"] == 1
                    assert health["installation_id"] == review["installation_id"]
                    duplicate = current.submit(recovery_plan["plan_id"], recovery_plan["plan_digest"], consent,
                        idempotency_key="installed-recovery", background=False)
                    assert duplicate["operation_id"] == operation_id and duplicate["status"] == "SUCCEEDED"
                    assert sha(document.read_bytes()) == expected_document
                    run_action(current, "stop", bundles["1"], "recovery-final-stop")
                    listener_stopped(config["port"])
                active_update.update(status="PASS", public_status=recovered["status"], public_operation_id=operation_id,
                    source_snapshot_sha256=snapshot_digest, before_recovery_receipt=proof,
                    before_recovery_archive_restored_as_oracle=True, latest_v2_data_preserved_in_safety=True,
                    restored_schema=1, recovery_hook_effect_rows=len(effects), migration_business_effect_rows=1,
                    retained_document_sha256=expected_document, target_hooks_signed_and_held=True,
                    real_native_authorizer=True, legacy_hooks_not_executed=True,
                    restored_private_files_owned_0600_and_datahome_0700=os.name == "posix",
                    factory_plan_submit_and_original_operation_resume=True, automatic_restore_retry=False)
                active_update = None
            if config["profile"] == "update-readiness":
                # Этот профиль доказывает только запрет update. Включение capability
                # требует отдельной полной installed 1→2→3 приёмки и нового профиля.
                assert current.capabilities(target)["actions"]["update"]["supported"] is False, (
                    "Update capability включена: нужна полная installed 1→2→3 приёмка")
                persistent_before = {name: tree(root / name) for name in manifest["paths"]}
                pointer = root / "services" / manifest["update"]["pointer_file"]
                pointer_before = pointer.read_bytes()
                update_review = current.prepare_migration("update", manifest, target, options=bundles["2"])
                assert update_review["from_release"] == "reference-v1" and update_review["to_release"] == "reference-v2"
                product_plan = update_review["product_plan"]
                assert product_plan["from_version"] == "1" and product_plan["to_version"] == "2"
                assert len(product_plan["steps"]) == 1 and product_plan["steps"][0]["backup_required"] is True
                assert {name: tree(root / name) for name in manifest["paths"]} == persistent_before
                assert pointer.read_bytes() == pointer_before and notes(database) == data_before
                rejected = []
                for version, options in (("2", bundles["2"]),
                        ("2-reviewed", {**bundles["2"], "migration_review_id": update_review["migration_review_id"]}),
                        ("3", bundles["3"])):
                    before = tree(root)
                    try:
                        current.plan("update", manifest, target, options=options)
                    except InstallerError as error:
                        assert error.code == "CAPABILITY_UNSUPPORTED", error.code
                        rejected.append({"target": version, "error_code": error.code})
                    else:
                        raise AssertionError("Unsupported update получил исполнимый plan")
                    assert tree(root) == before, "Отклонённый update изменил installation root"
                assert notes(database) == data_before and pointer.read_bytes() == pointer_before
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    assert db.execute("SELECT count(*) FROM product_adapter_effects").fetchone() == (1,)
                listener_stopped(config["port"])
                observations.append({"case": "installed_update_fail_closed", "status": "PASS",
                    "adapter_update_capability": False, "update_lifecycle_status": "NOT RUN",
                    "update_v2_preparation_status": "PASS", "rejected_plans": rejected,
                    "source_release": "reference-v1", "source_schema": 1, "business_effect_rows": 1,
                    "persistent_roots_pointer_and_user_data_unchanged": True,
                    "future_requirement": "Полная installed fresh1→update2→update3 с настоящим backup каждого шага"})
            if config["profile"] == "update":
                run_action(current, "start", bundles["1"], "update-source-v1-start")
                for source_version, target_version in ((1, 2), (2, 3)):
                    active_update = {"case": "true_adapter_update_" + str(source_version) + "_to_" + str(target_version),
                                     "status": "RUNNING"}
                    observations.append(active_update)
                    source_release = "reference-v" + str(source_version)
                    target_release = "reference-v" + str(target_version)
                    source_process = json.loads((root / ".hinstall/processes.json").read_text())["web"]
                    source_executable = Path("/proc/" + str(source_process["pid"]) + "/exe").resolve()
                    assert source_executable.is_relative_to(root / "releases" / ("app-" + source_release))
                    assert sha(source_executable.read_bytes()) == config["runtime_sha256"]
                    with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
                        source_health = json.load(response)
                    assert source_health["installation_id"] == review["installation_id"]
                    assert source_health["release"] == source_release and source_health["database_schema"] == source_version
                    assert source_health["ready"] and source_health["api_key_configured"]
                    source_data = notes(database)
                    assert source_data["schema"] == source_version and source_data["notes"] == data_before["notes"]
                    source_persistent = {name: tree(root / name) for name in manifest["paths"]}
                    update_review = current.prepare_migration("update", manifest, target, options=bundles[str(target_version)])
                    assert update_review["from_release"] == source_release and update_review["to_release"] == target_release
                    product_plan = update_review["product_plan"]
                    assert product_plan["from_version"] == str(source_version) and product_plan["to_version"] == str(target_version)
                    assert len(product_plan["steps"]) == 1 and product_plan["steps"][0]["backup_required"] is True
                    assert {name: tree(root / name) for name in manifest["paths"]} == source_persistent
                    update_options = {**bundles[str(target_version)], "migration_review_id": update_review["migration_review_id"]}
                    before_plan = tree(root)
                    update_plan = current.plan("update", manifest, target, options=update_options)
                    assert tree(root) == before_plan, "Обычный update plan вызвал product hook"
                    detail = update_plan["engine_plan"]
                    assert detail["product_adapter_binding"]["release"] == target_release
                    assert detail["product_adapter_source_binding"]["release"] == source_release
                    assert detail["update_failure_policy"] == "preserve_forward_journal_no_automatic_restore"
                    assert detail["migration_binding"]["migration_review_id"] == update_review["migration_review_id"]
                    if config.get("fault_profile") in PRE_APPLY_FAULTS and target_version == 2:
                        # Faults affect only this disposable fixture, after the real
                        # backup receipt, before the original public operation applies.
                        from homensai_install.migration_receipts import BackupReceiptStore
                        original_emit, original_verify = current._emit, BackupReceiptStore.verify
                        tampered, foreign = [], []

                        def tamper_after_backup(operation_id, event):
                            original_emit(operation_id, event)
                            if event.get("phase") == "backup:0" and event.get("status") == "SUCCEEDED":
                                assert not tampered
                                states = [json.loads(p.read_text()) for p in (root / ".hinstall/operations").glob("*.json")
                                          if json.loads(p.read_text()).get("migration_review_id") == update_review["migration_review_id"]]
                                assert len(states) == 1
                                refs = [r for r in states[0]["product_adapter_backup_refs"].values() if r["purpose"] == "backup:0"]
                                assert len(refs) == 1
                                receipt_path = root / ".hinstall/backup-receipts" / (refs[0]["receipt_id"] + ".json")
                                record = json.loads(receipt_path.read_text())
                                archive_path = Path(record["archive_path"])
                                before_hash = sha(archive_path.read_bytes())
                                assert before_hash == record["body"]["archive_sha256"]
                                if config["fault_profile"] == "tampered_backup":
                                    with archive_path.open("ab") as stream:
                                        stream.write(b"tampered-after-verified-backup")
                                    changed = {"before_sha256": before_hash, "after_sha256": sha(archive_path.read_bytes())}
                                elif config["fault_profile"] == "live_data_drift":
                                    before_document = sha(document.read_bytes())
                                    document.write_text("Синтетические новые данные после проверенного backup", encoding="utf-8")
                                    changed = {"before_sha256": before_document, "after_sha256": sha(document.read_bytes())}
                                else:
                                    changed = {"archive_sha256": before_hash}
                                tampered.append({**changed, "receipt_id": refs[0]["receipt_id"]})

                        def substitute_valid_foreign(store, receipt_id, *, expected_binding):
                            record = store._read(receipt_id)
                            foreign_binding = {**expected_binding, "operation_id": "op_foreign_acceptance"}
                            issued = store.issue(record["archive_path"], foreign_binding,
                                consistency_scope="owned_components_stopped_and_adapter_quiesced")
                            assert issued["receipt_id"] != receipt_id and issued["migration_eligible"] is True
                            # Prove the foreign receipt is valid for its own binding,
                            # then let the real verifier reject it for this operation.
                            assert original_verify(store, issued["receipt_id"], expected_binding=foreign_binding) == issued
                            foreign.append({"original_receipt_id": receipt_id, "foreign_receipt_id": issued["receipt_id"],
                                "archive_sha256": issued["archive_sha256"], "foreign_operation_id": issued["operation_id"],
                                "expected_operation_id": expected_binding["operation_id"], "valid_for_own_binding": True})
                            return original_verify(store, issued["receipt_id"], expected_binding=expected_binding)

                        current._emit = tamper_after_backup
                        if config["fault_profile"] == "foreign_backup":
                            BackupReceiptStore.verify = substitute_valid_foreign
                        try:
                            consent = [*update_plan["required_consents"], update_plan["review_consent_ref"]]
                            failed = current.submit(update_plan["plan_id"], update_plan["plan_digest"],
                                consent, idempotency_key="pre-apply-fault-update", background=False)
                        finally:
                            current._emit = original_emit
                            BackupReceiptStore.verify = original_verify
                        assert len(tampered) == 1
                        if config["fault_profile"] != "foreign_backup":
                            assert tampered[0]["before_sha256"] != tampered[0]["after_sha256"]
                        else:
                            assert len(foreign) == 1
                        assert failed["status"] == "RECOVERY_REQUIRED", failed
                        assert (failed.get("error") or {}).get("code") in {"PLAN_STALE", "RECOVERY_REQUIRED"}, failed
                        assert notes(database) == source_data
                        expected_document = (tampered[0]["after_sha256"] if config["fault_profile"] == "live_data_drift"
                                             else retained["document"])
                        assert sha(document.read_bytes()) == expected_document
                        assert sha(credential.read_bytes()) == retained["credential"]
                        assert sha(settings.read_bytes()) == retained["settings"]
                        assert json.loads((root / "services" / manifest["update"]["pointer_file"]).read_text()) == {"release": source_release}
                        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                            effects = db.execute("SELECT request_digest FROM product_adapter_effects").fetchall()
                            assert len(effects) == 1
                            assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,)]
                        native_paths = [p for p in (root / ".hinstall/operations").glob("*.json")
                                        if json.loads(p.read_text()).get("migration_review_id") == update_review["migration_review_id"]]
                        assert len(native_paths) == 1
                        state = json.loads(native_paths[0].read_text())
                        assert state.get("product_adapter_migration_complete") is not True
                        forward = json.loads((root / "services" / ("update-" + native_paths[0].stem + ".json")).read_text())
                        assert forward["direction"] == "forward" and forward["outcome"] == "running"
                        listener_stopped(config["port"])
                        current = api("fresh-sdk-state")
                        operation_id = failed["operation_id"]
                        reopened = current.status(operation_id)
                        assert reopened["status"] == "RECOVERY_REQUIRED"
                        duplicate = current.submit(update_plan["plan_id"], update_plan["plan_digest"],
                            consent, idempotency_key="pre-apply-fault-update", background=False)
                        assert duplicate["operation_id"] == operation_id and duplicate["status"] == "RECOVERY_REQUIRED"
                        if config["fault_profile"] == "foreign_backup":
                            BackupReceiptStore.verify = substitute_valid_foreign
                        try:
                            resumed = current.resume(operation_id, {}, background=False)
                        finally:
                            BackupReceiptStore.verify = original_verify
                        assert resumed["operation_id"] == operation_id and resumed["status"] == "RECOVERY_REQUIRED", resumed
                        assert notes(database) == source_data
                        assert sha(document.read_bytes()) == expected_document
                        assert sha(credential.read_bytes()) == retained["credential"] and sha(settings.read_bytes()) == retained["settings"]
                        assert json.loads((root / "services" / manifest["update"]["pointer_file"]).read_text()) == {"release": source_release}
                        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                            assert db.execute("SELECT request_digest FROM product_adapter_effects").fetchall() == effects
                            assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,)]
                        after_resume = json.loads(native_paths[0].read_text())
                        assert after_resume["product_adapter_backup_refs"] == state["product_adapter_backup_refs"]
                        assert after_resume.get("product_adapter_migration_complete") is not True
                        listener_stopped(config["port"])
                        active_update.update(status="PASS", expected_failure=True, source_schema=1,
                            business_effect_rows=1, public_operation_id=failed["operation_id"],
                            public_status=failed["status"], error_code=failed["error"]["code"],
                            fault_profile=config["fault_profile"], injection=tampered[0], foreign_receipts=foreign,
                            reopened_status=reopened["status"], duplicate_status=duplicate["status"], resume_status=resumed["status"],
                            same_original_operation_resumed=True, backup_refs_unchanged=True,
                            user_data_preserved=True, preserved_document_sha256=expected_document,
                            migration_apply_not_completed=True, forward_journal_preserved=True)
                        active_update = None
                        break
                    if config.get("fault_profile") in HEALTH_FAULTS and target_version == 2:
                        from homensai_install.engine import EngineError
                        original_health = NativeEngine._health
                        original_quiesce = NativeEngine._update_adapter_quiesce
                        health_injected, cleanup_injected = [], []

                        def fail_after_real_health(engine, ctx):
                            result = original_health(engine, ctx)
                            if ctx.get("action") == "update" and ctx["release"] == target_release:
                                assert not health_injected and result["ready"]
                                document.write_text("Новые данные candidate после реального health", encoding="utf-8")
                                candidate = json.loads((root / ".hinstall/processes.json").read_text())["web"]
                                health_injected.append({"document_sha256": sha(document.read_bytes()),
                                    "real_candidate_ready": True, "candidate_pid": candidate["pid"]})
                                raise EngineError("HEALTH_CHECK_FAILED", "Синтетический installed candidate fault")
                            return result

                        def fail_candidate_cleanup(engine, ctx, key, release):
                            if release == target_release and "/candidate_cleanup:" in key:
                                assert not cleanup_injected
                                cleanup_injected.append({"release": release, "phase_key": key})
                                raise EngineError("OPERATION_CONFLICT", "Синтетический installed cleanup fault")
                            return original_quiesce(engine, ctx, key, release)

                        consent = [*update_plan["required_consents"], update_plan["review_consent_ref"]]
                        key = "candidate-fault-update"
                        NativeEngine._health = fail_after_real_health
                        if config["fault_profile"] == "candidate_cleanup":
                            NativeEngine._update_adapter_quiesce = fail_candidate_cleanup
                        try:
                            failed = current.submit(update_plan["plan_id"], update_plan["plan_digest"],
                                consent, idempotency_key=key, background=False)
                        finally:
                            NativeEngine._health = original_health
                            NativeEngine._update_adapter_quiesce = original_quiesce
                        assert len(health_injected) == 1
                        assert failed["status"] == "RECOVERY_REQUIRED" and failed["error"]["code"] == "HEALTH_CHECK_FAILED", failed
                        operation_id = failed["operation_id"]
                        native_paths = [p for p in (root / ".hinstall/operations").glob("*.json")
                                        if json.loads(p.read_text()).get("migration_review_id") == update_review["migration_review_id"]]
                        assert len(native_paths) == 1
                        state = json.loads(native_paths[0].read_text())
                        assert state["primary_error_code"] == "HEALTH_CHECK_FAILED"
                        assert state["product_adapter_migration_complete"] is True
                        scopes = list(state["product_adapter_quiesce_scopes"].values())
                        if config["fault_profile"] == "candidate_cleanup":
                            assert len(cleanup_injected) == 1 and state["cleanup_error_code"] == "OPERATION_CONFLICT"
                            assert failed["error"]["details"]["primary_error_code"] == "HEALTH_CHECK_FAILED"
                            assert failed["error"]["details"]["cleanup_error"] == {
                                "phase": "candidate_cleanup", "code": "OPERATION_CONFLICT"}
                        else:
                            assert not cleanup_injected and "cleanup_error_code" not in state
                            assert {(x["release"], x["complete"]) for x in scopes} == {(source_release, True), (target_release, True)}
                        after_cleanup = json.loads((root / ".hinstall/processes.json").read_text())
                        if config["fault_profile"] == "candidate_cleanup":
                            assert after_cleanup["web"]["pid"] == health_injected[0]["candidate_pid"]
                            assert after_cleanup["web"]["release"] == target_release
                            executable = Path("/proc/" + str(after_cleanup["web"]["pid"]) + "/exe").resolve()
                            assert executable.is_relative_to(root / "releases" / ("app-" + target_release))
                            assert sha(executable.read_bytes()) == config["runtime_sha256"]
                        else:
                            assert not after_cleanup
                            listener_stopped(config["port"])
                        assert notes(database) == {"schema": 2, "notes": source_data["notes"]}
                        assert sha(document.read_bytes()) == health_injected[0]["document_sha256"]
                        assert sha(credential.read_bytes()) == retained["credential"]
                        assert sha(settings.read_bytes()) == retained["settings"]
                        pointer = root / "services" / manifest["update"]["pointer_file"]
                        assert json.loads(pointer.read_text()) == {"release": target_release}
                        forward_path = root / "services" / ("update-" + native_paths[0].stem + ".json")
                        forward = json.loads(forward_path.read_text())
                        assert forward["direction"] == "forward" and forward["outcome"] == "running"
                        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                            effects_before = db.execute("SELECT request_digest,result FROM product_adapter_effects ORDER BY request_digest").fetchall()
                            assert len(effects_before) == 2
                            assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
                        verified_backups = []
                        for reference in state["product_adapter_backup_refs"].values():
                            record = json.loads((root / ".hinstall/backup-receipts" / (reference["receipt_id"] + ".json")).read_text())
                            assert record["receipt_id"] == sha(canonical(record["body"]))
                            archive_path = Path(record["archive_path"])
                            assert archive_crypto.is_encrypted(archive_path)
                            assert sha(archive_path.read_bytes()) == record["body"]["archive_sha256"]
                            oracle = work / ("candidate-fault-" + reference["purpose"].replace(":", "-") + "-restore")
                            oracle.mkdir(mode=0o700)
                            archive_crypto.restore_archive(archive_path, oracle,
                                password_provider=lambda: config["archive_password"], expected_product_id=manifest["product_id"])
                            assert notes(oracle / "roots/data/reference.sqlite") == source_data
                            assert sha((oracle / "roots/data/user-files/Личные документы.txt").read_bytes()) == retained["document"]
                            verified_backups.append({"purpose": reference["purpose"], "receipt_id": record["receipt_id"],
                                "archive_sha256": record["body"]["archive_sha256"], "restored_source_schema": 1})
                        assert len(verified_backups) == 2 and {r["purpose"] for r in verified_backups} == {"archive", "backup:0"}
                        current = api("fresh-sdk-state")
                        reopened = current.status(operation_id)
                        assert reopened["status"] == "RECOVERY_REQUIRED" and reopened["error"]["code"] == "HEALTH_CHECK_FAILED"
                        duplicate = current.submit(update_plan["plan_id"], update_plan["plan_digest"],
                            consent, idempotency_key=key, background=False)
                        assert duplicate["operation_id"] == operation_id and duplicate["status"] == "RECOVERY_REQUIRED"
                        resumed = current.resume(operation_id, {}, background=False)
                        assert resumed["operation_id"] == operation_id and resumed["status"] == "SUCCEEDED", resumed
                        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                            assert db.execute("SELECT request_digest,result FROM product_adapter_effects ORDER BY request_digest").fetchall() == effects_before
                            assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
                        assert sha(document.read_bytes()) == health_injected[0]["document_sha256"]
                        assert sha(credential.read_bytes()) == retained["credential"] and sha(settings.read_bytes()) == retained["settings"]
                        assert json.loads(pointer.read_text()) == {"release": target_release}
                        completed_state = json.loads(native_paths[0].read_text())
                        assert completed_state["candidate_health_attempt"] == state["candidate_health_attempt"] + 1
                        assert completed_state["product_adapter_backup_refs"] == state["product_adapter_backup_refs"]
                        assert json.loads(forward_path.read_text())["outcome"] == "updated"
                        with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
                            recovered_health = json.load(response)
                        assert recovered_health["release"] == target_release and recovered_health["database_schema"] == 2
                        assert recovered_health["ready"] and recovered_health["installation_id"] == review["installation_id"]
                        run_action(current, "stop", bundles["2"], "candidate-fault-final-stop")
                        listener_stopped(config["port"])
                        active_update.update(status="PASS", expected_failure=True,
                            fault_profile=config["fault_profile"], public_operation_id=operation_id,
                            public_status=failed["status"], primary_error_code=state["primary_error_code"],
                            cleanup_error_code=state.get("cleanup_error_code"), candidate_stopped=not bool(after_cleanup),
                            candidate_pid_before_cleanup=health_injected[0]["candidate_pid"],
                            owned_candidate_identity_preserved_on_cleanup_failure=bool(after_cleanup),
                            reopened_status=reopened["status"], duplicate_status=duplicate["status"], resume_status=resumed["status"],
                            business_effect_rows=2, schema=2, verified_backups=verified_backups,
                            preserved_candidate_document_sha256=health_injected[0]["document_sha256"],
                            injection="failure_after_real_signed_candidate_health" + ("_and_candidate_quiesce_failure" if cleanup_injected else ""),
                            same_original_operation_resumed=True, forward_journal_preserved=True, automatic_restore=False)
                        active_update = None
                        break
                    if config.get("fault_profile") in {"lost_ack", "missing_ledger"} and target_version == 2:
                        # Test-only transport fault, after the real signed subprocess has
                        # committed its SQLite effect and returned a validated response.
                        # Receipt authorization and signature verification remain real.
                        from homensai_install.adapters import ExecutableProductAdapter
                        original_execute = ExecutableProductAdapter._execute
                        injected = []

                        def lose_ack(adapter, hook, request, request_digest):
                            result = original_execute(adapter, hook, request, request_digest)
                            if hook == "migration_apply" and adapter.options["release"] == target_release:
                                assert not injected
                                injected.append(request_digest)
                                raise InstallerError("ADAPTER_EXECUTION_FAILED")
                            return result

                        consent = [*update_plan["required_consents"], update_plan["review_consent_ref"]]
                        key = "true-adapter-update-" + str(target_version)
                        ExecutableProductAdapter._execute = lose_ack
                        try:
                            interrupted = current.submit(update_plan["plan_id"], update_plan["plan_digest"],
                                consent, idempotency_key=key, background=False)
                        finally:
                            ExecutableProductAdapter._execute = original_execute
                        assert len(injected) == 1
                        assert interrupted["status"] == "RECOVERY_REQUIRED", interrupted
                        assert (interrupted.get("error") or {}).get("code") == "RECOVERY_REQUIRED"
                        operation_id = interrupted["operation_id"]
                        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                            effects_before = db.execute("SELECT request_digest,result FROM product_adapter_effects ORDER BY request_digest").fetchall()
                            assert len(effects_before) == 2
                            assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
                        ledger = root / ".hinstall/product-adapter" / target_release / "adapter-invocations.sqlite3"
                        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
                            calls = db.execute("SELECT request_digest,state FROM calls WHERE hook='migration_apply'").fetchall()
                            assert calls == [(injected[0], "UNKNOWN")]
                        native = [json.loads(path.read_text()) for path in (root / ".hinstall/operations").glob("*.json")
                                  if json.loads(path.read_text()).get("migration_review_id") == update_review["migration_review_id"]]
                        assert len(native) == 1 and native[0]["product_adapter_migration_started"] is True
                        assert native[0].get("product_adapter_migration_complete") is not True
                        assert native[0].get("product_adapter_unknown") is True
                        assert notes(database)["notes"] == source_data["notes"]
                        assert sha(document.read_bytes()) == retained["document"]
                        assert sha(credential.read_bytes()) == retained["credential"]
                        assert sha(settings.read_bytes()) == retained["settings"]
                        if config.get("fault_profile") == "missing_ledger":
                            # Only the disposable target adapter ledger is removed;
                            # native/API journals and product SQLite stay untouched.
                            ledger_digest = sha(ledger.read_bytes())
                            ledger.unlink()
                            assert not ledger.exists()
                            current = api("fresh-sdk-state")
                            after_reopen = current.status(operation_id)
                            assert after_reopen["operation_id"] == operation_id
                            assert after_reopen["status"] == "RECOVERY_REQUIRED"
                            assert not ledger.exists()
                            duplicate = current.submit(update_plan["plan_id"], update_plan["plan_digest"],
                                consent, idempotency_key=key, background=False)
                            assert duplicate["operation_id"] == operation_id and duplicate["status"] == "RECOVERY_REQUIRED"
                            assert not ledger.exists()
                            resumed = current.resume(operation_id, {}, background=False)
                            assert resumed["operation_id"] == operation_id and resumed["status"] == "RECOVERY_REQUIRED", resumed
                            assert not ledger.exists()
                            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                                assert db.execute("SELECT request_digest,result FROM product_adapter_effects ORDER BY request_digest").fetchall() == effects_before
                                assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
                            assert notes(database)["notes"] == source_data["notes"]
                            assert sha(document.read_bytes()) == retained["document"]
                            assert sha(credential.read_bytes()) == retained["credential"]
                            assert sha(settings.read_bytes()) == retained["settings"]
                            assert json.loads((root / "services" / manifest["update"]["pointer_file"]).read_text()) == {"release": source_release}
                            native_paths = [p for p in (root / ".hinstall/operations").glob("*.json")
                                            if json.loads(p.read_text()).get("migration_review_id") == update_review["migration_review_id"]]
                            assert len(native_paths) == 1
                            forward = json.loads((root / "services" / ("update-" + native_paths[0].stem + ".json")).read_text())
                            assert forward["direction"] == "forward" and forward["outcome"] == "running"
                            listener_stopped(config["port"])
                            active_update.update(status="PASS", expected_failure=True,
                                case="installed_update_missing_ledger_same_operation", operation_id=operation_id,
                                ledger_before_sha256=ledger_digest, ledger_recreated=False,
                                before_status=interrupted["status"], reopened_status=after_reopen["status"],
                                duplicate_status=duplicate["status"], resume_status=resumed["status"],
                                business_effect_rows=len(effects_before), schema=2,
                                source_notes_sha256=sha(canonical(source_data["notes"])),
                                retained_user_files_sha256=dict(retained), request_digest=injected[0],
                                user_data_preserved=True, forward_journal_preserved=True)
                            active_update = None
                            break
                        duplicate = current.submit(update_plan["plan_id"], update_plan["plan_digest"],
                            consent, idempotency_key=key, background=False)
                        assert duplicate["operation_id"] == operation_id and duplicate["status"] == "RECOVERY_REQUIRED"
                        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                            assert db.execute("SELECT request_digest,result FROM product_adapter_effects ORDER BY request_digest").fetchall() == effects_before
                        updated = current.resume(operation_id, {}, background=False)
                        assert updated["operation_id"] == operation_id and updated["status"] == "SUCCEEDED", updated
                        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                            assert db.execute("SELECT request_digest,result FROM product_adapter_effects ORDER BY request_digest").fetchall() == effects_before
                        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
                            assert db.execute("SELECT request_digest,state FROM calls WHERE hook='migration_apply'").fetchall() == [(injected[0], "SUCCEEDED")]
                        observations.append({"case": "installed_update_lost_ack_same_operation", "status": "PASS",
                            "operation_id": operation_id, "injection": "transport_response_lost_after_real_signed_apply",
                            "before_status": interrupted["status"], "duplicate_status": duplicate["status"],
                            "resume_status": updated["status"], "business_effect_rows": len(effects_before),
                            "adapter_ledger_before": "UNKNOWN", "adapter_ledger_after": "SUCCEEDED",
                            "request_digest": injected[0], "user_data_preserved": True,
                            "real_native_authorizer": True, "automatic_restore": False})
                    else:
                        _, updated = run_action(current, "update", update_options, "true-adapter-update-" + str(target_version),
                                                reviewed_plan=update_plan)
                    assert updated["result"]["release"] == target_release
                    pointer = root / "services" / manifest["update"]["pointer_file"]
                    assert json.loads(pointer.read_text()) == {"release": target_release}
                    assert notes(database) == {"schema": target_version, "notes": source_data["notes"]}
                    assert sha(document.read_bytes()) == retained["document"]
                    assert sha(credential.read_bytes()) == retained["credential"]
                    assert sha(settings.read_bytes()) == retained["settings"]
                    assert not (root / "data/FORBIDDEN_LEGACY_MIGRATION").exists()
                    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                        effects = db.execute("SELECT request_digest,result FROM product_adapter_effects").fetchall()
                        assert len(effects) == target_version
                        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(v,) for v in range(1, target_version + 1)]
                        assert db.execute("SELECT id,normalized_text,text_length,category FROM notes ORDER BY id").fetchall() == [
                            (note_id, " ".join(text.split()).casefold(), len(" ".join(text.split()).casefold()), "general")
                            for note_id, text in source_data["notes"]]
                        if target_version == 3:
                            assert db.execute("SELECT id,content_sha256,is_archived FROM notes ORDER BY id").fetchall() == [
                                (note_id, sha(text.encode()), 0) for note_id, text in source_data["notes"]]
                            assert db.execute("SELECT note_id,term FROM search_terms ORDER BY note_id,term").fetchall() == [
                                (note_id, term) for note_id, text in source_data["notes"]
                                for term in sorted(set(" ".join(text.split()).casefold().split()))]
                    native = [p for p in (root / ".hinstall/operations").glob("*.json")
                              if json.loads(p.read_text()).get("migration_review_id") == update_review["migration_review_id"]]
                    assert len(native) == 1
                    native_path = native[0]
                    state = json.loads(native_path.read_text())
                    assert state["action"] == "update" and state["status"] == "SUCCEEDED"
                    assert state["product_adapter_migration_complete"] is True
                    assert state.get("product_adapter_unknown") is not True
                    forward = json.loads((root / "services" / ("update-" + native_path.stem + ".json")).read_text())
                    assert forward["direction"] == "forward" and forward["outcome"] == "updated"
                    assert forward["old_release"] == source_release and forward["release"] == target_release
                    private_review = json.loads((root / ".hinstall/migration-reviews" / (update_review["migration_review_id"] + ".json")).read_text())
                    ledger = root / ".hinstall/product-adapter" / target_release / "adapter-invocations.sqlite3"
                    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
                        applied = db.execute("SELECT request_digest,result,state FROM calls WHERE hook='migration_apply'").fetchall()
                    assert len(applied) == 1 and applied[0][2] == "SUCCEEDED"
                    assert [(digest, json.loads(value)) for digest, value in effects if digest == applied[0][0]] == [
                        (applied[0][0], json.loads(applied[0][1]))]
                    assert json.loads(applied[0][1]) == state["product_adapter_migration_result"]
                    backup_refs = state["product_adapter_backup_refs"]
                    assert len(backup_refs) == 2 and len({item["receipt_id"] for item in backup_refs.values()}) == 2
                    assert {item["purpose"] for item in backup_refs.values()} == {"archive", "backup:0"}
                    verified_backups = []
                    for phase_key, reference in sorted(backup_refs.items()):
                        assert phase_key.startswith(forward["transaction"] + "/")
                        record_path = root / ".hinstall/backup-receipts" / (reference["receipt_id"] + ".json")
                        record = json.loads(record_path.read_text())
                        body = record["body"]
                        assert record["receipt_id"] == reference["receipt_id"] == sha(canonical(body))
                        assert body["encrypted"] and body["includes_recovery_payload"] and body["migration_eligible"]
                        assert body["verification_scope"] == "encrypted_archive_and_recovery_payload_restored"
                        assert body["consistency_scope"] == "owned_components_stopped_and_adapter_quiesced"
                        assert body["product_id"] == manifest["product_id"] and body["installation_id"] == review["installation_id"]
                        assert body["operation_id"] == "op_" + native_path.stem
                        assert body["from_release"] == source_release and body["from_schema_version"] == str(source_version)
                        assert body["plan_binding_sha256"] == sha(canonical(private_review))
                        source_metadata = json.loads((root / ".hinstall/releases" / (source_release + ".json")).read_text())
                        assert body["from_manifest_sha256"] == sha(canonical(source_metadata["manifest"]))
                        assert body["from_inventory_sha256"] == sha(canonical(source_metadata["inventory"]))
                        archive_path = Path(record["archive_path"])
                        assert archive_crypto.is_encrypted(archive_path) and sha(archive_path.read_bytes()) == body["archive_sha256"]
                        oracle = work / ("update-" + str(target_version) + "-" + reference["purpose"].replace(":", "-") + "-restore")
                        oracle.mkdir(mode=0o700)
                        archive_manifest = archive_crypto.restore_archive(archive_path, oracle,
                            password_provider=lambda: config["archive_password"], expected_product_id=manifest["product_id"])
                        assert sha(canonical(archive_manifest)) == body["archive_manifest_sha256"]
                        saved = oracle / "roots"
                        assert notes(saved / "data/reference.sqlite") == source_data
                        assert sha((saved / "data/user-files/Личные документы.txt").read_bytes()) == retained["document"]
                        assert sha((saved / "data/synthetic-secret.txt").read_bytes()) == retained["credential"]
                        assert sha((saved / "config/settings.json").read_bytes()) == retained["settings"]
                        assert json.loads((saved / "services" / manifest["update"]["pointer_file"]).read_text()) == {"release": source_release}
                        recovery_runtime = saved / "runtime/recovery" / source_release / "payload/runtime/bin/python"
                        assert sha(recovery_runtime.read_bytes()) == config["runtime_sha256"]
                        verified_backups.append({"purpose": reference["purpose"], "receipt_id": reference["receipt_id"],
                            "archive_sha256": body["archive_sha256"], "restored_source_schema": source_version,
                            "backup_receipt_body": body})
                    process = json.loads((root / ".hinstall/processes.json").read_text())["web"]
                    executable = Path("/proc/" + str(process["pid"]) + "/exe").resolve()
                    assert executable.is_relative_to(root / "releases" / ("app-" + target_release))
                    assert sha(executable.read_bytes()) == config["runtime_sha256"]
                    assert process["pid"] != source_process["pid"]
                    commandline = Path("/proc/" + str(process["pid"]) + "/cmdline").read_bytes()
                    assert config["product_secret"].encode() not in commandline and config["archive_password"].encode() not in commandline
                    with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
                        health = json.load(response)
                    assert health["installation_id"] == review["installation_id"] and health["release"] == target_release
                    assert health["database_schema"] == target_version and health["ready"] and health["api_key_configured"]
                    run_action(current, "start", bundles[str(target_version)], "update-duplicate-start-" + str(target_version))
                    assert json.loads((root / ".hinstall/processes.json").read_text())["web"]["pid"] == process["pid"]
                    active_update.update({"status": "PASS", "schema": target_version, "business_effect_rows": len(effects),
                        "source_release": source_release, "target_release": target_release,
                        "source_runtime_sha256": sha(source_executable.read_bytes()),
                        "target_runtime_sha256": sha(executable.read_bytes()), "verified_backups": verified_backups,
                        "migration_review_id": update_review["migration_review_id"],
                        "migration_review_sha256": sha(canonical(private_review)),
                        "native_operation_token": native_path.stem, "native_journal_sha256": sha(canonical(state)),
                        "update_transaction": forward["transaction"], "source_notes_sha256": sha(canonical(source_data["notes"])),
                        "retained_user_files_sha256": dict(retained),
                        "public_factory_prepare_plan_submit": True, "real_native_authorizer": True,
                        "source_and_target_live_health_identity": True,
                        "user_data_and_config_preserved": True, "forward_journal_no_rollback": True,
                        "legacy_migration_marker_absent": True, "owned_pid_stable_on_duplicate_start": True})
                    active_update = None
                if config.get("fault_profile") not in {*PRE_APPLY_FAULTS, *HEALTH_FAULTS, "missing_ledger"}:
                    run_action(current, "stop", bundles["3"], "update-final-stop")
                listener_stopped(config["port"])
                for directory in (root / ".hinstall", work / "fresh-sdk-state"):
                    for path in directory.rglob("*"):
                        if path.is_file():
                            raw = path.read_bytes()
                            assert config["archive_password"].encode() not in raw and config["product_secret"].encode() not in raw
        else:
            legacy_root = work / "Legacy fixture с пробелами"
            def legacy_options(version):
                return {**bundles[version], "install_root": str(legacy_root), "work_dir": str(work / "legacy-recovery")}
            bootstrap = api("legacy-bootstrap-state", registered=False)
            run_action(bootstrap, "install", legacy_options("1"), "explicit-legacy-v1")
            data_before, config_before = tree(legacy_root / "data"), tree(legacy_root / "config")
            upgraded = api("upgrade-preparation-state")
            upgrade = upgraded.prepare_migration("update", manifest, target, options=legacy_options("2"))
            assert upgrade["from_release"] == "reference-v1" and upgrade["to_release"] == "reference-v2"
            assert upgrade["product_plan"]["from_version"] == "1" and upgrade["product_plan"]["to_version"] == "2"
            assert all(step["backup_required"] for step in upgrade["product_plan"]["steps"])
            assert tree(legacy_root / "data") == data_before and tree(legacy_root / "config") == config_before
            with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
                health = json.load(response)
            assert health["release"] == "reference-v1" and health["database_schema"] == 1 and health["ready"]
            try:
                upgraded.prepare_migration("install", manifest, target, options=legacy_options("2"))
            except InstallerError as error:
                assert error.code in {"RECOVERY_REQUIRED", "OPERATION_CONFLICT"}
            else:
                raise AssertionError("Existing installation became fresh")
            observations.append({"case": "public_upgrade_preparation_after_explicit_legacy_bootstrap", "status": "PASS",
                "from_version": "1", "to_version": "2", "database_and_config_unchanged": True,
                "old_reference_http_still_ready": True, "adapter_update_executed": False})
            ledger = root / ".hinstall/product-adapter/reference-v1/adapter-invocations.sqlite3"
            with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
                prior_calls = db.execute("SELECT count(*) FROM calls").fetchone()[0]
            (root / "data/unknown-user-file.txt").write_text("existing data after initial review")
            before = tree(root)
            try:
                current.prepare_migration("install", manifest, target, options=bundles["1"])
            except InstallerError as error:
                assert error.code in {"RECOVERY_REQUIRED", "OPERATION_CONFLICT"}
            else:
                raise AssertionError("Changed fresh-empty reservation was reused")
            with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
                assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == prior_calls
            assert tree(root) == before
            observations.append({"case": "data_added_after_fresh_review_rejected_before_hook", "status": "PASS"})
        unknown_root = work / "Unknown nonempty installation"
        unknown_root.mkdir(mode=0o700)
        (unknown_root / "user-owned.sqlite").write_bytes(b"unrecognized user data")
        before = tree(unknown_root)
        try:
            current.prepare_migration("install", manifest, target,
                options={**bundles["1"], "install_root": str(unknown_root)})
        except InstallerError as error:
            assert error.code in {"RECOVERY_REQUIRED", "OPERATION_CONFLICT"}
        else:
            raise AssertionError("Unknown data accepted as fresh-empty")
        assert tree(unknown_root) == before
        observations.append({"case": "unknown_nonempty_root_rejected", "status": "PASS", "user_data_preserved": True})
        origins = {}
        for name, module in sys.modules.copy().items():
            if name.split(".")[0] in {"homensai_install", "shared", "cryptography", "cffi", "pycparser", "_cffi_backend"}:
                filename = getattr(module, "__file__", None)
                if filename:
                    path = Path(filename).resolve()
                    assert path.is_relative_to(site), name
                    origins[name] = path.relative_to(site).as_posix()
        report.update(status="PASS", module_origins=origins)
    except Exception as error:
        code = error.code if isinstance(error, InstallerError) else "HARNESS_CHECK_FAILED"
        report["error"] = {"code": code, "type": type(error).__name__, "own_source_lines": _own_source_lines(error)}
        report["primary_last_action"] = dict(actions[-1]) if actions else None
        if active_update is not None:
            active_update.update(status="FAIL", error_code=code)
        raise
    finally:
        for cleanup_root, version in ((legacy_root, "1"), (root if config["profile"] in {"fresh", "update-readiness", "update", "recovery", *MAINTENANCE_PROFILES} else None, "1")):
            if cleanup_root is not None and (cleanup_root / ".hinstall/installation.json").exists():
                try:
                    if (config.get("fault_profile") in {*PRE_APPLY_FAULTS, "missing_ledger"}
                            or config.get("recovery_fault") == "missing_ledger") and cleanup_root == root:
                        listener_stopped(config["port"])
                        continue
                    run_action(api("cleanup-" + str(len(str(cleanup_root))), registered=False), "stop",
                        {**bundles[version], "install_root": str(cleanup_root),
                         "work_dir": str(work / "legacy-recovery") if cleanup_root == legacy_root else bundles[version]["work_dir"]},
                        "cleanup-owned-root")
                except Exception as error:
                    report["cleanup_error"] = {"code": "OWNED_ROOT_STOP_FAILED", "type": type(error).__name__}
                    report["status"] = "FAIL"
        text = json.dumps(report, ensure_ascii=False, indent=2)
        assert config["archive_password"] not in text and config["product_secret"] not in text
        Path(config["client_report"]).write_text(text + "\n", encoding="utf-8")
    assert report["status"] == "PASS", report.get("cleanup_error")
    print(json.dumps({"status": report["status"], "profile": config["profile"], "cases": len(observations)}))


if __name__ == "__main__":
    main()
