# HomenS.AI · © 2026 Serhii Khomenko
"""Compose v1: план по умолчанию, эффекты только через внедрённые callable.

Это низкоуровневые service/health порты, не полный durable UpdatePorts v2.
Манифест и Compose должны быть публичными и доверенными вызывающей стороной.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import subprocess
from typing import Callable
import urllib.request

from shared.datahome import checked_path
from shared.manifest import ManifestError, validate_manifest


class DockerAdapterError(RuntimeError):
    """Ошибка без stdout/stderr, argv и текста исключения внешнего инструмента."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_health(url: str, *, timeout: float) -> int:
    """Один HTTP-запрос без proxy, redirect, чтения тела и окружения."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        return response.status


def _timeout(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= 600):
        raise DockerAdapterError("Timeout должен быть числом от 0 до 600 секунд, исключая 0")
    return value


def _execution(value):
    if type(value) is not bool:
        raise DockerAdapterError("Требуется явный логический execute")
    return value


def _compose_path(value):
    # Абсолютный путь не может стать опцией -f; проверка до Path-нормализации.
    if not isinstance(value, (str, Path)):
        raise DockerAdapterError("Требуется путь Compose")
    if any(char in str(value) for char in ';|&$`<>\"\'()%'):
        raise DockerAdapterError("Недопустимый путь Compose")
    path = checked_path(value)
    if path.suffix.lower() not in (".yml", ".yaml") or (path.exists() and not path.is_file()):
        raise DockerAdapterError("Требуется обычный файл Compose .yml/.yaml")
    return path


class DockerComposeAdapter:
    """start/stop/ps/logs -> argv либо CompletedProcess; health -> план либо True.

    runner имеет контракт subprocess.run; fetcher(url, timeout=...) -> HTTP status.
    Ни конструктор, ни dry-run не вызывают runner/fetcher и ничего не записывают.
    execute=True разрешается отдельно для каждого вызова, без постоянного флага.
    """

    def __init__(self, manifest, compose_file: str | Path, *,
                 runner: Callable | None = None, fetcher: Callable | None = None,
                 timeout: float = 30, health_timeout: float = 3):
        manifest = validate_manifest(manifest)
        if manifest["schema_version"] != 1:
            raise ManifestError("Docker-адаптер поддерживает только Docker-рендер v1")
        product = manifest["product_id"]
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", product) is None:
            raise DockerAdapterError("Недопустимый ID проекта Compose")
        self._product = product
        self._compose = _compose_path(compose_file)
        # Дополнительная точная проверка: без DNS, credentials, query и fragment.
        url = manifest["healthcheck_url"]
        if re.fullmatch(r"http://127\.0\.0\.1:" + str(manifest["port"])
                        + r"/[A-Za-z0-9/_.~-]*", url) is None:
            raise DockerAdapterError("Health URL должен быть HTTP на 127.0.0.1 и порту продукта")
        self._url = url
        self._timeout = _timeout(timeout)
        self._health_timeout = _timeout(health_timeout)
        self._runner = subprocess.run if runner is None else runner
        self._fetcher = fetch_health if fetcher is None else fetcher
        if not callable(self._runner) or not callable(self._fetcher):
            raise DockerAdapterError("Runner и fetcher должны быть callable")

    def command(self, action: str, *, execute: bool = False):
        """Фиксированный набор действий: без произвольных argv, build и hooks."""
        _execution(execute)
        if type(action) is not str or action not in ("up", "stop", "ps", "logs"):
            raise DockerAdapterError("Неизвестное действие Compose")
        path = _compose_path(self._compose)
        argv = ["docker", "compose", "-p", self._product, "-f", str(path), action]
        if action == "up":
            argv.append("-d")
        if not execute:
            return argv
        if not path.is_file():
            raise DockerAdapterError("Файл Compose для исполнения отсутствует")
        try:
            result = self._runner(
                argv, shell=False, timeout=self._timeout, check=False,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                cwd=str(path.parent), env={"COMPOSE_DISABLE_ENV_FILE": "1"})
        except subprocess.TimeoutExpired:
            raise DockerAdapterError("Превышен timeout Compose") from None
        except Exception:
            raise DockerAdapterError("Вызов Compose не завершён") from None
        if result.returncode != 0:
            raise DockerAdapterError("Compose вернул ошибку")
        return result

    def start(self, *, execute: bool = False):
        return self.command("up", execute=execute)

    def stop(self, *, execute: bool = False):
        return self.command("stop", execute=execute)

    def ps(self, *, execute: bool = False):
        return self.command("ps", execute=execute)

    def logs(self, *, execute: bool = False):
        return self.command("logs", execute=execute)

    def health(self, *, execute: bool = False):
        """Одна проверка HTTP 200; не подтверждает идентичность выпуска/установки."""
        _execution(execute)
        if not execute:
            return {"url": self._url, "timeout": self._health_timeout}
        try:
            status = self._fetcher(self._url, timeout=self._health_timeout)
        except TimeoutError:
            raise DockerAdapterError("Превышен timeout HTTP health") from None
        except Exception:
            raise DockerAdapterError("HTTP health недоступен") from None
        if type(status) is not int or status != 200:
            raise DockerAdapterError("HTTP health не подтвердил статус 200")
        return True
