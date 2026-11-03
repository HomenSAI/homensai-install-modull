# HomenS.AI · © 2026 Serhii Khomenko
"""Проверка подготовленного runtime перед явным запуском свежей группы."""
from __future__ import annotations

import re
from pathlib import Path

from shared.runtime_native import NativeRuntimeError, verify_signed_linux_runtime
from shared.systemd_group import SystemdGroupError, SystemdUserGroup
from shared.wheel_audit import WheelAuditError, audit_signed_wheels


class RuntimeCheckedUserGroup:
    """Обёртка свежей группы; не является портом обновления или песочницей."""

    def __init__(self, group, package_path, envelope, install_root, *, trusted_public_key,
                 expected_product_id, expected_version, runtime_operation_id, reserve_bytes):
        if (not isinstance(group, SystemdUserGroup) or not isinstance(package_path, (str, Path))
                or type(install_root) is not str or type(envelope) is not bytes
                or type(trusted_public_key) is not bytes or len(trusted_public_key) != 32
                or type(runtime_operation_id) is not str
                or re.fullmatch(r'[a-f0-9]{32}', runtime_operation_id) is None
                or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 2**63 - 1):
            raise SystemdGroupError('Недопустимые параметры проверки runtime')
        self._group = group
        self._package_path, self._envelope, self._install_root = str(package_path), envelope, install_root
        self._options = dict(trusted_public_key=trusted_public_key, expected_product_id=expected_product_id,
                             expected_version=expected_version, operation_id=runtime_operation_id,
                             reserve_bytes=reserve_bytes)

    def command(self, action, *, execute=False):
        if action == 'start' and execute is True:
            try:
                audit_signed_wheels(self._package_path, self._envelope,
                    trusted_public_key=self._options['trusted_public_key'],
                    expected_product_id=self._options['expected_product_id'],
                    expected_version=self._options['expected_version'])
                verify_signed_linux_runtime(self._package_path, self._envelope, self._install_root, **self._options)
            except (NativeRuntimeError, WheelAuditError):
                raise SystemdGroupError('Не удалось подтвердить runtime перед запуском') from None
        return self._group.command(action, execute=execute)
