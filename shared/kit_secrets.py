# HomenS.AI · © 2026 Serhii Khomenko
"""Секреты продукта в установочном комплекте: объявление, ввод и защищённый файл.

Издатель объявляет имена в homensai-kit.json (раздел secrets); объявление
попадает в подписанный homensai-release.json. Значения вводит владелец при
установке, обновлении или командой secrets; они хранятся в файле вне корня
установки и вне пакета:

- Linux: ~/.config/homensai/<product_id>/secrets.env (каталог 0700, файл 0600);
- Windows: %ProgramData%\\HomenSAI\\.secrets\\<product_id>\\secrets.env
  (DACL только SYSTEM и Administrators).

homensai_entry.py выпуска читает файл при запуске компонента или миграции и
передаёт объявленные имена в окружение процесса. Значения не попадают в
манифест, журналы, состояние установщика и сообщения об ошибках.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets as _random

FILE_NAME = 'secrets.env'
MAX_SECRETS = 64
MAX_VALUE = 4096
MAX_FILE_BYTES = 512 * 1024
_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,127}')
# Имена, которые меняют поведение интерпретатора, загрузчика или самого установщика.
# HOMENSAI_ целиком не резервируется: продукты используют его (HOMENSAI_RENT_DEV_KEY).
_RESERVED_PREFIXES = ('HOMENSAI_SECRET_', 'HOMENSAI_WEB_', 'HOMENSAI_RELEASE', 'PYTHON', 'LD_', 'DYLD_', 'SYSTEMD_')
_RESERVED_NAMES = frozenset({'HOMENSAI_PRODUCT_ID', 'HOMENSAI_SECRETS_FILE', 'HOMENSAI_ARCHIVE_PASSWORD',
                             'HOMENSAI_API_TRACE', 'HOMENSAI_SETUP_DEBUG', 'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'TMP', 'TEMP', 'TMPDIR',
                             'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PROGRAMDATA', 'APPDATA',
                             'LOCALAPPDATA', 'USERPROFILE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'})


class SecretsError(ValueError):
    """Объявление или файл секретов недопустимы; сообщение не содержит значений."""


def check_declarations(items) -> list[dict]:
    """Проверить раздел secrets: [{name, required?, generate?, prompt?}] и вернуть нормализованный список."""
    if type(items) is not list or len(items) > MAX_SECRETS:
        raise SecretsError('secrets: список не длиннее ' + str(MAX_SECRETS) + ' элементов')
    result, names = [], set()
    for item in items:
        if type(item) is not dict or 'name' not in item or set(item) - {'name', 'required', 'generate', 'prompt'}:
            raise SecretsError('secrets: элемент {name, required, generate, prompt}')
        name = item['name']
        if type(name) is not str or _NAME.fullmatch(name) is None:
            raise SecretsError('secrets.name: заглавные латинские буквы, цифры и _, начиная с буквы')
        if name in _RESERVED_NAMES or name.startswith(_RESERVED_PREFIXES):
            raise SecretsError('secrets.name: имя ' + name + ' зарезервировано')
        if name in names:
            raise SecretsError('secrets.name: повтор ' + name)
        required, generate = item.get('required', True), item.get('generate', False)
        prompt = item.get('prompt', '')
        if type(required) is not bool or type(generate) is not bool:
            raise SecretsError('secrets: required и generate — логические значения')
        if type(prompt) is not str or len(prompt) > 200 or any(ord(char) < 32 for char in prompt):
            raise SecretsError('secrets.prompt: одна строка до 200 символов')
        names.add(name)
        result.append({'name': name, 'required': required, 'generate': generate, 'prompt': prompt})
    return result


def check_value(value) -> bool:
    return (type(value) is str and 0 < len(value) <= MAX_VALUE
            and not any(char in value for char in '\r\n\x00'))


def location(product_id: str, *, windows: bool | None = None, program_data: Path | None = None) -> Path:
    """Путь файла секретов; тот же путь вычисляет homensai_entry.py выпуска."""
    if windows if windows is not None else os.name == 'nt':
        base = program_data or Path(os.environ.get('ProgramData') or r'C:\ProgramData')
        return base / 'HomenSAI' / '.secrets' / product_id / FILE_NAME
    return Path.home() / '.config' / 'homensai' / product_id / FILE_NAME


def read(path: Path) -> dict[str, str]:
    """Прочитать NAME=value; пустые строки и строки с # пропускаются."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SecretsError('Файл секретов должен быть обычным файлом')
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise SecretsError('Файл секретов слишком велик')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise SecretsError('Файл секретов должен быть в UTF-8') from None
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if not separator or _NAME.fullmatch(name) is None or not check_value(value):
            raise SecretsError('Недопустимая строка в файле секретов')
        values[name] = value
    return values


def write(path: Path, values: dict[str, str]):
    """Атомарно записать файл (на POSIX — 0600 в каталоге 0700); каталог готовит вызывающий."""
    path = Path(path)
    if any(_NAME.fullmatch(name) is None or not check_value(value) for name, value in values.items()):
        raise SecretsError('Недопустимое имя или значение секрета')
    text = ''.join(name + '=' + values[name] + '\n' for name in sorted(values))
    temporary = path.with_name('.' + path.name + '.' + _random.token_hex(4) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def prepare_posix_dir(path: Path):
    """Каталог секретов Linux: создаётся 0700, чужой или открытый другим каталог отклоняется."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid():
        raise SecretsError('Каталог секретов принадлежит другому пользователю')
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def generated() -> str:
    return _random.token_urlsafe(32)


def missing(declared: list[dict], values: dict[str, str]) -> list[str]:
    """Обязательные секреты без значения."""
    return [item['name'] for item in declared if item['required'] and item['name'] not in values]
