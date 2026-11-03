# HomenS.AI · © 2026 Serhii Khomenko
"""Сборщик установочного комплекта продукта: `python -m shared.kit`.

Из каталога продукта с `homensai-kit.json` собирает подписанный пакет,
переносимый runtime, зависимости для каждой цели и установщик с собственным
Python. Сеть не используется; все входные архивы и wheels заранее загружены и
закреплены хэшами. Закрытый ключ издателя хранится вне репозитория продукта.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile

from shared.kit_wheels import TARGETS, KitWheelError, install_wheel, read_lock, select_wheels
from shared.manifest import ManifestError, validate_manifest
from shared.preflight import NAMES

CONFIG_NAME = 'homensai-kit.json'
KIT_FORMAT = 1
ENTRY = 'homensai_entry.py'
RELEASE_INFO = 'homensai-release.json'
_RESERVED = ('runtime-linux', 'runtime-windows', 'systemd-user', 'locks', 'vendor', 'pydeps', 'windows',
             ENTRY, RELEASE_INFO, 'package.json')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_HEX64 = re.compile(r'[a-f0-9]{64}\Z')
_DEFAULT_PATHS = {name: name for name in NAMES}
_DEFAULT_ESTIMATES = {'data': 256 * 1024 * 1024, 'config': 16 * 1024 * 1024}
_SKIP = ('__pycache__', '.git', '.hg', '.svn', '.mypy_cache', '.pytest_cache')
_INSTALLER_PACKAGES = ('shared', 'homensai_installer_client')
# Корень установки и служебные каталоги Windows занимают до ~120 символов из 260.
MAX_RELEASE_PATH = 120
CRYPTO_LOCK = Path(__file__).resolve().parent.parent / 'requirements-crypto.txt'
_LINUX_TARGET = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
_WINDOWS_TARGET = {'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'}

ENTRY_SOURCE = '''# Создано сборщиком HomenS.AI kit; не изменять вручную.
"""Запуск модуля продукта из выпуска с зависимостями текущей платформы."""
import json
import os
import runpy
import sys


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    if sys.platform.startswith('linux'):
        platform = 'linux-x86_64'
    elif sys.platform == 'win32':
        platform = 'windows-x86_64'
    else:
        raise SystemExit('homensai: unsupported platform')
    with open(os.path.join(base, 'homensai-release.json'), encoding='utf-8') as stream:
        release = json.load(stream)
    sys.path[:0] = [os.path.join(base, *item.split('/')) for item in release['python_paths']]
    sys.path.insert(len(release['python_paths']), os.path.join(base, 'pydeps', platform))
    os.environ['HOMENSAI_PRODUCT_ID'] = release['product_id']
    os.environ['HOMENSAI_RELEASE'] = release['version']
    os.environ['HOMENSAI_RELEASE_DIR'] = base
    if len(sys.argv) < 2:
        raise SystemExit('homensai: module name required')
    module = sys.argv[1]
    sys.argv = [module] + sys.argv[2:]
    runpy.run_module(module, run_name='__main__', alter_sys=True)


main()
'''

SETUP_SH = '''#!/bin/sh
# Установщик HomenS.AI: запуск встроенного Python комплекта без системных зависимостей.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) python="$here/installer/linux-x86_64/python/bin/python" ;;
    *) echo "Unsupported system: $(uname -s) $(uname -m)" >&2; exit 2 ;;
esac
exec "$python" -I -B "$here/installer/app/homensai_setup.py" --kit "$here" "$@"
'''

SETUP_CMD = '''@echo off
rem HomenS.AI installer: runs the bundled Python of this kit (no system dependencies).
setlocal
set "HERE=%~dp0"
"%HERE%installer\\windows-x86_64\\python\\python.exe" -I -B "%HERE%installer\\app\\homensai_setup.py" --kit "%HERE%." %*
exit /b %ERRORLEVEL%
'''
WINSW_VERSION = '2.12.0'

SETUP_PY = '''# Создано сборщиком HomenS.AI kit.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared.kit_setup import main

raise SystemExit(main())
'''


class KitError(ValueError):
    """Комплект не собран или не проверен; сообщение не содержит секретов."""


def _relative(value, what):
    if (type(value) is not str or not value or value.startswith('/') or '\\' in value or ':' in value
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise KitError(f'{what}: требуется относительный путь без ..')
    return value


def _file(base: Path, value, what) -> Path:
    if type(value) is not str or not value:
        raise KitError(f'{what}: требуется путь')
    path = (base / value).resolve()
    if not path.is_file():
        raise KitError(f'{what}: файл не найден')
    return path


def _strict(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KitError('Повторяющийся ключ JSON')
        result[key] = value
    return result


def _read_json(path: Path, limit=4 * 1024 * 1024):
    raw = Path(path).read_bytes()
    if len(raw) > limit:
        raise KitError('JSON слишком велик')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_strict)
    except (UnicodeError, ValueError):
        raise KitError('Недопустимый JSON') from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_config(path) -> dict:
    """Прочитать и проверить homensai-kit.json; пути разрешаются от его каталога."""
    path = Path(path).resolve()
    value = _read_json(path)
    base = path.parent
    allowed = {'format', 'manifest', 'sources', 'python_paths', 'targets', 'locks', 'wheelhouse',
               'runtime', 'migrations', 'modes', 'estimates', 'glibc', 'winsw'}
    required = {'format', 'manifest', 'sources', 'python_paths', 'locks', 'wheelhouse', 'runtime'}
    if type(value) is not dict or not required <= set(value) or set(value) - allowed:
        raise KitError('homensai-kit.json: неизвестные или отсутствующие поля')
    if value['format'] != KIT_FORMAT or type(value['format']) is not int:
        raise KitError('homensai-kit.json: поддерживается format 1')
    targets = value.get('targets', ['linux-x86_64'])
    if (type(targets) is not list or not targets or len(set(targets)) != len(targets)
            or any(target not in TARGETS for target in targets)):
        raise KitError('targets: допустимы linux-x86_64 и windows-x86_64')
    winsw = None
    if 'windows-x86_64' in targets:
        item = value.get('winsw')
        if (type(item) is not dict or set(item) != {'exe', 'sha256', 'license'}
                or type(item['sha256']) is not str or _HEX64.fullmatch(item['sha256']) is None):
            raise KitError('winsw: для Windows нужен {exe, sha256, license} WinSW ' + WINSW_VERSION + ' x64')
        winsw = {'exe': _file(base, item['exe'], 'winsw.exe'), 'sha256': item['sha256'],
                 'license': _file(base, item['license'], 'winsw.license')}
    elif 'winsw' in value:
        raise KitError('winsw: используется только для цели windows-x86_64')
    sources = value['sources']
    if type(sources) is not list or not sources:
        raise KitError('sources: требуется непустой список')
    parsed_sources = []
    for item in sources:
        if type(item) is not dict or set(item) != {'from', 'to'}:
            raise KitError('sources: элемент {from, to}')
        origin = base / _relative(item['from'], 'sources.from')
        if not origin.exists() or origin.is_symlink() or not origin.resolve().is_relative_to(base):
            raise KitError('sources.from: путь не найден или выходит за проект')
        origin = origin.resolve()
        target = _relative(item['to'], 'sources.to')
        if target.split('/')[0] in _RESERVED:
            raise KitError('sources.to: имя зарезервировано установщиком')
        parsed_sources.append((origin, target))
    python_paths = value['python_paths']
    if type(python_paths) is not list or len(set(python_paths)) != len(python_paths):
        raise KitError('python_paths: требуется список относительных путей')
    for item in python_paths:
        _relative(item, 'python_paths')
    locks = value['locks']
    if type(locks) is not dict or set(locks) != {'linux', 'windows'}:
        raise KitError('locks: требуются lock-файлы linux и windows')
    if type(value['wheelhouse']) is not str or not value['wheelhouse']:
        raise KitError('wheelhouse: требуется путь')
    wheelhouse = (base / value['wheelhouse']).resolve()
    if not wheelhouse.is_dir():
        raise KitError('wheelhouse: каталог не найден')
    runtimes = value['runtime']
    if type(runtimes) is not dict or set(runtimes) != {target.split('-')[0] for target in targets}:
        raise KitError('runtime: требуется описание runtime для каждой цели')
    parsed_runtimes = {}
    for name, item in runtimes.items():
        if (type(item) is not dict or set(item) != {'archive', 'sha256', 'origin', 'python_version'}
                or type(item['sha256']) is not str or _HEX64.fullmatch(item['sha256']) is None
                or type(item['origin']) is not str or not item['origin'].startswith('https://')
                or type(item['python_version']) is not str
                or re.fullmatch(r'3\.14\.\d{1,4}', item['python_version']) is None):
            raise KitError('runtime: {archive, sha256, origin, python_version}')
        parsed_runtimes[name] = {**item, 'archive': _file(base, item['archive'], 'runtime.archive')}
    migrations = value.get('migrations', [])
    if (type(migrations) is not list or len(set(migrations)) != len(migrations)
            or any(type(item) is not str or _IDENT.fullmatch(item) is None for item in migrations)):
        raise KitError('migrations: список уникальных идентификаторов')
    modes = value.get('modes')
    if modes is not None and (type(modes) is not list or not modes or len(set(modes)) != len(modes)
                              or any(type(item) is not str for item in modes)):
        raise KitError('modes: непустой список режимов')
    estimates = value.get('estimates', {})
    if (type(estimates) is not dict or set(estimates) - {'data', 'backup', 'config'}
            or any(type(item) is not int or not 0 <= item <= 2 ** 50 for item in estimates.values())):
        raise KitError('estimates: байты для data, backup, config')
    glibc = value.get('glibc', '2.28')
    if type(glibc) is not str or re.fullmatch(r'2\.\d{1,2}', glibc) is None:
        raise KitError('glibc: версия вида 2.28')
    return {'base': base, 'manifest': _file(base, value['manifest'], 'manifest'), 'sources': parsed_sources,
            'python_paths': python_paths, 'targets': targets,
            'locks': {name: _file(base, locks[name], 'locks.' + name) for name in ('linux', 'windows')},
            'wheelhouse': wheelhouse, 'runtime': parsed_runtimes, 'migrations': migrations, 'modes': modes,
            'estimates': {**_DEFAULT_ESTIMATES, **estimates},
            'glibc': tuple(int(part) for part in glibc.split('.')), 'winsw': winsw}


def _expand_module(command, *, service):
    """`["@module", "pkg.mod", ...]` → запуск через homensai_entry.py выпуска."""
    if type(command) is not list or not command or command[0] != '@module':
        return command
    if len(command) < 2 or type(command[1]) is not str or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', command[1]) is None:
        raise KitError('@module: требуется имя модуля Python')
    if service:
        return ['@runtime/bin/python', '-I', '-B', '@release/' + ENTRY, *command[1:]]
    return ['python', ENTRY, *command[1:]]


def render_manifest(template: dict, config: dict, lock_entries: dict, wheels: list[dict]) -> dict:
    """Дополнить шаблон манифеста продукта сведениями, которые формирует сборщик."""
    if type(template) is not dict:
        raise KitError('Шаблон манифеста должен быть объектом')
    if 'runtime' in template:
        raise KitError('Шаблон манифеста не должен содержать runtime: его формирует сборщик')
    manifest = copy.deepcopy(template)
    manifest.setdefault('schema_version', 2)
    manifest.setdefault('archive_before_replace', True)
    manifest.setdefault('paths', dict(_DEFAULT_PATHS))
    if 'compatibility' not in manifest:
        manifest['compatibility'] = {'targets': [dict(_LINUX_TARGET) if target == 'linux-x86_64'
                                                 else dict(_WINDOWS_TARGET) for target in config['targets']]}
    manifest['runtime'] = {'python': '3.14', 'lock_files': lock_entries, 'vendor_wheels': wheels, 'offline': True}
    for component in manifest.get('components', []):
        if type(component) is dict and 'cmd' in component:
            component['cmd'] = _expand_module(component['cmd'], service=True)
    for section in ('migrations', 'backup', 'restore'):
        item = manifest.get(section)
        if type(item) is dict and 'cmd' in item:
            item['cmd'] = _expand_module(item['cmd'], service=False)
    try:
        return validate_manifest(manifest)
    except ManifestError as error:
        raise KitError('Манифест продукта не прошёл проверку: ' + str(error)) from None


def _copy_tree(origin: Path, target: Path):
    if origin.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise KitError('sources: пересечение путей в payload')
        shutil.copyfile(origin, target)
        return
    for current, directories, files in os.walk(origin):
        directories[:] = sorted(name for name in directories if name not in _SKIP)
        for name in sorted(files):
            source = Path(current) / name
            if source.is_symlink():
                raise KitError('sources: символические ссылки не поддерживаются')
            if name.endswith(('.pyc', '.pyo')):
                continue
            destination = target / source.relative_to(origin)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise KitError('sources: пересечение путей в payload')
            shutil.copyfile(source, destination)
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise KitError('sources: символические ссылки не поддерживаются')


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in Path(path).rglob('*') if item.is_file())


def _load_private_key(path: Path, passphrase_env: str | None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    password = None
    if passphrase_env is not None:
        value = os.environ.get(passphrase_env)
        if not value:
            raise KitError('Пароль ключа подписи не задан в окружении')
        password = value.encode('utf-8')
    info = Path(path).stat()
    if os.name == 'posix' and info.st_mode & 0o077:
        raise KitError('Закрытый ключ доступен другим пользователям (нужны права 0600)')
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=password)
    except (ValueError, TypeError):
        raise KitError('Не удалось прочитать закрытый ключ подписи') from None
    if not isinstance(key, Ed25519PrivateKey):
        raise KitError('Требуется ключ Ed25519')
    return key


def generate_keys(directory, *, name='publisher', passphrase_env=None) -> dict:
    """Создать пару Ed25519: PEM закрытого ключа (0600) и 32 байта открытого."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    directory = Path(directory).resolve()
    if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) is None:
        raise KitError('Недопустимое имя ключа')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_path, public_path = directory / (name + '-ed25519.pem'), directory / (name + '-ed25519.pub')
    if private_path.exists() or public_path.exists():
        raise KitError('Файлы ключа уже существуют')
    encryption = serialization.NoEncryption()
    if passphrase_env is not None:
        value = os.environ.get(passphrase_env)
        if not value:
            raise KitError('Пароль ключа подписи не задан в окружении')
        encryption = serialization.BestAvailableEncryption(value.encode('utf-8'))
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(pem)
    public_path.write_bytes(public)
    return {'private_key': str(private_path), 'public_key': str(public_path),
            'public_key_sha256': hashlib.sha256(public).hexdigest()}


def _installer_python(runtime_block: Path, destination: Path, crypto_wheels: list[Path], *, windows=False):
    """Python установщика: файлы подготовленного runtime и cryptography в site-packages."""
    for item in sorted(runtime_block.rglob('*')):
        relative = item.relative_to(runtime_block).as_posix()
        if item.is_dir() or relative in ('runtime-v1.json', 'runtime-provenance-v1.json', 'homensai-runtime.json'):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)
        os.chmod(target, 0o644)
    if not windows:
        os.chmod(destination / 'bin/python', 0o755)
    site = destination / ('Lib/site-packages' if windows else 'lib/python3.14/site-packages')
    owned = {}
    if site.is_dir():
        for item in site.rglob('*'):
            if item.is_file():
                owned[item.relative_to(site).as_posix().casefold()] = 'runtime'
    for wheel in crypto_wheels:
        install_wheel(wheel.read_bytes(), site, owned)


def _copy_installer_code(destination: Path):
    root = Path(__file__).resolve().parent.parent
    for package in _INSTALLER_PACKAGES:
        for item in sorted((root / package).rglob('*')):
            if item.is_dir() or '__pycache__' in item.parts or item.suffix in ('.pyc', '.pyo'):
                continue
            target = destination / package / item.relative_to(root / package)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
    (destination / 'homensai_setup.py').write_text(SETUP_PY, encoding='utf-8')


def _write_tar(kit: Path, archive: Path):
    with tarfile.open(archive, 'x:gz', compresslevel=6) as tar:
        for item in [kit, *sorted(kit.rglob('*'))]:
            info = tar.gettarinfo(str(item), arcname=(PurePosixPath(kit.name) / item.relative_to(kit)).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            if item.is_file():
                with item.open('rb') as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)


def build_kit(config_path, *, version: str, signing_key, output, passphrase_env=None,
              installer_wheelhouse=None, archive=True) -> dict:
    """Собрать комплект `<product>-<version>` в каталоге output; ничего не устанавливает."""
    from shared.package import build_package
    from shared.package_signing import sign_package
    from shared.release_inventory import release_inventory
    from shared.runtime_pbs import prepare_pbs_archive
    from shared.runtime_windows import prepare_windows_runtime
    from shared.systemd_pointer import RECIPE_PATH, RECIPE_V2
    from cryptography.hazmat.primitives import serialization

    if type(version) is not str or _VERSION.fullmatch(version) is None or version.endswith('.'):
        raise KitError('Недопустимая версия выпуска')
    config = load_config(config_path)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    key = _load_private_key(Path(signing_key), passphrase_env)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    template = _read_json(config['manifest'])
    product_id = template.get('product_id') if type(template) is dict else None
    if type(product_id) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', product_id) is None:
        raise KitError('manifest.product_id: недопустимый идентификатор')
    name = product_id + '-' + version
    kit = output / name
    tar_path = output / (name + '.tar.gz')
    if kit.exists() or tar_path.exists():
        raise KitError('Комплект этой версии уже существует в каталоге назначения')
    work = Path(tempfile.mkdtemp(prefix='.kit-', dir=output))
    try:
        payload = work / 'payload'
        payload.mkdir(mode=0o700)
        for origin, target in config['sources']:
            _copy_tree(origin, payload / target)
        # Зависимости: wheels по lock-файлам целей и распакованные каталоги pydeps.
        (payload / 'locks').mkdir()
        (payload / 'vendor').mkdir()
        lock_entries, wheels, seen = {}, [], {}
        for os_name in ('linux', 'windows'):
            target_file = 'locks/' + os_name + '.lock'
            shutil.copyfile(config['locks'][os_name], payload / target_file)
            lock_entries[os_name] = {'file': target_file, 'sha256': _sha256_file(payload / target_file)}
        dependency_report = {}
        for target in config['targets']:
            lock = read_lock(config['locks'][target.split('-')[0]])
            selected = select_wheels(lock, config['wheelhouse'], target, glibc=config['glibc'])
            pydeps = payload / 'pydeps' / target
            pydeps.mkdir(parents=True)
            owned = {}
            for wheel in selected:
                data = wheel.read_bytes()
                install_wheel(data, pydeps, owned)
                if wheel.name not in seen:
                    seen[wheel.name] = True
                    shutil.copyfile(wheel, payload / 'vendor' / wheel.name)
                    wheels.append({'file': 'vendor/' + wheel.name, 'sha256': hashlib.sha256(data).hexdigest()})
            dependency_report[target] = [wheel.name for wheel in selected]
        if config['winsw'] is not None:
            if _sha256_file(config['winsw']['exe']) != config['winsw']['sha256']:
                raise KitError('winsw: SHA-256 файла не совпадает с закреплённым')
            template = {**template, 'winsw': {'version': WINSW_VERSION, 'sha256': config['winsw']['sha256']}}
            (payload / 'windows').mkdir()
            shutil.copyfile(config['winsw']['exe'], payload / 'windows/WinSW-x64.exe')
            shutil.copyfile(config['winsw']['license'], payload / 'windows/WinSW-LICENSE.txt')
        manifest = render_manifest(template, config, lock_entries, wheels)
        if not manifest['runtime']['vendor_wheels']:
            raise KitError('Манифест v2 требует хотя бы одну зависимость в lock-файле')
        modes = config['modes'] or [manifest['modes'][0]['name']]
        known = {mode['name'] for mode in manifest['modes']}
        if any(mode not in known for mode in modes):
            raise KitError('modes: режим отсутствует в манифесте')
        (payload / ENTRY).write_text(ENTRY_SOURCE, encoding='utf-8')
        (payload / RELEASE_INFO).write_text(json.dumps(
            {'product_id': product_id, 'version': version, 'python_paths': config['python_paths'],
             'migrations': config['migrations']},
            ensure_ascii=True, sort_keys=True) + '\n', encoding='utf-8')
        if 'linux' in config['runtime']:
            runtime = config['runtime']['linux']
            prepare_pbs_archive(str(runtime['archive']), str(payload / 'runtime-linux'),
                                expected_sha256=runtime['sha256'], origin=runtime['origin'],
                                python_version=runtime['python_version'], pointer_launcher=True,
                                minimal=True)
            recipe = payload / RECIPE_PATH
            recipe.parent.mkdir()
            recipe.write_bytes(RECIPE_V2)
        if 'windows' in config['runtime']:
            runtime = config['runtime']['windows']
            prepare_windows_runtime(runtime['archive'], payload / 'runtime-windows',
                                    expected_sha256=runtime['sha256'], origin=runtime['origin'],
                                    python_version=runtime['python_version'])
        longest = max((item for item in payload.rglob('*') if item.is_file()),
                      key=lambda item: len(item.relative_to(payload).as_posix()))
        if len(longest.relative_to(payload).as_posix()) > MAX_RELEASE_PATH:
            raise KitError('Путь в выпуске длиннее ' + str(MAX_RELEASE_PATH) + ' символов (предел пути Windows): '
                           + longest.relative_to(payload).as_posix())
        # Пакет, подпись и перечень состава.
        kit_work = work / 'kit'
        package_dir = kit_work / 'package'
        package_dir.mkdir(parents=True)
        package_path = package_dir / (name + '.zip')
        receipt = build_package(manifest, version, str(payload), str(package_path))
        envelope = sign_package(str(package_path), private_key=key, expected_product_id=product_id,
                                expected_version=version)
        (package_dir / (name + '.sig')).write_bytes(envelope)
        (package_dir / 'publisher-ed25519.pub').write_bytes(public)
        inventory = release_inventory(str(package_path), envelope, trusted_public_key=public,
                                      expected_product_id=product_id, expected_version=version)
        (package_dir / 'inventory.json').write_text(json.dumps(inventory, ensure_ascii=False, indent=1,
                                                               sort_keys=True) + '\n', encoding='utf-8')
        # Установщик с собственным Python и закреплённой cryptography.
        crypto_lock = read_lock(CRYPTO_LOCK)
        installer_wheels = Path(installer_wheelhouse).resolve() if installer_wheelhouse else config['wheelhouse']
        if 'linux-x86_64' in config['targets']:
            crypto_wheels = select_wheels(crypto_lock, installer_wheels, 'linux-x86_64', glibc=config['glibc'])
            _installer_python(payload / 'runtime-linux', kit_work / 'installer/linux-x86_64/python', crypto_wheels)
            setup = kit_work / 'setup.sh'
            setup.write_text(SETUP_SH, encoding='utf-8')
            os.chmod(setup, 0o755)
        if 'windows-x86_64' in config['targets']:
            crypto_wheels = select_wheels(crypto_lock, installer_wheels, 'windows-x86_64')
            _installer_python(payload / 'runtime-windows', kit_work / 'installer/windows-x86_64/python',
                              crypto_wheels, windows=True)
            (kit_work / 'setup.cmd').write_bytes(SETUP_CMD.replace('\n', '\r\n').encode('ascii'))
        _copy_installer_code(kit_work / 'installer/app')
        release_size = _tree_size(payload)
        runtime_size = max(_tree_size(payload / name) for name in ('runtime-linux', 'runtime-windows')
                           if (payload / name).is_dir())
        estimates = config['estimates']
        data_estimate = estimates['data'] + estimates['config']
        required = {'app': 0, 'releases': 3 * release_size, 'data': estimates['data'],
                    'backup': estimates.get('backup', 2 * (data_estimate + release_size)),
                    'services': 1024 * 1024, 'config': estimates['config'], 'runtime': 2 * runtime_size}
        metadata = {'format': KIT_FORMAT, 'product_id': product_id, 'product_name': manifest['product_name'],
                    'version': version, 'package': 'package/' + package_path.name,
                    'envelope': 'package/' + name + '.sig', 'public_key': 'package/publisher-ed25519.pub',
                    'package_sha256': receipt['sha256'], 'public_key_sha256': hashlib.sha256(public).hexdigest(),
                    'modes': modes, 'all_modes': sorted(known), 'migrations': config['migrations'],
                    'required_bytes': required, 'reserve_bytes': 64 * 1024 * 1024,
                    'paths': manifest['paths'], 'web': manifest['web'],
                    'components': [component['name'] for component in manifest['components']],
                    'targets': config['targets'], 'dependencies': dependency_report,
                    'python_version': next(iter(config['runtime'].values()))['python_version']}
        (kit_work / 'kit.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                                           encoding='utf-8')
        (kit_work / 'README.txt').write_text(_readme(metadata), encoding='utf-8')
        sums = []
        for item in sorted(kit_work.rglob('*')):
            if item.is_file():
                sums.append(_sha256_file(item) + '  ' + item.relative_to(kit_work).as_posix())
        (kit_work / 'SHA256SUMS').write_text('\n'.join(sums) + '\n', encoding='utf-8')
        os.chmod(kit_work, 0o755)
        kit_work.rename(kit)
        if archive:
            _write_tar(kit, tar_path)
        return {'kit': str(kit), 'archive': str(tar_path) if archive else None, 'product_id': product_id,
                'version': version, 'package_sha256': receipt['sha256'],
                'public_key_sha256': metadata['public_key_sha256'], 'file_count': receipt['file_count'],
                'dependencies': dependency_report}
    except (KitWheelError, OSError) as error:
        raise KitError(str(error)) from None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _readme(metadata: dict) -> str:
    return f'''{metadata['product_name']} {metadata['version']} — установочный комплект HomenS.AI

Linux x86-64 (systemd пользователя, без root, без сети):

    ./setup.sh install            установка
    ./setup.sh status             состояние
    ./setup.sh update             обновление (запускать из комплекта новой версии)
    ./setup.sh uninstall          удаление с сохранением данных (--purge — полностью)

Перед установкой сверьте отпечаток ключа издателя с опубликованным
издателем по независимому каналу:

    SHA-256 ключа издателя: {metadata['public_key_sha256']}
    SHA-256 пакета:         {metadata['package_sha256']}

Подробности: docs/KIT.md и docs/ADMIN-GUIDE.md модуля установки HomenS.AI.
'''


def verify_kit(kit_dir, *, expected_key_sha256=None) -> dict:
    """Проверить SHA256SUMS, подпись пакета и согласованность kit.json."""
    from shared.package_signing import verify_signed_package
    kit = Path(kit_dir).resolve()
    metadata = _read_json(kit / 'kit.json')
    listed = {}
    for line in (kit / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        digest, _, name = line.partition('  ')
        if _HEX64.fullmatch(digest) is None or not name:
            raise KitError('SHA256SUMS: недопустимая строка')
        listed[name] = digest
    present = {item.relative_to(kit).as_posix() for item in kit.rglob('*') if item.is_file()} - {'SHA256SUMS'}
    if set(listed) != present:
        raise KitError('Состав комплекта не совпадает с SHA256SUMS')
    for name, digest in listed.items():
        if _sha256_file(kit / name) != digest:
            raise KitError('Файл комплекта изменён: ' + name)
    public = (kit / metadata['public_key']).read_bytes()
    if hashlib.sha256(public).hexdigest() != metadata['public_key_sha256']:
        raise KitError('Ключ издателя не совпадает с kit.json')
    if expected_key_sha256 is not None and metadata['public_key_sha256'] != expected_key_sha256:
        raise KitError('Ключ издателя не совпадает с ожидаемым отпечатком')
    verified = verify_signed_package(str(kit / metadata['package']), (kit / metadata['envelope']).read_bytes(),
                                     trusted_public_key=public, expected_product_id=metadata['product_id'],
                                     expected_version=metadata['version'])
    return {'product_id': metadata['product_id'], 'version': metadata['version'],
            'public_key_sha256': metadata['public_key_sha256'], 'file_count': len(verified['files']),
            'signature_verified': True, 'publisher_trust_verified': expected_key_sha256 is not None}


INIT_CONFIG = {
    'format': 1,
    'manifest': 'installer/manifest.json',
    'sources': [{'from': 'src', 'to': 'src'}],
    'python_paths': ['src'],
    'targets': ['linux-x86_64'],
    'locks': {'linux': 'installer/linux.lock', 'windows': 'installer/windows.lock'},
    'wheelhouse': 'installer/wheelhouse',
    'runtime': {'linux': {
        'archive': 'installer/inputs/cpython-3.14.10+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz',
        'sha256': '4d3c16c59ed0802a3d1d34d8bce504b15ceeaff39d62dadecc6a9fa803412aed',
        'origin': 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/'
                  'cpython-3.14.10%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz',
        'python_version': '3.14.10'}},
    'migrations': [],
}


def init_project(directory, *, product_id: str, product_name: str, port: int = 8800) -> list[str]:
    """Создать заготовки homensai-kit.json и шаблона манифеста, не перезаписывая файлы."""
    directory = Path(directory).resolve()
    if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', product_id or '') is None:
        raise KitError('Недопустимый product_id')
    package = re.sub(r'[^a-z0-9_]', '_', product_id)
    manifest = {
        'schema_version': 2, 'product_name': product_name, 'product_id': product_id,
        'archive_before_replace': True,
        'components': [{'name': 'web', 'cmd': ['@module', package + '.web', '--data-dir', '@data'],
                        'after': [], 'heartbeat': {'file': 'heartbeats/web.json', 'interval': 60, 'timeout': 180},
                        'restart': {'delay': 5, 'max': 5, 'window': 600}}],
        'web': {'host': '127.0.0.1', 'port': port, 'healthz': '/healthz'},
        'secrets': [], 'modes': [{'name': 'default', 'components': ['web']}], 'mutual_exclusion': [],
        'migrations': {'cmd': ['@module', package + '.migrate', '--data-dir', '@DATA_DIR@',
                               '--migration', '@MIGRATION_ID@'],
                       'backup_before_each': True, 'new_db_no_backup': False},
        'backup': {'cmd': ['@module', package + '.backup'], 'encrypted': True},
        'restore': {'cmd': ['@module', package + '.restore']},
        'update': {'staged_releases': True, 'pointer_file': 'active.json', 'state_file': 'update.json',
                   'health_wait': 60, 'backup_before_rollback': True, 'retain_previous_release': True,
                   'retain_runtime': True},
        'hooks': {'pre_start': [], 'health_checks': [], 'post_update': []},
    }
    files = {directory / CONFIG_NAME: INIT_CONFIG, directory / 'installer/manifest.json': manifest}
    if any(path.exists() for path in files):
        raise KitError('Файлы конфигурации уже существуют')
    created = []
    for path, value in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        created.append(str(path))
    return created


def fetch_wheels(config_path, *, python=sys.executable) -> list[list[str]]:
    """Скачать wheels lock-файлов в wheelhouse через pip (нужна сеть сборщика)."""
    import subprocess
    config = load_config(config_path)
    platforms = {'linux-x86_64': ['manylinux_2_28_x86_64', 'manylinux_2_17_x86_64', 'manylinux2014_x86_64'],
                 'windows-x86_64': ['win_amd64']}
    commands = []
    for target in config['targets']:
        command = [python, '-m', 'pip', 'download', '--only-binary=:all:', '--require-hashes', '--no-deps',
                   '--implementation', 'cp', '--python-version', '3.14', '--abi', 'cp314', '--abi', 'abi3',
                   '--abi', 'none', '--dest', str(config['wheelhouse']),
                   '-r', str(config['locks'][target.split('-')[0]])]
        for platform in platforms[target]:
            command += ['--platform', platform]
        subprocess.run(command, check=True)
        commands.append(command)
    return commands


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m shared.kit', description='Сборщик комплекта HomenS.AI')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='создать заготовки в каталоге продукта')
    init.add_argument('directory')
    init.add_argument('--product-id', required=True)
    init.add_argument('--product-name', required=True)
    init.add_argument('--port', type=int, default=8800)
    keygen = sub.add_parser('keygen', help='создать ключ издателя Ed25519')
    keygen.add_argument('directory')
    keygen.add_argument('--name', default='publisher')
    keygen.add_argument('--passphrase-env')
    fetch = sub.add_parser('fetch', help='скачать wheels по lock-файлам (сеть)')
    fetch.add_argument('--config', required=True)
    build = sub.add_parser('build', help='собрать подписанный комплект')
    build.add_argument('--config', required=True)
    build.add_argument('--version', required=True)
    build.add_argument('--signing-key', required=True)
    build.add_argument('--passphrase-env')
    build.add_argument('--installer-wheelhouse')
    build.add_argument('--output', required=True)
    build.add_argument('--no-archive', action='store_true')
    verify = sub.add_parser('verify', help='проверить собранный комплект')
    verify.add_argument('kit')
    verify.add_argument('--expect-key-sha256')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = init_project(args.directory, product_id=args.product_id, product_name=args.product_name,
                                  port=args.port)
        elif args.command == 'keygen':
            result = generate_keys(args.directory, name=args.name, passphrase_env=args.passphrase_env)
        elif args.command == 'fetch':
            result = fetch_wheels(args.config)
        elif args.command == 'build':
            result = build_kit(args.config, version=args.version, signing_key=args.signing_key,
                               output=args.output, passphrase_env=args.passphrase_env,
                               installer_wheelhouse=args.installer_wheelhouse, archive=not args.no_archive)
        else:
            result = verify_kit(args.kit, expected_key_sha256=args.expect_key_sha256)
    except (KitError, KitWheelError, OSError, ValueError) as error:
        print('Ошибка: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
