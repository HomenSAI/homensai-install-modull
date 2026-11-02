# HomenS.AI · © 2026 Serhii Khomenko
"""Проверка подписанного рецепта и построение переносимых user-units.

Только чтение и генерация текста: запись units, systemctl и запуск отсутствуют.
"""
from __future__ import annotations

import hashlib

from shared.datahome import DataHomeError, checked_path, resolve_datahome
from shared.fresh_prepare import FreshPrepareError, verify_fresh_release
from shared.manifest import ManifestError
from shared.package import PackageError
from shared.package_signing import verify_signed_package
from shared.services import render_portable_user_services_v1
from shared.systemd_user_publish import UnitPublicationError, _read_unit

RECIPE_PATH = 'systemd-user/portable-v1.json'
RECIPE_V1 = b'{"format":1,"generator":"homensai-systemd-user-portable-v1"}\n'
MAX_UNIT_BYTES = 1024 * 1024


class PortableUnitError(RuntimeError):
    """Подписанный рецепт, выпуск или формирование units не подтверждены."""


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
