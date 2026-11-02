"""Ограниченная проверка закреплённого Docker CLI и локального демона."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from shared.datahome import DataHomeError, checked_path
from shared.native_migration import _checked_file
from shared.update_plan import UpdateError


class DockerPreflightError(RuntimeError):
    """Вход или доверенная программа не подтверждены; детали не раскрываются."""


_VERSION = r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.\-]{1,40})?'


def check_docker(*, executable: str, executable_sha256: str, socket_path: str | None,
                 timeout_seconds: int = 5, runner=None) -> dict:
    """Проверить версии без запуска контейнеров и без пользовательской Docker-конфигурации.

    Хэш Docker CLI предоставляется независимо доверенной стороной. Это не
    удостоверяет Compose plugin, демон, образы, содержимое или готовность ОС.
    """
    if (type(executable) is not str or type(executable_sha256) is not str
            or re.fullmatch(r'[a-f0-9]{64}', executable_sha256) is None
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 10
            or (runner is not None and not callable(runner))):
        raise DockerPreflightError('Недопустимые параметры проверки Docker')
    try:
        program = _checked_file(executable, executable_sha256, executable=True)
        if sys.platform.startswith('linux'):
            if type(socket_path) is not str:
                raise DockerPreflightError('Требуется локальный Unix socket')
            socket = checked_path(socket_path)
            if socket == Path(socket.anchor):
                raise DockerPreflightError('Недопустимый socket')
            host = 'unix://' + str(socket)
        elif os.name == 'nt':
            if socket_path is not None:
                raise DockerPreflightError('Для Windows используется локальный именованный канал')
            host = 'npipe:////./pipe/docker_engine'
        else:
            raise DockerPreflightError('Платформа проверки Docker не поддерживается')
        run = subprocess.run if runner is None else runner
        with tempfile.TemporaryDirectory(prefix='homensai-docker-check-') as config:
            env = {'PATH': os.defpath, 'HOME': config, 'DOCKER_CONFIG': config,
                   'COMPOSE_DISABLE_ENV_FILE': '1'}
            if os.name == 'nt':
                system_root = os.environ.get('SystemRoot')
                if not system_root:
                    raise DockerPreflightError('Не определён системный каталог Windows')
                env['SystemRoot'] = system_root
                env['PATH'] = str(Path(system_root) / 'System32')
            prefix = [str(program), '--config', config, '--host', host]
            def invoke(arguments):
                _checked_file(executable, executable_sha256, executable=True)
                try:
                    result = run(prefix + arguments, shell=False, timeout=timeout_seconds, check=False,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, env=env)
                except Exception:
                    result = None
                _checked_file(executable, executable_sha256, executable=True)
                try:
                    if (result is None or type(result.returncode) is not int or result.returncode != 0
                            or type(result.stdout) is not str or len(result.stdout) > 8192):
                        return None
                    return result.stdout.strip()
                except Exception:
                    return None
            result = {'client_version': None, 'compose_version': None, 'server_version': None,
                      'compose_flags_present': False, 'installation_ready': False, 'images_verified': False}
            client = invoke(['--version'])
            match = re.fullmatch(r'Docker version (' + _VERSION + r')(?:, build [A-Za-z0-9_.-]{1,64})?', client or '')
            if match is None:
                return result
            result['client_version'] = match.group(1)
            compose = invoke(['compose', 'version', '--short'])
            match = re.fullmatch(r'v?(' + _VERSION + r')', compose or '')
            if match is not None:
                result['compose_version'] = match.group(1)
                help_text = invoke(['compose', 'up', '--help'])
                result['compose_flags_present'] = bool(help_text and re.search(r'(?m)^\s*--no-build(?:\s|$)', help_text)
                    and re.search(r'(?m)^\s*--pull(?:[ =\s]|$)', help_text))
            server = invoke(['version', '--format', '{{.Server.Version}}'])
            if re.fullmatch(_VERSION, server or ''):
                result['server_version'] = server
            return result
    except (OSError, DataHomeError, UpdateError, ValueError):
        raise DockerPreflightError('Не удалось подтвердить программу или путь Docker') from None
