"""Read-only planning of pointer-based systemd units from a signed release."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import stat

from shared.datahome import checked_path
from shared.runtime_native import _definition, _inspect_runtime, verify_signed_linux_runtime
from shared.services import render_pointer_user_services_v2
from shared.systemd_portable import MAX_UNIT_BYTES, PortableUnitError, _publication_binding
from shared.systemd_user import _run
from shared.systemd_user_publish import (_confirm_manager, _confirm_published, _existing_lock,
    _new_publication_receipt, _publication_receipt, _publish_files, _read_unit, _require_absent, _unit_directory)
from shared.update_plan import _atomic_write

RECIPE_PATH = 'systemd-user/pointer-v2.json'
RECIPE_V2 = b'{"format":2,"generator":"homensai-systemd-user-pointer-v2"}\n'
LAUNCHER_PATH = 'runtime-linux/lib/homensai/portable_launcher.py'
# Bump the signed recipe/generator version when the launcher contract changes.
LAUNCHER_SHA256 = '643a39c67d920a5baf157066c6f00844f05ab0310e18231939729cb30e82f006'


def _pointer_publication_binding(*, runtime_operation_id, reserve_bytes, **options):
    base = _publication_binding(**options)
    return hashlib.sha256((base + ':' + runtime_operation_id + ':' + str(reserve_bytes)).encode('ascii')).hexdigest()


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


def publish_pointer_signed_user_units(package_path, envelope, install_root, *, trusted_public_key,
                                     expected_product_id, expected_version, modes, unit_dir,
                                     runtime_operation_id, reserve_bytes, operation_id,
                                     execute=False, runner=None, timeout=30):
    """Publish only after verifying the exact prepared runtime; never reload or start."""
    if (type(execute) is not bool or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 120 or (runner is not None and not callable(runner))
            or any(type(key) is not str or re.fullmatch(r'[a-f0-9]{32}', key) is None
                   for key in (runtime_operation_id, operation_id))
            or runtime_operation_id == operation_id or type(reserve_bytes) is not int
            or not 0 <= reserve_bytes <= 2**63 - 1):
        raise PortableUnitError('Недопустимые параметры публикации')
    try:
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        plan = plan_pointer_signed_user_units(package_path, envelope, install_root, **options, modes=modes)
        public = {'state': 'published' if execute else 'planned', 'generator': plan['generator'],
                  'units': plan['units'], 'prepared_runtime_verified': False, 'services_started': False}
        if not execute:
            return public
        definition = _definition(package_path, envelope, install_root, options)
        metadata, verified, root, home, _, runtime_recipe, files = definition
        control = checked_path(root / '.homensai-control')
        runtime = checked_path(home.roots['runtime'])
        for path in (root, control):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PortableUnitError('Каталог управления не защищён')
        runtime_receipt_path = checked_path(control / 'linux-runtime.json')
        runtime_receipt = _publication_receipt(runtime_receipt_path)
        if (runtime_receipt is None or runtime_receipt['status'] != 'done'
                or runtime_receipt['operation_id'] != runtime_operation_id):
            raise PortableUnitError('Подготовка runtime не подтверждена')
        verify_signed_linux_runtime(package_path, envelope, install_root, **options,
                                    operation_id=runtime_operation_id, reserve_bytes=reserve_bytes)
        if _publication_receipt(runtime_receipt_path) != runtime_receipt:
            raise PortableUnitError('Отметка runtime изменилась')
        directory = _unit_directory(unit_dir)
        units = [(item['name'], item['sha256'], plan['unit_texts'][item['name']].encode('utf-8'))
                 for item in plan['units']]
        names = [item[0] for item in units]
        binding = _pointer_publication_binding(product_id=expected_product_id, version=expected_version,
            transaction=verified['transaction'], plan=plan, modes=modes, root=root,
            directory=directory, trusted_public_key=trusted_public_key,
            runtime_operation_id=runtime_operation_id, reserve_bytes=reserve_bytes)
        receipt = checked_path(control / 'user-systemd-publication.json')
        pending = {'version': 1, 'operation_id': operation_id, 'binding': binding, 'status': 'pending'}
        done = {**pending, 'status': 'done'}
        runner = _run if runner is None else runner
        public['prepared_runtime_verified'] = True

        def verify_again():
            if (plan_pointer_signed_user_units(package_path, envelope, install_root, **options, modes=modes) != plan
                    or _definition(package_path, envelope, install_root, options) != definition
                    or _publication_receipt(runtime_receipt_path) != runtime_receipt):
                raise PortableUnitError('Выпуск или отметка runtime изменились')
            _inspect_runtime(runtime, files, set(runtime_recipe['executables']))

        with _existing_lock(checked_path(root / '.homensai-bootstrap.lock')):
            with _existing_lock(checked_path(home.roots['services'] / 'operation.lock')):
                verify_again()
                state = checked_path(home.roots['services'] / metadata['manifest']['update']['state_file'].replace('\\', '/'))
                _require_absent(state)
                old = _publication_receipt(receipt)
                if old == done:
                    _confirm_published(runner, directory, units, expected_product_id, timeout)
                    verify_again()
                    if _publication_receipt(receipt) != done:
                        raise PortableUnitError('Отметка публикации изменилась')
                    return public
                if old is not None:
                    raise PortableUnitError('Публикация требует восстановления')
                _confirm_manager(runner, directory, names, timeout)
                _new_publication_receipt(receipt, pending)
                _publish_files(directory, names, units, public, expected_product_id, expected_version, verify_again)
                if _publication_receipt(receipt) != pending:
                    raise PortableUnitError('Отметка публикации изменилась')
                _atomic_write(receipt, done)
                if _publication_receipt(receipt) != done:
                    raise PortableUnitError('Публикация не подтверждена')
        return public
    except Exception:
        raise PortableUnitError('Не удалось опубликовать службы с загрузчиком') from None
