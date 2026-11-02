# HomenS.AI · © 2026 Serhii Khomenko
"""Экспериментальный порт подписанной миграции для локального UpdatePlan.

Не обеспечивает остановку всех писателей, безопасный runtime или разрешение
неизвестного результата. Каталог work и вызывающий процесс должны быть доверены.
"""
from __future__ import annotations

import hashlib
import re

from shared import archive_crypto
from shared.adapters_fs import FilesystemPorts
from shared.effect_receipt import read_effect_receipt
from shared.native_migration import run_signed_native_migration
from shared.update_plan import (EffectIndeterminate, UpdateError, UpdatePlan,
                                _ACTIVE_EFFECT, _atomic_write)

_KEY = re.compile(r'([a-f0-9]{32})/migrate:(0|[1-9][0-9]{0,5})\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')


class SignedMigrationPort:
    """Миграция под блокировкой плана, после двух проверенных шифрованных копий.

    Устойчивый pending не допускает автоматического повтора команды; отдельной
    процедуры разрешения неопределённого исхода этот ранний порт пока не имеет.
    """
    def __init__(self, plan: UpdatePlan, ports: FilesystemPorts, *, install_root: str,
                 package_path: str, envelope: bytes, trusted_public_key: bytes,
                 interpreter: str, interpreter_sha256: str, timeout: float):
        if (not isinstance(plan, UpdatePlan) or not isinstance(ports, FilesystemPorts)
                or plan.ports is not ports or not plan.guard_effects
                or ports.manifest != plan.manifest or ports.state != plan.state_path
                or ports.password_provider is None or type(install_root) is not str
                or type(package_path) is not str or type(interpreter) is not str
                or type(envelope) is not bytes or not envelope or type(trusted_public_key) is not bytes
                or len(trusted_public_key) != 32 or type(interpreter_sha256) is not str
                or _HASH.fullmatch(interpreter_sha256) is None):
            raise UpdateError('Недопустимая конфигурация порта миграции')
        # Дополнительно сверяем корень с реально привязанными к FS-портам путями.
        from shared.datahome import resolve_datahome
        home = resolve_datahome(plan.manifest, install_root)
        if home != ports.home:
            raise UpdateError('Установка не соответствует портам')
        self.plan, self.ports, self.install_root = plan, ports, install_root
        self.package_path, self.envelope, self.public_key = package_path, envelope, trusted_public_key
        self.interpreter, self.interpreter_sha256, self.timeout = interpreter, interpreter_sha256, timeout

    _read = staticmethod(read_effect_receipt)

    def migrate(self, key: str, migration: str, command: tuple[str, ...]) -> None:
        match = _KEY.fullmatch(key) if type(key) is str else None
        if match is None:
            raise UpdateError('Недопустимый ключ миграции')
        transaction, index = match.group(1), int(match.group(2))
        phase = 'migrate:' + str(index)
        active = _ACTIVE_EFFECT.get()
        if (active is None or active[0] is not self.plan or active[1:] != (transaction, phase)
                or index >= len(self.plan.migrations) or self.plan.migrations[index] != migration
                or command != tuple(self.plan.manifest['migrations']['cmd'])):
            raise UpdateError('Вызов не соответствует активному плану')
        state = self.plan.status()
        if (state is None or state['transaction'] != transaction or state['direction'] != 'forward'
                or state['outcome'] != 'running' or state.get('indeterminate') is not True
                or self.plan.forward[state['index']] != phase or not state['archive_ready']):
            raise UpdateError('Не подтверждён checkpoint миграции')
        for name in ('archive', 'backup:' + str(index)):
            path = self.ports.archive_path(transaction + '/' + name)
            try:
                if not archive_crypto.is_encrypted(path):
                    raise UpdateError('Требуется проверенная шифрованная копия')
                snapshot = self.ports._verify_archive(path)
                if set(snapshot['roots']) != set(self.ports.home.roots):
                    raise UpdateError('Копия не соответствует установке')
            except Exception:
                raise EffectIndeterminate('Копия миграции не подтверждена') from None
        binding = {'version': 1, 'plan': self.plan.digest, 'key': key, 'migration': migration,
                   'installation_sha256': hashlib.sha256(self.install_root.encode('utf-8')).hexdigest(),
                   'envelope_sha256': hashlib.sha256(self.envelope).hexdigest(),
                   'publisher_sha256': hashlib.sha256(self.public_key).hexdigest(),
                   'interpreter_sha256': self.interpreter_sha256}
        options = dict(package_path=self.package_path, envelope=self.envelope, trusted_public_key=self.public_key,
                       expected_product_id=self.plan.manifest['product_id'], expected_version=self.plan.release,
                       release_root=str(self.ports.release_path(self.plan.release)), expected_manifest=self.plan.manifest,
                       install_root=self.install_root, migration_id=migration, interpreter=self.interpreter,
                       interpreter_sha256=self.interpreter_sha256, timeout=self.timeout)
        try:
            path = self.ports._path(key, '.migration.json')
            record = self._read(path)
        except EffectIndeterminate:
            raise
        except Exception:
            raise EffectIndeterminate('Недоступна отметка миграции') from None
        if record is not None:
            if record == {**binding, 'status': 'done'}:
                try:
                    run_signed_native_migration(**options)
                except Exception:
                    raise EffectIndeterminate('Подготовленный выпуск больше не подтверждён') from None
                return
            if record == {**binding, 'status': 'pending'}:
                raise EffectIndeterminate('Предыдущий вызов требует проверки')
            raise EffectIndeterminate('Отметка относится к другой операции')
        run_signed_native_migration(**options)  # Предварительная проверка без исполнения.
        _atomic_write(path, {**binding, 'status': 'pending'})
        try:
            run_signed_native_migration(**options, execute=True)
            run_signed_native_migration(**options)  # Не подтверждать изменённый выпуск.
            _atomic_write(path, {**binding, 'status': 'done'})
        except Exception:
            raise EffectIndeterminate('Результат миграции требует проверки') from None
