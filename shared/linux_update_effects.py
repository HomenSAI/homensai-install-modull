# HomenS.AI · © 2026 Serhii Khomenko
"""Связка ограниченных портов обновления для Linux user-systemd."""
from __future__ import annotations

import re

from shared.datahome import checked_path
from shared.effect_receipt import read_effect_receipt
from shared.signed_migration_port import SignedMigrationPort
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import EffectIndeterminate, UpdateError, _ACTIVE_EFFECT, _atomic_write

_BACKUP_KEY = re.compile(r'([a-f0-9]{32})/(archive|backup:(?:0|[1-9][0-9]{0,5})|before_rollback)\Z')


class LinuxSignedUpdateEffects:
    """Связать уже настроенные порты одной транзакции, не создавая доверие к ним.

    Вызывающий код отвечает за доверенный выпуск, группу, runtime и остановку
    всех остальных писателей. Произвольные hooks не поддерживаются.
    """
    def __init__(self, lifecycle, migration, health):
        if (not isinstance(lifecycle, SystemdLifecyclePort)
                or not isinstance(migration, SignedMigrationPort)
                or not isinstance(health, LocalUpdateHealthPort)
                or lifecycle.plan is not migration.plan or lifecycle.plan is not health.plan
                or lifecycle.ports is not migration.ports or lifecycle.ports is not health.ports
                or lifecycle.plan.manifest['hooks']['post_update']):
            raise UpdateError('Несовместимые порты обновления')
        self.plan = lifecycle.plan
        self.lifecycle, self.migration, self.health_port = lifecycle, migration, health

    def stop(self, key, components):
        return self.lifecycle.stop(key, components)

    def start(self, key, components, pre_start):
        return self.lifecycle.start(key, components, pre_start)

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
            record = read_effect_receipt(marker)
            done = {**binding, 'status': 'done'}
            if record is not None and record != done:
                raise EffectIndeterminate('Предыдущая копия требует проверки')
            if (record is None and ports.archive_path(key).exists()) or (record == done and not ports.archive_path(key).exists()):
                raise EffectIndeterminate('Копия не соответствует отметке')
            confirm_stopped()
            if record is None:
                _atomic_write(marker, {**binding, 'status': 'pending'})
            backup(key, purpose, command)
            confirm_stopped()
            if record is None:
                if read_effect_receipt(marker) != {**binding, 'status': 'pending'}:
                    raise EffectIndeterminate('Отметка копии изменилась')
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
