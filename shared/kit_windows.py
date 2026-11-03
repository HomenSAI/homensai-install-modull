# HomenS.AI · © 2026 Serhii Khomenko
"""Windows-бэкенд установщика комплекта: службы WinSW и транзакционное обновление.

Требует прав администратора: службы работают без входа пользователя от имени
LocalSystem. Каталог установки и состояния закрываются ACL (SYSTEM и
Administrators). Обновление выполняет общая транзакция UpdatePlan с
файловыми портами: остановка, проверенный зашифрованный полный архив,
размещение выпуска, копия перед каждой миграцией, миграции, переключение
указателя, запуск и проверка здоровья; при сбое — копия перед откатом,
восстановление данных, прежний выпуск и запуск.
"""
from __future__ import annotations

import base64
import http.client
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
from xml.sax.saxutils import escape

from shared.kit_setup import _SECRET_ENV, STATE_FORMAT, Setup, SetupError, _key, _private_dir, signed_release_info

_CREATE_NO_WINDOW = 0x08000000
_SYSTEM_SID, _ADMINS_SID = '*S-1-5-18', '*S-1-5-32-544'
_INSTALL_STEPS = ('release', 'runtime', 'control', 'services', 'start')
_WINDOWS_HOOKS = ('pre_start', 'health_checks', 'post_update')
# Грамматики идентификаторов манифеста (shared/manifest.schema.json): точки нет ни в одной.
_PRODUCT_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,62}')
_COMPONENT = re.compile(r'[a-z][a-z0-9_-]{0,62}')
_SERVICES_KEY = 'SYSTEM\\CurrentControlSet\\Services\\'


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _run(argv, *, timeout=120, check=True):
    completed = subprocess.run([str(item) for item in argv], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW, check=False)
    if check and completed.returncode != 0:
        raise SetupError('kit.windows_command_failed', command=Path(str(argv[0])).name,
                         code=str(completed.returncode))
    return completed


def secure_directory(path: Path):
    """Новая DACL без наследования: только SYSTEM и Administrators (на дочерние объекты)."""
    _run(['icacls', str(path), '/inheritance:r', '/grant:r', _SYSTEM_SID + ':(OI)(CI)F',
          '/grant:r', _ADMINS_SID + ':(OI)(CI)F', '/Q'])


_TRUSTED_SIDS = {'S-1-5-18', 'S-1-5-32-544'}


def _acl_sids(path: Path) -> tuple[str, set[str]]:
    """Владелец и SID всех записей DACL каталога (через Get-Acl, путь передаётся окружением)."""
    script = ('$a = Get-Acl -LiteralPath $env:HOMENSAI_ACL_PATH; '
              '$a.GetOwner([Security.Principal.SecurityIdentifier]).Value; '
              '$a.Access | ForEach-Object { $_.IdentityReference.Translate('
              '[Security.Principal.SecurityIdentifier]).Value }')
    completed = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                               creationflags=_CREATE_NO_WINDOW, env={**os.environ, 'HOMENSAI_ACL_PATH': str(path)})
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        raise SetupError('kit.windows_command_failed', command='Get-Acl', code=str(completed.returncode))
    return lines[0], set(lines[1:])


def secure_owned(path: Path):
    """Каталог только для SYSTEM и Administrators; чужой заранее созданный каталог отклоняется.

    Без этого обычный пользователь мог бы заранее создать каталог в ProgramData,
    сохранить свои права и подменить файлы, которые затем исполняет администратор.
    """
    if path.exists():
        owner, _ = _acl_sids(path)
        if owner not in _TRUSTED_SIDS or path.is_symlink() or not path.is_dir():
            raise SetupError('kit.unsafe_state')
    else:
        path.mkdir(parents=True)
    _run(['icacls', str(path), '/setowner', _ADMINS_SID, '/Q'])
    secure_directory(path)
    owner, sids = _acl_sids(path)
    for sid in sorted(sids - _TRUSTED_SIDS):
        _run(['icacls', str(path), '/remove:g', '*' + sid, '/Q'])
    owner, sids = _acl_sids(path)
    if owner not in _TRUSTED_SIDS or not sids <= _TRUSTED_SIDS:
        raise SetupError('kit.unsafe_state')


def _service_image_path(service_id: str) -> str | None:
    """ImagePath зарегистрированной службы из реестра SCM; None — службы с таким ID нет.

    Реестр читается напрямую (winreg): значение приходит в Unicode, без разбора
    локализованного вывода sc.exe и перекодировок консоли.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SERVICES_KEY + service_id)
    except FileNotFoundError:
        return None
    with key:
        try:
            value, kind = winreg.QueryValueEx(key, 'ImagePath')
        except FileNotFoundError:
            return ''
    if type(value) is not str:
        return ''
    return winreg.ExpandEnvironmentStrings(value) if kind == winreg.REG_EXPAND_SZ else value


def _image_executable(image: str) -> str:
    """Путь исполняемого файла из ImagePath: WinSW 2.x регистрирует его в кавычках без аргументов."""
    text = image.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else ''
    return text


def _same_file(left: str, right) -> bool:
    if not left:
        return False
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(str(right)))


def check_windows_hooks(manifest):
    """Хуки pre_start, health_checks, post_update на Windows не исполняются: отказ до любых эффектов."""
    used = [name for name in _WINDOWS_HOOKS if manifest['hooks'][name]]
    if used:
        raise SetupError('kit.windows_hooks_unsupported', hooks=', '.join(used))


def _selected_components(manifest, modes):
    """Фактический состав выбранных режимов; режим, которого нет в манифесте, — None."""
    from shared.manifest import ManifestError
    from shared.services import component_order
    try:
        return component_order(manifest, tuple(modes))
    except ManifestError:
        return None


def _expand(argument: str, roots: dict, *, executable: bool) -> str:
    if not argument.startswith('@'):
        return argument
    marker, separator, suffix = argument.partition('/')
    name = marker[1:]
    if name not in roots or (separator and not suffix):
        raise SetupError('kit.windows_command_failed', command='manifest', code='template')
    if executable and name == 'runtime' and suffix in ('bin/python', 'bin/python3'):
        return str(roots['runtime'] / 'python.exe')
    return str(roots[name].joinpath(*suffix.split('/'))) if separator else str(roots[name])


class WindowsServices:
    """Службы компонентов через WinSW: XML пересоздаётся для текущего выпуска перед запуском.

    Перед install/start/stop/uninstall каждая уже зарегистрированная служба
    проверяется на принадлежность: её ImagePath должен указывать на нашу обёртку
    WinSW в каталоге app/services этой установки. Чужая служба с тем же ID
    не изменяется — команда отклоняется до любых действий.
    """

    def __init__(self, manifest, home, modes, product_id):
        from shared.services import component_order
        self.manifest, self.home, self.product_id = manifest, home, product_id
        self.components = component_order(manifest, tuple(modes))
        if _PRODUCT_ID.fullmatch(product_id) is None or any(
                _COMPONENT.fullmatch(item['name']) is None for item in manifest['components']):
            raise SetupError('kit.kit_unreadable')
        self.directory = home.app / 'services'
        self.logs = home.app / 'logs'
        self.winsw_source = None

    def service_id(self, component):
        # Точка не входит ни в product_id, ни в имя компонента: пара восстанавливается
        # однозначно, поэтому ID разных пар (продукт, компонент) не совпадают.
        return 'homensai.' + self.product_id + '.' + component

    def exe(self, component):
        return self.directory / (self.service_id(component) + '.exe')

    def xml(self, component, release: str) -> str:
        definition = next(item for item in self.manifest['components'] if item['name'] == component)
        release_dir = self.home.releases / ('app-' + release)
        roots = {'runtime': self.home.roots['runtime'], 'data': self.home.roots['data'],
                 'app': self.home.app, 'release': release_dir}
        command = [_expand(arg, roots, executable=index == 0) for index, arg in enumerate(definition['cmd'])]
        arguments = subprocess.list2cmdline(command[1:])
        depends = ''.join(f'  <depend>{escape(self.service_id(name))}</depend>\n'
                          for name in definition['after'] if name in self.components)
        restart = definition['restart']
        return (f'<service>\n  <id>{escape(self.service_id(component))}</id>\n'
                f'  <name>{escape(self.manifest["product_name"] + " — " + component)}</name>\n'
                f'  <description>{escape("HomenS.AI " + self.product_id + " " + component + " " + release)}'
                f'</description>\n  <executable>{escape(command[0])}</executable>\n'
                f'  <arguments>{escape(arguments)}</arguments>\n'
                f'  <workingdirectory>{escape(str(release_dir))}</workingdirectory>\n'
                f'  <startmode>Automatic</startmode>\n{depends}'
                f'  <stoptimeout>30 sec</stoptimeout>\n'
                f'  <onfailure action="restart" delay="{max(1, restart["delay"])} sec"/>\n'
                f'  <resetfailure>{restart["window"]} sec</resetfailure>\n'
                f'  <logpath>{escape(str(self.logs))}</logpath>\n'
                f'  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>4</keepFiles></log>\n'
                f'</service>\n')

    def write(self, release: str, winsw: Path):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        for component in self.components:
            exe = self.exe(component)
            if not exe.exists() or exe.read_bytes() != winsw.read_bytes():
                shutil.copyfile(winsw, exe)
            exe.with_suffix('.xml').write_text(self.xml(component, release), encoding='utf-8')

    def state(self, component) -> str:
        if not self.exe(component).exists():
            return 'NonExistent'
        output = _run([self.exe(component), 'status'], check=False).stdout.strip()
        return output.splitlines()[-1].strip() if output else 'Unknown'

    def registered(self, component) -> bool:
        """Зарегистрирована ли наша служба; служба с тем же ID и чужим ImagePath — отказ."""
        image = _service_image_path(self.service_id(component))
        if image is None:
            return False
        if not _same_file(_image_executable(image), self.exe(component)):
            raise SetupError('kit.windows_service_foreign', service=self.service_id(component))
        return True

    def check_owned(self, components=None):
        """Проверить принадлежность всех служб набора до первого действия над любой из них."""
        for component in components or self.components:
            self.registered(component)

    def install(self):
        self.check_owned()
        for component in self.components:
            if self.state(component) == 'NonExistent':
                _run([self.exe(component), 'install'])

    def _wait(self, component, expected, timeout=60):
        deadline = time.monotonic() + timeout
        while self.state(component) != expected:
            if time.monotonic() > deadline:
                raise SetupError('kit.windows_service_state', service=self.service_id(component), state=expected)
            time.sleep(0.5)

    def start(self, components=None):
        components = tuple(components or self.components)
        self.check_owned(components)
        for component in components:
            if self.state(component) != 'Started':
                _run([self.exe(component), 'start'], check=False)
                self._wait(component, 'Started')

    def stop(self, components=None):
        components = tuple(components or reversed(self.components))
        self.check_owned(components)
        for component in components:
            if self.state(component) not in ('Stopped', 'NonExistent'):
                _run([self.exe(component), 'stop'], check=False)
                self._wait(component, 'Stopped')

    def uninstall(self):
        self.check_owned()
        self.stop()
        for component in reversed(self.components):
            if self.state(component) != 'NonExistent':
                _run([self.exe(component), 'uninstall'])


def http_health(manifest, product_id, version, timeout) -> bool:
    web = manifest['web']
    url = f'http://127.0.0.1:{web["port"]}{web["healthz"]}'
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                body = json.loads(response.read(4096).decode('utf-8'))
            if response.status == 200 and body == {'product': product_id, 'release': version, 'healthy': True}:
                return True
        except (OSError, ValueError, http.client.HTTPException):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


class WindowsEffects:
    """Внедрённые эффекты UpdatePlan для Windows: службы, миграции, health.

    services — WindowsServices целевого выпуска; previous — прежнего (для отката),
    чтобы прежний выпуск запускался со своими командами из своего манифеста.
    """

    def __init__(self, services: WindowsServices, ports_ref, *, product_id, target_release, winsw: Path,
                 previous: tuple[str, WindowsServices, Path] | None = None):
        self.services, self.ports_ref = services, ports_ref
        self.product_id, self.target_release, self.winsw = product_id, target_release, winsw
        self.previous = previous

    def _active(self):
        release = self.ports_ref().current_release()
        if self.previous is not None and release == self.previous[0]:
            return release, self.previous[1], self.previous[2]
        return release, self.services, self.winsw

    def stop(self, key, components):
        # Останавливается весь выбранный набор активного выпуска (прежнего — его
        # собственным манифестом), а не только состав плана: ни один прежний
        # писатель не работает во время архива и миграций.
        services = self._active()[1]
        extra = tuple(name for name in components if name not in services.components)
        services.stop(tuple(reversed(services.components)) + extra)

    def migrate(self, key, migration, command):
        from shared.update_plan import EffectIndeterminate
        if len(command) < 2 or command[0] != 'python' or not command[1].endswith('.py'):
            raise RuntimeError('unsupported migration command')
        home = self.services.home
        release = home.releases / ('app-' + self.target_release)
        arguments = [str(home.roots['data']) if arg == '@DATA_DIR@' else migration if arg == '@MIGRATION_ID@'
                     else arg for arg in command[2:]]
        argv = [str(home.roots['runtime'] / 'python.exe'), '-I', '-B',
                str(release.joinpath(*command[1].split('/'))), *arguments]
        try:
            completed = subprocess.run(argv, cwd=str(release), stdin=subprocess.DEVNULL, capture_output=True,
                                       timeout=600, creationflags=_CREATE_NO_WINDOW,
                                       env={'SystemRoot': os.environ.get('SystemRoot', r'C:\Windows'),
                                            'PATH': os.environ.get('SystemRoot', r'C:\Windows') + r'\System32'})
        except subprocess.TimeoutExpired:
            raise EffectIndeterminate('Время миграции истекло') from None
        if completed.returncode != 0:
            raise RuntimeError('migration failed')

    def start(self, key, components, pre_start):
        if pre_start:
            raise RuntimeError('pre_start hooks are not supported on Windows')
        release, services, winsw = self._active()
        services.write(release, winsw)
        services.install()
        services.start(components)

    def health(self, key, url, timeout, checks):
        if checks:
            raise RuntimeError('health_checks hooks are not supported on Windows')
        release, services, _ = self._active()
        if not http_health(services.manifest, self.product_id, release, timeout):
            raise RuntimeError('health check failed')

    def post_update(self, key, hooks):
        if hooks:
            raise RuntimeError('post_update hooks are not supported on Windows')


class WindowsSetup(Setup):
    """install / update / status / uninstall для Windows x86-64 (администратор)."""

    def __init__(self, kit, *, state_dir=None, **options):
        program_data = Path(os.environ.get('ProgramData', r'C:\ProgramData'))
        super().__init__(kit, state_dir=state_dir or program_data / 'HomenSAI' / '.state' / kit.product_id,
                         **options)
        self.program_data = program_data

    # --- общее ---------------------------------------------------------------------
    def prepare_state_dir(self):
        base = self.program_data / 'HomenSAI'
        for path in (base, base / '.state'):
            secure_owned(path)
        secure_owned(self.state_dir)

    def _key_of(self, release) -> bytes:
        state = self.state()
        return base64.b64decode(self.release_key(state, release) if state else release['public_key'])

    def _verified(self, release):
        from shared.package_signing import verify_signed_package
        return verify_signed_package(release['package'], Path(release['envelope']).read_bytes(),
                                     trusted_public_key=self._key_of(release),
                                     expected_product_id=self.kit.product_id, expected_version=release['version'])

    def _home(self, manifest, root):
        from shared.datahome import resolve_datahome
        return resolve_datahome(manifest, root)

    def _winsw(self, home, version) -> Path:
        path = home.releases / ('app-' + version) / 'windows' / 'WinSW-x64.exe'
        if not path.is_file():
            raise SetupError('kit.kit_unreadable')
        return path

    def _release_matches(self, directory: Path, metadata) -> bool:
        from shared.package import _files
        try:
            entries, _ = _files(directory, verify_crypto_block=False, verify_runtime_block=False)
        except Exception:
            return False
        return entries == metadata['files']

    def _stage(self, release, metadata, destination: Path):
        from shared.package_prepare import prepare_signed_package
        if destination.exists():
            if not self._release_matches(destination, metadata):
                raise SetupError('kit.release_mismatch', path=destination)
            return
        prepare_signed_package(release['package'], Path(release['envelope']).read_bytes(), str(destination),
                               trusted_public_key=self._key_of(release), expected_product_id=self.kit.product_id,
                               expected_version=release['version'])

    def _copy_runtime(self, release_dir: Path, runtime: Path, metadata):
        source = release_dir / 'runtime-windows'
        expected = {item['path'][len('runtime-windows/'):]: item['sha256'] for item in metadata['files']
                    if item['path'].startswith('runtime-windows/')}
        if not expected:
            raise SetupError('kit.kit_unreadable')
        from shared.kit import _sha256_file
        runtime.mkdir(parents=True, exist_ok=True)
        for name, digest in sorted(expected.items()):
            target = runtime.joinpath(*name.split('/'))
            if target.is_file() and _sha256_file(target) == digest:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source.joinpath(*name.split('/')), target)
            if _sha256_file(target) != digest:
                raise SetupError('kit.release_mismatch', path=target)

    # --- команды -------------------------------------------------------------------
    def install(self, *, root=None, unit_dir=None, modes=None, expect_key=None):
        from shared.manifest import check_target
        from shared.update_plan import _atomic_write
        meta = self.kit.meta
        if expect_key is not None and expect_key != meta['public_key_sha256']:
            raise SetupError('kit.key_mismatch')
        if 'windows-x86_64' not in meta['targets']:
            raise SetupError('kit.unsupported_host')
        state = self.state()
        if state is not None and state['status'] == 'installed':
            raise SetupError('kit.already_installed', version=state['version'])
        if state is not None and state['status'] == 'uninstalled' and state.get('data_policy') == 'purge':
            state = None
        self.check_key(state, expect_key)
        pending = state.get('install') if state is not None else None
        if pending is not None and pending['version'] != self.kit.version:
            raise SetupError('kit.pending_other_version', version=pending['version'])
        if pending is None:
            install_root = Path(root).resolve() if root else (
                Path(state['install_root']) if state else self.program_data / 'HomenSAI' / self.kit.product_id)
            selected = list(modes) if modes else (state['modes'] if state else list(meta['modes']))
            if any(mode not in meta['all_modes'] for mode in selected):
                raise SetupError('kit.unknown_mode')
            reuse = state is not None and state['status'] == 'uninstalled'
            if reuse and state.get('version') != self.kit.version:
                raise SetupError('kit.reinstall_version', version=state.get('version'))
            if not reuse and install_root.exists():
                raise SetupError('kit.root_exists', path=install_root)
        else:
            install_root, selected, reuse = Path(pending['install_root']), pending['modes'], pending['reuse']
        self.show_kit()
        self.out(self.t('kit.summary_root', path=install_root))
        self.out(self.t('kit.summary_modes', modes=', '.join(selected)))
        if reuse:
            self.out(self.t('kit.reuse_data'))
        if pending is not None:
            self.out(self.t('kit.resume'))
        self.confirm('kit.confirm_install')
        self.prepare_state_dir()
        release = self.store_release(self.state_dir, self.kit)
        metadata = self._verified(release)
        if check_target(metadata['manifest'], {'os': 'windows', 'architecture': 'x86_64',
                                               'execution': 'native'})['status'] != 'supported':
            raise SetupError('kit.unsupported_host')
        check_windows_hooks(metadata['manifest'])
        if pending is None:
            pending = {'version': self.kit.version, 'install_root': str(install_root), 'modes': selected,
                       'reuse': reuse, 'index': 0}
            state = {**(state or {}), 'format': STATE_FORMAT, 'product_id': self.kit.product_id,
                     'status': state['status'] if state else 'installing', 'install': pending,
                     'install_root': str(install_root), 'modes': selected,
                     'web': meta['web'], 'paths': meta['paths'], 'platform': 'windows'}
            state.setdefault('releases', {})
            self.save(state)
        manifest = metadata['manifest']
        home = self._home(manifest, install_root)
        release_dir = home.releases / ('app-' + self.kit.version)
        services = WindowsServices(manifest, home, selected, self.kit.product_id)
        self.out(self.t('kit.installing'))
        while pending['index'] < len(_INSTALL_STEPS):
            step = _INSTALL_STEPS[pending['index']]
            if step == 'release':
                if not install_root.exists():
                    install_root.parent.mkdir(parents=True, exist_ok=True)
                secure_owned(install_root)
                for path in (home.app, home.releases, *home.roots.values()):
                    path.mkdir(parents=True, exist_ok=True)
                self._stage(release, metadata, release_dir)
            elif step == 'runtime':
                self._copy_runtime(release_dir, home.roots['runtime'], metadata)
            elif step == 'control':
                pointer = home.roots['services'] / manifest['update']['pointer_file']
                pointer.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(pointer, {'release': self.kit.version})
            elif step == 'services':
                services.write(self.kit.version, self._winsw(home, self.kit.version))
                services.install()
            elif step == 'start':
                services.start()
            pending['index'] += 1
            self.save(state)
        state['releases'][self.kit.version] = release
        state.update(status='installed', version=self.kit.version, base=self.kit.version,
                     public_key=self._b64key(), applied_migrations=list(release['migrations']),
                     history=state.get('history', []) + [
                         {'action': 'install', 'version': self.kit.version, 'time': int(time.time())}])
        state.pop('install', None)
        state.pop('data_policy', None)
        self.save(state)
        self.store_installer()
        self.out(self.t('kit.installed', version=self.kit.version))
        healthy = http_health(manifest, self.kit.product_id, self.kit.version, 60)
        self.out(self.t('kit.health_ok') if healthy else self.t('kit.health_unknown'))
        self.out(self.t('kit.manage_hint', path=self.state_dir / 'installer' / 'setup.cmd'))
        return 0

    def _b64key(self):
        import base64
        return base64.b64encode(self.kit.public_key).decode('ascii')

    def update(self, *, accept_key=None, reconcile_stop=False):
        from shared import archive_crypto
        from shared.adapters_fs import FilesystemPorts, ReleaseSource
        from shared.update_plan import UpdateIndeterminate, UpdatePlan
        state = self.state()
        if state is None or state['status'] not in ('installed', 'updating'):
            raise SetupError('kit.not_installed')
        self.check_key(state, accept_key)
        pending = state.get('update')
        if pending is not None and pending['new_version'] != self.kit.version:
            raise SetupError('kit.pending_other_version', version=pending['new_version'])
        if pending is None and self.kit.version == state['version']:
            raise SetupError('kit.same_version', version=state['version'])
        applied = state.get('applied_migrations', [])
        kit_migrations = signed_release_info(self.kit.package, self.kit.envelope.read_bytes(), self.kit.public_key,
                                             self.kit.product_id, self.kit.version)['migrations']
        if kit_migrations[:len(applied)] != applied:
            raise SetupError('kit.migrations_diverged')
        new_migrations = kit_migrations[len(applied):] if pending is None else pending['migration_ids']
        self.show_kit()
        self.out(self.t('kit.update_from', old=state['version'], new=self.kit.version))
        self.out(self.t('kit.update_migrations', migrations=', '.join(new_migrations) or '—'))
        if pending is not None:
            self.out(self.t('kit.resume'))
        self.confirm('kit.confirm_update')
        release = self.store_release(self.state_dir, self.kit)
        metadata = self._verified(release)
        check_windows_hooks(metadata['manifest'])
        old_release = state['releases'][state['version']]
        old_metadata = self._verified(old_release)
        old_manifest = old_metadata['manifest']
        # Сравнивается и фактический состав выбранных режимов: иначе служба, исключённая
        # из режима новым выпуском, не остановилась бы и писала во время архива и миграций.
        old_selected = _selected_components(old_manifest, state['modes'])
        if ([item['name'] for item in old_manifest['components']]
                != [item['name'] for item in metadata['manifest']['components']]
                or old_selected is None
                or old_selected != _selected_components(metadata['manifest'], state['modes'])
                or old_manifest['paths'] != metadata['manifest']['paths']
                or old_manifest['update'] != metadata['manifest']['update']):
            raise SetupError('kit.components_changed')
        runtime = lambda files: {item['path']: item['sha256'] for item in files
                                 if item['path'].startswith('runtime-windows/')}
        if runtime(old_metadata['files']) != runtime(metadata['files']):
            raise SetupError('kit.runtime_changed')
        self._password()
        if pending is None:
            key = _key()
            # Короткие имена: пути Windows ограничены 260 символами.
            pending = {'new_version': self.kit.version, 'idempotency_key': key, 'migration_ids': new_migrations,
                       'work_dir': str(_private_dir(self.state_dir / 'w' / ('u' + key[:10])))}
            state.update(status='updating', update=pending)
            self.save(state)
        work = Path(pending['work_dir'])
        payload = work / 'p'
        manifest = metadata['manifest']
        home = self._home(manifest, Path(state['install_root']))
        if 'finished' in pending:
            return self._finish_update(state, pending, release, home, payload)
        self._stage(release, metadata, payload)
        services = WindowsServices(manifest, home, state['modes'], self.kit.product_id)
        previous = (state['version'], WindowsServices(old_manifest, home, state['modes'], self.kit.product_id),
                    self._winsw(home, state['version']))
        ports = None
        effects = WindowsEffects(services, lambda: ports, product_id=self.kit.product_id,
                                 target_release=self.kit.version, winsw=payload / 'windows' / 'WinSW-x64.exe',
                                 previous=previous)
        ports = FilesystemPorts(manifest, Path(state['install_root']), work_dir=work / 't',
                                payloads={self.kit.version: ReleaseSource(
                                    payload, {item['path']: item['sha256'] for item in metadata['files']})},
                                effects=effects, password_provider=archive_crypto.password_provider(env_var=_SECRET_ENV))
        plan = UpdatePlan(manifest, Path(state['install_root']), ports, release=self.kit.version,
                          modes=tuple(state['modes']), migration_ids=tuple(pending['migration_ids']))
        self.out(self.t('kit.updating'))
        try:
            result = plan.run(resume_indeterminate=reconcile_stop)
        except UpdateIndeterminate:
            raise SetupError('kit.update_indeterminate') from None
        except Exception:
            raise SetupError('kit.api_failed', operation='update', code='update_failed') from None
        finally:
            os.environ.pop(_SECRET_ENV, None)
        # Исход сохраняется до переноса файла транзакции: после сбоя повтор завершит учёт.
        pending['finished'] = {'transaction': result['transaction'], 'outcome': result['outcome'],
                               'state_path': str(plan.state_path)}
        self.save(state)
        return self._finish_update(state, pending, release, home, payload)

    def _finish_update(self, state, pending, release, home, payload):
        finished = pending['finished']
        outcome, transaction = finished['outcome'], finished['transaction']
        # Завершённая транзакция переносится в журнал: следующая начнётся с чистого состояния.
        history_dir = home.roots['services'] / 'history'
        history_dir.mkdir(exist_ok=True)
        if Path(finished['state_path']).exists():
            os.replace(finished['state_path'], history_dir / ('update-' + transaction + '.json'))
        work = Path(pending['work_dir'])
        applied = state.get('applied_migrations', [])
        history = state.get('history', [])
        if outcome == 'updated':
            state['releases'][self.kit.version] = release
            state.update(version=self.kit.version, base=self.kit.version, public_key=self._b64key(),
                         applied_migrations=applied + list(pending['migration_ids']))
            history.append({'action': 'update', 'version': self.kit.version, 'time': int(time.time())})
            code = 0
        else:
            history.append({'action': 'rollback', 'version': state['version'], 'attempted': self.kit.version,
                            'time': int(time.time())})
            code = 4
        state.update(status='installed', history=history, last_update={
            'transaction': transaction, 'outcome': outcome, 'work_dir': pending['work_dir'], 'code': code})
        state.pop('update', None)
        self.save(state)
        shutil.rmtree(payload, ignore_errors=True)
        if outcome == 'updated':
            self.store_installer()
            self.out(self.t('kit.updated', version=self.kit.version))
        else:
            self.out(self.t('kit.rolled_back', version=state['version']))
        self.out(self.t('kit.archive_hint', path=work / 't'))
        return code

    def status(self):
        state = self.state()
        if state is None:
            self.out(self.t('kit.status_none'))
            return 1
        self.out(self.t('kit.status_line', product=self.kit.product_id, status=state['status'],
                        version=state.get('version', '—')))
        self.out(self.t('kit.summary_root', path=state['install_root']))
        if state['status'] != 'installed':
            return 0 if state['status'] == 'uninstalled' else 1
        manifest = self._verified(state['releases'][state['version']])['manifest']
        services = WindowsServices(manifest, self._home(manifest, Path(state['install_root'])), state['modes'],
                                   self.kit.product_id)
        for component in services.components:
            self.out('  ' + services.service_id(component) + ': ' + services.state(component))
        healthy = http_health(manifest, self.kit.product_id, state['version'], 3)
        self.out(self.t('kit.health_ok') if healthy else self.t('kit.health_failed'))
        return 0 if healthy else 1

    def uninstall(self, *, purge=False):
        state = self.state()
        if state is None or state['status'] not in ('installed', 'uninstalling'):
            raise SetupError('kit.not_installed')
        pending = state.get('uninstall') or {'data_policy': 'purge' if purge else 'keep'}
        if purge != (pending['data_policy'] == 'purge'):
            raise SetupError('kit.pending_uninstall')
        self.out(self.t('kit.status_line', product=self.kit.product_id, status=state['status'],
                        version=state['version']))
        self.out(self.t('kit.summary_root', path=state['install_root']))
        if purge:
            self.out(self.t('kit.purge_warning'))
            if not self.yes:
                try:
                    answer = self.ask(self.t('kit.confirm_purge', product=self.kit.product_id) + ' ')
                except EOFError:
                    answer = ''
                if answer.strip() != self.kit.product_id:
                    raise SetupError('kit.cancelled', 3)
        else:
            self.out(self.t('kit.keep_notice'))
            self.confirm('kit.confirm_uninstall')
        state.update(status='uninstalling', uninstall=pending)
        self.save(state)
        manifest = self._verified(state['releases'][state['version']])['manifest']
        root = Path(state['install_root'])
        home = self._home(manifest, root)
        WindowsServices(manifest, home, state['modes'], self.kit.product_id).uninstall()
        removable = [home.app, home.releases, home.roots['runtime'], home.roots['services']]
        if purge:
            removable = [root]
        for path in removable:
            if path.exists():
                shutil.rmtree(path)
        state.update(status='uninstalled', data_policy=pending['data_policy'])
        state.pop('uninstall', None)
        state['history'] = state.get('history', []) + [
            {'action': 'uninstall', 'version': state['version'], 'data_policy': pending['data_policy'],
             'time': int(time.time())}]
        self.save(state)
        self.out(self.t('kit.uninstalled_purge') if purge else self.t('kit.uninstalled_keep', path=root))
        return 0

