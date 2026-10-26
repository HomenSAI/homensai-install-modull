"""Installed adapter transport: настоящий nonzero до/после product COMMIT.

Trusted authorizer здесь синтетический exact-request oracle. Это отдельный
transport/hook scope, не proof настоящего engine fresh/update или архива.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tree(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    site = Path(config["site_packages"])
    assert sys.flags.isolated and not Path.cwd().is_relative_to(Path(config["checkout"]))
    from homensai_install.adapters import ExecutableProductAdapter
    from homensai_install.errors import InstallerError
    from homensai_install.trust import load_trust_config
    assert inspect.signature(ExecutableProductAdapter).parameters["migration_authorizer"].default is None
    options = json.loads(Path(config["options"]).read_text())
    manifest = json.loads(Path(config["manifest"]).read_text())
    verifier = load_trust_config(config["trust"], allow_development=True)
    work = Path(config["work"])
    observations = []

    def adapter(case, authorizer=None):
        return ExecutableProductAdapter(manifest, config["target"], options, verifier=verifier,
            state_dir=work / ("sdk-ledger-" + case), environment={
                "HOMENSAI_DATA_HOME": str(work / ("data-" + case)),
                "HOMENSAI_CONFIG_HOME": str(work / ("config-" + case))}, migration_authorizer=authorizer)

    def prepare(instance, case):
        plan = instance.invoke("migration_plan", {"operation_id": "op_plan_" + case,
            "idempotency_key": "plan-" + case, "phase": "diagnostic", "data": {
                "installation_id": "instance-" + case, "from_release": "reference-v1",
                "to_release": "reference-v2", "mode": "upgrade"}})["result"]
        return {"operation_id": "op_" + case, "idempotency_key": "apply-" + case, "phase": "execute",
            "data": {"plan_digest": plan["plan_digest"], "backup_receipt_id": "a" * 64,
                "migration_binding": {"migration_review_id": "synthetic-review-" + case, "binding_sha256": "b" * 64,
                    "installation_id": "instance-" + case, "from_release": "reference-v1", "to_release": "reference-v2",
                    "from_version": "1", "to_version": "2"}}}

    denied = adapter("denied")
    denied_before = tree(work / "data-denied")
    request = prepare(denied, "denied")
    try:
        denied.invoke("migration_apply", request)
    except InstallerError as error:
        assert error.code == "CAPABILITY_UNSUPPORTED"
    else:
        raise AssertionError("Default migration authorizer did not deny")
    assert tree(work / "data-denied") == denied_before
    observations.append({"case": "default_deny", "status": "PASS", "product_writes": 0})

    pre = adapter("pre")
    pre_before = tree(work / "data-pre")
    request = prepare(pre, "pre")
    pre = adapter("pre", lambda supplied: supplied == request)
    try:
        pre.invoke("migration_apply", request)
    except InstallerError as error:
        assert error.code == "ADAPTER_EXECUTION_FAILED"
    else:
        raise AssertionError("Injected pre-effect nonzero was accepted")
    assert pre.reconcile(request["idempotency_key"])["outcome"] == "unknown"
    try:
        pre.invoke("migration_apply", request)
    except InstallerError as error:
        assert error.code == "RECOVERY_REQUIRED"
    else:
        raise AssertionError("UNKNOWN pre-effect invocation repeated")
    assert tree(work / "data-pre") == pre_before
    observations.append({"case": "actual_subprocess_exit_before_effect", "status": "PASS",
                         "outcome": "unknown", "product_writes": 0, "exit_code_injected": 78})

    post = adapter("post")
    request = prepare(post, "post")
    post = adapter("post", lambda supplied: supplied == request)
    try:
        post.invoke("migration_apply", request)
    except InstallerError as error:
        assert error.code == "ADAPTER_EXECUTION_FAILED"
    else:
        raise AssertionError("Injected lost ACK nonzero was accepted")
    data = work / "data-post"
    with sqlite3.connect(data / "reference.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM product_adapter_effects").fetchone() == (1,)
    before = tree(data)
    reconciled = post.reconcile(request["idempotency_key"])
    assert reconciled["outcome"] == "applied" and reconciled["result"]["state_version"] == "2"
    # Новый controller с default-deny всё равно может читать завершённый replay.
    replayed = adapter("post").invoke("migration_apply", request)
    assert replayed["replayed"] and replayed["result"] == reconciled["result"] and tree(data) == before
    changed = json.loads(canonical(request))
    changed["idempotency_key"] = "foreign-backup"
    changed["data"]["backup_receipt_id"] = "c" * 64
    try:
        post.invoke("migration_apply", changed)
    except InstallerError as error:
        assert error.code == "RECOVERY_REQUIRED"
    else:
        raise AssertionError("Foreign receipt passed exact-request authorizer")
    assert tree(data) == before
    observations.append({"case": "actual_subprocess_exit_after_business_commit", "status": "PASS",
        "outcome": "applied", "exit_code_injected": 79, "schema": 2, "business_effect_rows": 1,
        "completed_replay_default_deny_controller": True, "foreign_receipt_denied_before_product_mutation": True})
    module_origins = {}
    for name, module in sys.modules.copy().items():
        if name.split(".")[0] in {"homensai_install", "shared", "cryptography", "cffi", "pycparser", "_cffi_backend"}:
            filename = getattr(module, "__file__", None)
            if filename:
                path = Path(filename).resolve()
                assert path.is_relative_to(site), name
                module_origins[name] = path.relative_to(site).as_posix()
    result = {"status": "PASS", "pid": os.getpid(), "observations": observations,
              "module_origins": module_origins, "authorizer": "SYNTHETIC exact-request constructor fixture",
              "not_run": ["Engine fresh/update", "Real backup receipt authorization", "Process kill/power loss"]}
    Path(config["client_report"]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "cases": len(observations)}))


if __name__ == "__main__":
    main()
