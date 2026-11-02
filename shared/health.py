# HomenS.AI · © 2026 Serhii Khomenko
"""Ограниченная локальная проверка публичной идентичности продукта."""
from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request

MAX_RESPONSE = 16384
_PRODUCT = re.compile(r'[a-z0-9][a-z0-9_-]{0,62}\Z')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
_PATH = re.compile(r'/[A-Za-z0-9/_.~-]{0,1023}\Z')


class HealthError(RuntimeError):
    """Состояние не подтверждено; значения из ответа не раскрываются."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HealthError('Повтор поля ответа')
        result[key] = value
    return result


def check_local_health(port: int, *, product_id: str, version: str, path: str = '/health',
                       timeout: float = 3.0) -> None:
    """Подтвердить HTTP 200 и точные публичные поля продукта на 127.0.0.1.

    Только для адаптеров, явно реализующих этот JSON-контракт. Это не доказывает
    принадлежность сокета процессу или защищённость от локального подражания.
    """
    if (type(port) is not int or not 1 <= port <= 65535
            or type(product_id) is not str or _PRODUCT.fullmatch(product_id) is None
            or type(version) is not str or _VERSION.fullmatch(version) is None
            or type(path) is not str or _PATH.fullmatch(path) is None
            or type(timeout) not in (int, float) or not 0 < timeout <= 30
            or (type(timeout) is float and not math.isfinite(timeout))):
        raise HealthError('Некорректные параметры проверки')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        url = f'http://127.0.0.1:{port}{path}'
        with opener.open(url, timeout=timeout) as response:
            if response.status != 200:
                raise HealthError('Состояние не подтверждено')
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise HealthError('Ответ слишком велик')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(data) is not dict or set(data) != {'product', 'release', 'healthy'}
                or data['product'] != product_id or data['release'] != version
                or data['healthy'] is not True):
            raise HealthError('Идентичность или состояние не подтверждены')
    except (OSError, ValueError, UnicodeError, RecursionError, urllib.error.URLError):
        raise HealthError('Не удалось подтвердить состояние продукта') from None
