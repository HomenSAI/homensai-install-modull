"""Машинный CLI общего публичного API. stdout: один JSON envelope."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import uuid

from . import API_VERSION
from .api.model import MAX_JSON_BYTES, parse_json, target_model
from .api.v1 import ALL_ACTIONS, InstallerAPI


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, _message):
        from .errors import InstallerError
        raise InstallerError("INVALID_REQUEST")


def default_state_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "HomenSAI" / "Installer"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "homensai-install"


def _common(parser, *, root=False):
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--state-dir", default=default)
    parser.add_argument("--locale", choices=("ru", "en", "de"), default=None if root else argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=False if root else argparse.SUPPRESS)
    parser.add_argument("--trust-config", default=default)
    parser.add_argument("--allow-development", action="store_true", default=False if root else argparse.SUPPRESS)
    parser.add_argument("--development-runtime", default=default)
    parser.add_argument("--docker-cli-config", default=default,
                        help="Доверенный локальный JSON с executable/SHA-256/endpoint/config_dir Docker CLI")


def _target(parser):
    parser.add_argument("--os", choices=("linux", "windows"))
    parser.add_argument("--arch", choices=("x64", "arm64"))
    parser.add_argument("--mode", choices=("native", "docker"))
    parser.add_argument("--scope", choices=("user", "service"))


def _input(parser):
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--options", help="Публичный JSON-файл параметров; значения секретов запрещены")
    _target(parser)


def _prerequisites_parser(commands):
    group = commands.add_parser("prerequisites", help="Подготовка зависимостей через доверенный coordinator")
    _common(group)
    actions = group.add_subparsers(dest="prerequisites_command", required=True)
    for name in ("identity", "capabilities", "license", "plan", "start", "status", "operations",
                 "events", "attempts", "recheck", "resume", "cancel", "choose-boundary", "replace-plan"):
        sub = actions.add_parser(name)
        _common(sub)
        if name in {"start", "status", "events", "attempts", "recheck", "resume", "cancel",
                    "choose-boundary", "replace-plan"}:
            sub.add_argument("operation_id")
        if name in {"license", "attempts"}:
            sub.add_argument("component_id")
        if name == "plan":
            sub.add_argument("--choices", help="JSON object: component_id → auto/manual/use/skip")
            sub.add_argument("--offline", action="store_true")
            sub.add_argument("--local-artifacts", help="JSON object: artifact_id → абсолютный локальный путь")
        if name in {"start", "replace-plan"}:
            sub.add_argument("--plan", required=True, help="JSON plan SDK или успешный JSON envelope команды plan")
        if name in {"start", "replace-plan", "resume"}:
            sub.add_argument("--consent-ref", action="append", default=None if name == "resume" else [])
        if name in {"events", "attempts", "operations"}:
            sub.add_argument("--limit", type=int, default=100)
        if name == "events":
            sub.add_argument("--after-sequence", type=int, default=0)
        if name == "attempts":
            sub.add_argument("--after", type=int, default=0)
        if name == "operations":
            sub.add_argument("--all", action="store_true", help="Включить READY/CANCELED")
            sub.add_argument("--after-id")
        if name == "choose-boundary":
            sub.add_argument("when", choices=("now", "later"))


def parser() -> Parser:
    root = Parser(description="HomenS.AI Install Module: публичный JSON API")
    _common(root, root=True)
    commands = root.add_subparsers(dest="command", required=True)
    api = commands.add_parser("api", help="Офлайн-описание и доступные возможности")
    _common(api)
    apis = api.add_subparsers(dest="api_command", required=True)
    describe = apis.add_parser("describe")
    _common(describe)
    capabilities = apis.add_parser("capabilities")
    _common(capabilities)
    _target(capabilities)
    schema = apis.add_parser("schema")
    _common(schema)
    schema.add_argument("name", choices=("manifest", "request", "plan", "operation", "event", "normalized"))
    scaffold = commands.add_parser("init-adapter", help="Создать подключение продукта из реальных inputs или DRAFT/BLOCKED")
    _common(scaffold)
    scaffold.add_argument("--output-dir", required=True)
    scaffold.add_argument("--spec", help="JSON: product/target/payload/runtime/locks/wheels/commands")
    validate = commands.add_parser("validate")
    _common(validate)
    validate.add_argument("--manifest", required=True)
    for name in ("discover", "contract-test"):
        sub = commands.add_parser(name)
        _common(sub)
        _input(sub)
    plan = commands.add_parser("plan")
    _common(plan)
    plan.add_argument("action", choices=ALL_ACTIONS)
    _input(plan)
    submit = commands.add_parser("submit")
    _common(submit)
    submit.add_argument("--plan-id", required=True)
    submit.add_argument("--plan-digest", required=True)
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--consent-ref", action="append", default=[])
    operations = commands.add_parser("operations", help="Найти сохранённые операции после перезапуска UI")
    _common(operations)
    operations.add_argument("--product-id")
    operations.add_argument("--all", action="store_true", help="Включить завершённые операции")
    operations.add_argument("--limit", type=int, default=100)
    for name in ("status", "events", "cancel", "resume"):
        sub = commands.add_parser(name)
        _common(sub)
        sub.add_argument("operation_id")
        if name == "events":
            sub.add_argument("--after-sequence", type=int, default=0)
        if name == "resume":
            sub.add_argument("--decisions")
    errors = commands.add_parser("errors")
    _common(errors)
    errors.add_argument("--code")
    errors.add_argument("--query", default="")
    statistics = commands.add_parser("statistics")
    _common(statistics)
    statistics.add_argument("--product-id")
    statistics.add_argument("--product-version")
    statistics.add_argument("--action")
    statistics.add_argument("--status")
    statistics.add_argument("--target")
    statistics.add_argument("--since")
    statistics.add_argument("--until")
    diagnostics = commands.add_parser("diagnostics")
    _common(diagnostics)
    diagnostics.add_argument("operation_id")
    diagnostics.add_argument("--export")
    diagnostics.add_argument("--preview", action="store_true")
    diagnostics.add_argument("--expected-digest")
    diagnostics.add_argument("--confirm-export", action="store_true")
    _prerequisites_parser(commands)
    gui = commands.add_parser("gui", add_help=False)
    gui.add_argument("args", nargs=argparse.REMAINDER)
    return root


def _json_file(path: str | None) -> dict | None:
    if path is None:
        return None
    from .errors import InstallerError
    try:
        with Path(path).open("rb") as stream:
            result = parse_json(stream.read(MAX_JSON_BYTES + 1))
    except OSError:
        raise InstallerError("INVALID_REQUEST") from None
    if type(result) is not dict:
        raise InstallerError("INVALID_REQUEST")
    return result


def _sdk(args) -> InstallerAPI:
    from .errors import InstallerError

    def secret_provider(reference):
        key = reference.removeprefix("env:") if type(reference) is str else ""
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) is None or key not in os.environ:
            raise InstallerError("PREREQUISITE_MISSING")
        return os.environ[key]

    return InstallerAPI.from_bootstrap(
        args.state_dir or default_state_dir(), trust_config=args.trust_config,
        allow_development=args.allow_development, secret_provider=secret_provider,
        development_runtime=args.development_runtime, docker_cli_config=args.docker_cli_config,
        locale=args.locale or "ru")


def _prerequisite_plan_file(path):
    """Accept a raw SDK plan or the exact successful CLI envelope, not code."""
    value = _json_file(path)
    if type(value) is dict and {"api_version", "request_id", "result"} <= set(value):
        from .errors import InstallerError
        if (set(value) != {"api_version", "request_id", "result"} or value["api_version"] != API_VERSION
                or type(value["request_id"]) is not str
                or re.fullmatch(r"req_[0-9a-f]{32}", value["request_id"]) is None
                or type(value["result"]) is not dict):
            raise InstallerError("INVALID_REQUEST")
        return value["result"]
    return value


def _prerequisites_execute(args, api):
    command = args.prerequisites_command
    if command == "capabilities":
        return api.prerequisites_capabilities()
    # An unconfigured ordinary CLI refuses before reading a requested plan or
    # artifact map. Identity is read-only and does not re-verify the catalogue,
    # so historical status remains readable when payload verification is blocked.
    identity = api.prerequisites_identity()
    if command == "identity":
        return identity
    if command == "license":
        return api.prerequisites_license(args.component_id)
    if command == "plan":
        return api.prerequisites_plan(_json_file(args.choices), locale=args.locale or api.locale, offline=args.offline,
                                      local_artifacts=_json_file(args.local_artifacts))
    if command == "start":
        return api.prerequisites_start(args.operation_id, _prerequisite_plan_file(args.plan),
                                       args.consent_ref, background=False)
    if command == "resume":
        return api.prerequisites_resume(args.operation_id, args.consent_ref, background=False)
    if command == "status":
        return api.prerequisites_status(args.operation_id)
    if command == "operations":
        return api.prerequisites_operations_list(unfinished_only=not args.all, limit=args.limit,
                                                 after_id=args.after_id)
    if command == "events":
        return api.prerequisites_events(args.operation_id, after_sequence=args.after_sequence, limit=args.limit)
    if command == "attempts":
        return api.prerequisites_attempts(args.operation_id, args.component_id, after=args.after, limit=args.limit)
    if command == "recheck":
        return api.prerequisites_recheck(args.operation_id)
    if command == "cancel":
        return api.prerequisites_cancel(args.operation_id)
    if command == "choose-boundary":
        return api.prerequisites_choose_boundary(args.operation_id, args.when)
    if command == "replace-plan":
        return api.prerequisites_replace_plan(args.operation_id, _prerequisite_plan_file(args.plan), args.consent_ref)
    raise AssertionError("unhandled prerequisites command")


def _execute(args, *, api: InstallerAPI | None = None):
    if api is not None:
        from .errors import InstallerError
        if (not isinstance(api, InstallerAPI) or args.command == "init-adapter"
                or args.allow_development
                or any(getattr(args, name) is not None for name in
                       ("state_dir", "trust_config", "development_runtime", "docker_cli_config"))):
            raise InstallerError("INVALID_REQUEST", details={"reason": "injected_api_bootstrap_override"})
    if args.command == "init-adapter":
        from .scaffold import scaffold_product
        return scaffold_product(args.output_dir, _json_file(args.spec))
    api = _sdk(args) if api is None else api
    if args.command == "prerequisites":
        return _prerequisites_execute(args, api)
    target = {key: getattr(args, key) for key in ("os", "arch", "mode", "scope")
              if getattr(args, key, None) is not None}
    if args.command == "api":
        if args.api_command == "describe":
            return api.describe()
        if args.api_command == "schema":
            return api.schema(args.name)
        return api.capabilities(target)
    if args.command == "validate":
        return api.validate(args.manifest)
    if args.command == "discover":
        return api.discover(args.manifest, target, _json_file(args.options))
    if args.command == "contract-test":
        return api.contract_test(args.manifest, target, _json_file(args.options))
    if args.command == "plan":
        return api.plan(args.action, args.manifest, target, options=_json_file(args.options))
    if args.command == "submit":
        # CLI живёт до конца шага. Background обеспечивается долгоживущим GUI/SDK,
        # а не daemon-thread, уничтожаемым сразу после выхода процесса CLI.
        return api.submit(args.plan_id, args.plan_digest, args.consent_ref,
                          idempotency_key=args.idempotency_key, background=False)
    if args.command == "status":
        return api.status(args.operation_id)
    if args.command == "operations":
        return api.operations_list(args.product_id, unfinished_only=not args.all, limit=args.limit)
    if args.command == "events":
        return api.events(args.operation_id, args.after_sequence)
    if args.command == "cancel":
        return api.cancel(args.operation_id)
    if args.command == "resume":
        return api.resume(args.operation_id, _json_file(args.decisions), background=False)
    if args.command == "errors":
        return api.errors_describe(args.code) if args.code else {"matches": api.errors_search(args.query)}
    if args.command == "statistics":
        return api.statistics_query(**{key: getattr(args, key) for key in ("product_id", "product_version", "action", "status", "target", "since", "until")
                                       if getattr(args, key) is not None})
    if args.command == "diagnostics":
        if args.export:
            return api.diagnostics_export(args.export, operation_id=args.operation_id,
                                           confirmed=args.confirm_export, expected_digest=args.expected_digest)
        if args.preview:
            return api.diagnostics_export_preview(args.operation_id)
        return api.diagnostics_get(args.operation_id)
    raise AssertionError("unhandled CLI command")


def _exit_code(args, result):
    state = result.get("status") if isinstance(result, dict) else None
    if args.command != "prerequisites":
        return 2 if state == "BLOCKED" else 1 if state in {"FAILED", "FAILED_ROLLED_BACK", "RECOVERY_REQUIRED"} else 0
    if args.prerequisites_command in {"identity", "capabilities", "license", "plan", "operations", "attempts",
                                      "cancel", "choose-boundary", "replace-plan"}:
        # For these commands success acknowledges the query/control request,
        # not prerequisite readiness. Consumers must inspect the JSON result.
        return 0
    outcome = result.get("snapshot", {}) if args.prerequisites_command == "events" else result
    state = outcome.get("status") if isinstance(outcome, dict) else None
    if state == "CANCELED":
        return 4
    if isinstance(outcome, dict) and outcome.get("error"):
        return 1
    if state == "READY" and outcome.get("ready") is True:
        return 0
    if state in {"RUNNING", "QUEUED", "AWAITING_USER", "REBOOT_REQUIRED", "RELOGIN_REQUIRED"}:
        return 3
    return 1  # Unknown/malformed state cannot claim readiness.


def main(argv: list[str] | None = None, *, api: InstallerAPI | None = None) -> int:
    """Trusted Python bootstrap may inject an API; argv never loads a registry."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if api is None and argv and argv[0] == "gui":
        from .gui import main as gui_main
        return gui_main(argv[1:])
    request_id = "req_" + uuid.uuid4().hex
    args = None
    from .errors import InstallerError
    try:
        args = parser().parse_args(argv)
        if args.command == "gui":
            if api is not None:
                raise InstallerError("INVALID_REQUEST", details={"reason": "injected_api_not_gui_bootstrap"})
            from .gui import main as gui_main
            return gui_main(args.args)
        result = _execute(args, api=api)
        response = {"api_version": API_VERSION, "request_id": request_id, "result": result}
        exit_code = _exit_code(args, result)
    except InstallerError as error:
        response = {"api_version": API_VERSION, "request_id": request_id,
                    "error": error.to_dict(locale=getattr(args, "locale", None) or
                                           (api.locale if isinstance(api, InstallerAPI) else "ru"))}
        exit_code = 2
    except Exception:
        response = {"api_version": API_VERSION, "request_id": request_id,
                    "error": InstallerError("UNEXPECTED_ERROR").to_dict(locale=getattr(args, "locale", None) or
                                                                         (api.locale if isinstance(api, InstallerAPI) else "ru"))}
        exit_code = 1
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
