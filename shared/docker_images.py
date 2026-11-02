"""Проверка локальных ссылок образов подготовленного подписанного Compose."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time

from shared.datahome import checked_path
from shared.docker_preflight import _bounded_run
from shared.docker_prepare import prepare_signed_compose
from shared.docker_recipe import plan_signed_compose
from shared.host_target import native_target
from shared.native_migration import _checked_file
from shared.package import _object, _reject_constant


class DockerImagesError(RuntimeError):
    """Локальные ссылки образов не подтверждены; детали входа скрыты."""


def inspect_signed_compose_images(package_path: str, envelope: bytes, *, trusted_public_key: bytes,
                                  expected_product_id: str, expected_version: str, target: dict,
                                  modes: tuple[str, ...], instance_id: str, work_dir: str,
                                  operation_id: str, executable: str, executable_sha256: str,
                                  socket_path: str, timeout_seconds: int = 5) -> dict:
    """Сверить подготовку и ответы доверенного локального daemon без pull/start.

    Эта выборочная проверка не доказывает безопасность содержимого или состояние
    daemon в будущем. Проверяется локальная ссылка, не сертификат издателя образа.
    """
    try:
        if (type(executable) is not str or type(executable_sha256) is not str
                or re.fullmatch(r'[a-f0-9]{64}', executable_sha256) is None
                or type(socket_path) is not str or type(timeout_seconds) is not int
                or not 1 <= timeout_seconds <= 30
                or native_target() != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
                or target != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'docker'}):
            raise DockerImagesError('Недопустимая среда')
        socket = checked_path(socket_path)
        if not stat.S_ISSOCK(socket.lstat().st_mode):
            raise DockerImagesError('Нужен локальный socket')
        options = {'trusted_public_key': trusted_public_key, 'expected_product_id': expected_product_id,
                   'expected_version': expected_version, 'target': target, 'modes': modes,
                   'instance_id': instance_id}
        prepare_options = {**options, 'work_dir': work_dir, 'operation_id': operation_id, 'verify_only': True}
        prepared = prepare_signed_compose(package_path, envelope, **prepare_options)
        plan = plan_signed_compose(package_path, envelope, **options)
        if prepared['compose_sha256'] != plan['compose_sha256']:
            raise DockerImagesError('План изменился')
        compose = json.loads(plan['compose_json'], object_pairs_hook=_object, parse_constant=_reject_constant)
        images = sorted({item['image'] for item in compose['services'].values()})
        if not images:
            raise DockerImagesError('Нет образов')
        root = checked_path(work_dir)
        deadline = time.monotonic() + min(300, timeout_seconds * len(images))
        with tempfile.TemporaryDirectory(prefix='.docker-image-check-', dir=root) as config:
            env = {'PATH': os.defpath, 'HOME': config, 'DOCKER_CONFIG': config,
                   'COMPOSE_DISABLE_ENV_FILE': '1'}
            for image in images:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerImagesError('Время проверки истекло')
                program = _checked_file(executable, executable_sha256, executable=True)
                argv = [str(program), '--config', config, '--host', 'unix://' + str(socket),
                        'image', 'inspect', '--format', '{{json .RepoDigests}}', image]
                response = _bounded_run(argv, timeout=min(timeout_seconds, remaining), env=env)
                _checked_file(executable, executable_sha256, executable=True)
                if (response is None or type(response.returncode) is not int or response.returncode != 0
                        or type(response.stdout) is not str):
                    raise DockerImagesError('Daemon не подтвердил образ')
                digests = json.loads(response.stdout, object_pairs_hook=_object, parse_constant=_reject_constant)
                if (type(digests) is not list or not 1 <= len(digests) <= 128
                        or any(type(value) is not str or len(value) > 326 for value in digests)
                        or image not in digests):
                    raise DockerImagesError('Ссылка образа не подтверждена')
        if (prepare_signed_compose(package_path, envelope, **prepare_options) != prepared
                or plan_signed_compose(package_path, envelope, **options) != plan):
            raise DockerImagesError('Подготовка изменилась')
        return {'state': 'checked', 'image_count': len(images), 'compose_sha256': plan['compose_sha256'],
                'instance_id': instance_id, 'daemon_reported_matches': True,
                'image_contents_verified': False, 'publisher_verified': False,
                'host_verified': False, 'containers_started': False, 'installation_complete': False}
    except Exception:
        raise DockerImagesError('Не удалось подтвердить локальные ссылки образов') from None
