# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальный локальный клиент установщика для независимых продуктов.

Требуется доверенный Python и каталог с модулем shared.api; его можно явно
передать как api_root. Вызовы идут через JSON stdin/stdout без shell.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid

_MAX_BYTES = 1024 * 1024
_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_SECRET_ENV = re.compile(r'HOMENSAI_SECRET_[A-Z0-9_]{1,96}\Z')


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


def _run_bounded(argv, *, input: bytes, timeout: float, env: dict, cwd: str | None = None):
    """Ограничить ответ при чтении; stderr не накапливается и не раскрывается."""
    process = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env, cwd=cwd, start_new_session=(os.name == 'posix'))
    output = bytearray()
    overflow, failed = threading.Event(), threading.Event()

    def stop():
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError:
            failed.set()

    def read_response():
        try:
            while chunk := os.read(process.stdout.fileno(), 8192):
                if len(output) + len(chunk) > _MAX_BYTES:
                    overflow.set()
                    stop()
                    return
                output.extend(chunk)
        except (OSError, ValueError):
            failed.set()

    def write_request():
        try:
            view = memoryview(input)
            while view:
                count = os.write(process.stdin.fileno(), view)
                if count <= 0:
                    raise OSError('write failed')
                view = view[count:]
        except (OSError, ValueError):
            failed.set()
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                failed.set()

    reader = threading.Thread(target=read_response, daemon=True)
    writer = threading.Thread(target=write_request, daemon=True)
    try:
        reader.start()
        writer.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise ValueError('process did not terminate') from None
            raise subprocess.TimeoutExpired(argv, timeout) from None
        writer.join(timeout=1)
        reader.join(timeout=1)
        if writer.is_alive() or reader.is_alive():
            stop()
            writer.join(timeout=1)
            reader.join(timeout=1)
            raise ValueError('process pipes remain open')
        lingering = False
        if os.name == 'posix':
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            except OSError:
                failed.set()
            else:
                lingering = True
                stop()
        if failed.is_set() or overflow.is_set() or lingering:
            raise ValueError('process output not confirmed')
        return subprocess.CompletedProcess(argv, process.returncode, stdout=bytes(output))
    finally:
        if process.poll() is None:
            stop()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for thread in (writer, reader):
            if thread.ident is not None and thread.is_alive():
                thread.join(timeout=1)
        if not reader.is_alive():
            process.stdout.close()


class InstallerClient:
    """Согласование возможностей и вызов одного локального API."""

    def __init__(self, *, python_executable: str = sys.executable, timeout: float = 30,
                 api_root: str | None = None):
        if api_root is None:
            candidate = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
            if not os.path.isfile(os.path.join(candidate, 'shared', 'api.py')):
                raise ValueError('api_root is required when installer is not alongside the client')
            api_root = candidate
        if (type(python_executable) is not str or not os.path.isabs(python_executable)
                or type(timeout) not in (int, float) or not 0 < timeout <= 600
                or type(api_root) is not str or not os.path.isabs(api_root) or not os.path.isdir(api_root)):
            raise ValueError('invalid client configuration')
        self._python = python_executable
        self._timeout = timeout
        self._api_root = api_root

    def _call(self, operation: str, params: dict, language: str | None, *, secret_env_name=None, event_log_path=None) -> dict:
        request_id = uuid.uuid4().hex
        request = {'api_major': 1, 'request_id': request_id, 'operation': operation, 'params': params}
        if language is not None:
            request['language'] = language
        if event_log_path is not None:
            request['event_log_path'] = event_log_path
        try:
            body = json.dumps(request, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if len(body) > _MAX_BYTES:
                raise ValueError('request too large')
            # Keep secrets and proxy settings from the caller's environment out
            # of this child. SystemRoot is required by Python on Windows.
            env = {'PATH': os.defpath, 'PYTHONIOENCODING': 'utf-8'}
            if os.name == 'nt' and 'SystemRoot' in os.environ:
                env['SystemRoot'] = os.environ['SystemRoot']
            if secret_env_name is not None:
                value = os.environ.get(secret_env_name)
                if value is None or not value or len(value) > 8192:
                    raise InstallerClientError('secret_unavailable', 'Required installer secret is unavailable')
                env[secret_env_name] = value
            completed = _run_bounded([self._python, '-B', '-m', 'shared.api'], input=body,
                                     timeout=self._timeout, env=env, cwd=self._api_root)
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
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError, RecursionError, RuntimeError):
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
             language: str | None = None, secret_env_name: str | None = None,
             event_log_path: str | None = None) -> dict:
        if (type(operation) is not str or not operation or type(params) is not dict
                or type(min_minor) is not int or min_minor < 0
                or (language is not None and type(language) is not str)
                or (event_log_path is not None and (type(event_log_path) is not str
                    or not os.path.isabs(event_log_path) or len(event_log_path) > 8192))):
            raise ValueError('invalid API request')
        if secret_env_name is not None and (type(secret_env_name) is not str
                or _SECRET_ENV.fullmatch(secret_env_name) is None
                or operation not in ('import-update-archive', 'run-pointer-linux-update')
                or params.get('password_env') != secret_env_name):
            raise ValueError('invalid secret channel')
        capabilities = self.capabilities()
        required_minor = max(min_minor, 63) if event_log_path is not None else min_minor
        if (capabilities['api_minor'] < required_minor or operation not in capabilities['operations']
                or (language is not None and language not in capabilities['languages'])):
            raise InstallerClientError('incompatible_api', 'Required installer capability is unavailable')
        # In particular, do not retry modifying operations or invent their
        # idempotency keys; their owner retains the exact request and key.
        return self._call(operation, params, language, secret_env_name=secret_env_name, event_log_path=event_log_path)
