# HomenS.AI · © 2026 Serhii Khomenko
"""Определение среды текущего процесса для нативного варианта."""
from __future__ import annotations

import platform
import struct
import sys


class HostTargetError(ValueError):
    """Не удалось надёжно сопоставить среду с поддерживаемой целью."""


def native_target() -> dict[str, str]:
    """Получить известную ОС и архитектуру текущего процесса без побочных действий.

    Проверяется видимая процессу среда, не хост вне контейнера и не
    физическая архитектура оборудования при эмуляции. Docker не определяется.
    """
    operating_system = {'linux': 'linux', 'win32': 'windows'}.get(sys.platform)
    try:
        machine = platform.machine().lower()
        pointer_bits = struct.calcsize('P') * 8
    except (OSError, ValueError, AttributeError):
        raise HostTargetError('Не удалось определить платформу процесса') from None
    allowed = {'linux': {'x86_64'}, 'windows': {'amd64', 'x86_64'}}
    if operating_system is None or machine not in allowed[operating_system] or pointer_bits != 64:
        raise HostTargetError('Платформа процесса не поддерживается')
    return {'os': operating_system, 'architecture': 'x86_64', 'execution': 'native'}
