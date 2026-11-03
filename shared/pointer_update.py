"""Experimental pointer-v2 update assembly from a confirmed fresh installation."""
from __future__ import annotations

import re

from shared.datahome import checked_path
from shared.package_signing import verify_signed_package
from shared.runtime_native import _definition, _inspect_runtime, verify_signed_linux_runtime
from shared.systemd_user_load import _pointer_signed_user_group
from shared.systemd_user_publish import _publication_receipt
from shared.update_assembly import assemble_prepared_linux_update
from shared.update_plan import UpdateError
from shared.update_prepare import _check_pointer_runtime_compatibility


class _RuntimeEvidence:
    """Captured while old release is active; does not require its pointer later."""
    def __init__(self, *, package, envelope, root, options, runtime_key, reserve_bytes):
        self.package, self.envelope, self.options = package, envelope, options
        definition = _definition(package, envelope, root, options)
        self.metadata, _, self.root, home, _, self.recipe, self.files = definition
        self.runtime = checked_path(home.roots['runtime'])
        self.receipt_path = checked_path(self.root / '.homensai-control/linux-runtime.json')
        self.receipt = _publication_receipt(self.receipt_path)
        if (self.receipt is None or self.receipt['status'] != 'done'
                or self.receipt['operation_id'] != runtime_key):
            raise UpdateError('Runtime не подтверждён')
        verify_signed_linux_runtime(package, envelope, root, **options,
                                    operation_id=runtime_key, reserve_bytes=reserve_bytes)
        self.verify()

    def verify(self):
        if (verify_signed_package(self.package, self.envelope, **self.options) != self.metadata
                or _publication_receipt(self.receipt_path) != self.receipt):
            raise UpdateError('Подписанный runtime изменился')
        _inspect_runtime(checked_path(self.runtime), self.files, set(self.recipe['executables']))
        if _publication_receipt(self.receipt_path) != self.receipt:
            raise UpdateError('Отметка runtime изменилась')


def assemble_pointer_linux_update(*, unit_dir, load_key, runtime_key, runtime_reserve_bytes,
                                  password_provider, interpreter, interpreter_sha256, timeout,
                                  runner=None, writer_lock=None, **options):
    """Prepare initial plan from signed pointer units; do not run it or start services.

    The old release must still be active. Reopening an interrupted update is
    not implemented here; inspect its state and use an independently reviewed
    recovery path. Caller remains responsible for publisher keys, interpreter,
    runtime provenance and every writer outside the confirmed systemd group.
    """
    try:
        if (any(type(key) is not str or re.fullmatch(r'[a-f0-9]{32}', key) is None
                for key in (load_key, runtime_key, options['operation_id']))
                or len({load_key, runtime_key, options['operation_id']}) != 3
                or type(runtime_reserve_bytes) is not int or not 0 <= runtime_reserve_bytes <= 2**63 - 1):
            raise UpdateError('Недопустимые ключи обновления')
        old_options = dict(trusted_public_key=options['old_key'], expected_product_id=options['product_id'],
                           expected_version=options['old_version'])
        old_metadata = verify_signed_package(options['old_package'], options['old_envelope'], **old_options)
        new_metadata = verify_signed_package(options['new_package'], options['new_envelope'],
            trusted_public_key=options['new_key'], expected_product_id=options['product_id'],
            expected_version=options['new_version'])
        if not _check_pointer_runtime_compatibility(old_metadata, new_metadata):
            raise UpdateError('Требуется рецепт pointer-v2')
        evidence = _RuntimeEvidence(package=options['old_package'], envelope=options['old_envelope'],
            root=options['install_root'], options=old_options, runtime_key=runtime_key,
            reserve_bytes=runtime_reserve_bytes)
        group = _pointer_signed_user_group(options['old_package'], options['old_envelope'], options['install_root'],
            **old_options, modes=options['modes'], unit_dir=unit_dir, operation_id=load_key,
            runtime_operation_id=runtime_key, reserve_bytes=runtime_reserve_bytes,
            runner=runner, _require_active=True)
        plan = assemble_prepared_linux_update(group=group, password_provider=password_provider,
            interpreter=interpreter, interpreter_sha256=interpreter_sha256, timeout=timeout,
            writer_lock=writer_lock, runtime_guard=evidence.verify, **options)
        evidence.verify()
        return plan
    except Exception:
        raise UpdateError('Не удалось собрать обновление pointer-v2') from None
