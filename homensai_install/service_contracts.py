# HomenS.AI · © 2026 Serhii Khomenko
"""Подписанный contract служб и чистые renders; SCM/systemd не вызываются.

Linux apply реализован отдельным service_manager platform port. Сам renderer
не является evidence установки или приёмки системного manager.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import xml.etree.ElementTree as ET

from .errors import InstallerError
from .trust import canonical_bytes
from shared.datahome import checked_path, _windows_reserved
from shared.manifest import validate_manifest
from shared.services import _unit_arg, _windows_arg, component_order


CONTRACT_FILE = "service-contract.json"


def _fail(code="INVALID_MANIFEST", reason="invalid_service_contract"):
    raise InstallerError(code, phase="service_contract", details={"reason": reason})


def _text(value, *, maximum=4096):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                   or c in "\u0085\u2028\u2029\ufffe\uffff" for c in value)):
        _fail()
    return value


def _relative(value):
    _text(value)
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or _windows_reserved(value)):
        _fail(reason="service_path_outside_release")
    return value


def _command(command, files, runtime, *, empty=False):
    if not isinstance(command, list) or len(command) > 64 or (not command and not empty):
        _fail(reason="invalid_service_command")
    for item in command:
        _text(item)
    if command:
        executable = runtime if command[0] == "python" else _relative(command[0])
        if executable not in files:
            _fail("INTEGRITY_FAILED", "service_executable_not_signed")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(reason="duplicate_service_contract_key")
        result[key] = value
    return result


@dataclass(frozen=True)
class VerifiedServiceContract:
    """Неизменяемый результат trusted loader; сами configs ещё не установлены."""
    manifest_json: bytes
    document_json: bytes
    os_name: str
    release: str
    runtime_executable: str
    files: tuple[tuple[str, str], ...]
    contract_sha256: str

    @property
    def manifest(self):
        return json.loads(self.manifest_json)

    @property
    def document(self):
        return json.loads(self.document_json)


def load_verified_service_contract(manifest, target, options, *, verifier=None):
    """Читает только файл из полностью проверенного signed payload.

    verifier внедряется trusted SDK/bootstrap, никогда не HTTP-параметром.
    Секреты/config passwords не являются частью этого contract.
    """
    if verifier is None:
        _fail("INTEGRITY_FAILED", "trusted_verifier_required")
    source = validate_manifest(manifest)
    if source["schema_version"] != 2:
        _fail("CAPABILITY_UNSUPPORTED", "service_contract_requires_v2")
    files = verifier(source, target, options)
    if not isinstance(files, dict) or CONTRACT_FILE not in files:
        _fail("INTEGRITY_FAILED", "service_contract_not_signed")
    path = checked_path(Path(options["payload_dir"]) / CONTRACT_FILE)
    if path.stat().st_size > 256 * 1024:
        _fail(reason="service_contract_too_large")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != files[CONTRACT_FILE]:
        _fail("INTEGRITY_FAILED", "service_contract_changed")
    try:
        value = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        _fail(reason="invalid_service_contract_json")
    if not isinstance(value, dict):
        _fail()
    backend = value.get("backend")
    expected = {"schema_version", "backend", "components"}
    if backend == "windows-winsw":
        expected.add("wrapper")
    if set(value) != expected or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _fail()
    if (backend not in {"systemd-user", "windows-winsw"} or target.get("mode") != "native"
            or target.get("os") != ("windows" if backend == "windows-winsw" else "linux")
            or target.get("scope", "user") != ("machine" if backend == "windows-winsw" else "user")):
        _fail("CAPABILITY_UNSUPPORTED", "service_backend_target_mismatch")
    entries = value["components"]
    if not isinstance(entries, list) or len(entries) != len(source["components"]):
        _fail(reason="service_components_mismatch")
    declared = {item["name"]: item for item in source["components"]}
    seen = set()
    runtime = options["runtime_executable"]
    _relative(runtime)
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "stop_cmd", "stop_timeout_seconds"}:
            _fail()
        name = entry["name"]
        if not isinstance(name, str) or name in seen or name not in declared:
            _fail(reason="service_components_mismatch")
        seen.add(name)
        timeout = entry["stop_timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= 600:
            _fail(reason="invalid_stop_timeout")
        _command(declared[name]["cmd"], files, runtime)
        _command(entry["stop_cmd"], files, runtime, empty=backend == "systemd-user")
        if backend == "windows-winsw" and len(entry["stop_cmd"]) < 2:
            _fail(reason="winsw_requires_nonempty_stoparguments")
    if backend == "windows-winsw":
        wrapper = value["wrapper"]
        if (not isinstance(wrapper, dict) or set(wrapper) != {"file", "version", "license", "license_file"}
                or wrapper["license"] != "MIT" or wrapper["version"] != "2.12.0"):
            _fail(reason="unapproved_winsw_contract")
        for field in ("file", "license_file"):
            if _relative(wrapper[field]) not in files:
                _fail("INTEGRITY_FAILED", "winsw_or_notice_not_signed")
        pins = source.get("winsw", {})
        if pins.get("version") != wrapper["version"] or pins.get("sha256") != files[wrapper["file"]]:
            _fail("INTEGRITY_FAILED", "winsw_manifest_pin_mismatch")
    return VerifiedServiceContract(canonical_bytes(source), canonical_bytes(value), target["os"],
                                   options["release"], runtime, tuple(sorted(files.items())), files[CONTRACT_FILE])


def service_id(product_id, installation_id, component_id):
    if re.fullmatch(r"[0-9a-f]{32}", str(installation_id)) is None:
        _fail(reason="invalid_installation_identity")
    return "hsai" + hashlib.sha256((product_id + ":" + component_id).encode()).hexdigest()[:16] + installation_id


def _layout(contract, install_root):
    _text(install_root)
    factory = PureWindowsPath if contract.os_name == "windows" else PurePosixPath
    base = factory(install_root)
    if (not base.is_absolute() or base == factory(base.anchor) or ".." in base.parts
            or _windows_reserved(install_root) or install_root.startswith(("\\\\", "//"))):
        _fail(reason="invalid_service_install_root")
    if contract.os_name == "linux" and ("\\" in install_root or ":" in install_root):
        _fail(reason="invalid_service_install_root")
    paths = {}
    for name, relative in contract.manifest["paths"].items():
        # Этот узкий contract не переинтерпретирует absolute DataHome другого
        # хоста. Native machine adapter добавляется с отдельной path policy.
        relative = _relative(relative.replace("\\", "/"))
        paths[name] = base / factory(relative)
    values = list(paths.values())
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(values) for b in values[i + 1:]):
        _fail(reason="overlapping_service_paths")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(contract.release)) is None:
        _fail(reason="invalid_release")
    release = paths["releases"] / ("app-" + contract.release)
    return factory, paths, release


def render_service_plan(contract, *, install_root, installation_id, modes):
    """Возвращает reviewable конфигурации и ownership receipts, ничего не пишет.

    Шаблоны со значениями секретов не создаются. Пока credential loader не
    подключён, manifest с product secrets отклоняется явно.
    """
    if not isinstance(contract, VerifiedServiceContract):
        _fail("INTEGRITY_FAILED", "verified_service_contract_required")
    manifest, document = contract.manifest, contract.document
    payload_sha256 = hashlib.sha256(canonical_bytes(dict(contract.files))).hexdigest()
    if manifest["secrets"]:
        _fail("CAPABILITY_UNSUPPORTED", "service_credential_loader_required")
    order = component_order(manifest, tuple(modes))
    factory, paths, release = _layout(contract, install_root)
    items = {item["name"]: item for item in manifest["components"]}
    stops = {item["name"]: item for item in document["components"]}
    identity = {name: service_id(manifest["product_id"], installation_id, name) for name in order}
    files, services = {}, []

    def argv(command):
        if not command:
            return []
        executable = contract.runtime_executable if command[0] == "python" else command[0]
        return [str(release / factory(executable)), *command[1:]]

    for name in order:
        item, stop = items[name], stops[name]
        sid = identity[name]
        start_argv, stop_argv = argv(item["cmd"]), argv(stop["stop_cmd"])
        environment = {"HOMENSAI_PRODUCT_ID": manifest["product_id"],
                       "HOMENSAI_INSTALLATION_ID": installation_id,
                       "HOMENSAI_RELEASE": contract.release, "HOMENSAI_COMPONENT": name,
                       "HOMENSAI_DATA_HOME": str(paths["data"]), "HOMENSAI_CONFIG_HOME": str(paths["config"]),
                       "HOMENSAI_WEB_HOST": "127.0.0.1", "HOMENSAI_WEB_PORT": str(manifest["web"]["port"]),
                       "HOMENSAI_MAINTENANCE": "1", "PYTHONNOUSERSITE": "1",
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        if document["backend"] == "systemd-user":
            environment["PATH"] = "/usr/bin:/bin"
        receipt = {"schema_version": 1, "product_id": manifest["product_id"],
                   "installation_id": installation_id, "component_id": name,
                   "service_id": sid, "backend": document["backend"],
                   "contract_sha256": contract.contract_sha256, "executable": start_argv[0],
                   "payload_sha256": payload_sha256,
                   "working_directory": str(release), "automatic_adoption": False,
                   "start_argv": start_argv, "stop_argv": stop_argv,
                   "environment": environment,
                   "stop_timeout_seconds": stop["stop_timeout_seconds"],
                   "restart": dict(item["restart"]),
                   "environment_sha256": hashlib.sha256(canonical_bytes(environment)).hexdigest()}
        if document["backend"] == "systemd-user":
            deps = " ".join(identity[dep] + ".service" for dep in item["after"])
            command = lambda args: " ".join("\\;" if arg == ";" else _unit_arg(arg) for arg in args)
            lines = ["# HomenS.AI managed installation " + installation_id,
                     "[Unit]", "Description=" + sid, "After=" + deps, "Requires=" + deps,
                     "StartLimitIntervalSec=" + str(item["restart"]["window"]),
                     "StartLimitBurst=" + str(item["restart"]["max"]), "", "[Service]", "Type=exec",
                     "ExecStart=" + command(start_argv),
                     "WorkingDirectory=" + _unit_arg(str(release)).replace("$$", "$"),
                     "KillMode=control-group", "KillSignal=SIGTERM", "SendSIGKILL=no",
                     "TimeoutStopFailureMode=terminate", "TimeoutStartFailureMode=terminate",
                     "TimeoutStartSec=" + str(stop["stop_timeout_seconds"]),
                     "TimeoutStopSec=" + str(stop["stop_timeout_seconds"]), "UMask=0077",
                     "NoNewPrivileges=true", "Restart=on-failure", "RestartSec=" + str(item["restart"]["delay"]),
                     "UnsetEnvironment=PYTHONPATH PYTHONHOME LD_PRELOAD LD_LIBRARY_PATH",
                     "StandardOutput=null", "StandardError=null"]
            if stop_argv:
                lines.append("ExecStop=" + command(stop_argv))
            for key, value in sorted(environment.items()):
                lines.append("Environment=" + _unit_arg(key + "=" + value).replace("$$", "$"))
            lines.extend(["", "[Install]", "WantedBy=default.target", ""])
            text = "\n".join(lines)
            filename = sid + ".service"
            receipt["service_account"] = "current-user"
            receipt.update(dropin_paths=[], extra_exec_hooks=[])
        else:
            if any("%" in text for text in [*start_argv, *stop_argv, str(release), *environment.values()]):
                _fail(reason="winsw_environment_expansion_forbidden")
            xml = ET.Element("service")
            fields = {"id": sid, "name": sid, "description": "HomenS.AI managed installation " + installation_id,
                      "executable": start_argv[0],
                      "startarguments": " ".join(_windows_arg(value) for value in start_argv[1:]),
                      "stopexecutable": stop_argv[0],
                      "stoparguments": " ".join(_windows_arg(value) for value in stop_argv[1:]),
                      "workingdirectory": str(release), "stoptimeout": str(stop["stop_timeout_seconds"]) + " sec",
                      "startmode": "Automatic", "delayedAutoStart": "true",
                      "resetfailure": str(item["restart"]["window"]) + " sec"}
            for key, value in fields.items():
                ET.SubElement(xml, key).text = value
            account = ET.SubElement(xml, "serviceaccount")
            ET.SubElement(account, "domain").text = "NT AUTHORITY"
            ET.SubElement(account, "user").text = "LocalService"
            for dep in item["after"]:
                ET.SubElement(xml, "depend").text = identity[dep]
            for key, value in sorted(environment.items()):
                ET.SubElement(xml, "env", name=key, value=value)
            for _ in range(item["restart"]["max"] - 1):
                ET.SubElement(xml, "onfailure", action="restart", delay=str(item["restart"]["delay"]) + " sec")
            ET.SubElement(xml, "onfailure", action="none")
            ET.SubElement(xml, "log", mode="none")
            ET.indent(xml, space="  ")
            text = ET.tostring(xml, encoding="unicode") + "\n"
            filename = sid + ".xml"
            wrapper = document["wrapper"]
            receipt.update(service_account="NT AUTHORITY\\LocalService",
                           wrapper_source=str(release / factory(wrapper["file"])),
                           wrapper_destination=str(paths["services"] / (sid + ".exe")),
                           wrapper_sha256=dict(contract.files)[wrapper["file"]],
                           wrapper_license=wrapper["license"], wrapper_license_file=wrapper["license_file"])
            receipt["manager_image_argv"] = [receipt["wrapper_destination"]]
            receipt["service_type"] = "own_process"
        receipt.update(configuration_file=filename, configuration_sha256=hashlib.sha256(text.encode()).hexdigest())
        files[filename] = text
        services.append(receipt)
    return {"schema_version": 1, "kind": "service-render-plan", "backend": document["backend"],
            "payload_sha256": payload_sha256,
            "files": files, "services": services, "start_order": [identity[name] for name in order],
            "stop_order": [identity[name] for name in reversed(order)],
            "execution_supported": False, "production_ready": False,
            "required_gates": ["live_host_adapter", "manager_ownership_check", "credential_and_acl_check",
                               "environment_allowlist_check",
                               "signed_runtime_reverification", "graceful_stop_and_no_remaining_writers",
                               "install_update_restore_uninstall_vm_acceptance"]}


def verify_service_ownership(expected, observed):
    """Pure compare реального manager inventory с durable receipt без adoption.

    Adapter должен получать observed из SCM/systemd, а не из клиентского JSON.
    Отсутствие поля или изменение image/config/account блокирует mutation.
    """
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        _fail("OPERATION_CONFLICT", "missing_service_ownership_inventory")
    required = {"service_id", "backend", "configuration_sha256", "executable", "working_directory", "service_account",
                "start_argv", "stop_argv", "environment_sha256"}
    if expected.get("backend") == "windows-winsw":
        required |= {"wrapper_destination", "wrapper_sha256", "manager_image_argv", "service_type"}
    elif expected.get("backend") == "systemd-user":
        required |= {"dropin_paths", "extra_exec_hooks"}
    else:
        _fail("OPERATION_CONFLICT", "unknown_service_backend")
    mismatch = sorted(key for key in required if key not in expected or observed.get(key) != expected[key])
    if mismatch:
        raise InstallerError("OPERATION_CONFLICT", phase="service_ownership", details={"mismatched_fields": mismatch})
    return {"owned": True, "service_id": expected["service_id"]}
