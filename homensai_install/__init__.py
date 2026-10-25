"""Публичный пакет HomenS.AI Install Module.

Версия 0.1 — рабочий контракт разработки, не сертификат готовности установщиков.
"""

__version__ = "0.1.0"
API_VERSION = "1.0"


def __getattr__(name):
    if name == "InstallerAPI":
        from .api.v1 import InstallerAPI
        return InstallerAPI
    raise AttributeError(name)


__all__ = ["InstallerAPI", "API_VERSION", "__version__"]
