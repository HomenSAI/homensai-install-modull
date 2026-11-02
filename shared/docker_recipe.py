"""Проверяемый план нескольких Compose-сервисов из подписанного пакета v2.

Только план: здесь нет запуска Docker, сети, записи файлов или проверки образов.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile

from shared.datahome import checked_path
from shared.manifest import check_target
from shared.package import _object, _reject_constant
from shared.package_signing import verify_signed_package
from shared.services import component_order

RECIPE_PATH = 'homensai/docker/recipe-v1.json'
_MAX_RECIPE = 64 * 1024
_MAX_COMPOSE = 256 * 1024
_IMAGE = re.compile(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?'
                    r'(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[a-f0-9]{64}\Z')


class DockerRecipeError(RuntimeError):
    """Подписанный рецепт не подтверждён; входные данные не раскрываются."""


def plan_signed_compose(package_path: str, envelope: bytes, *, trusted_public_key: bytes,
                        expected_product_id: str, expected_version: str,
                        target: dict, modes: tuple[str, ...], instance_id: str) -> dict:
    """Построить детерминированный Compose JSON; ничего не исполнять и не писать.

    Рецепт — файл внутри подписанного ZIP, не отдельный доверенный ввод.
    Целевая ОС лишь сверяется с декларацией, не определяется для хоста.
    """
    try:
        if (type(package_path) is not str or type(target) is not dict
                or type(instance_id) is not str or re.fullmatch(r'[a-f0-9]{32}', instance_id) is None
                or target.get('execution') != 'docker' or type(modes) is not tuple or not modes
                or any(type(mode) is not str for mode in modes) or len(set(modes)) != len(modes)):
            raise DockerRecipeError('Недопустимый запрос')
        path = checked_path(package_path)
        options = {'trusted_public_key': trusted_public_key, 'expected_product_id': expected_product_id,
                   'expected_version': expected_version}
        metadata = verify_signed_package(path, envelope, **options)
        manifest = metadata['manifest']
        if (manifest['schema_version'] != 2 or check_target(manifest, target)['status'] != 'supported'
                or any('service' in secret['contexts'] for secret in manifest['secrets'])
                or any(manifest['hooks'][name] for name in ('pre_start', 'health_checks', 'post_update'))):
            raise DockerRecipeError('Профиль не поддерживается')
        order = component_order(manifest, modes)
        entries = {item['path']: item for item in metadata['files']}
        entry = entries.get(RECIPE_PATH)
        if entry is None or type(entry['size']) is not int or not 0 < entry['size'] <= _MAX_RECIPE:
            raise DockerRecipeError('Рецепт недоступен')
        with zipfile.ZipFile(path) as archive:
            with archive.open('payload/' + RECIPE_PATH) as stream:
                body = stream.read(_MAX_RECIPE + 1)
        if len(body) != entry['size'] or hashlib.sha256(body).hexdigest() != entry['sha256']:
            raise DockerRecipeError('Рецепт изменился')
        recipe = json.loads(body.decode('utf-8'), object_pairs_hook=_object, parse_constant=_reject_constant)
        names = {component['name'] for component in manifest['components']}
        if (type(recipe) is not dict or set(recipe) != {'format', 'images', 'web_component', 'container_port'}
                or type(recipe['format']) is not int or recipe['format'] != 1
                or type(recipe['images']) is not dict or set(recipe['images']) != names
                or any(type(value) is not str or len(value) > 326 or _IMAGE.fullmatch(value) is None
                       for value in recipe['images'].values())
                or type(recipe['web_component']) is not str or recipe['web_component'] not in order
                or type(recipe['container_port']) is not int or not 1 <= recipe['container_port'] <= 65535):
            raise DockerRecipeError('Недопустимый рецепт')
        components = {component['name']: component for component in manifest['components']}
        services = {}
        for name in order:
            component = components[name]
            services[name] = {'image': recipe['images'][name], 'pull_policy': 'never',
                'entrypoint': [], 'command': [argument.replace('$', '$$') for argument in component['cmd']],
                'user': '10001:10001', 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'], 'volumes': ['data:/data']}
            if component['after']:
                services[name]['depends_on'] = component['after']
            if name == recipe['web_component']:
                services[name]['ports'] = [f"127.0.0.1:{manifest['web']['port']}:{recipe['container_port']}"]
        compose = {'name': manifest['product_id'] + '-' + instance_id,
                   'services': services, 'volumes': {'data': {}}}
        output = json.dumps(compose, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n'
        if len(output) > _MAX_COMPOSE:
            raise DockerRecipeError('План слишком велик')
        if verify_signed_package(path, envelope, **options) != metadata:
            raise DockerRecipeError('Пакет изменился')
        return {'state': 'planned', 'compose_json': output,
                'compose_sha256': hashlib.sha256(output.encode('ascii')).hexdigest(),
                'components': list(order), 'target': dict(target), 'instance_id': instance_id, 'host_verified': False,
                'images_verified': False, 'containers_started': False, 'installation_complete': False}
    except Exception:
        raise DockerRecipeError('Не удалось подтвердить подписанный Docker-план') from None
