# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальный локальный API v1: проверки и подготовка выпуска.

Это не установщик и не сетевой сервис. Новые операции нельзя объявлять до их
реализации и проверки; значения входных данных не возвращаются в ошибках.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from typing import Any

from shared.fresh_prepare import (FreshConflict, FreshPrepareError, prepare_idempotent_native,
                                  status_fresh_native, verify_fresh_release,
                                  _check_fresh_storage, _layout)
from shared.datahome import DataHomeError, checked_path
from shared.host_target import HostTargetError, native_target
from shared.health import HealthError, check_local_health
from shared.i18n import LanguageError, available_languages, translate
from shared.manifest import ManifestError, check_target, validate_manifest
from shared.package import PackageError
from shared.package_signing import MAX_ENVELOPE, _crypto, verify_signed_package
from shared.preflight import MAX_BYTES as MAX_DISK_BYTES, NAMES as PATH_NAMES, PreflightError, preflight_fresh
from shared.systemd_user_publish import UnitPublicationError, publish_signed_user_units
from shared.systemd_user_load import UnitLoadError, load_signed_user_units
from shared.update_plan import UpdateError, UpdatePlan

API_MAJOR = 1
API_MINOR = 16
MAX_BYTES = 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 50000
MAX_STRING = 8192
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class InvalidRequest(ValueError):
    pass


def _check_tree(value: Any) -> None:
    # Явный обход не зависит от рекурсивной глубины Python и обнаруживает
    # циклические структуры, переданные напрямую через Python API.
    pending = [(value, 0)]
    seen = set()
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise InvalidRequest()
        if type(item) is dict:
            if id(item) in seen:
                raise InvalidRequest()
            seen.add(id(item))
            for key, child in item.items():
                if type(key) is not str or len(key) > MAX_STRING:
                    raise InvalidRequest()
                pending.append((child, depth + 1))
        elif type(item) is list:
            if id(item) in seen:
                raise InvalidRequest()
            seen.add(id(item))
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > MAX_STRING:
                raise InvalidRequest()
        elif item is not None and type(item) not in (bool, int):
            raise InvalidRequest()


def _error(request_id: str | None, code: str, message: str,
           language: str | None = None, key: str | None = None) -> dict:
    if language is not None and key is not None:
        try:
            message = translate(language, key)
        except LanguageError:
            # Сбой словаря не должен скрывать ошибку проверки пакета или пути.
            pass
    return {"api_major": API_MAJOR, "request_id": request_id, "ok": False,
            "error": {"code": code, "message": message, "retryable": False}}


def _signature_available() -> bool:
    try:
        _, public_key, _, unsupported = _crypto()
    except (PackageError, OSError):
        return False
    try:
        public_key.from_public_bytes(b"\0" * 32)
        return True
    except (unsupported, ValueError, OSError):
        return False


def _systemd_plan_available() -> bool:
    try:
        return native_target() == {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
    except HostTargetError:
        return False


def _base64(value: Any, max_decoded: int) -> bytes:
    if type(value) is not str or not value or len(value) > ((max_decoded + 2) // 3) * 4:
        raise InvalidRequest()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidRequest() from None
    if len(decoded) > max_decoded or base64.b64encode(decoded).decode("ascii") != value:
        raise InvalidRequest()
    return decoded


def handle(request: Any) -> dict:
    """Обработать локальный запрос; health обращается к 127.0.0.1."""
    request_id = None
    language = None
    try:
        _check_tree(request)
        required = {"api_major", "operation", "request_id", "params"}
        if (type(request) is not dict or not required <= request.keys()
                or request.keys() - required - {"language"}):
            raise InvalidRequest()
        candidate = request["request_id"]
        if type(candidate) is not str or _ID.fullmatch(candidate) is None:
            raise InvalidRequest()
        request_id = candidate
        if "language" in request:
            try:
                translate(request["language"], "api.invalid_request")
            except LanguageError:
                raise InvalidRequest() from None
            language = request["language"]
        if type(request["api_major"]) is not int:
            raise InvalidRequest()
        if request["api_major"] != API_MAJOR:
            return _error(request_id, "unsupported_version", "Версия API не поддерживается", language, "api.unsupported_version")
        operation, params = request["operation"], request["params"]
        if type(operation) is not str or type(params) is not dict:
            raise InvalidRequest()
        if operation == "capabilities":
            if params:
                raise InvalidRequest()
            operations = ["capabilities", "validate", "preflight-fresh", "check-target", "update-plan-status", "check-local-health"]
            mutating_operations = []
            if _signature_available():
                operations.extend(["verify-package", "inspect-fresh-package", "inspect-native-fresh-package", "fresh-status", "verify-native-release", "prepare-native-release"])
                mutating_operations.append("prepare-native-release")
                if _systemd_plan_available():
                    operations.extend(["plan-systemd-user-units", "publish-systemd-user-units", "load-systemd-user-units"])
                    mutating_operations.extend(["publish-systemd-user-units", "load-systemd-user-units"])
            result = {"api_major": API_MAJOR, "api_minor": API_MINOR,
                      "operations": operations,
                      "mutating_operations": mutating_operations,
                      "languages": available_languages(),
                      "manifest_versions": [1, 2], "execution_modes": [],
                      "limits": {"request_bytes": MAX_BYTES, "depth": MAX_DEPTH,
                                 "nodes": MAX_NODES, "string_chars": MAX_STRING}}
        elif operation == "validate":
            if set(params) != {"manifest"}:
                raise InvalidRequest()
            try:
                manifest = validate_manifest(params["manifest"])
            except (ManifestError, RecursionError):
                return _error(request_id, "invalid_request", "Манифест не соответствует контракту", language, "api.invalid_manifest")
            result = {"product_id": manifest["product_id"], "schema_version": manifest["schema_version"]}
        elif operation == "check-target":
            if set(params) != {"manifest", "target"}:
                raise InvalidRequest()
            try:
                result = check_target(params["manifest"], params["target"])
            except (ManifestError, RecursionError):
                return _error(request_id, "invalid_request", "Манифест или цель не соответствуют контракту", language, "api.invalid_target")
        elif operation == "check-local-health":
            fields = {'port', 'product_id', 'version', 'path', 'timeout_seconds'}
            if (set(params) != fields or type(params['port']) is not int or not 1 <= params['port'] <= 65535
                    or type(params['product_id']) is not str
                    or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', params['product_id']) is None
                    or type(params['version']) is not str
                    or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', params['version']) is None
                    or type(params['path']) is not str
                    or re.fullmatch(r'/[A-Za-z0-9/_.~-]{0,1023}', params['path']) is None
                    or type(params['timeout_seconds']) is not int or not 1 <= params['timeout_seconds'] <= 30):
                raise InvalidRequest()
            try:
                check_local_health(params['port'], product_id=params['product_id'], version=params['version'],
                                   path=params['path'], timeout=params['timeout_seconds'])
            except HealthError:
                return _error(request_id, 'health_check_failed', 'Не удалось подтвердить состояние продукта',
                              language, 'api.health_check_failed')
            result = {'health_response_matches': True, 'process_identity_verified': False}
        elif operation == "preflight-fresh":
            if set(params) != {"manifest", "install_root", "required_bytes", "reserve_bytes"}:
                raise InvalidRequest()
            if type(params["install_root"]) is not str:
                raise InvalidRequest()
            try:
                result = preflight_fresh(params["manifest"], params["install_root"],
                                         required_bytes=params["required_bytes"],
                                         reserve_bytes=params["reserve_bytes"])
            except PreflightError:
                return _error(request_id, "preflight_failed", "Предварительная проверка не пройдена", language, "api.preflight_failed")
        elif operation == "update-plan-status":
            fields = {"manifest", "install_root", "release", "modes", "migration_ids", "guard_effects"}
            if (set(params) != fields or type(params['manifest']) is not dict
                    or type(params['install_root']) is not str or not params['install_root']
                    or type(params['release']) is not str or type(params['guard_effects']) is not bool
                    or type(params['modes']) is not list or not params['modes']
                    or type(params['migration_ids']) is not list
                    or any(type(item) is not str for item in params['modes'] + params['migration_ids'])):
                raise InvalidRequest()
            try:
                plan = UpdatePlan(params['manifest'], checked_path(params['install_root']), object(),
                                  release=params['release'], modes=tuple(params['modes']),
                                  migration_ids=tuple(params['migration_ids']), guard_effects=params['guard_effects'])
                result = {'state': plan.status()}
            except (DataHomeError, ManifestError, UpdateError, OSError, RecursionError):
                return _error(request_id, "update_status_failed", "Не удалось прочитать состояние обновления", language, "api.update_status_failed")
        elif operation == "prepare-native-release":
            fields = {"package_path", "envelope", "trusted_public_key", "expected_product_id", "expected_version",
                      "install_root", "required_bytes", "reserve_bytes", "idempotency_key"}
            strings = fields - {"required_bytes", "reserve_bytes"}
            if set(params) != fields or any(type(params[field]) is not str for field in strings):
                raise InvalidRequest()
            sizes, reserve = params['required_bytes'], params['reserve_bytes']
            if (type(sizes) is not dict or set(sizes) != set(PATH_NAMES)
                    or any(type(value) is not int or not 0 <= value <= MAX_DISK_BYTES for value in sizes.values())
                    or type(reserve) is not int or not 0 <= reserve <= MAX_DISK_BYTES):
                raise InvalidRequest()
            if not _signature_available():
                return _error(request_id, "unsupported_operation", "Проверка подписи недоступна", language, "api.signature_unavailable")
            if (not params['package_path'] or not params['install_root']
                    or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", params['expected_product_id']) is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", params['expected_version']) is None
                    or re.fullmatch(r"[a-f0-9]{32}", params['idempotency_key']) is None):
                raise InvalidRequest()
            envelope = _base64(params['envelope'], MAX_ENVELOPE)
            key = _base64(params['trusted_public_key'], 32)
            if len(key) != 32:
                raise InvalidRequest()
            try:
                state = prepare_idempotent_native(params['package_path'], envelope, params['install_root'],
                                                  trusted_public_key=key,
                                                  expected_product_id=params['expected_product_id'],
                                                  expected_version=params['expected_version'],
                                                  required_bytes=params['required_bytes'],
                                                  reserve_bytes=params['reserve_bytes'],
                                                  operation_id=params['idempotency_key'])
            except FreshConflict:
                return _error(request_id, "conflict", "Запрос не соответствует операции", language, "api.operation_conflict")
            except FreshPrepareError:
                return _error(request_id, "fresh_prepare_failed", "Не удалось подготовить выпуск", language, "api.fresh_prepare_failed")
            result = {'transaction': state['transaction'], 'product_id': state['product_id'],
                      'version': state['version'], 'status': state['status'],
                      'process_target': state['process_target'], 'installation_complete': False}
        elif operation in ("plan-systemd-user-units", "publish-systemd-user-units", "load-systemd-user-units"):
            fields = {"package_path", "envelope", "trusted_public_key", "expected_product_id",
                      "expected_version", "install_root", "modes"}
            publishing = operation == "publish-systemd-user-units"
            loading = operation == "load-systemd-user-units"
            if publishing or loading:
                fields |= {"unit_dir", "idempotency_key"}
            if publishing and 'verify_generated_units' in params:
                fields.add('verify_generated_units')
            strings = fields - {"modes", "verify_generated_units"}
            if (set(params) != fields or any(type(params[field]) is not str for field in strings)
                    or type(params['modes']) is not list or not params['modes']
                    or any(type(mode) is not str for mode in params['modes'])
                    or (publishing and 'verify_generated_units' in params
                        and type(params['verify_generated_units']) is not bool)):
                raise InvalidRequest()
            if not _signature_available() or not _systemd_plan_available():
                return _error(request_id, "unsupported_operation", "Операция API не поддерживается", language, "api.unsupported_operation")
            if (not params['package_path'] or not params['install_root']
                    or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", params['expected_product_id']) is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", params['expected_version']) is None
                    or ((publishing or loading) and (not params['unit_dir']
                        or re.fullmatch(r'[a-f0-9]{32}', params['idempotency_key']) is None))):
                raise InvalidRequest()
            envelope = _base64(params['envelope'], MAX_ENVELOPE)
            key = _base64(params['trusted_public_key'], 32)
            if len(key) != 32:
                raise InvalidRequest()
            try:
                function = load_signed_user_units if loading else publish_signed_user_units
                generated_policy = {'verify_generated_units': params.get('verify_generated_units', False)} if publishing else {}
                plan = function(params['package_path'], envelope, params['install_root'],
                    trusted_public_key=key, expected_product_id=params['expected_product_id'],
                    expected_version=params['expected_version'], modes=tuple(params['modes']),
                    unit_dir=params['unit_dir'] if publishing or loading else '', execute=publishing or loading,
                    operation_id=params['idempotency_key'] if publishing or loading else None, **generated_policy)
            except (UnitPublicationError, UnitLoadError):
                if loading:
                    return _error(request_id, 'systemd_load_failed', 'Не удалось подтвердить загрузку служб',
                                  language, 'api.systemd_load_failed')
                if publishing:
                    return _error(request_id, 'systemd_publish_failed', 'Не удалось подтвердить публикацию служб',
                                  language, 'api.systemd_publish_failed')
                return _error(request_id, 'systemd_plan_failed', 'Не удалось проверить состав служб',
                              language, 'api.systemd_plan_failed')
            result = ({**plan, 'installation_complete': False} if publishing or loading else
                      {**plan, 'manager_checked': False, 'publication_readiness_verified': False})
        elif operation in ("fresh-status", "verify-native-release"):
            fields = {"package_path", "envelope", "trusted_public_key", "expected_product_id", "expected_version", "install_root"}
            if set(params) != fields or any(type(params[field]) is not str for field in fields):
                raise InvalidRequest()
            if not _signature_available():
                return _error(request_id, "unsupported_operation", "Проверка подписи недоступна", language, "api.signature_unavailable")
            if (not params['package_path'] or not params['install_root']
                    or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", params['expected_product_id']) is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", params['expected_version']) is None):
                raise InvalidRequest()
            envelope = _base64(params['envelope'], MAX_ENVELOPE)
            key = _base64(params['trusted_public_key'], 32)
            if len(key) != 32:
                raise InvalidRequest()
            try:
                verifier = status_fresh_native if operation == "fresh-status" else verify_fresh_release
                result = verifier(params['package_path'], envelope, params['install_root'],
                                  trusted_public_key=key,
                                  expected_product_id=params['expected_product_id'],
                                  expected_version=params['expected_version'])
            except FreshPrepareError:
                if operation == "verify-native-release":
                    return _error(request_id, "fresh_verify_failed", "Не удалось проверить подготовленный выпуск", language, "api.fresh_verify_failed")
                return _error(request_id, "fresh_status_failed", "Не удалось прочитать состояние подготовки", language, "api.fresh_status_failed")
        elif operation in ("verify-package", "inspect-fresh-package", "inspect-native-fresh-package"):
            package_fields = {"package_path", "envelope", "trusted_public_key", "expected_product_id", "expected_version"}
            inspection_fields = {"install_root", "required_bytes", "reserve_bytes"}
            if operation == "inspect-fresh-package":
                inspection_fields.add("target")
            if set(params) != package_fields | (inspection_fields if operation != "verify-package" else set()):
                raise InvalidRequest()
            if any(type(params[field]) is not str for field in package_fields):
                raise InvalidRequest()
            if not _signature_available():
                return _error(request_id, "unsupported_operation", "Проверка подписи недоступна", language, "api.signature_unavailable")
            if (not params["package_path"]
                    or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", params["expected_product_id"]) is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", params["expected_version"]) is None):
                raise InvalidRequest()
            envelope = _base64(params["envelope"], MAX_ENVELOPE)
            key = _base64(params["trusted_public_key"], 32)
            if len(key) != 32:
                raise InvalidRequest()
            try:
                metadata = verify_signed_package(params["package_path"], envelope, trusted_public_key=key,
                                                 expected_product_id=params["expected_product_id"],
                                                 expected_version=params["expected_version"])
            except PackageError:
                return _error(request_id, "verification_failed", "Подпись или пакет не прошли проверку", language, "api.verification_failed")
            result = {"product_id": metadata["product_id"], "version": metadata["version"],
                      "file_count": len(metadata["files"])}
            if operation in ("inspect-fresh-package", "inspect-native-fresh-package"):
                if operation == "inspect-native-fresh-package":
                    try:
                        selected_target = native_target()
                    except HostTargetError:
                        return _error(request_id, "unsupported_platform", "Платформа процесса не поддерживается", language, "api.unsupported_platform")
                else:
                    selected_target = params["target"]
                try:
                    target = check_target(metadata["manifest"], selected_target)
                except ManifestError:
                    raise InvalidRequest() from None
                if target["status"] != "supported":
                    return _error(request_id, "target_not_supported", "Цель не подтверждена подписанным манифестом", language, "api.target_not_supported")
                if type(params["install_root"]) is not str:
                    raise InvalidRequest()
                try:
                    result["preflight"] = preflight_fresh(metadata["manifest"], params["install_root"],
                                                          required_bytes=params["required_bytes"],
                                                          reserve_bytes=params["reserve_bytes"])
                except PreflightError:
                    return _error(request_id, "preflight_failed", "Предварительная проверка не пройдена", language, "api.preflight_failed")
                result["declared_target"] = "supported"
                if operation == "inspect-native-fresh-package":
                    result["process_target"] = selected_target
                    try:
                        root = checked_path(params["install_root"])
                        if root == root.parent or root.exists() or not root.parent.is_dir():
                            raise FreshPrepareError('Требуется новый корень установки')
                        _layout(metadata['manifest'], root)
                        result['storage_estimate'] = _check_fresh_storage(
                            params['package_path'], metadata, root, params['reserve_bytes'])
                    except (FreshPrepareError, DataHomeError, OSError, ValueError, KeyError, TypeError):
                        return _error(request_id, "preflight_failed", "Предварительная проверка не пройдена", language, "api.preflight_failed")
        else:
            return _error(request_id, "unsupported_operation", "Операция API не поддерживается", language, "api.unsupported_operation")
    except (InvalidRequest, RecursionError):
        return _error(request_id, "invalid_request", "Неверный запрос API", language, "api.invalid_request")
    return {"api_major": API_MAJOR, "request_id": request_id, "ok": True, "result": result}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest()
        result[key] = value
    return result


def _constant(_):
    raise InvalidRequest()


def main() -> int:
    """Прочитать один ограниченный JSON из stdin и записать один ответ."""
    try:
        if len(sys.argv) != 1:
            raise InvalidRequest()
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise InvalidRequest()
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_constant)
        response = handle(request)
    except (InvalidRequest, ValueError, UnicodeError, RecursionError):
        response = _error(None, "invalid_request", "Неверный запрос API")
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n")
    if response["ok"]:
        return 0
    return 1 if response["error"]["code"] in ("verification_failed", "preflight_failed", "fresh_status_failed", "fresh_verify_failed", "fresh_prepare_failed", "update_status_failed", "health_check_failed", "systemd_plan_failed", "systemd_publish_failed", "systemd_load_failed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
