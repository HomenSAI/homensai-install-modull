# HomenS.AI · © 2026 Serhii Khomenko
"""Отказ от неподходящих версий криптозависимостей, без проверки файлов wheel."""
from importlib.metadata import PackageNotFoundError, version

LOCKED_VERSIONS = {'cryptography': '50.0.2', 'cffi': '2.1.1', 'pycparser': '3.0'}


class CryptoPolicyError(RuntimeError):
    """Установленные метаданные не соответствуют закреплённому кандидату."""


def require_locked_crypto() -> None:
    """Проверка версий — не проверка происхождения или целостности файлов."""
    try:
        if any(version(name) != expected for name, expected in LOCKED_VERSIONS.items()):
            raise CryptoPolicyError('Криптозависимости не соответствуют lock')
    except (PackageNotFoundError, OSError, ValueError):
        raise CryptoPolicyError('Криптозависимости недоступны') from None
