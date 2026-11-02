# HomenS.AI · © 2026 Serhii Khomenko
"""Переносимый рецепт и ограниченная публикация user-units без их запуска."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from shared.datahome import DataHomeError, checked_path, resolve_datahome
from shared.fresh_prepare import FreshPrepareError, verify_fresh_release
from shared.manifest import ManifestError
from shared.package import PackageError
from shared.package_signing import verify_signed_package
from shared.services import render_portable_user_services_v1
from shared.systemd_user import SystemdUserError, _run
from shared.systemd_user_publish import (UnitPublicationError, _confirm_manager, _confirm_published,
    _existing_lock, _new_publication_receipt, _publication_receipt, _publish_files, _read_unit,
    _require_absent, _unit_directory)
from shared.update_plan import UpdateError, _atomic_write

RECIPE_PATH = 'systemd-user/portable-v1.json'
RECIPE_V1 = b'{"format":1,"generator":"homensai-systemd-user-portable-v1"}\n'
MAX_UNIT_BYTES = 1024 * 1024


class PortableUnitError(RuntimeError):
    """Подписанный рецепт, выпуск или формирование units не подтверждены."""


def _publication_binding(*, product_id, version, transaction, plan, modes, root, directory, trusted_public_key):
    context = {'product_id': product_id, 'version': version, 'transaction': transaction,
               'generator': plan['generator'], 'units': plan['units'], 'modes': modes,
               'install_sha256': hashlib.sha256(str(root).encode()).hexdigest(),
               'directory_sha256': hashlib.sha256(str(directory).encode()).hexdigest(),
               'publisher_sha256': hashlib.sha256(trusted_public_key).hexdigest()}
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan_portable_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                    expected_product_id, expected_version, modes):
    """Вернуть публичные тексты и хэши units, без изменения системы.

    Только подписанный точный рецепт v1 разрешает локальную генерацию. Приёмка
    среды, подлинность runtime и публикация результата остаются вне операции.
    """
    try:
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        metadata = verify_signed_package(package_path, envelope, **options)
        verified = verify_fresh_release(package_path, envelope, install_root, **options)
        if verified['process_target'] != {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}:
            raise PortableUnitError('Требуется нативный Linux x86-64')
        entries = {item['path']: item for item in metadata['files']}
        recipe = entries.get(RECIPE_PATH)
        if (recipe is None or recipe['size'] != len(RECIPE_V1)
                or any(name.startswith('systemd-user/') and name != RECIPE_PATH for name in entries)):
            raise PortableUnitError('Подписанный рецепт отсутствует или неоднозначен')
        root = checked_path(install_root)
        home = resolve_datahome(metadata['manifest'], root)
        recipe_file = home.releases / ('app-' + expected_version) / RECIPE_PATH
        if _read_unit(recipe_file, recipe['sha256']) != RECIPE_V1:
            raise PortableUnitError('Неподдерживаемый рецепт')
        units = render_portable_user_services_v1(metadata['manifest'], modes=modes,
                                                  install_root=str(root), version=expected_version)
        if not units or any(len(text.encode('utf-8')) > MAX_UNIT_BYTES for text in units.values()):
            raise PortableUnitError('Недопустимый размер units')
        if (verify_signed_package(package_path, envelope, **options) != metadata
                or verify_fresh_release(package_path, envelope, install_root, **options) != verified):
            raise PortableUnitError('Выпуск изменился во время проверки')
        return {'state': 'planned', 'generator': 'homensai-systemd-user-portable-v1',
                'units': [{'name': name, 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
                          for name, text in units.items()], 'unit_texts': units,
                'services_started': False, 'publication_readiness_verified': False}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, DataHomeError,
            ManifestError, PackageError, FreshPrepareError, UnitPublicationError):
        raise PortableUnitError('Не удалось проверить переносимый рецепт служб') from None


def publish_portable_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                       expected_product_id, expected_version, modes, unit_dir,
                                       execute=False, operation_id=None, runner=None, timeout=30):
    """Опубликовать units из подписанного рецепта без reload, enable или start.

    Запись доступна только с ключом операции. При pending автоматический повтор
    запрещён; done проверяет опубликованные файлы без повторной записи.
    """
    if (type(execute) is not bool or type(timeout) not in (int, float) or not 0 < timeout <= 120
            or (runner is not None and not callable(runner))
            or (operation_id is not None and (type(operation_id) is not str
                or re.fullmatch(r'[a-f0-9]{32}', operation_id) is None))
            or (execute and operation_id is None)):
        raise PortableUnitError('Недопустимые параметры публикации')
    try:
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        plan = plan_portable_signed_user_units(package_path, envelope, install_root, **options, modes=modes)
        public = {'state': 'published' if execute else 'planned', 'generator': plan['generator'],
                  'units': plan['units'], 'services_started': False}
        if not execute:
            return public
        metadata = verify_signed_package(package_path, envelope, **options)
        verified = verify_fresh_release(package_path, envelope, install_root, **options)
        root = checked_path(install_root)
        home = resolve_datahome(metadata['manifest'], root)
        control = checked_path(root / '.homensai-control')
        for path in (root, control):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PortableUnitError('Каталог управления не защищён')
        directory = _unit_directory(unit_dir)
        units = [(item['name'], item['sha256'], plan['unit_texts'][item['name']].encode('utf-8'))
                 for item in plan['units']]
        names = [item[0] for item in units]
        binding = _publication_binding(product_id=expected_product_id, version=expected_version,
                    transaction=verified['transaction'], plan=plan, modes=modes, root=root,
                    directory=directory, trusted_public_key=trusted_public_key)
        receipt = checked_path(control / 'user-systemd-publication.json')
        pending = {'version': 1, 'operation_id': operation_id, 'binding': binding, 'status': 'pending'}
        done = {**pending, 'status': 'done'}
        runner = _run if runner is None else runner

        def verify_again():
            if (plan_portable_signed_user_units(package_path, envelope, install_root, **options, modes=modes) != plan
                    or verify_signed_package(package_path, envelope, **options) != metadata
                    or verify_fresh_release(package_path, envelope, install_root, **options) != verified):
                raise PortableUnitError('Выпуск изменился при публикации')

        bootstrap = checked_path(root / '.homensai-bootstrap.lock')
        service_lock = checked_path(home.roots['services'] / 'operation.lock')
        with _existing_lock(bootstrap):
            with _existing_lock(service_lock):
                verify_again()
                update_state = checked_path(home.roots['services'] / metadata['manifest']['update']['state_file'].replace('\\', '/'))
                _require_absent(update_state)
                old = _publication_receipt(receipt)
                if old == done:
                    try:
                        _confirm_published(runner, directory, units, expected_product_id, timeout)
                    except Exception:
                        raise PortableUnitError('Публикация не подтверждена') from None
                    verify_again()
                    if _publication_receipt(receipt) != done:
                        raise PortableUnitError('Отметка публикации изменилась')
                    return public
                if old is not None:
                    raise PortableUnitError('Публикация требует восстановления')
                try:
                    _confirm_manager(runner, directory, names, timeout)
                except Exception:
                    raise PortableUnitError('Менеджер служб не подтвердил публикацию') from None
                _new_publication_receipt(receipt, pending)
                _publish_files(directory, names, units, public, expected_product_id, expected_version, verify_again)
                if _publication_receipt(receipt) != pending:
                    raise PortableUnitError('Отметка публикации изменилась')
                _atomic_write(receipt, done)
                if _publication_receipt(receipt) != done:
                    raise PortableUnitError('Отметка публикации не подтверждена')
        return public
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, DataHomeError,
            ManifestError, PackageError, FreshPrepareError, SystemdUserError, UnitPublicationError, UpdateError):
        raise PortableUnitError('Не удалось опубликовать переносимые службы') from None
