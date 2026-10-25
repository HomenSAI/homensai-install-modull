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
    parser.add_argument("--locale", choices=("ru", "en", "de"), default="ru" if root else argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=False if root else argparse.SUPPRESS)
    parser.add_argument("--trust-config", default=default)
    parser.add_argument("--allow-development", action="store_true", default=False if root else argparse.SUPPRESS)
    parser.add_argument("--development-runtime", default=default)


def _target(parser):
    parser.add_argument("--os", choices=("linux", "windows"))
    parser.add_argument("--arch", choices=("x64", "arm64"))
    parser.add_argument("--mode", choices=("native", "docker"))
    parser.add_argument("--scope", choices=("user", "service"))


def _input(parser):
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--options", help="Публичный JSON-файл параметров; значения секретов запрещены")
    _target(parser)


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
    from .engine import NativeEngine
    from .errors import InstallerError
    if args.development_runtime and not args.allow_development:
        raise InstallerError("CONSENT_REQUIRED")
    verifier = None
    if args.trust_config:
        from .trust import load_trust_config
        verifier = load_trust_config(args.trust_config, allow_development=args.allow_development)

    def secret_provider(reference):
        key = reference.removeprefix("env:") if type(reference) is str else ""
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) is None or key not in os.environ:
            raise InstallerError("PREREQUISITE_MISSING")
        return os.environ[key]

    engine = NativeEngine(verifier=verifier, secret_provider=secret_provider,
                          development_runtime=args.development_runtime)
    return InstallerAPI(args.state_dir or default_state_dir(), engine=engine, locale=args.locale)


def _execute(args):
    api = _sdk(args)
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "gui":
        from .gui import main as gui_main
        return gui_main(argv[1:])
    request_id = "req_" + uuid.uuid4().hex
    args = None
    from .errors import InstallerError
    try:
        args = parser().parse_args(argv)
        if args.command == "gui":
            from .gui import main as gui_main
            return gui_main(args.args)
        result = _execute(args)
        response = {"api_version": API_VERSION, "request_id": request_id, "result": result}
        state = result.get("status") if isinstance(result, dict) else None
        exit_code = 1 if state in {"FAILED", "FAILED_ROLLED_BACK", "RECOVERY_REQUIRED"} else 0
    except InstallerError as error:
        response = {"api_version": API_VERSION, "request_id": request_id,
                    "error": error.to_dict(locale=getattr(args, "locale", "ru"))}
        exit_code = 2
    except Exception:
        response = {"api_version": API_VERSION, "request_id": request_id,
                    "error": InstallerError("UNEXPECTED_ERROR").to_dict(locale=getattr(args, "locale", "ru"))}
        exit_code = 1
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
