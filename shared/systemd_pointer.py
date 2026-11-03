"""Read-only planning of pointer-based systemd units from a signed release."""
from __future__ import annotations

import hashlib
from pathlib import Path

from shared.runtime_native import _definition
from shared.services import render_pointer_user_services_v2
from shared.systemd_portable import MAX_UNIT_BYTES, PortableUnitError
from shared.systemd_user_publish import _read_unit

RECIPE_PATH = 'systemd-user/pointer-v2.json'
RECIPE_V2 = b'{"format":2,"generator":"homensai-systemd-user-pointer-v2"}\n'
LAUNCHER_PATH = 'runtime-linux/lib/homensai/portable_launcher.py'
# Bump the signed recipe/generator version when the launcher contract changes.
LAUNCHER_SHA256 = '643a39c67d920a5baf157066c6f00844f05ab0310e18231939729cb30e82f006'


def plan_pointer_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                   expected_product_id, expected_version, modes):
    """Verify signed recipe and launcher bytes; return texts without publishing.

    This verifies neither the prepared runtime nor publisher trust, code safety,
    actual process identity or readiness to install. The local installer code
    and provided key must be independently trusted by the caller.
    """
    try:
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        definition = _definition(package_path, envelope, install_root, options)
        metadata, verified, root, home, release, runtime_recipe, runtime_files = definition
        entries = {item['path']: item for item in metadata['files']}
        recipe = entries.get(RECIPE_PATH)
        launcher = entries.get(LAUNCHER_PATH)
        if (recipe is None or recipe['size'] != len(RECIPE_V2)
                or any(name.startswith('systemd-user/') and name != RECIPE_PATH for name in entries)
                or launcher is None or launcher['sha256'] != LAUNCHER_SHA256
                or runtime_files.get('lib/homensai/portable_launcher.py') != launcher):
            raise PortableUnitError('Рецепт или загрузчик не подтверждены')
        if (_read_unit(release / RECIPE_PATH, recipe['sha256']) != RECIPE_V2
                or _read_unit(release / LAUNCHER_PATH, LAUNCHER_SHA256)
                != _read_unit(Path(__file__).with_name('portable_launcher.py'), LAUNCHER_SHA256)):
            raise PortableUnitError('Рецепт или загрузчик изменились')
        units = render_pointer_user_services_v2(metadata['manifest'], modes=modes, install_root=str(root))
        if not units or any(len(text.encode('utf-8')) > MAX_UNIT_BYTES for text in units.values()):
            raise PortableUnitError('Недопустимый размер units')
        if _definition(package_path, envelope, install_root, options) != definition:
            raise PortableUnitError('Выпуск изменился во время проверки')
        return {'state': 'planned', 'generator': 'homensai-systemd-user-pointer-v2',
                'units': [{'name': name, 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
                          for name, text in units.items()], 'unit_texts': units,
                'launcher_sha256': LAUNCHER_SHA256, 'signed_package_verified': True,
                'prepared_runtime_verified': False, 'publisher_trust_verified': False,
                'services_started': False, 'publication_readiness_verified': False}
    except Exception:
        raise PortableUnitError('Не удалось проверить подписанный рецепт загрузчика') from None
