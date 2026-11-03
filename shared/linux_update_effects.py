# HomenS.AI · © 2026 Serhii Khomenko
"""Связка ограниченных портов обновления для Linux user-systemd."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import re

from shared.datahome import DataHomeError, checked_path, contains
from shared.adapters_fs import _tree
from shared.effect_receipt import read_effect_receipt
from shared.host_target import native_target
from shared.manifest import check_target
from shared.package_signing import verify_signed_package
from shared.signed_migration_port import SignedMigrationPort
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import EffectIndeterminate, LockBusy, UpdateError, _ACTIVE_EFFECT, _atomic_write
from shared.writer_guard import CooperativeWriterLock

_BACKUP_KEY = re.compile(r'([a-f0-9]{32})/(archive|backup:(?:0|[1-9][0-9]{0,5})|before_rollback)\Z')


def _previous_manifest_compatible(previous, current):
    """Разрешить различия только в выпускных полях, сохраняя контракт служб и данных."""
    variable = {'product_name', 'migrations', 'compatibility', 'runtime'}
    if ({key: value for key, value in previous.items() if key not in variable}
            != {key: value for key, value in current.items() if key not in variable}):
        return False
    excluded = {'lock_files', 'vendor_wheels'}
    return ({key: value for key, value in previous['runtime'].items() if key not in excluded}
            == {key: value for key, value in current['runtime'].items() if key not in excluded})


@dataclass(frozen=True)
class PreviousSignedRelease:
    """Независимо доверенные пакет, подпись и ключ предыдущего выпуска."""
    package_path: str
    envelope: bytes
    public_key: bytes

    def __post_init__(self):
        if (type(self.package_path) is not str or not self.package_path
                or type(self.envelope) is not bytes or not self.envelope
                or type(self.public_key) is not bytes or len(self.public_key) != 32):
            raise UpdateError('Недопустимые сведения о прежнем выпуске')


class LinuxSignedUpdateEffects:
    """Связать уже настроенные порты одной транзакции, не создавая доверие к ним.

    Вызывающий код отвечает за доверенный выпуск, группу, runtime и остановку
    всех остальных писателей. Произвольные hooks не поддерживаются.
    """
    def __init__(self, lifecycle, migration, health, *, previous_release=None, writer_lock=None):
        if (not isinstance(lifecycle, SystemdLifecyclePort)
                or not isinstance(migration, SignedMigrationPort)
                or not isinstance(health, LocalUpdateHealthPort)
                or lifecycle.plan is not migration.plan or lifecycle.plan is not health.plan
                or lifecycle.ports is not migration.ports or lifecycle.ports is not health.ports
                or lifecycle.plan.manifest['hooks']['post_update']
                or (previous_release is not None and type(previous_release) is not PreviousSignedRelease)
                or (writer_lock is not None and type(writer_lock) is not CooperativeWriterLock)):
            raise UpdateError('Несовместимые порты обновления')
        if writer_lock is not None:
            home = lifecycle.ports.home
            protected = [home.app, home.releases, *home.roots.values(), lifecycle.ports.work]
            if any(contains(checked_path(path), writer_lock.path) for path in protected):
                raise UpdateError('Блокировка пересекается с выпуском, данными или архивом')
        self.plan = lifecycle.plan
        self.lifecycle, self.migration, self.health_port = lifecycle, migration, health
        self.previous_release = previous_release
        self.writer_lock = writer_lock

    @contextmanager
    def operation_guard(self):
        """Блокировать fresh start/stop на всё время транзакции обновления."""
        from shared.systemd_fresh_start import FreshStartError, _lifecycle_lock
        try:
            with _lifecycle_lock(self.migration.install_root):
                yield
        except (FreshStartError, DataHomeError, OSError):
            raise LockBusy('Управление службами занято или не подтверждено') from None

    def stop(self, key, components):
        if type(key) is str and key.endswith('/stop'):
            self._active_phase(key, 'stop', 'forward')
            try:
                self._verify_previous()
                if self.lifecycle.ports.password_provider is not None:
                    from shared import archive_crypto
                    # Probe the exact archive filesystem before stopping services.
                    # This is not a reservation: later failures remain possible.
                    work = checked_path(self.lifecycle.ports.work)
                    work.mkdir(parents=True, exist_ok=True)
                    archive_crypto._aesgcm()
                    with archive_crypto._anonymous_temporary(work):
                        pass
                self.lifecycle.stop(key, components)
                self._verify_previous()
            except Exception:
                raise EffectIndeterminate('Прежний выпуск или остановка не подтверждены') from None
            return
        return self.lifecycle.stop(key, components)

    def start(self, key, components, pre_start):
        if type(key) is str and key.endswith('/start'):
            self._active_phase(key, 'start', 'forward')
            try:
                self._verify_release(self.lifecycle.ports.release_path(self.plan.release))
                self.lifecycle.start(key, components, pre_start)
                self._verify_release(self.lifecycle.ports.release_path(self.plan.release))
            except Exception:
                raise EffectIndeterminate('Подписанный выпуск или запуск не подтверждён') from None
            return
        self._active_phase(key, 'rollback_start', 'rollback')
        try:
            self._verify_previous()
            self.lifecycle.start(key, components, pre_start)
            self._verify_previous()
        except Exception:
            raise EffectIndeterminate('Прежний выпуск или запуск не подтверждены') from None

    def _active_phase(self, key, phase, direction):
        active = _ACTIVE_EFFECT.get()
        state = self.plan.status()
        phases = self.plan.forward if direction == 'forward' else self.plan.rollback
        if (type(key) is not str or active is None or active[0] is not self.plan
                or active[2] != phase or key != active[1] + '/' + phase or state is None
                or state['transaction'] != active[1] or state['direction'] != direction
                or state['outcome'] != 'running' or state.get('indeterminate') is not True
                or state['index'] >= len(phases) or phases[state['index']] != phase):
            raise UpdateError('Действие не соответствует активному плану')

    def _verify_release(self, directory, *, previous=False):
        evidence = self.previous_release if previous else self.migration
        state = self.plan.status() if previous else None
        if evidence is None or (previous and state is None):
            raise UpdateError('Нет доверенных сведений о прежнем выпуске')
        version = state['old_release'] if previous else self.plan.release
        metadata = verify_signed_package(evidence.package_path, evidence.envelope,
            trusted_public_key=evidence.public_key, expected_product_id=self.plan.manifest['product_id'],
            expected_version=version)
        compatible = (_previous_manifest_compatible(metadata['manifest'], self.plan.manifest) if previous
                      else metadata['manifest'] == self.plan.manifest)
        if (not compatible
                or check_target(metadata['manifest'], native_target())['status'] != 'supported'):
            raise UpdateError('Подписанный выпуск не соответствует плану или среде')
        files = {item['path']: item['sha256'] for item in metadata['files']}
        directories = {'.'}
        for name in files:
            parts = name.split('/')
            directories.update('/'.join(parts[:index]) for index in range(1, len(parts)))
        if _tree(directory) != (directories, files):
            raise UpdateError('Состав выпуска не соответствует подписанному пакету')

    def _verify_previous(self):
        state = self.plan.status()
        if state is None:
            raise UpdateError('Нет состояния предыдущего выпуска')
        self._verify_release(self.lifecycle.ports.release_path(state['old_release']), previous=True)

    def guarded_stage_release(self, key, release, stage):
        self._active_phase(key, 'stage', 'forward')
        if release != self.plan.release or not callable(stage):
            raise UpdateError('Выпуск не соответствует плану')
        try:
            source = self.lifecycle.ports.payloads[release]
            self._verify_release(source.directory)
            stage(key, release)
            self._verify_release(self.lifecycle.ports.release_path(release))
        except Exception:
            raise EffectIndeterminate('Подготовленный выпуск не подтверждён') from None

    def guarded_switch_pointer(self, key, release, switch):
        if type(key) is str and key.endswith('/pointer_back'):
            self._active_phase(key, 'pointer_back', 'rollback')
            if not callable(switch) or release != self.plan.status()['old_release']:
                raise UpdateError('Откат не соответствует плану')
            try:
                self._verify_previous()
                switch(key, release)
                self._verify_previous()
            except Exception:
                raise EffectIndeterminate('Прежний выпуск или переключение не подтверждены') from None
            return
        self._active_phase(key, 'switch', 'forward')
        if release != self.plan.release or not callable(switch):
            raise UpdateError('Выпуск не соответствует плану')
        try:
            self._verify_release(self.lifecycle.ports.release_path(release))
            switch(key, release)
            self._verify_release(self.lifecycle.ports.release_path(release))
        except Exception:
            raise EffectIndeterminate('Переключённый выпуск не подтверждён') from None

    def guarded_restore(self, key, archive_key, command, restore):
        self._active_phase(key, 'restore', 'rollback')
        if not callable(restore) or archive_key != key.split('/', 1)[0] + '/archive':
            raise UpdateError('Восстановление не соответствует плану')
        try:
            self._verify_previous()
            restore(key, archive_key, command)
            self._verify_previous()
        except Exception:
            raise EffectIndeterminate('Прежний выпуск или восстановление не подтверждены') from None

    def migrate(self, key, migration, command):
        return self.migration.migrate(key, migration, command)

    def health(self, key, url, timeout, checks):
        return self.health_port.health(key, url, timeout, checks)

    def guarded_backup(self, key, purpose, command, backup):
        """Проверить остановку группы вокруг снимка; pending не повторяется."""
        match = _BACKUP_KEY.fullmatch(key) if type(key) is str else None
        if (match is None or type(command) is not tuple or not callable(backup)
                or purpose != match.group(2).replace('before_rollback', 'before-rollback')):
            raise UpdateError('Недопустимый запрос копии')
        transaction, phase = match.groups()
        active = _ACTIVE_EFFECT.get()
        if active != (self.plan, transaction, phase):
            raise UpdateError('Копия не соответствует активному плану')
        direction = 'rollback' if phase == 'before_rollback' else 'forward'
        stop_phase = 'rollback_stop' if direction == 'rollback' else 'stop'
        ports = self.lifecycle.ports
        binding = {'version': 1, 'plan': self.plan.digest, 'key': key,
                   'stop_key': transaction + '/' + stop_phase,
                   'installation_sha256': self.lifecycle.install_hash,
                   'units_sha256': self.lifecycle.units_hash}
        writer_lock = self.writer_lock
        if writer_lock is not None:
            binding['writer_lock_sha256'] = hashlib.sha256(str(writer_lock.path).encode('utf-8')).hexdigest()
        stop_binding = {'version': 1, 'plan': self.plan.digest, 'key': binding['stop_key'],
                        'action': 'stop', 'installation_sha256': self.lifecycle.install_hash,
                        'units_sha256': self.lifecycle.units_hash, 'status': 'done'}
        def confirm_stopped():
            state = self.plan.status()
            phases = self.plan.rollback if direction == 'rollback' else self.plan.forward
            if (state is None or state['transaction'] != transaction or state['direction'] != direction
                    or state['outcome'] != 'running' or state.get('indeterminate') is not True
                    or phases[state['index']] != phase or self.lifecycle._units_hash() != self.lifecycle.units_hash
                    or read_effect_receipt(ports._path(binding['stop_key'], '.service.json')) != stop_binding):
                raise EffectIndeterminate('Остановка перед копией не подтверждена')
            self.lifecycle._check_state('stop')
        try:
            checked_path(ports.work).mkdir(parents=True, exist_ok=True)
            marker = ports._path(key, '.quiescence.json')
            with writer_lock.hold(exclusive=True) if writer_lock is not None else nullcontext() as identity:
                if writer_lock is not None:
                    binding['writer_lock_file_sha256'] = hashlib.sha256(
                        f'{identity.device}:{identity.inode}'.encode('ascii')).hexdigest()
                record = read_effect_receipt(marker)
                done = {**binding, 'status': 'done'}
                if record is not None and record != done:
                    raise EffectIndeterminate('Предыдущая копия требует проверки')
                if ((record is None and ports.archive_path(key).exists())
                        or (record == done and not ports.archive_path(key).exists())):
                    raise EffectIndeterminate('Копия не соответствует отметке')
                confirm_stopped()
                if record is None:
                    _atomic_write(marker, {**binding, 'status': 'pending'})
                backup(key, purpose, command)
                confirm_stopped()
                if record is None:
                    if read_effect_receipt(marker) != {**binding, 'status': 'pending'}:
                        raise EffectIndeterminate('Отметка копии изменилась')
            if record is None:
                _atomic_write(marker, done)
            if read_effect_receipt(marker) != done:
                raise EffectIndeterminate('Копия не подтверждена')
        except EffectIndeterminate:
            raise
        except Exception:
            raise EffectIndeterminate('Согласованность копии не подтверждена') from None

    def post_update(self, key, hooks):
        active = _ACTIVE_EFFECT.get()
        if (type(key) is not str or type(hooks) is not tuple or hooks
                or active is None or active[0] is not self.plan
                or active[2] != 'post_update' or key != active[1] + '/post_update'):
            raise UpdateError('Недопустимый вызов post_update')
        state = self.plan.status()
        if (state is None or state['transaction'] != active[1] or state['direction'] != 'forward'
                or state['outcome'] != 'running' or state.get('indeterminate') is not True
                or self.plan.forward[state['index']] != 'post_update'):
            raise UpdateError('Не подтверждён checkpoint post_update')
