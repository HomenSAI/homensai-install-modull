"""Public installed SDK reference preparation/fresh/update; без checkout imports."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from urllib.request import ProxyHandler, build_opener


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
        if registered and config["profile"] in {"fresh", "update-readiness", "update"}:
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
        if config["profile"] in {"fresh", "update-readiness", "update"}:
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
                    if config.get("fault_profile") == "lost_ack" and target_version == 2:
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
        report["error"] = {"code": code, "type": type(error).__name__}
        if active_update is not None:
            active_update.update(status="FAIL", error_code=code)
        raise
    finally:
        for cleanup_root, version in ((legacy_root, "1"), (root if config["profile"] in {"fresh", "update-readiness", "update"} else None, "1")):
            if cleanup_root is not None and (cleanup_root / ".hinstall/installation.json").exists():
                try:
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
