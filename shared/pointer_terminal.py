"""Internal locked observation of a completed pointer-v2 transaction."""
from contextlib import contextmanager

from shared.adapters_fs import FilesystemPorts
from shared.datahome import checked_path
from shared.package_signing import verify_signed_package
from shared.pointer_recovery import _inspect_started_pointer_group
from shared.update_plan import UpdateError, UpdatePlan
from shared.update_prepare import _locked_update_scope
from shared.writer_guard import writer_lock_digest


def _unit_binding(group):
    return tuple((name, str(group.adapters[name].path), group.adapters[name].digest) for name in group.order)


@contextmanager
def _verified_terminal_group(*, unit_dir, load_key, runtime_key, runtime_reserve_bytes,
                             runner=None, timeout=30, _verify_runtime=True, **options):
    """Hold both cooperative locks and recheck signed state before and after use.

    Internal building block only. The group is valid only within the context;
    it does not authenticate process identities or stop noncooperating writers.
    """
    try:
        root, work = checked_path(options['install_root']), checked_path(options['work_dir'])
        metadata = verify_signed_package(options['new_package'], options['new_envelope'],
            trusted_public_key=options['new_key'], expected_product_id=options['product_id'],
            expected_version=options['new_version'])
        ports = FilesystemPorts(metadata['manifest'], root, work_dir=work, payloads={}, effects=None,
                                password_provider=lambda: None)
        plan = UpdatePlan(metadata['manifest'], root, ports, release=options['new_version'],
                          modes=options['modes'], migration_ids=options['migration_ids'], guard_effects=True,
                          writer_lock_sha256=writer_lock_digest(options.get('writer_lock_path')))
        inspect_options = dict(unit_dir=unit_dir, load_key=load_key, runtime_key=runtime_key,
            runtime_reserve_bytes=runtime_reserve_bytes, runner=runner, timeout=timeout,
            _locks_held=True, _verify_runtime=_verify_runtime, **options)
        with _locked_update_scope(root, plan.lock_path):
            before, group = _inspect_started_pointer_group(**inspect_options)
            if before['state']['outcome'] not in ('updated', 'rolled_back'):
                raise UpdateError('Обновление ещё не завершено')
            binding = _unit_binding(group)
            try:
                yield group, before
            finally:
                after, checked_group = _inspect_started_pointer_group(**inspect_options)
                if after['state'] != before['state'] or _unit_binding(checked_group) != binding:
                    raise UpdateError('Состояние обновления или группы изменилось')
    except Exception:
        raise UpdateError('Не удалось подтвердить блокировку группы после обновления') from None
