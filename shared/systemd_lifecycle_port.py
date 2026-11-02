# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальный журнал управления заранее доверенной группой systemd."""
from __future__ import annotations

import hashlib
import json
import re

from shared.adapters_fs import FilesystemPorts
from shared.datahome import checked_path, resolve_datahome
from shared.effect_receipt import read_effect_receipt
from shared.systemd_group import SystemdUserGroup
from shared.update_plan import EffectIndeterminate, UpdateError, UpdatePlan, _ACTIVE_EFFECT, _atomic_write

_KEY = re.compile(r'([a-f0-9]{32})/(stop|start|rollback_stop|rollback_start)\Z')


class SystemdLifecyclePort:
    """Управляет только выбранными user-units, не всеми писателями продукта.

    После неизвестного результата не повторяет команду. Нет регистрации units,
    pre-start хуков и автоматического разрешения pending; источники доверенных
    путей/хэшей и все процессы вне группы контролирует вызывающая сторона.
    """
    def __init__(self, plan: UpdatePlan, ports: FilesystemPorts, *, install_root: str,
                 group: SystemdUserGroup, reconcile_key: str | None = None):
        if (not isinstance(plan, UpdatePlan) or not isinstance(ports, FilesystemPorts)
                or not isinstance(group, SystemdUserGroup) or plan.ports is not ports or not plan.guard_effects
                or ports.manifest != plan.manifest or ports.state != plan.state_path
                or type(install_root) is not str or plan.manifest['hooks']['pre_start']
                or (reconcile_key is not None and (type(reconcile_key) is not str or _KEY.fullmatch(reconcile_key) is None))
                or tuple(group.order) != plan.components):
            raise UpdateError('Недопустимая конфигурация группы служб')
        if reconcile_key is not None and _KEY.fullmatch(reconcile_key).group(2) not in ('stop', 'rollback_stop'):
            raise UpdateError('Подтверждение запуска пока не поддерживается')
        if resolve_datahome(plan.manifest, install_root) != ports.home:
            raise UpdateError('Установка не соответствует портам')
        self.plan, self.ports, self.group = plan, ports, group
        self.reconcile_key = reconcile_key
        self.install_hash = hashlib.sha256(install_root.encode('utf-8')).hexdigest()
        self.units_hash = self._units_hash()

    def _units_hash(self):
        if tuple(self.group.order) != self.plan.components or set(self.group.adapters) != set(self.plan.components):
            raise UpdateError('Состав служб изменился')
        units = []
        for name in self.group.order:
            adapter = self.group.adapters[name]
            if adapter.unit != self.plan.manifest['product_id'] + '-' + name + '.service':
                raise UpdateError('Служба не соответствует плану')
            units.append([adapter.unit, str(adapter.path), adapter.digest])
        return hashlib.sha256(json.dumps(units, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()

    def _check_state(self, action):
        for name in self.group.order:
            adapter = self.group.adapters[name]
            status = adapter.inspect() if action == 'start' else adapter.confirm_stopped()
            if status['active_state'] != ('active' if action == 'start' else 'inactive'):
                raise EffectIndeterminate('Состояние служб не подтверждено')

    def _apply(self, key, components, action, pre_start=()):
        match = _KEY.fullmatch(key) if type(key) is str else None
        if match is None or type(components) is not tuple or type(pre_start) is not tuple or pre_start:
            raise UpdateError('Недопустимая операция служб')
        transaction, phase = match.groups()
        expected_action = 'stop' if phase in ('stop', 'rollback_stop') else 'start'
        expected_components = tuple(reversed(self.plan.components)) if action == 'stop' else self.plan.components
        active = _ACTIVE_EFFECT.get()
        if (action != expected_action or components != expected_components or active is None
                or active[0] is not self.plan or active[1:] != (transaction, phase)):
            raise UpdateError('Вызов не соответствует активному плану')
        state = self.plan.status()
        phases = self.plan.forward if state and state['direction'] == 'forward' else self.plan.rollback
        if (state is None or state['transaction'] != transaction or state['outcome'] != 'running'
                or state.get('indeterminate') is not True or phases[state['index']] != phase):
            raise UpdateError('Не подтверждён checkpoint служб')
        try:
            if self._units_hash() != self.units_hash:
                raise UpdateError('Состав служб изменился')
        except Exception:
            raise EffectIndeterminate('Состав служб не подтверждён') from None
        binding = {'version': 1, 'plan': self.plan.digest, 'key': key, 'action': action,
                   'installation_sha256': self.install_hash, 'units_sha256': self.units_hash}
        try:
            work = checked_path(self.ports.work)
            work.mkdir(parents=True, exist_ok=True)
            path = self.ports._path(key, '.service.json')
            record = read_effect_receipt(path)
        except EffectIndeterminate:
            raise
        except Exception:
            raise EffectIndeterminate('Недоступна отметка служб') from None
        if record is not None:
            if record == {**binding, 'status': 'done'}:
                try:
                    self._check_state(action)
                except Exception:
                    raise EffectIndeterminate('Состояние служб изменилось') from None
                return
            if record == {**binding, 'status': 'pending'} and action == 'stop' and self.reconcile_key == key:
                try:
                    # Только наблюдение: не запускать start/stop повторно.
                    self._check_state(action)
                    if self._units_hash() != self.units_hash or read_effect_receipt(path) != record:
                        raise UpdateError('Данные изменились при проверке')
                    _atomic_write(path, {**binding, 'status': 'done'})
                    self._check_state(action)
                except Exception:
                    raise EffectIndeterminate('Состояние служб не подтверждено') from None
                self.reconcile_key = None
                return
            raise EffectIndeterminate('Результат управления службами требует проверки')
        _atomic_write(path, {**binding, 'status': 'pending'})
        try:
            self.group.command(action, execute=True)
            self._check_state(action)
            _atomic_write(path, {**binding, 'status': 'done'})
            self._check_state(action)
        except Exception:
            raise EffectIndeterminate('Результат управления службами требует проверки') from None

    def stop(self, key, components):
        self._apply(key, components, 'stop')

    def start(self, key, components, pre_start):
        self._apply(key, components, 'start', pre_start)
