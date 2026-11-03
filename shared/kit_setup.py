# HomenS.AI · © 2026 Serhii Khomenko
"""Установщик из комплекта на целевой системе: install, update, status, uninstall.

Запускается встроенным Python комплекта (`setup.sh`). Все изменяющие действия
выполняются операциями локального API через клиент; ключи операций и копии
установленных пакетов сохраняются в каталоге состояния до вызова, поэтому
прерванную операцию повторяет та же команда. Пароли не сохраняются.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from shared.i18n import LanguageError, available_languages, translate

STATE_FORMAT = 1
PASSWORD_ENV = 'HOMENSAI_ARCHIVE_PASSWORD'
_SECRET_ENV = 'HOMENSAI_SECRET_ARCHIVE_PASSWORD'
_KEY_NAMES = ('release', 'runtime', 'publish', 'load', 'start')
_TIMEOUT = 3600


class SetupError(RuntimeError):
    """Команда не выполнена; key — ключ сообщения kit.* для пользователя."""

    def __init__(self, key, exit_code=1, **variables):
        super().__init__(key)
        self.key, self.exit_code, self.variables = key, exit_code, variables


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SetupError('kit.invalid_arguments', 2)


def _language(value):
    if value is not None:
        return value
    for name in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        prefix = os.environ.get(name, '')[:2].lower()
        if prefix in available_languages():
            return prefix
    return 'en'


def _key():
    return secrets.token_hex(16)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not path.is_dir() or path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
            or (os.name == 'posix' and info.st_uid != os.getuid())):
        raise SetupError('kit.unsafe_state')
    if os.name == 'posix':
        os.chmod(path, 0o700)
    return path


def _write_json(path: Path, value: dict):
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(4) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=1, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name == 'posix':
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _read_json(path: Path) -> dict:
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if type(value) is not dict:
        raise SetupError('kit.state_corrupt')
    return value


def signed_release_info(package, envelope: bytes, key: bytes, product_id: str, version: str) -> dict:
    """homensai-release.json из подписанного пакета: история миграций не берётся из kit.json."""
    import zipfile
    from shared.kit import RELEASE_INFO
    from shared.package_signing import verify_signed_package
    metadata = verify_signed_package(str(package), envelope, trusted_public_key=key,
                                     expected_product_id=product_id, expected_version=version)
    expected = {item['path']: item['sha256'] for item in metadata['files']}.get(RELEASE_INFO)
    if expected is None:
        raise SetupError('kit.kit_unreadable')
    with zipfile.ZipFile(package) as archive:
        raw = archive.read('payload/' + RELEASE_INFO)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SetupError('kit.kit_unreadable')
    info = json.loads(raw.decode('utf-8'))
    migrations = info.get('migrations', [])
    if (type(info) is not dict or info.get('product_id') != product_id or info.get('version') != version
            or type(migrations) is not list or any(type(item) is not str for item in migrations)):
        raise SetupError('kit.kit_unreadable')
    return {**info, 'migrations': migrations}


class Kit:
    """Комплект на диске: kit.json, пакет, подпись, ключ и установщик."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        try:
            self.meta = _read_json(self.root / 'kit.json')
        except (OSError, ValueError):
            raise SetupError('kit.kit_unreadable') from None
        if self.meta.get('format') != 1:
            raise SetupError('kit.kit_unreadable')
        try:
            self.product_id = self.meta['product_id']
            self.version = self.meta['version']
            self.package = self.root / self.meta['package']
            self.envelope = self.root / self.meta['envelope']
            self.public_key = (self.root / self.meta['public_key']).read_bytes()
        except (OSError, KeyError, TypeError):
            raise SetupError('kit.kit_unreadable') from None
        if hashlib.sha256(self.public_key).hexdigest() != self.meta['public_key_sha256']:
            raise SetupError('kit.kit_unreadable')


class Setup:
    def __init__(self, kit: Kit, *, language: str, state_dir: Path | None, yes: bool,
                 out=print, ask=input, ask_secret=getpass.getpass, client=None):
        self.kit, self.language, self.yes = kit, language, yes
        self.out, self.ask, self.ask_secret = out, ask, ask_secret
        home = Path.home()
        base = Path(os.environ.get('XDG_STATE_HOME') or home / '.local/state')
        self.state_dir = (state_dir or base / 'homensai' / kit.product_id).resolve()
        self.state_file = self.state_dir / 'state.json'
        self._client = client
        self.finalized = False

    # --- служебное -----------------------------------------------------------------
    def t(self, key, **variables):
        return translate(self.language, key, **{name: str(value) for name, value in variables.items()})

    @property
    def client(self):
        if self._client is None:
            from homensai_installer_client import InstallerClient
            self._client = InstallerClient(timeout=_TIMEOUT, api_root=str(Path(__file__).resolve().parent.parent))
        return self._client

    def call(self, operation, params, *, min_minor, secret_env_name=None):
        from homensai_installer_client import InstallerClientError
        try:
            return self.client.call(operation, params, min_minor=min_minor, language=self.language,
                                    secret_env_name=secret_env_name)
        except InstallerClientError as error:
            raise SetupError('kit.api_failed', operation=operation, code=error.code) from None

    def state(self):
        if not self.state_file.exists():
            return None
        try:
            value = _read_json(self.state_file)
        except (OSError, ValueError):
            raise SetupError('kit.state_corrupt') from None
        if value.get('format') != STATE_FORMAT or value.get('product_id') != self.kit.product_id:
            raise SetupError('kit.state_corrupt')
        return value

    def save(self, state):
        _write_json(self.state_file, state)

    def prepare_state_dir(self):
        _private_dir(self.state_dir)

    @contextlib.contextmanager
    def locked(self):
        """Один запуск установщика на продукт: состояние не перезаписывается параллельно."""
        from shared.update_plan import LockBusy, operation_lock
        self.prepare_state_dir()
        try:
            with operation_lock(self.state_dir / 'setup.lock'):
                self.finish_installer()
                yield
        except LockBusy:
            raise SetupError('kit.busy') from None

    def confirm(self, prompt_key, **variables):
        if self.yes:
            return
        try:
            answer = self.ask(self.t(prompt_key, **variables) + ' ')
        except EOFError:
            answer = ''
        if answer.strip().lower() not in ('yes', 'y', 'да', 'ja'):
            raise SetupError('kit.cancelled', 3)

    def store_release(self, state_dir: Path, kit: Kit) -> dict:
        """Скопировать пакет и подпись версии в каталог состояния (нужны для update/uninstall)."""
        target = _private_dir(state_dir / 'packages' / kit.version)
        package, envelope = target / kit.package.name, target / kit.envelope.name
        for source, destination in ((kit.package, package), (kit.envelope, envelope)):
            if destination.exists() and _sha256(destination) == _sha256(source):
                continue
            temporary = destination.with_name('.' + destination.name + '.' + secrets.token_hex(4) + '.tmp')
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        if _sha256(package) != kit.meta['package_sha256']:
            raise SetupError('kit.kit_unreadable')
        info = signed_release_info(package, envelope.read_bytes(), kit.public_key, kit.product_id, kit.version)
        return {'version': kit.version, 'package': str(package), 'envelope': str(envelope),
                'migrations': info['migrations'], 'public_key': _b64(kit.public_key)}

    def store_installer(self):
        """Сохранить установщик текущей версии для status/uninstall без исходного комплекта."""
        source = self.kit.root
        target = self.state_dir / 'installer'
        staging = self.state_dir / '.installer.tmp'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o700)
        platform = 'windows-x86_64' if sys.platform == 'win32' else 'linux-x86_64'
        for name in ('app', platform):
            shutil.copytree(source / 'installer' / name, staging / 'installer' / name, symlinks=False)
        for name in ('setup.sh', 'setup.cmd', 'kit.json', 'README.txt'):
            if (source / name).exists():
                shutil.copy2(source / name, staging / name)
        package_dir = staging / 'package'
        package_dir.mkdir()
        shutil.copyfile(source / self.kit.meta['public_key'], package_dir / Path(self.kit.meta['public_key']).name)
        # Ссылки на пакет в сохранённой копии указывают на копию версии в packages/.
        meta = dict(self.kit.meta)
        stored = self.state_dir / 'packages' / self.kit.version
        meta['package'] = os.path.relpath(stored / self.kit.package.name, target)
        meta['envelope'] = os.path.relpath(stored / self.kit.envelope.name, target)
        (staging / 'kit.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                                          encoding='utf-8')
        previous = self.state_dir / '.installer.old'
        shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            os.replace(target, previous)
        os.replace(staging, target)
        shutil.rmtree(previous, ignore_errors=True)

    def publish_installer(self, state):
        """Финализация после сохранённого итогового статуса: отметка уже записана."""
        self.store_installer()
        state.pop('pending_installer', None)
        self.save(state)

    def finish_installer(self):
        """Довести прерванную публикацию сохранённого установщика (A-06).

        Сбой между переносом installer → .installer.old и публикацией новой копии
        восстанавливается из .installer.old; незавершённая публикация версии этого
        комплекта выполняется заново. Возвращает True, если что-то было завершено.
        """
        target, previous = self.state_dir / 'installer', self.state_dir / '.installer.old'
        if not target.exists() and previous.is_dir():
            os.replace(previous, target)
        state = self.state()
        self.finalized = state is not None and state.get('pending_installer') == self.kit.version
        if self.finalized:
            self.publish_installer(state)
        return self.finalized

    def manifest(self, package: str, envelope: str, version: str, key: bytes | None = None) -> dict:
        from shared.package_signing import verify_signed_package
        return verify_signed_package(package, Path(envelope).read_bytes(),
                                     trusted_public_key=key or self.trusted_key(),
                                     expected_product_id=self.kit.product_id, expected_version=version)

    @staticmethod
    def release_key(state, release) -> str:
        """Ключ (base64), которым был проверен сохранённый выпуск."""
        return release.get('public_key') or state['public_key']

    def check_key(self, state, accept_key):
        """Ключ комплекта должен совпадать с закреплённым или быть явно подтверждён."""
        if state is not None and 'public_key' in state and _b64(self.kit.public_key) != state['public_key']:
            if accept_key != self.kit.meta['public_key_sha256']:
                raise SetupError('kit.key_changed', digest=self.kit.meta['public_key_sha256'])

    def trusted_key(self) -> bytes:
        state = self.state()
        if state is not None and 'public_key' in state:
            return base64.b64decode(state['public_key'])
        return self.kit.public_key

    def units(self, manifest: dict, modes) -> list[str]:
        from shared.services import component_order
        return [self.kit.product_id + '-' + name + '.service' for name in component_order(manifest, tuple(modes))]

    def health(self, state, version, *, wait=0.0) -> bool:
        web = state['web']
        deadline = time.monotonic() + wait
        while True:
            try:
                result = self.client.call('check-local-health', {
                    'port': web['port'], 'product_id': self.kit.product_id, 'version': version,
                    'path': web['healthz'], 'timeout_seconds': 3}, min_minor=12, language=self.language)
                if result.get('health_response_matches'):
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def show_kit(self):
        meta = self.kit.meta
        self.out(self.t('kit.summary_product', name=meta['product_name'], product=self.kit.product_id,
                        version=self.kit.version))
        self.out(self.t('kit.summary_key', digest=meta['public_key_sha256']))

    # --- команды -------------------------------------------------------------------
    def install(self, *, root=None, unit_dir=None, modes=None, expect_key=None):
        meta = self.kit.meta
        if expect_key is not None and expect_key != meta['public_key_sha256']:
            raise SetupError('kit.key_mismatch')
        state = self.state()
        if state is not None and state['status'] == 'installed':
            if self.finalized and state['version'] == self.kit.version:
                self.out(self.t('kit.installed', version=self.kit.version))
                return 0
            raise SetupError('kit.already_installed', version=state['version'])
        if state is not None and state['status'] == 'uninstalled' and state.get('data_policy') == 'purge':
            state = None
        self.check_key(state, expect_key)
        home = Path.home()
        pending = state.get('install') if state is not None else None
        if pending is not None and pending['version'] != self.kit.version:
            raise SetupError('kit.pending_other_version', version=pending['version'])
        if pending is None:
            install_root = Path(root).resolve() if root else (
                Path(state['install_root']) if state is not None else home / '.local/share/homensai' / self.kit.product_id)
            units = Path(unit_dir).resolve() if unit_dir else (
                Path(state['unit_dir']) if state is not None else home / '.config/systemd/user')
            selected = list(modes) if modes else (state['modes'] if state is not None else list(meta['modes']))
            if any(mode not in meta['all_modes'] for mode in selected):
                raise SetupError('kit.unknown_mode')
            reuse = state is not None and state['status'] == 'uninstalled'
            if reuse and state.get('version') != self.kit.version:
                raise SetupError('kit.reinstall_version', version=state.get('version'))
            if not reuse and install_root.exists():
                raise SetupError('kit.root_exists', path=install_root)
        else:
            install_root, units, selected = Path(pending['install_root']), Path(pending['unit_dir']), pending['modes']
            reuse = pending['reuse_preserved_data']
        self.show_kit()
        self.out(self.t('kit.summary_root', path=install_root))
        self.out(self.t('kit.summary_units', path=units))
        self.out(self.t('kit.summary_modes', modes=', '.join(selected)))
        if reuse:
            self.out(self.t('kit.reuse_data'))
        if pending is not None:
            self.out(self.t('kit.resume'))
        self.confirm('kit.confirm_install')
        _private_dir(self.state_dir)
        if units.exists():
            if not units.is_dir():
                raise SetupError('kit.unsafe_state')
        else:
            units.mkdir(mode=0o700, parents=True)
        release = self.store_release(self.state_dir, self.kit)
        if pending is None:
            workflow = _key()
            pending = {'version': self.kit.version, 'install_root': str(install_root), 'unit_dir': str(units),
                       'modes': selected, 'workflow_key': workflow, 'keys': {name: _key() for name in _KEY_NAMES},
                       'work_dir': str(_private_dir(self.state_dir / 'work' / ('install-' + workflow))),
                       'reuse_preserved_data': reuse}
            state = {**(state or {}), 'format': STATE_FORMAT, 'product_id': self.kit.product_id,
                     'status': state['status'] if state else 'installing', 'install': pending,
                     'public_key': _b64(self.kit.public_key), 'install_root': str(install_root),
                     'unit_dir': str(units), 'modes': selected, 'web': meta['web'], 'paths': meta['paths']}
            state.setdefault('releases', {})
            self.save(state)
        params = {'package_path': release['package'], 'envelope': _b64(Path(release['envelope']).read_bytes()),
                  'trusted_public_key': _b64(self.kit.public_key), 'expected_product_id': self.kit.product_id,
                  'expected_version': self.kit.version, 'install_root': pending['install_root'],
                  'unit_dir': pending['unit_dir'], 'work_dir': pending['work_dir'], 'modes': pending['modes'],
                  'required_bytes': meta['required_bytes'], 'reserve_bytes': meta['reserve_bytes'],
                  'workflow_key': pending['workflow_key'], 'keys': pending['keys']}
        if pending['reuse_preserved_data']:
            params['reuse_preserved_data'] = True
        self.out(self.t('kit.installing'))
        result = self.call('install-portable-linux', params, min_minor=67)
        if result.get('state') != 'recorded_complete':
            raise SetupError('kit.install_incomplete')
        state['releases'][self.kit.version] = release
        state.update(status='installed', version=self.kit.version, base=self.kit.version, group={
            'load_key': pending['keys']['load'], 'runtime_key': pending['keys']['runtime'],
            'reserve_bytes': params['reserve_bytes']},
            applied_migrations=list(release['migrations']), public_key=_b64(self.kit.public_key),
            pending_installer=self.kit.version, history=state.get('history', []) + [
                {'action': 'install', 'version': self.kit.version, 'time': int(time.time())}])
        state.pop('install', None)
        state.pop('data_policy', None)
        self.save(state)
        self.publish_installer(state)
        self.out(self.t('kit.installed', version=self.kit.version))
        healthy = self.health(state, self.kit.version, wait=30)
        self.out(self.t('kit.health_ok') if healthy else self.t('kit.health_unknown'))
        self.out(self.t('kit.manage_hint', path=self.state_dir / 'installer' / 'setup.sh'))
        return 0

    def _password(self):
        value = os.environ.get(PASSWORD_ENV)
        if not value:
            if self.yes:
                raise SetupError('kit.password_required')
            first = self.ask_secret(self.t('kit.password_prompt') + ' ')
            second = self.ask_secret(self.t('kit.password_repeat') + ' ')
            if not first or first != second:
                raise SetupError('kit.password_mismatch')
            value = first
        if len(value) < 8:
            raise SetupError('kit.password_short')
        os.environ[_SECRET_ENV] = value

    def update(self, *, accept_key=None, reconcile_stop=False):
        state = self.state()
        if state is not None and state['status'] == 'consolidating':
            return self._consolidate(state)
        if state is None or state['status'] not in ('installed', 'updating'):
            raise SetupError('kit.not_installed')
        self.check_key(state, accept_key)
        pending = state.get('update')
        if pending is not None and pending['new_version'] != self.kit.version:
            raise SetupError('kit.pending_other_version', version=pending['new_version'])
        if pending is None and self.kit.version == state['version']:
            if self.finalized:
                self.out(self.t('kit.updated', version=self.kit.version))
                return 0
            raise SetupError('kit.same_version', version=state['version'])
        old = state['releases'][state['version']]
        old_key = self.release_key(state, old)
        applied = state.get('applied_migrations', [])
        info = signed_release_info(self.kit.package, self.kit.envelope.read_bytes(), self.kit.public_key,
                                   self.kit.product_id, self.kit.version)
        kit_migrations = info['migrations']
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
        new_meta = self.manifest(release['package'], release['envelope'], self.kit.version, self.kit.public_key)
        old_meta = self.manifest(old['package'], old['envelope'], state['version'], base64.b64decode(old_key))
        runtime_old = {item['path']: item['sha256'] for item in old_meta['files'] if item['path'].startswith('runtime-linux/')}
        runtime_new = {item['path']: item['sha256'] for item in new_meta['files'] if item['path'].startswith('runtime-linux/')}
        if runtime_old != runtime_new:
            raise SetupError('kit.runtime_changed')
        if self.units(old_meta['manifest'], state['modes']) != self.units(new_meta['manifest'], state['modes']):
            raise SetupError('kit.components_changed')
        self._password()
        if pending is None:
            key = _key()
            pending = {'new_version': self.kit.version, 'idempotency_key': key, 'migration_ids': new_migrations,
                       'work_dir': str(_private_dir(self.state_dir / 'work' / ('update-' + key))), 'started': False}
            state.update(status='updating', update=pending)
            self.save(state)
        interpreter = Path(state['install_root']) / state['paths']['runtime'] / 'bin/python'
        python_sha = {item['path']: item['sha256'] for item in old_meta['files']}['runtime-linux/bin/python']
        base = {'new_package': release['package'], 'new_envelope': _b64(Path(release['envelope']).read_bytes()),
                'new_key': _b64(self.kit.public_key), 'old_package': old['package'],
                'old_envelope': _b64(Path(old['envelope']).read_bytes()), 'old_key': old_key,
                'product_id': self.kit.product_id, 'old_version': state['version'], 'new_version': self.kit.version,
                'install_root': state['install_root'], 'work_dir': pending['work_dir'], 'modes': state['modes'],
                'migration_ids': pending['migration_ids'], 'idempotency_key': pending['idempotency_key'],
                'unit_dir': state['unit_dir'], 'load_key': state['group']['load_key'],
                'runtime_key': state['group']['runtime_key'],
                'runtime_reserve_bytes': state['group'].get('reserve_bytes', 0)}
        observed = None
        if pending['started']:
            # Прежний вызов мог не записать транзакцию или потерять ответ: сначала узнать состояние.
            try:
                observed = self.call('inspect-started-pointer-update', base, min_minor=59)['state']
            except SetupError:
                observed = None
        if observed is not None and observed.get('outcome') in ('updated', 'rolled_back'):
            result = {'state': observed}
        else:
            reconcile_key = None
            if observed is None:
                action = 'start'
            elif reconcile_stop:
                phases = 'stop' if observed.get('direction') == 'forward' else 'rollback_stop'
                action, reconcile_key = 'reconcile-stop', observed['transaction'] + '/' + phases
            else:
                action = 'reopen'
            if not pending['started']:
                pending['started'] = True
                state['update'] = pending
                self.save(state)
            params = {**base, 'password_env': _SECRET_ENV, 'action': action, 'reconcile_stop_key': reconcile_key,
                      'interpreter': str(interpreter), 'interpreter_sha256': python_sha, 'timeout_seconds': 600}
            self.out(self.t('kit.updating'))
            try:
                result = self.call('run-pointer-linux-update', params, min_minor=62, secret_env_name=_SECRET_ENV)
            finally:
                os.environ.pop(_SECRET_ENV, None)
        outcome = result['state'].get('outcome')
        history = state.get('history', [])
        if outcome == 'updated':
            state['releases'][self.kit.version] = release
            state.update(status='installed', version=self.kit.version, public_key=_b64(self.kit.public_key),
                         applied_migrations=applied + list(pending['migration_ids']),
                         pending_installer=self.kit.version)
            history.append({'action': 'update', 'version': self.kit.version, 'time': int(time.time())})
            code = 0
        elif outcome == 'rolled_back':
            state['status'] = 'installed'
            history.append({'action': 'rollback', 'version': state['version'], 'attempted': self.kit.version,
                            'time': int(time.time())})
            code = 4
        else:
            raise SetupError('kit.update_indeterminate')
        state['history'] = history
        state['last_update'] = {'transaction': result['state'].get('transaction'), 'outcome': outcome,
                                'work_dir': pending['work_dir'], 'code': code}
        state.pop('update', None)
        if outcome == 'updated':
            self.out(self.t('kit.updated', version=self.kit.version))
        else:
            self.out(self.t('kit.rolled_back', version=state['version']))
        self.out(self.t('kit.archive_hint', path=pending['work_dir']))
        # Завершённая транзакция остаётся в services/; следующая требует заново
        # привязанной установки активной версии (см. _consolidate).
        workflow = _key()
        state.update(status='consolidating', consolidate={
            'target': state['version'], 'uninstall_id': _key(), 'uninstalled': False, 'workflow_key': workflow,
            'keys': {name: _key() for name in _KEY_NAMES},
            'work_dir': str(_private_dir(self.state_dir / 'work' / ('install-' + workflow)))})
        self.save(state)
        return self._consolidate(state)

    def _consolidate(self, state):
        """Удалить (keep) пакетом базовой версии и переустановить активную версию на тех же данных.

        Квитанции установки (runtime, службы, журнал первой установки) привязаны к
        версии, установленной последней; после транзакции обновления они заново
        создаются штатной переустановкой. Данные, копии и настройки сохраняются.
        """
        job = state['consolidate']
        self.out(self.t('kit.consolidating', version=job['target']))
        if not job['uninstalled']:
            base = state['releases'][state['base']]
            self.call('uninstall-portable-linux', {
                'package_path': base['package'], 'envelope': _b64(Path(base['envelope']).read_bytes()),
                'trusted_public_key': self.release_key(state, base), 'expected_product_id': self.kit.product_id,
                'expected_version': state['base'], 'install_root': state['install_root'],
                'unit_dir': state['unit_dir'], 'modes': state['modes'], 'operation_id': job['uninstall_id'],
                'data_policy': 'keep'}, min_minor=67)
            job['uninstalled'] = True
            self.save(state)
        target = state['releases'][job['target']]
        result = self.call('install-portable-linux', {
            'package_path': target['package'], 'envelope': _b64(Path(target['envelope']).read_bytes()),
            'trusted_public_key': self.release_key(state, target), 'expected_product_id': self.kit.product_id,
            'expected_version': job['target'], 'install_root': state['install_root'],
            'unit_dir': state['unit_dir'], 'work_dir': job['work_dir'], 'modes': state['modes'],
            'required_bytes': self.kit.meta['required_bytes'], 'reserve_bytes': self.kit.meta['reserve_bytes'],
            'workflow_key': job['workflow_key'], 'keys': job['keys'], 'reuse_preserved_data': True}, min_minor=67)
        if result.get('state') != 'recorded_complete':
            raise SetupError('kit.install_incomplete')
        state.update(status='installed', base=job['target'], group={
            'load_key': job['keys']['load'], 'runtime_key': job['keys']['runtime'],
            'reserve_bytes': self.kit.meta['reserve_bytes']})
        state.pop('consolidate', None)
        self.save(state)
        if state.get('pending_installer') == self.kit.version:
            self.publish_installer(state)
        healthy = self.health(state, state['version'], wait=30)
        self.out(self.t('kit.health_ok') if healthy else self.t('kit.health_unknown'))
        return state.get('last_update', {}).get('code', 0)

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
        release = state['releases'][state['version']]
        manifest = self.manifest(release['package'], release['envelope'], state['version'],
                                 base64.b64decode(self.release_key(state, release)))['manifest']
        for unit in self.units(manifest, state['modes']):
            self.out('  ' + unit + ': ' + _unit_state(unit))
        healthy = self.health(state, state['version'])
        self.out(self.t('kit.health_ok') if healthy else self.t('kit.health_failed'))
        return 0 if healthy else 1

    def uninstall(self, *, purge=False):
        state = self.state()
        if state is not None and state['status'] == 'consolidating':
            raise SetupError('kit.consolidation_pending')
        if state is None or state['status'] not in ('installed', 'uninstalling'):
            raise SetupError('kit.not_installed')
        pending = state.get('uninstall')
        if pending is None:
            pending = {'operation_id': _key(), 'data_policy': 'purge' if purge else 'keep'}
        elif purge != (pending['data_policy'] == 'purge'):
            raise SetupError('kit.pending_uninstall')
        base = state.get('base', state['version'])
        release = state['releases'][base]
        self.out(self.t('kit.status_line', product=self.kit.product_id, status=state['status'],
                        version=state['version']))
        self.out(self.t('kit.summary_root', path=state['install_root']))
        if pending['data_policy'] == 'purge':
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
        params = {'package_path': release['package'], 'envelope': _b64(Path(release['envelope']).read_bytes()),
                  'trusted_public_key': self.release_key(state, release), 'expected_product_id': self.kit.product_id,
                  'expected_version': base, 'install_root': state['install_root'],
                  'unit_dir': state['unit_dir'], 'modes': state['modes'], 'operation_id': pending['operation_id'],
                  'data_policy': pending['data_policy']}
        if pending['data_policy'] == 'purge':
            params['confirm_product_id'] = self.kit.product_id
        result = self.call('uninstall-portable-linux', params, min_minor=67)
        if result.get('state') != 'uninstalled':
            raise SetupError('kit.uninstall_incomplete')
        state.update(status='uninstalled', data_policy=pending['data_policy'])
        state.pop('uninstall', None)
        state['history'] = state.get('history', []) + [
            {'action': 'uninstall', 'version': state['version'], 'data_policy': pending['data_policy'],
             'time': int(time.time())}]
        self.save(state)
        self.out(self.t('kit.uninstalled_purge') if pending['data_policy'] == 'purge'
                 else self.t('kit.uninstalled_keep', path=state['install_root']))
        return 0


def _unit_state(unit: str) -> str:
    environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                   'XDG_RUNTIME_DIR': os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))}
    if 'DBUS_SESSION_BUS_ADDRESS' in os.environ:
        environment['DBUS_SESSION_BUS_ADDRESS'] = os.environ['DBUS_SESSION_BUS_ADDRESS']
    try:
        shown = subprocess.run(['systemctl', '--user', 'is-active', '--', unit], env=environment,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return shown.stdout.strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def _console():
    # -I игнорирует PYTHONIOENCODING: вывод в канал на Windows иначе был бы в кодировке ANSI.
    for stream in (sys.stdout, sys.stderr):
        try:
            if sys.platform == 'win32' and not stream.isatty():
                stream.reconfigure(encoding='utf-8')
            else:
                stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None) -> int:
    _console()
    parser = _Parser(prog='setup.sh', add_help=True)
    parser.add_argument('--kit', required=True)
    parser.add_argument('--language', choices=available_languages())
    parser.add_argument('--state-dir')
    parser.add_argument('--yes', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)
    install = sub.add_parser('install')
    install.add_argument('--root')
    install.add_argument('--unit-dir')
    install.add_argument('--modes')
    install.add_argument('--expect-key-sha256')
    update = sub.add_parser('update')
    update.add_argument('--accept-key-sha256')
    update.add_argument('--reconcile-stop', action='store_true')
    sub.add_parser('status')
    uninstall = sub.add_parser('uninstall')
    uninstall.add_argument('--purge', action='store_true')
    language = 'en'
    try:
        args = parser.parse_args(argv)
        language = _language(args.language)
        if sys.platform == 'win32':
            from shared.kit_windows import WindowsSetup, is_admin
            if not is_admin():
                raise SetupError('kit.windows_admin_required', 2)
            backend = WindowsSetup
        elif sys.platform.startswith('linux') and hasattr(os, 'getuid') and os.getuid() != 0:
            backend = Setup
        else:
            raise SetupError('kit.unsupported_host', 2)
        setup = backend(Kit(Path(args.kit)), language=language,
                        state_dir=Path(args.state_dir) if args.state_dir else None, yes=args.yes)
        with setup.locked() if args.command != 'status' else contextlib.nullcontext():
            return _dispatch(setup, args)
    except SetupError as error:
        try:
            print(translate(language, error.key, **{k: str(v) for k, v in error.variables.items()}), file=sys.stderr)
        except LanguageError:
            print(error.key, file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print(translate(language, 'kit.interrupted'), file=sys.stderr)
        return 130
    except Exception as error:  # Понятное сообщение вместо трассировки.
        if os.environ.get('HOMENSAI_SETUP_DEBUG') == '1':
            import traceback
            traceback.print_exc()
        print(translate(language, 'kit.unexpected', error=type(error).__name__), file=sys.stderr)
        return 1


def _dispatch(setup, args) -> int:
    if args.command == 'install':
        return setup.install(root=args.root, unit_dir=args.unit_dir,
                             modes=args.modes.split(',') if args.modes else None,
                             expect_key=args.expect_key_sha256)
    if args.command == 'update':
        return setup.update(accept_key=args.accept_key_sha256, reconcile_stop=args.reconcile_stop)
    if args.command == 'status':
        return setup.status()
    return setup.uninstall(purge=args.purge)


if __name__ == '__main__':
    raise SystemExit(main())
