# HomenS.AI · © 2026 Serhii Khomenko
"""Связка ограниченных портов обновления для Linux user-systemd."""
from __future__ import annotations

from shared.signed_migration_port import SignedMigrationPort
from shared.systemd_lifecycle_port import SystemdLifecyclePort
from shared.update_health_port import LocalUpdateHealthPort
from shared.update_plan import UpdateError, _ACTIVE_EFFECT


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
