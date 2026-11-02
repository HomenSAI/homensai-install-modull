# HomenS.AI · © 2026 Serhii Khomenko
"""Локальная проверка состояния для активной транзакции обновления."""
from __future__ import annotations

import re
import time

from shared.adapters_fs import FilesystemPorts
from shared.health import HealthError, check_local_health
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _ACTIVE_EFFECT

_KEY = re.compile(r'([a-f0-9]{32})/(health|rollback_health)\Z')


class LocalUpdateHealthPort:
    """Проверка loopback JSON без hooks; процесс за сокетом не аутентифицируется."""

    def __init__(self, plan: UpdatePlan, ports: FilesystemPorts):
        if (not isinstance(plan, UpdatePlan) or not isinstance(ports, FilesystemPorts)
                or plan.ports is not ports or not plan.guard_effects
                or plan.manifest != ports.manifest or plan.state_path != ports.state
                or plan.manifest['hooks']['health_checks']):
            raise UpdateError('Недопустимая конфигурация проверки состояния')
        self.plan, self.ports = plan, ports

    def health(self, key, url, timeout, checks):
        match = _KEY.fullmatch(key) if type(key) is str else None
        if match is None or type(checks) is not tuple or checks:
            raise UpdateError('Недопустимый запрос проверки состояния')
        transaction, phase = match.groups()
        active = _ACTIVE_EFFECT.get()
        web = self.plan.manifest['web']
        if (active is None or active != (self.plan, transaction, phase)
                or url != f"http://127.0.0.1:{web['port']}{web['healthz']}"
                or type(timeout) is not int or timeout != self.plan.manifest['update']['health_wait']):
            raise UpdateError('Запрос не соответствует активному плану')
        try:
            state = self.plan.status()
            direction = 'forward' if phase == 'health' else 'rollback'
            phases = self.plan.forward if direction == 'forward' else self.plan.rollback
            if (state is None or state['transaction'] != transaction or state['direction'] != direction
                    or state['outcome'] != 'running' or state.get('indeterminate') is not True
                    or phases[state['index']] != phase):
                raise ValueError('checkpoint')
            version = self.plan.release if phase == 'health' else state['old_release']
            if self.ports.current_release() != version:
                raise ValueError('release')
        except Exception:
            raise EffectIndeterminate('Состояние обновления не подтверждено') from None

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HealthError('Продукт не подтвердил готовность')
            try:
                check_local_health(web['port'], product_id=self.plan.manifest['product_id'],
                                   version=version, path=web['healthz'], timeout=min(2.0, remaining))
            except HealthError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HealthError('Продукт не подтвердил готовность') from None
                time.sleep(min(0.2, remaining))
                continue
            try:
                if self.plan.status() != state or self.ports.current_release() != version:
                    raise ValueError('state changed')
            except Exception:
                raise EffectIndeterminate('Состояние изменилось при проверке') from None
            return
