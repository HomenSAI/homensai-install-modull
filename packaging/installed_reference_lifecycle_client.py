"""Public installed SDK reference preparation/fresh; без checkout imports."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.request import ProxyHandler, build_opener


def sha(value):
    return hashlib.sha256(value).hexdigest()


def tree(root):
    return {path.relative_to(root).as_posix(): sha(path.read_bytes()) for path in root.rglob("*") if path.is_file()}


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
    observations, actions, legacy_root = [], [], None

    def api(state, *, registered=True):
        if registered and config["profile"] == "fresh":
            return InstallerAPI.from_bootstrap(work / state, trust_config=config["trust"], allow_development=True,
                product_adapter_descriptor="installer/adapter.json", secret_provider=secret, locale="ru")
        engine = NativeEngine(verifier, secret, **({"product_adapter_descriptor": "installer/adapter.json"} if registered else {}))
        return InstallerAPI(work / state, engine=engine, locale="ru")

    def run_action(instance, name, options, key):
        plan = instance.plan(name, manifest, target, options=options)
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
        if config["profile"] == "fresh":
            assert supported, "Этот SDK revision ещё не поддерживает adapter fresh install"
            plan, operation = run_action(current, "install", prepared, "true-adapter-fresh")
            result = operation["result"]
            assert result["health"]["ready"] and result["health"]["product_adapter"]["ready"]
            with sqlite3.connect(root / "data/reference.sqlite") as db:
                assert db.execute("PRAGMA user_version").fetchone() == (1,)
                assert db.execute("SELECT count(*) FROM product_adapter_effects").fetchone() == (1,)
            assert not (root / "data/FORBIDDEN_LEGACY_MIGRATION").exists()
            receipts = list((root / ".hinstall/fresh-receipts").glob("*.json"))
            assert len(receipts) == 1
            owned_pid = json.loads((root / ".hinstall/processes.json").read_text())["web"]["pid"]
            run_action(current, "start", bundles["1"], "fresh-duplicate-start")
            assert json.loads((root / ".hinstall/processes.json").read_text())["web"]["pid"] == owned_pid
            _, backup = run_action(current, "backup", bundles["1"], "fresh-backup")
            assert backup["result"]["archive"]["encrypted"]
            run_action(current, "stop", bundles["1"], "fresh-stop")
            observations.append({"case": "true_adapter_fresh_install", "status": "PASS", "schema": 1,
                "business_effect_rows": 1, "fresh_receipts": 1, "legacy_migration_forbidden_marker_absent": True,
                "owned_pid_stable_on_start": True, "real_encrypted_backup": True, "owned_listener_stopped": True})
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
    finally:
        for cleanup_root, version in ((legacy_root, "1"), (root if config["profile"] == "fresh" else None, "1")):
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
