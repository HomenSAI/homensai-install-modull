# HomenS.AI · © 2026 Serhii Khomenko
"""Пример локального клиента установщика для независимого тестового продукта.

Требуется доверенный Python с установленным модулем shared.api; вызовы идут
через JSON stdin/stdout без shell. Это пример, а не опубликованный SDK.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid

_MAX_BYTES = 1024 * 1024
_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')


class InstallerClientError(RuntimeError):
    """Код ошибки API либо локальная ошибка связи без содержимого stderr."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


class InstallerClient:
    """Пример согласования возможностей и вызова одного локального API."""

    def __init__(self, *, python_executable: str = sys.executable, timeout: float = 30):
        if (type(python_executable) is not str or not os.path.isabs(python_executable)
                or type(timeout) not in (int, float) or not 0 < timeout <= 600):
            raise ValueError('invalid client configuration')
        self._python = python_executable
        self._timeout = timeout

    def _call(self, operation: str, params: dict, language: str | None) -> dict:
        request_id = uuid.uuid4().hex
        request = {'api_major': 1, 'request_id': request_id, 'operation': operation, 'params': params}
        if language is not None:
            request['language'] = language
        try:
            body = json.dumps(request, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if len(body) > _MAX_BYTES:
                raise ValueError('request too large')
            # Keep secrets and proxy settings from the caller's environment out
            # of this child. SystemRoot is required by Python on Windows.
            env = {'PATH': os.defpath, 'PYTHONIOENCODING': 'utf-8'}
            if os.name == 'nt' and 'SystemRoot' in os.environ:
                env['SystemRoot'] = os.environ['SystemRoot']
            completed = subprocess.run([self._python, '-B', '-m', 'shared.api'], input=body,
                                       capture_output=True, shell=False, timeout=self._timeout,
                                       check=False, env=env)
            if len(completed.stdout) > _MAX_BYTES:
                raise ValueError('response too large')
            response = json.loads(completed.stdout.decode('utf-8'), object_pairs_hook=_unique,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
            if (type(response) is not dict or response.get('api_major') != 1
                    or type(response.get('api_major')) is not int or response.get('request_id') != request_id
                    or type(response.get('ok')) is not bool):
                raise ValueError('invalid response')
            if response['ok']:
                if completed.returncode != 0 or type(response.get('result')) is not dict or 'error' in response:
                    raise ValueError('invalid success')
                return response['result']
            error = response.get('error')
            if (completed.returncode not in (1, 2) or type(error) is not dict or 'result' in response
                    or type(error.get('code')) is not str or _CODE.fullmatch(error['code']) is None
                    or type(error.get('message')) is not str or len(error['message']) > 8192
                    or type(error.get('retryable')) is not bool):
                raise ValueError('invalid failure')
            raise InstallerClientError(error['code'], error['message'])
        except InstallerClientError:
            raise
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError, RecursionError):
            raise InstallerClientError('transport_error', 'Installer API communication failed') from None

    def capabilities(self) -> dict:
        value = self._call('capabilities', {}, None)
        if (type(value.get('api_major')) is not int or value['api_major'] != 1
                or type(value.get('api_minor')) is not int or value['api_minor'] < 0
                or type(value.get('operations')) is not list
                or any(type(item) is not str for item in value['operations'])
                or type(value.get('languages')) is not list
                or any(type(item) is not str for item in value['languages'])):
            raise InstallerClientError('incompatible_api', 'Installer capabilities are invalid')
        return value

    def call(self, operation: str, params: dict, *, min_minor: int = 0,
             language: str | None = None) -> dict:
        if (type(operation) is not str or not operation or type(params) is not dict
                or type(min_minor) is not int or min_minor < 0
                or (language is not None and type(language) is not str)):
            raise ValueError('invalid API request')
        capabilities = self.capabilities()
        if (capabilities['api_minor'] < min_minor or operation not in capabilities['operations']
                or (language is not None and language not in capabilities['languages'])):
            raise InstallerClientError('incompatible_api', 'Required installer capability is unavailable')
        # In particular, do not retry modifying operations or invent their
        # idempotency keys; their owner retains the exact request and key.
        return self._call(operation, params, language)
