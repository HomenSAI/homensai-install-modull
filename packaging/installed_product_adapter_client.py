"""Независимый клиент установленного SDK; запускается только вне checkout.

Lifecycle проходит через InstallerAPI. NativeEngine и verifier — явные trusted
порты хоста, выбранные до получения пользовательского плана. Legacy archive
reader используется только независимым oracle для проверки восстановимых байтов.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from urllib.request import ProxyHandler, build_opener


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def install_guard(config):
    """Даже прямой доступ к checkout и внешней сети блокируется до эффекта."""
    checkout, work = Path(config["checkout"]).resolve(), Path(config["work"]).resolve()
    denied = []

    def reject(event):
        denied.append(event)
        raise PermissionError("installed acceptance guard: " + event)

    def audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            if Path(os.fsdecode(args[0])).resolve().is_relative_to(checkout):
                reject("checkout_read")
        elif event in {"socket.connect", "socket.getaddrinfo"}:
            address = args[1] if event == "socket.connect" else args[0]
            host = address[0] if isinstance(address, tuple) else address
            if host not in {"127.0.0.1", "localhost", "::1"}:
                reject("external_network")
        elif event == "subprocess.Popen":
            executable = Path(os.fsdecode(args[0])).absolute()
            if not executable.is_relative_to(work / "Установка с пробелами/releases"):
                reject("unowned_subprocess")
        elif event in {"os.system", "os.exec", "os.posix_spawn"}:
            reject("unowned_subprocess")

    sys.addaudithook(audit)
    return denied


def origins(site, checkout):
    result = {}
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] not in {"homensai_install", "shared", "cryptography", "cffi", "pycparser", "_cffi_backend"}:
            continue
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            assert path.is_relative_to(site) and not path.is_relative_to(checkout), name
            result[name] = str(path.relative_to(site))
    return result


def database(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return {"schema": db.execute("PRAGMA user_version").fetchone()[0],
                "notes": [list(row) for row in db.execute("SELECT id,text FROM notes ORDER BY id")]}


def process_record(root):
    return json.loads((root / ".hinstall/processes.json").read_text())["web"]


def http(config):
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=3) as response:
        value = json.load(response)
    assert value["product_id"] == "homensai-reference" and value["release"] == "reference-v1"
    assert value["ready"] is True and value["database_schema"] == 1 and value["api_key_configured"] is True
    return value


def effect_count(root):
    path = root / "data/reference-adapter.sqlite3"
    if not path.exists():
        return 0
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT count(*) FROM effects WHERE hook='quiesce'").fetchone()[0]


def run(config, phase):
    site, checkout = Path(config["site_packages"]).resolve(), Path(config["checkout"]).resolve()
    assert not Path.cwd().resolve().is_relative_to(checkout)
    assert not Path(__file__).resolve().is_relative_to(checkout)
    assert sys.flags.isolated and sys.flags.no_user_site and sys.prefix != sys.base_prefix
    assert not any(Path(path).resolve().is_relative_to(checkout) for path in sys.path if path)
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.engine import NativeEngine
    from homensai_install.errors import InstallerError
    from homensai_install.trust import load_trust_config

    if phase == "guard":
        for operation in (lambda: (checkout / "pyproject.toml").read_bytes(),
                          lambda: socket.create_connection(("203.0.113.1", 80), timeout=0.01),
                          lambda: subprocess.Popen([sys.executable, "-I", "-c", "raise SystemExit(1)"])):
            try:
                operation()
            except PermissionError:
                pass
            else:
                raise AssertionError("Guard допустил запрещённый эффект")
        return {"phase": phase, "status": "PASS", "pid": os.getpid(),
                "observation": {"checkout_reads_blocked": True, "external_network_blocked": True,
                                "unowned_subprocess_blocked": True}, "module_origins": origins(site, checkout)}

    root, work = Path(config["root"]), Path(config["work"])
    manifest = json.loads(Path(config["manifest"]).read_text())
    options = json.loads(Path(config["options"]).read_text())
    verifier = load_trust_config(config["trust"], allow_development=True)
    def secrets(reference):
        return config["archive_password"] if reference == "acceptance.archive" else config["product_secret"]
    # Maintenance explicitly opts in; bootstrap is the existing legacy path.
    engine = NativeEngine(verifier, secrets, **({} if phase == "bootstrap" else
                         {"product_adapter_descriptor": "installer/adapter.json"}))
    api = InstallerAPI(work / "sdk-state", engine=engine, locale="ru")
    before_effects = effect_count(root)

    def action(name, key):
        plan = api.plan(name, manifest, config["target"], options=options)
        assert effect_count(root) == before_effects, "Plan исполнил hook"
        consents = [*plan["required_consents"], plan["review_consent_ref"]]
        result = api.submit(plan["plan_id"], plan["plan_digest"], consents,
                            idempotency_key=key, background=False)
        assert result["status"] == "SUCCEEDED", result.get("error")
        write_json(work / (phase + "-operation.json"), {"plan": plan, "operation": result})
        return result

    if phase == "bootstrap":
        operation = action("install", "legacy-bootstrap-v1")
        assert operation["result"]["health"]["ready"]
        assert effect_count(root) == 0
        user = root / "data/user-files/личные документы.txt"
        user.write_text("Синтетические документы installed acceptance\n", encoding="utf-8")
        secret_path = root / "data/synthetic-secret.txt"
        secret_path.write_text(config["product_secret"], encoding="utf-8")
        secret_path.chmod(0o600)
        settings = root / "config/settings.json"
        value = json.loads(settings.read_text())
        value.update(site_title="Installed acceptance — данные владельца", language="de")
        write_json(settings, value)
        with sqlite3.connect(root / "data/reference.sqlite") as db:
            db.execute("INSERT INTO notes(id,text) VALUES(2,?)", ("  Пользовательская заметка  ",))
            db.commit()
        baseline = {"database": database(root / "data/reference.sqlite"),
                    "user_file_sha256": sha(user.read_bytes()), "settings_sha256": sha(settings.read_bytes()),
                    "secret_sha256": sha(secret_path.read_bytes()), "pid": process_record(root)["pid"]}
        write_json(work / "data-baseline.json", baseline)
        observation = {"legacy_bootstrap": True, "adapter_fresh_install": False, "http": http(config)}
    elif phase == "start":
        record = process_record(root)
        operation = action("start", "adapter-start-v1")
        health = operation["result"]["health"]
        assert health["product_adapter"]["ready"]
        assert process_record(root)["pid"] == record["pid"], "Повторный start создал процесс"
        executable = Path("/proc/" + str(record["pid"]) + "/exe").resolve()
        assert executable.is_relative_to(root / "releases")
        assert sha(executable.read_bytes()) == config["runtime_sha256"]
        commandline = Path("/proc/" + str(record["pid"]) + "/cmdline").read_bytes()
        assert config["product_secret"].encode() not in commandline and config["archive_password"].encode() not in commandline
        observation = {"http": http(config), "same_owned_pid": True,
                       "actual_signed_runtime_sha256": sha(executable.read_bytes()), "secrets_absent_from_argv": True,
                       "adapter_observed_identity": health["product_adapter"]["observed_identity"]}
    elif phase == "backup":
        operation = action("backup", "adapter-backup-v1")
        archive = operation["result"]["archive"]
        assert archive["encrypted"] and archive["includes_recovery_payload"]
        assert sha(Path(archive["path"]).read_bytes()) == archive["sha256"]
        assert effect_count(root) == before_effects + 1
        assert http(config)["ready"]
        write_json(work / "backup-result.json", archive)
        observation = {"archive_sha256": archive["sha256"], "quiesce_effects": effect_count(root),
                       "restarted_http": http(config)}
    elif phase == "archive":
        # Oracle не управляет lifecycle и не подменяет ProductAdapter.restore.
        from shared import archive_crypto
        archive = json.loads((work / "backup-result.json").read_text())
        assert archive_crypto.is_encrypted(archive["path"])
        target = work / "verified-restore-candidate"
        target.mkdir(mode=0o700)
        verified = archive_crypto.restore_archive(archive["path"], target,
            password_provider=lambda: config["archive_password"], expected_product_id=manifest["product_id"])
        restored = target / "roots"
        baseline = json.loads((work / "data-baseline.json").read_text())
        assert database(restored / "data/reference.sqlite") == baseline["database"]
        assert sha((restored / "data/user-files/личные документы.txt").read_bytes()) == baseline["user_file_sha256"]
        assert sha((restored / "config/settings.json").read_bytes()) == baseline["settings_sha256"]
        assert sha((restored / "data/synthetic-secret.txt").read_bytes()) == baseline["secret_sha256"]
        assert (restored / "runtime/recovery/reference-v1/payload/runtime/bin/python").is_file()
        assert (restored / "runtime/recovery/reference-v1/payload/installer/adapter.json").is_file()
        observation = {"encrypted_archive_restored": True, "database_schema": baseline["database"]["schema"],
                       "sqlite_notes_preserved": True, "documents_preserved": True,
                       "settings_preserved": True, "synthetic_secret_preserved": True,
                       "release_runtime_and_signed_adapter_present": True,
                       "reader": "installed shared.archive_crypto independent verification oracle"}
    elif phase == "stop":
        record = process_record(root)
        operation = action("stop", "adapter-stop-v1")
        assert operation["result"]["stopped"] and effect_count(root) == before_effects + 1
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", config["port"])) != 0, "Listener остался после stop"
        assert not Path("/proc/" + str(record["pid"])).exists() or Path("/proc/" + str(record["pid"]) + "/stat").read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}
        observation = {"owned_listener_stopped": True, "quiesce_effects": effect_count(root)}
    elif phase == "replay":
        previous = json.loads((work / "stop-operation.json").read_text())
        plan, operation = previous["plan"], previous["operation"]
        replayed = api.submit(plan["plan_id"], plan["plan_digest"],
            [*plan["required_consents"], plan["review_consent_ref"]], idempotency_key="adapter-stop-v1", background=False)
        assert replayed["operation_id"] == operation["operation_id"] and replayed["result"] == operation["result"]
        assert effect_count(root) == before_effects
        observation = {"same_operation_id": True, "no_repeated_quiesce": True}
    elif phase == "unsupported":
        for name in ("install", "update", "restore", "rollback", "repair", "reinstall", "uninstall"):
            assert api.capabilities(config["target"])["actions"][name]["supported"] is False
            try:
                api.plan(name, manifest, config["target"], options=options)
            except InstallerError as error:
                assert error.code == "CAPABILITY_UNSUPPORTED"
            else:
                raise AssertionError("Unsupported adapter action accepted: " + name)
        assert effect_count(root) == before_effects
        observation = {"unsupported_actions_rejected_before_effects": 7}
    else:
        raise AssertionError("Unknown client phase")
    baseline = json.loads((work / "data-baseline.json").read_text())
    assert database(root / "data/reference.sqlite") == baseline["database"]
    for key, path in (("user_file_sha256", "data/user-files/личные документы.txt"),
                      ("settings_sha256", "config/settings.json"), ("secret_sha256", "data/synthetic-secret.txt")):
        assert sha((root / path).read_bytes()) == baseline[key]
    if phase != "bootstrap":
        assert stat_private(root / "data/synthetic-secret.txt")
    for directory in (root / ".hinstall", work / "sdk-state"):
        for path in directory.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                assert config["product_secret"].encode() not in raw and config["archive_password"].encode() not in raw
    module_origins = origins(site, checkout)
    assert module_origins and "homensai_install.api.v1" in module_origins
    return {"phase": phase, "status": "PASS", "pid": os.getpid(), "observation": observation,
            "module_origins": module_origins, "data_unchanged": True, "secrets_absent_from_control_state_and_logs": True}


def stat_private(path):
    info = path.stat()
    return info.st_uid == os.getuid() and info.st_mode & 0o077 == 0


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    phase = sys.argv[2]
    denied = install_guard(config)
    report = {"phase": phase, "status": "FAIL", "pid": os.getpid()}
    try:
        report = run(config, phase)
    finally:
        report["denied_attempts"] = denied
        text = json.dumps(report, ensure_ascii=False)
        assert config["archive_password"] not in text and config["product_secret"] not in text
        write_json(Path(config["work"]) / "reports" / (phase + ".json"), report)
    print(json.dumps({"phase": phase, "status": report["status"]}))


if __name__ == "__main__":
    main()
