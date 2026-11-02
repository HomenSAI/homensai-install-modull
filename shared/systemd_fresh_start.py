# HomenS.AI · © 2026 Serhii Khomenko
"""Однократный запуск свежей подписанной группы с устойчивой отметкой."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from shared.datahome import checked_path
from shared.runtime_native import verify_signed_linux_runtime
from shared.systemd_user_load import signed_user_group, portable_signed_runtime_user_group
from shared.systemd_user_publish import _new_publication_receipt, _publication_receipt
from shared.update_plan import _atomic_write


class FreshStartError(RuntimeError):
    """Результат запуска не подтверждён; повтор действия может быть небезопасен."""


def start_portable_fresh(package_path, envelope, install_root, *, trusted_public_key,
                         expected_product_id, expected_version, modes, unit_dir,
                         load_key, runtime_key, reserve_bytes, operation_id, runner=None):
    """Однократно отправить start; pending никогда автоматически не повторяется."""
    if (any(type(key) is not str or re.fullmatch(r'[a-f0-9]{32}', key) is None
            for key in (load_key, runtime_key, operation_id))
            or len({load_key, runtime_key, operation_id}) != 3
            or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 2**63 - 1
            or type(envelope) is not bytes or type(trusted_public_key) is not bytes
            or len(trusted_public_key) != 32 or type(modes) is not tuple or not modes):
        raise FreshStartError('Недопустимые параметры запуска')
    try:
        root, directory = checked_path(install_root), checked_path(unit_dir)
        control = checked_path(root / '.homensai-control')
        for path in (root, control):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FreshStartError('Каталог управления не подтверждён')
        publication = _publication_receipt(control / 'user-systemd-publication.json')
        if publication is None or publication['status'] != 'done' or publication['operation_id'] == operation_id:
            raise FreshStartError('Публикация не подтверждена')
        binding = {'product_id': expected_product_id, 'version': expected_version,
                   'envelope_sha256': hashlib.sha256(envelope).hexdigest(),
                   'publisher_sha256': hashlib.sha256(trusted_public_key).hexdigest(),
                   'install_root': str(root), 'unit_dir': str(directory), 'modes': modes,
                   'load_key': load_key, 'runtime_key': runtime_key, 'reserve_bytes': reserve_bytes}
        digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        pending = {'version': 1, 'operation_id': operation_id, 'binding': digest, 'status': 'pending'}
        done = {**pending, 'status': 'done'}
        receipt = checked_path(control / 'user-systemd-start.json')
        old = _publication_receipt(receipt)
        if old is not None and old != done:
            raise FreshStartError('Предыдущий запуск требует проверки')
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                       expected_version=expected_version)
        verify_signed_linux_runtime(package_path, envelope, str(root), **options,
                                    operation_id=runtime_key, reserve_bytes=reserve_bytes)
        if old == done:
            group = signed_user_group(package_path, envelope, str(root), **options, modes=modes,
                        unit_dir=str(directory), operation_id=load_key, portable=True,
                        runner=runner, _require_active=True)
        else:
            group = portable_signed_runtime_user_group(package_path, envelope, str(root), **options,
                        modes=modes, unit_dir=str(directory), operation_id=load_key,
                        runtime_operation_id=runtime_key, runtime_reserve_bytes=reserve_bytes, runner=runner)
            _new_publication_receipt(receipt, pending)
            group.command('start', execute=True)
            if _publication_receipt(receipt) != pending:
                raise FreshStartError('Отметка запуска изменилась')
            _atomic_write(receipt, done)
        if _publication_receipt(receipt) != done:
            raise FreshStartError('Отметка запуска не подтверждена')
        return {'state': 'started', 'services_started': True, 'installation_complete': False,
                'health_verified': False, 'python_compatibility_verified': False}
    except FreshStartError:
        raise
    except Exception:
        raise FreshStartError('Не удалось подтвердить запуск служб') from None
