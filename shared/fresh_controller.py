"""Ограниченный контроллер первой установки Linux user-systemd через API."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from shared.datahome import checked_path, contains
from shared.effect_receipt import read_effect_receipt
from shared.host_target import native_target
from shared.i18n import available_languages
from shared.manifest import check_target
from shared.package_signing import MAX_ENVELOPE, verify_signed_package
from shared.preflight import MAX_BYTES, NAMES
from shared.services import component_order
from shared.systemd_user_publish import _unit_directory
from shared.update_plan import _atomic_write, operation_lock

_KEY = re.compile(r'[a-f0-9]{32}\Z')
_NAMES = ('release', 'runtime', 'publish', 'load', 'start')


class FreshControllerError(RuntimeError):
    """Шаг не подтверждён; журнал сохранён для проверки и повторного вызова."""

    def __init__(self, code='controller_failed'):
        super().__init__('Не удалось подтвердить установочный сценарий')
        self.code = code


def run_portable_linux_install(*, package_path: str, envelope: bytes, trusted_public_key: bytes,
                               product_id: str, version: str, install_root: str, unit_dir: str,
                               work_dir: str, modes: tuple[str, ...], required_bytes: dict,
                               reserve_bytes: int, workflow_key: str, keys: dict,
                               language: str = 'en', inspect_only: bool = False) -> dict:
    """Выполнить последовательность API с долговечным checkpoint каждого шага.

    Ключи и workspace заранее сохраняет вызывающая сторона. Pending не доказывает
    отсутствие побочного действия: повтор передаёт ровно тот же ключ, а порт
    самостоятельно решает, возможно ли продолжение. Никаких секретов не принимает.
    """
    try:
        if (type(inspect_only) is not bool or type(workflow_key) is not str or _KEY.fullmatch(workflow_key) is None
                or type(keys) is not dict or set(keys) != set(_NAMES)
                or any(type(value) is not str or _KEY.fullmatch(value) is None for value in keys.values())
                or len({workflow_key, *keys.values()}) != len(_NAMES) + 1
                or type(envelope) is not bytes or not 0 < len(envelope) <= MAX_ENVELOPE
                or type(trusted_public_key) is not bytes or len(trusted_public_key) != 32
                or type(modes) is not tuple or not modes or any(type(x) is not str for x in modes)
                or len(set(modes)) != len(modes) or type(required_bytes) is not dict
                or set(required_bytes) != set(NAMES)
                or any(type(value) is not int or not 0 <= value <= MAX_BYTES for value in required_bytes.values())
                or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= MAX_BYTES
                or type(language) is not str or language not in available_languages()):
            raise FreshControllerError()
        root, work, directory = checked_path(install_root), checked_path(work_dir), _unit_directory(unit_dir)
        package = checked_path(package_path)
        info = work.lstat()
        if (contains(root, work) or contains(work, root) or contains(directory, work) or contains(work, directory)
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}):
            raise FreshControllerError()
        metadata = verify_signed_package(package, envelope, trusted_public_key=trusted_public_key,
                                         expected_product_id=product_id, expected_version=version)
        if check_target(metadata['manifest'], native_target())['status'] != 'supported':
            raise FreshControllerError()
        component_order(metadata['manifest'], modes)
        from shared.systemd_pointer import LAUNCHER_PATH, LAUNCHER_SHA256, RECIPE_PATH, RECIPE_V2
        entries = {item['path']: item for item in metadata['files']}
        pointer = RECIPE_PATH in entries
        if pointer:
            recipe, launcher = entries[RECIPE_PATH], entries.get(LAUNCHER_PATH)
            if (recipe['size'] != len(RECIPE_V2) or recipe['sha256'] != hashlib.sha256(RECIPE_V2).hexdigest()
                    or launcher is None or launcher['sha256'] != LAUNCHER_SHA256
                    or any(name.startswith('systemd-user/') and name != RECIPE_PATH for name in entries)):
                raise FreshControllerError()
        if not inspect_only:
            from shared.wheel_audit import WheelAuditError, audit_signed_wheels
            try:
                audit_signed_wheels(package, envelope, trusted_public_key=trusted_public_key,
                                    expected_product_id=product_id, expected_version=version)
            except WheelAuditError:
                raise FreshControllerError('wheel_audit_failed') from None
        common = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode('ascii'),
                  'trusted_public_key': base64.b64encode(trusted_public_key).decode('ascii'),
                  'expected_product_id': product_id, 'expected_version': version, 'install_root': str(root)}
        services = {**common, 'modes': list(modes), 'unit_dir': str(directory)}
        start = {**services, 'load_key': keys['load'], 'runtime_key': keys['runtime'],
                 'reserve_bytes': reserve_bytes, 'idempotency_key': keys['start']}
        web = metadata['manifest']['web']
        publish_name = 'publish-pointer-systemd-user-units' if pointer else 'publish-portable-systemd-user-units'
        load_name = 'load-pointer-systemd-user-units' if pointer else 'load-portable-systemd-user-units'
        start_name = 'start-pointer-linux' if pointer else 'start-portable-linux'
        runtime_binding = {'runtime_key': keys['runtime'], 'reserve_bytes': reserve_bytes} if pointer else {}
        steps = [
            ('prepare-native-release', {**common, 'required_bytes': required_bytes,
                                       'reserve_bytes': reserve_bytes, 'idempotency_key': keys['release']},
             'status', 'prepared'),
            ('prepare-linux-runtime', {**common, 'reserve_bytes': reserve_bytes,
                                       'idempotency_key': keys['runtime']}, 'state', 'prepared'),
            (publish_name, {**services, **runtime_binding, 'idempotency_key': keys['publish']},
             'state', 'published'),
            (load_name, {**services, **runtime_binding, 'idempotency_key': keys['load']},
             'state', 'loaded'),
            (start_name, start, 'state', 'started'),
            ('check-local-health', {'port': web['port'], 'path': web['healthz'],
                                    'product_id': product_id, 'version': version, 'timeout_seconds': 3},
             'health_response_matches', True),
        ]
        binding_data = {'workflow_key': workflow_key, 'work': str(work), 'steps': steps,
                        'metadata': metadata}
        binding = hashlib.sha256(json.dumps(binding_data, sort_keys=True, ensure_ascii=True,
                                             separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()
        legacy_binding = hashlib.sha256(json.dumps({**binding_data, 'language': language}, sort_keys=True,
            ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()
        marker = checked_path(work / ('install-' + workflow_key + '.json'))
        lock = checked_path(work / ('install-' + workflow_key + '.lock'))
        from shared.api import handle
        with operation_lock(lock):
            state = read_effect_receipt(marker)
            if state is None:
                if inspect_only:
                    return {'state': 'no_checkpoint', 'completed_steps': None, 'total_steps': len(steps),
                            'current_step': None, 'step_may_have_executed': None,
                            'checkpoint_matches_request': False, 'effects_verified': False,
                            'current_health_verified': False, 'resume_authorized': False,
                            'installation_complete': False}
                state = {'version': 2, 'binding': binding, 'index': 0, 'status': 'pending'}
                _atomic_write(marker, state)
            if (set(state) != {'version', 'binding', 'index', 'status'}
                    or type(state['version']) is not int or state['version'] not in (1, 2)
                    or state['binding'] != (legacy_binding if state['version'] == 1 else binding)
                    or type(state['index']) is not int
                    or not 0 <= state['index'] <= len(steps)
                    or state['status'] != ('done' if state['index'] == len(steps) else 'pending')):
                raise FreshControllerError()
            if inspect_only:
                if read_effect_receipt(marker) != state:
                    raise FreshControllerError()
                complete = state['status'] == 'done'
                return {'state': 'recorded_complete' if complete else 'pending',
                        'completed_steps': state['index'], 'total_steps': len(steps),
                        'current_step': None if complete else steps[state['index']][0],
                        'step_may_have_executed': not complete, 'checkpoint_matches_request': True,
                        'effects_verified': False, 'current_health_verified': False,
                        'resume_authorized': False, 'installation_complete': False}
            while state['index'] < len(steps):
                name, params, result_key, expected = steps[state['index']]
                deadline = time.monotonic() + min(metadata['manifest']['update']['health_wait'], 30)
                while True:
                    response = handle({'api_major': 1, 'request_id': 'fresh-workflow', 'operation': name,
                                       'params': params, 'language': language})
                    if (name != 'check-local-health' or response.get('ok')
                            or response.get('error', {}).get('code') != 'health_check_failed'
                            or time.monotonic() >= deadline):
                        break
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))
                if not response.get('ok'):
                    code = response.get('error', {}).get('code', 'controller_failed')
                    raise FreshControllerError(code if type(code) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code)
                                               else 'controller_failed')
                if (type(response.get('result')) is not dict
                        or type(response['result'].get(result_key)) is not type(expected)
                        or response['result'][result_key] != expected or read_effect_receipt(marker) != state):
                    raise FreshControllerError()
                state = {**state, 'index': state['index'] + 1}
                if state['index'] == len(steps):
                    state['status'] = 'done'
                _atomic_write(marker, state)
            if read_effect_receipt(marker) != state:
                raise FreshControllerError()
        return {'state': 'recorded_complete', 'completed_steps': len(steps),
                'health_verified_at_execution': True, 'current_health_verified': False,
                'process_identity_verified': False, 'installation_complete': False}
    except FreshControllerError:
        raise
    except Exception:
        raise FreshControllerError() from None
