"""Conservative execution of a signed pointer-v2 update plan."""
from __future__ import annotations

from shared import archive_crypto
from shared.effect_receipt import read_effect_receipt
from shared.pointer_update import assemble_pointer_linux_update
from shared.update_plan import UpdateError
from shared.writer_guard import CooperativeWriterLock


def run_pointer_linux_update(*, password_env, action, reconcile_stop_key=None, writer_lock_path=None, **options):
    """Start or reopen an update; only a verified stop may resolve uncertainty.

    All signing keys, interpreter provenance, systemd manager, archive password,
    and writers outside the selected group remain the integrator's responsibility.
    This is not a sandbox for signed code. A completed transaction cannot yet be
    reopened through this controller, and a lost response must be inspected.
    """
    try:
        if action not in ('start', 'reopen', 'reconcile-stop') or type(action) is not str:
            raise UpdateError('Недопустимое действие')
        if (action == 'reconcile-stop') != (reconcile_stop_key is not None):
            raise UpdateError('Недопустимый ключ сверки')
        provider = archive_crypto.password_provider(env_var=password_env)
        if password_env is None:
            raise UpdateError('Требуется переменная пароля')
        password = archive_crypto._password(provider)
        if writer_lock_path is not None:
            if 'writer_lock' in options:
                raise UpdateError('Повторное описание блокировки')
            options['writer_lock'] = CooperativeWriterLock(writer_lock_path)
        # Snapshot the password for the transaction without logging or persisting it.
        plan = assemble_pointer_linux_update(password_provider=lambda: password,
            reopen=action != 'start', reconcile_stop_key=reconcile_stop_key, **options)
        if action == 'reconcile-stop':
            state = plan.status()
            phases = plan.forward if state and state['direction'] == 'forward' else plan.rollback
            if (state is None or state['outcome'] != 'running' or state.get('indeterminate') is not True
                    or state['index'] >= len(phases) or phases[state['index']] not in ('stop', 'rollback_stop')
                    or reconcile_stop_key != state['transaction'] + '/' + phases[state['index']]):
                raise UpdateError('Сверка не соответствует checkpoint')
            receipt = read_effect_receipt(plan.ports._path(reconcile_stop_key, '.service.json'))
            if receipt is None or receipt.get('status') not in ('pending', 'done'):
                raise UpdateError('Нет отметки выполненной или начатой остановки')
        state = plan.run(resume_indeterminate=action == 'reconcile-stop')
        return {'state': state, 'health_verified_at_execution': True,
                'process_identity_verified': False, 'all_writers_verified': False}
    except Exception:
        raise UpdateError('Не удалось подтвердить выполнение обновления') from None
