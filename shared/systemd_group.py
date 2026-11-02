# HomenS.AI · © 2026 Serhii Khomenko
"""Порядок пользовательских systemd units для одного выбранного режима."""
from __future__ import annotations

from shared.manifest import ManifestError, validate_manifest
from shared.services import component_order
from shared.systemd_user import SystemdUserAdapter


class SystemdGroupError(RuntimeError):
    """Не все компоненты подтвердили требуемое состояние."""


class SystemdUserGroup:
    """Группа заранее подтверждаемых units; не является портом миграции/архива.

    После ошибки часть служб могла изменить состояние. Автоматический откат и
    повтор действий без повторной проверки этим классом не гарантируются.
    """
    def __init__(self, manifest, *, modes, adapters):
        try:
            manifest = validate_manifest(manifest)
            order = component_order(manifest, modes)
            if type(adapters) is not dict or set(adapters) != set(order):
                raise SystemdGroupError('Требуются адаптеры всех выбранных компонентов')
            if any(not isinstance(adapters[name], SystemdUserAdapter)
                   or adapters[name].unit != manifest['product_id'] + '-' + name + '.service'
                   for name in order):
                raise SystemdGroupError('Адаптер не соответствует компоненту')
            self.order = order
            self.adapters = dict(adapters)
        except (ManifestError, TypeError, ValueError):
            raise SystemdGroupError('Недопустимый состав служб') from None

    def command(self, action, *, execute=False):
        if type(action) is not str or action not in ('start', 'stop') or type(execute) is not bool:
            raise SystemdGroupError('Недопустимая операция')
        order = self.order if action == 'start' else tuple(reversed(self.order))
        if not execute:
            return [self.adapters[name].command(action) for name in order]
        try:
            for name in order:
                self.adapters[name].command(action, execute=True)
            results = []
            for name in self.order:
                adapter = self.adapters[name]
                status = adapter.inspect() if action == 'start' else adapter.confirm_stopped()
                if status['active_state'] != ('active' if action == 'start' else 'inactive'):
                    raise SystemdGroupError('Не все компоненты подтвердили состояние')
                results.append(status)
            return results
        except Exception:
            raise SystemdGroupError('Не удалось подтвердить состояние всех компонентов') from None
