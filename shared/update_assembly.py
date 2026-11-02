"""Сборка ограниченного плана из подготовленного подписанного выпуска."""
from __future__ import annotations

import math
import re
from pathlib import Path

from shared.adapters_fs import FilesystemPorts, ReleaseSource, _tree
from shared.linux_update_effects import LinuxSignedUpdateEffects, PreviousSignedRelease
from shared.package_signing import verify_signed_package
from shared.signed_migration_port import SignedMigrationPort
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import UpdateError, UpdatePlan
from shared.update_prepare import _expected_tree, prepare_signed_linux_update


def assemble_prepared_linux_update(*, group, password_provider, interpreter: str,
                                   interpreter_sha256: str, timeout: float, **options) -> UpdatePlan:
    """Связать проверенную подготовку и порты; не исполнять план или процессы.

    Группа, источники ключей, пароль, runtime и отсутствие сторонних писателей
    остаются ответственностью интегратора. Только для новой транзакции: чтение
    или восстановление уже начатой операции здесь не поддерживается.
    """
    try:
        if (not callable(password_provider) or type(interpreter) is not str
                or not Path(interpreter).is_absolute() or type(interpreter_sha256) is not str
                or re.fullmatch(r'[a-f0-9]{64}', interpreter_sha256) is None
                or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 3600):
            raise UpdateError('Недопустимые параметры исполнения')
        metadata = verify_signed_package(options['new_package'], options['new_envelope'],
            trusted_public_key=options['new_key'], expected_product_id=options['product_id'],
            expected_version=options['new_version'])
        source = Path(options['work_dir']) / ('payload-' + options['operation_id'])
        ports = FilesystemPorts(metadata['manifest'], Path(options['install_root']),
            work_dir=Path(options['work_dir']), payloads={options['new_version']: ReleaseSource(
                source, {item['path']: item['sha256'] for item in metadata['files']})},
            effects=None, password_provider=password_provider)
        plan = UpdatePlan(metadata['manifest'], Path(options['install_root']), ports,
            release=options['new_version'], modes=options['modes'], migration_ids=options['migration_ids'],
            guard_effects=True)
        lifecycle = SystemdLifecyclePort(plan, ports, install_root=options['install_root'], group=group)
        migration = SignedMigrationPort(plan, ports, install_root=options['install_root'],
            package_path=options['new_package'], envelope=options['new_envelope'],
            trusted_public_key=options['new_key'], interpreter=interpreter,
            interpreter_sha256=interpreter_sha256, timeout=timeout)
        previous = PreviousSignedRelease(options['old_package'], options['old_envelope'], options['old_key'])
        ports.effects = LinuxSignedUpdateEffects(lifecycle, migration, LocalUpdateHealthPort(plan, ports),
                                                previous_release=previous)
        result = prepare_signed_linux_update(**options)
        if (result['plan_sha256'] != plan.digest or result['payload_name'] != source.name
                or verify_signed_package(options['new_package'], options['new_envelope'],
                    trusted_public_key=options['new_key'], expected_product_id=options['product_id'],
                    expected_version=options['new_version']) != metadata
                or _tree(source) != _expected_tree(metadata)):
            raise UpdateError('Подготовка не соответствует плану')
        return plan
    except Exception:
        raise UpdateError('Не удалось собрать план подписанного обновления') from None
