"""Безопасная CI-сводка installed receipts; только стандартная библиотека."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


CHECKS = (
    ("sdk.json", "SDK / CLI", "source_baseline"),
    ("prerequisites.json", "Prerequisites", "source_head"),
    ("reference-update.json", "Reference fresh → v2 → v3", "sdk_source_revision"),
)
FAULT_PROFILES = (
    "update-lost-ack", "update-missing-ledger", "update-tampered-backup", "update-foreign-backup",
    "update-live-data-drift", "update-candidate-health", "update-candidate-cleanup",
    "recovery-restore", "recovery-rollback", "recovery-lost-ack", "recovery-missing-ledger",
    "uninstall-restore",
)
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
REVISION = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("non-finite number")


def inspect_receipt(path: Path, source_field: str, source_revision: str, *, expected_profile=None):
    """Возвращает только проверенный digest или фиксированную безопасную ошибку."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
    except FileNotFoundError:
        return None, "Квитанция отсутствует."
    except OSError:
        return None, "Квитанция недоступна."
    if len(raw) > MAX_RECEIPT_BYTES:
        return None, "Превышен размер квитанции."
    try:
        receipt = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError):
        return None, "Некорректная квитанция JSON."
    if type(receipt) is not dict or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        return None, "Неподдерживаемая структура квитанции."
    if receipt.get("status") != "PASS":
        return None, "Статус проверки не PASS."
    if receipt.get(source_field) != source_revision:
        return None, "Ревизия исходников не совпадает."
    wheel = receipt.get("wheel")
    digest = wheel.get("sha256") if type(wheel) is dict else None
    if type(digest) is not str or SHA256.fullmatch(digest) is None:
        return None, "Некорректный SHA-256 wheel."
    if expected_profile is not None and receipt.get("profile") != expected_profile:
        return None, ("Не подтверждён профиль последовательных updates." if expected_profile == "update"
                      else "Профиль квитанции не совпадает.")
    return digest, None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--include-fault-profiles", action="store_true",
                        help="Требовать также 12 fault/recovery/uninstall receipts и единый SHA-256 SDK wheel")
    args = parser.parse_args(argv)
    valid_revision = REVISION.fullmatch(args.source_revision) is not None
    rows, failed, sdk_digest = [], False, None
    checks = [(filename, label, field, "update" if filename == "reference-update.json" else None)
              for filename, label, field in CHECKS]
    if args.include_fault_profiles:
        checks.extend((profile + ".json", profile, "sdk_source_revision", profile) for profile in FAULT_PROFILES)
    for filename, label, source_field, expected_profile in checks:
        digest, error = (inspect_receipt(args.receipts / filename, source_field, args.source_revision,
                                        expected_profile=expected_profile)
                         if valid_revision else (None, "Некорректная ожидаемая ревизия исходников."))
        if filename == "sdk.json":
            sdk_digest = digest
        elif args.include_fault_profiles and digest is not None and sdk_digest is not None and digest != sdk_digest:
            digest, error = None, "SHA-256 SDK wheel отличается от базовой проверки."
        failed = failed or error is not None
        rows.append(f"| {label} | {'FAIL: ' + error if error else 'PASS'} | {f'`{digest}`' if digest else '—'} |")
    lines = ["## Проверки установленного SDK (DEV)", ""]
    if valid_revision:
        lines += [f"Source revision: `{args.source_revision}`", ""]
    lines += ["| Проверка | Результат | SHA-256 wheel |", "| --- | --- | --- |", *rows, "",
              "Это проверки Linux DEV. Готовность production и полные release gates не подтверждены.", ""]
    summary = "\n".join(lines)
    print(summary, end="")
    if args.summary is not None:
        try:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n" + summary)
        except OSError:
            print("Не удалось записать сводку проверок.", file=sys.stderr)
            return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
