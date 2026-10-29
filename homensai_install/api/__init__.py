"""Версионированные публичные интерфейсы; реализация ядра не является API."""

from .v1 import InstallerAPI, ProductAdapter

__all__ = ["InstallerAPI", "ProductAdapter"]
