"""Ограниченный одноразовый start/stop подписанного Compose на Linux."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time

from shared.datahome import checked_path
from shared.docker_images import inspect_signed_compose_images
from shared.docker_preflight import _bounded_run
from shared.docker_prepare import prepare_signed_compose
from shared.docker_recipe import plan_signed_compose
from shared.effect_receipt import read_effect_receipt
from shared.host_target import native_target
from shared.native_migration import _checked_file
from shared.package import _object, _reject_constant
from shared.update_plan import _atomic_write, operation_lock


class DockerLifecycleError(RuntimeError):
    """Действие не подтверждено; оно могло частично выполниться."""


def _binding(package_path, root, socket_path, executable, executable_sha256, envelope,
             trusted_public_key, plan, target, modes, preparation_id, instance_id):
    context = {'package': str(checked_path(package_path)), 'work': str(root),
               'socket': str(checked_path(socket_path)), 'executable': str(checked_path(executable)),
               'executable_sha256': executable_sha256, 'envelope': hashlib.sha256(envelope).hexdigest(),
               'key': hashlib.sha256(trusted_public_key).hexdigest(), 'plan': plan['compose_sha256'],
               'target': target, 'modes': modes, 'preparation': preparation_id, 'instance': instance_id}
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def _validate_record(record, binding, preparation_id):
    if record is not None and (set(record) != {'version', 'binding', 'start_key', 'stop_key', 'status'}
            or type(record['version']) is not int or record['version'] != 1 or record['binding'] != binding
            or type(record['start_key']) is not str or re.fullmatch(r'[a-f0-9]{32}', record['start_key']) is None
            or record['start_key'] == preparation_id
            or record['status'] not in ('start_pending', 'running', 'stop_pending', 'stopped')
            or (record['status'] in ('start_pending', 'running') and record['stop_key'] is not None)
            or (record['status'] in ('stop_pending', 'stopped') and
                (type(record['stop_key']) is not str or re.fullmatch(r'[a-f0-9]{32}', record['stop_key']) is None
                 or record['stop_key'] in (record['start_key'], preparation_id)))):
        raise DockerLifecycleError('Журнал не соответствует операции')


def status_signed_compose(package_path: str, envelope: bytes, *, trusted_public_key: bytes,
                          expected_product_id: str, expected_version: str, target: dict,
                          modes: tuple[str, ...], instance_id: str, work_dir: str,
                          preparation_id: str, executable: str, executable_sha256: str,
                          socket_path: str) -> dict:
    """Read a bound receipt without launching a CLI or authorizing recovery."""
    try:
        if (type(preparation_id) is not str or re.fullmatch(r'[a-f0-9]{32}', preparation_id) is None
                or type(executable) is not str or type(socket_path) is not str
                or type(executable_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', executable_sha256) is None
                or native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
                or target != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'docker'}):
            raise DockerLifecycleError('Недопустимая среда')
        root = checked_path(work_dir)
        common = {'trusted_public_key': trusted_public_key, 'expected_product_id': expected_product_id,
                  'expected_version': expected_version, 'target': target, 'modes': modes, 'instance_id': instance_id}
        verified = prepare_signed_compose(package_path, envelope, **common, work_dir=str(root),
                                           operation_id=preparation_id, verify_only=True)
        plan = plan_signed_compose(package_path, envelope, **common)
        if verified['compose_sha256'] != plan['compose_sha256']:
            raise DockerLifecycleError('План изменился')
        binding = _binding(package_path, root, socket_path, executable, executable_sha256, envelope,
                           trusted_public_key, plan, target, modes, preparation_id, instance_id)
        marker = checked_path(root / ('compose-lifecycle-' + instance_id + '.json'))
        lock = checked_path(root / ('compose-lifecycle-' + instance_id + '.lock'))
        with operation_lock(lock):
            record = read_effect_receipt(marker)
            _validate_record(record, binding, preparation_id)
            state = 'not_recorded' if record is None else record['status']
        return {'state': state, 'receipt_present': record is not None,
                'daemon_state_verified': False, 'recovery_authorized': False}
    except Exception:
        raise DockerLifecycleError('Не удалось прочитать состояние управления контейнерами') from None


def _status_records(output, project, services, *, stopped=False):
    """Проверить ровно один контейнер на сервис по ответу доверенного daemon."""
    if not output.strip():
        raise DockerLifecycleError('Нет контейнеров')
    try:
        stripped = output.strip()
        rows = (json.loads(stripped, object_pairs_hook=_object, parse_constant=_reject_constant)
                if stripped.startswith('[') else
                [json.loads(line, object_pairs_hook=_object, parse_constant=_reject_constant)
                 for line in stripped.splitlines()])
    except (ValueError, RecursionError):
        raise DockerLifecycleError('Некорректный ответ') from None
    if type(rows) is not list or len(rows) != len(services):
        raise DockerLifecycleError('Состав контейнеров не подтверждён')
    found = {}
    allowed = ('exited',) if stopped is True else ('running',) if stopped is False else ('running', 'exited', 'created')
    for row in rows:
        if (type(row) is not dict or row.get('Project') != project or row.get('Service') not in services
                or type(row.get('ID')) is not str or re.fullmatch(r'[a-f0-9]{12,64}', row['ID']) is None
                or row['Service'] in found
                or row.get('State') not in allowed):
            raise DockerLifecycleError('Состояние контейнера не подтверждено')
        found[row['Service']] = row['ID']
    if set(found) != set(services) or len(set(found.values())) != len(found):
        raise DockerLifecycleError('Неполный состав')
    return found


def control_signed_compose(package_path: str, envelope: bytes, *, trusted_public_key: bytes,
                           expected_product_id: str, expected_version: str, target: dict,
                           modes: tuple[str, ...], instance_id: str, work_dir: str,
                           preparation_id: str, action_id: str, action: str,
                           executable: str, executable_sha256: str, socket_path: str,
                           timeout_seconds: int = 30) -> dict:
    """Один start и один stop; неизвестный результат не повторяется автоматически.

    Проверяет ответы доверенного daemon, но не безопасность образов и не всех
    писателей. Внешнее изменение daemon и конкурирующие управляющие процессы
    находятся вне блокировки этого модуля.
    """
    try:
        if (type(action) is not str or action not in ('start', 'stop')
                or type(preparation_id) is not str or re.fullmatch(r'[a-f0-9]{32}', preparation_id) is None
                or type(action_id) is not str or re.fullmatch(r'[a-f0-9]{32}', action_id) is None
                or action_id == preparation_id or type(executable) is not str
                or type(executable_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', executable_sha256) is None
                or type(socket_path) is not str or type(timeout_seconds) is not int
                or not 1 <= timeout_seconds <= 120
                or native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
                or target != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'docker'}):
            raise DockerLifecycleError('Недопустимый запрос')
        socket = checked_path(socket_path)
        if not stat.S_ISSOCK(socket.lstat().st_mode):
            raise DockerLifecycleError('Недоступен socket')
        root = checked_path(work_dir)
        common = {'trusted_public_key': trusted_public_key, 'expected_product_id': expected_product_id,
                  'expected_version': expected_version, 'target': target, 'modes': modes, 'instance_id': instance_id}
        prep = {**common, 'work_dir': str(root), 'operation_id': preparation_id, 'verify_only': True}
        verified = prepare_signed_compose(package_path, envelope, **prep)
        plan = plan_signed_compose(package_path, envelope, **common)
        compose = json.loads(plan['compose_json'], object_pairs_hook=_object, parse_constant=_reject_constant)
        if verified['compose_sha256'] != plan['compose_sha256']:
            raise DockerLifecycleError('План изменился')
        project, services = compose['name'], compose['services']
        deadline = time.monotonic() + 300
        binding = _binding(package_path, root, str(socket), executable, executable_sha256, envelope,
                           trusted_public_key, plan, target, modes, preparation_id, instance_id)
        marker = checked_path(root / ('compose-lifecycle-' + instance_id + '.json'))
        lock = checked_path(root / ('compose-lifecycle-' + instance_id + '.lock'))
        with operation_lock(lock):
            record = read_effect_receipt(marker)
            _validate_record(record, binding, preparation_id)
            if (record is None and action != 'start') or (record is not None and
                    ((action == 'start' and (record['start_key'] != action_id or record['status'] != 'running'))
                     or (action == 'stop' and (action_id == record['start_key'] or
                         record['status'] not in ('running', 'stopped') or
                         (record['status'] == 'stopped' and record['stop_key'] != action_id))))):
                raise DockerLifecycleError('Повтор требует проверки')
            if action == 'start':
                inspect_signed_compose_images(package_path, envelope, **common, work_dir=str(root),
                    operation_id=preparation_id, executable=executable, executable_sha256=executable_sha256,
                    socket_path=str(socket), timeout_seconds=min(timeout_seconds, 30))
            with tempfile.TemporaryDirectory(prefix='.docker-control-', dir=root) as config:
                env = {'PATH': os.defpath, 'HOME': config, 'DOCKER_CONFIG': config,
                       'COMPOSE_DISABLE_ENV_FILE': '1'}
                def run(args):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DockerLifecycleError('Время операции истекло')
                    program = _checked_file(executable, executable_sha256, executable=True)
                    prefix = [str(program), '--config', config, '--host', 'unix://' + str(socket)]
                    response = _bounded_run(prefix + args, timeout=min(timeout_seconds, remaining), env=env)
                    _checked_file(executable, executable_sha256, executable=True)
                    if (response is None or type(response.returncode) is not int or response.returncode != 0
                            or type(response.stdout) is not str):
                        raise DockerLifecycleError('Команда не подтверждена')
                    return response.stdout
                version = run(['compose', 'version', '--short']).strip()
                if re.fullmatch(r'v?2\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.\-]{1,40})?', version) is None:
                    raise DockerLifecycleError('Нужен Compose v2')
                compose_path = str(checked_path(root / verified['compose_name']))
                prefix = ['compose', '-p', project, '-f', compose_path]
                def verify_containers(*, stopped):
                    rows = _status_records(run(prefix + ['ps', '--all', '--format', 'json']),
                                           project, services, stopped=stopped)
                    for name, container_id in rows.items():
                        text = run(['inspect', '--format', '{{json .Config.Image}}', container_id])
                        if json.loads(text, object_pairs_hook=_object, parse_constant=_reject_constant) != services[name]['image']:
                            raise DockerLifecycleError('Образ контейнера не соответствует плану')
                if record is None:
                    help_text = run(['compose', 'up', '--help'])
                    if any(re.search(r'(?m)^\s*' + re.escape(flag) + r'(?:[=\s]|$)', help_text) is None
                           for flag in ('--no-build', '--no-recreate', '--pull')):
                        raise DockerLifecycleError('Необходимые флаги недоступны')
                    existing = run(['container', 'ls', '--all', '--filter',
                                    'label=com.docker.compose.project=' + project, '--quiet'])
                    volumes = run(['volume', 'ls', '--quiet']).splitlines()
                    networks = run(['network', 'ls', '--format', '{{.Name}}']).splitlines()
                    if (any(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', name) is None
                            for name in volumes + networks)
                            or len(set(volumes)) != len(volumes) or len(set(networks)) != len(networks)
                            or existing.strip() or project + '_data' in volumes or project + '_default' in networks):
                        raise DockerLifecycleError('Ресурсы проекта уже существуют')
                    if (prepare_signed_compose(package_path, envelope, **prep) != verified
                            or read_effect_receipt(marker) is not None):
                        raise DockerLifecycleError('Подготовка изменилась')
                    record = {'version': 1, 'binding': binding, 'start_key': action_id,
                              'stop_key': None, 'status': 'start_pending'}
                    _atomic_write(marker, record)
                    run(prefix + ['up', '--detach', '--no-build', '--no-recreate', '--pull', 'never'])
                    verify_containers(stopped=False)
                    if read_effect_receipt(marker) != record:
                        raise DockerLifecycleError('Журнал изменился')
                    record = {**record, 'status': 'running'}
                    _atomic_write(marker, record)
                elif action == 'stop' and record['status'] == 'running':
                    verify_containers(stopped=None)
                    if read_effect_receipt(marker) != record:
                        raise DockerLifecycleError('Журнал изменился')
                    record = {**record, 'stop_key': action_id, 'status': 'stop_pending'}
                    _atomic_write(marker, record)
                    run(prefix + ['stop', '--timeout', '10'])
                    verify_containers(stopped=True)
                    if read_effect_receipt(marker) != record:
                        raise DockerLifecycleError('Журнал изменился')
                    record = {**record, 'status': 'stopped'}
                    _atomic_write(marker, record)
                else:
                    verify_containers(stopped=(action == 'stop'))
                if (read_effect_receipt(marker) != record
                        or prepare_signed_compose(package_path, envelope, **prep) != verified):
                    raise DockerLifecycleError('Данные изменились')
        return {'state': 'running' if action == 'start' else 'stopped',
                'instance_id': instance_id, 'container_count': len(services),
                'daemon_state_verified': True, 'health_verified': False,
                'image_contents_verified': False, 'all_writers_verified': False,
                'installation_complete': False}
    except Exception:
        raise DockerLifecycleError('Не удалось подтвердить управление контейнерами') from None
