"""Отдельное разрешение destructive recovery, не являющееся migration proof.

Использует тот же private ciphertext snapshot/decrypt/restore механизм, что и
миграции. Каждая проверка повторно восстанавливает полный архив и вызывает
trusted verifier ядра. Наличие sidecar или строки receipt ID недостаточно.
"""
from copy import deepcopy
import errno
import os
from pathlib import Path

from shared.archive import InsufficientSpaceError
from shared.datahome import checked_path
from shared.update_plan import _atomic_write

from .api.model import checked_product_id, digest
from .errors import InstallerError
from .migration_receipts import BackupReceiptStore, _HASH, _NAME, _fail


_BINDING = {'product_id', 'installation_id', 'operation_id', 'from_release',
    'from_manifest_sha256', 'from_inventory_sha256', 'from_metadata_sha256',
    'recovery_action', 'source_archive_sha256'}
_VERIFIED = 'full_snapshot_and_signed_recovery_payload_restored'


def _verification_failure(error, reason):
    """Only typed budget/OS ENOSPC failures get a disk-space diagnosis."""
    if isinstance(error, InsufficientSpaceError) or isinstance(error, OSError) and error.errno == errno.ENOSPC:
        raise InstallerError('DISK_SPACE_LOW', phase='product_adapter',
            details={'reason': 'recovery_receipt_insufficient_space'}) from None
    _fail(reason)


def _binding(value):
    if type(value) is not dict or set(value) != _BINDING:
        _fail('recovery_receipt_binding_shape')
    checked_product_id(value['product_id'])
    if value['recovery_action'] not in ('restore', 'rollback'):
        _fail('recovery_receipt_action')
    for key in ('installation_id', 'operation_id', 'from_release'):
        if type(value[key]) is not str or not _NAME.fullmatch(value[key]):
            _fail('recovery_receipt_identity')
    for key in ('from_manifest_sha256', 'from_inventory_sha256', 'from_metadata_sha256', 'source_archive_sha256'):
        if type(value[key]) is not str or not _HASH.fullmatch(value[key]):
            _fail('recovery_receipt_digest')
    return deepcopy(value)


class RecoveryBackupReceiptStore(BackupReceiptStore):
    """Только before-restore/before-rollback, с точным operation/source binding."""

    def issue(self, archive_path, binding, *, consistency_scope):
        try:
            binding = _binding(binding)
            if consistency_scope not in ('owned_components_stopped', 'owned_components_stopped_and_adapter_quiesced'):
                _fail('recovery_consistency_not_established')
            directory = self._directory(create=True)
            verified = self._verified(archive_path, binding)
            body = {'schema_version': 1, 'kind': 'before_recovery_backup', **binding, **verified,
                'encrypted': True, 'includes_recovery_payload': True, 'migration_eligible': False,
                'verification_scope': _VERIFIED, 'consistency_scope': consistency_scope}
            receipt_id = digest(body)
            record = {'receipt_id': receipt_id, 'body': body,
                      'archive_path': str(checked_path(Path(archive_path).absolute()))}
            path = directory / (receipt_id + '.json')
            if path.exists():
                if self._read(receipt_id) != record:
                    _fail('recovery_receipt_id_conflict')
            else:
                _atomic_write(path, record)
                if os.name != 'nt':
                    path.chmod(0o600)
            return {'receipt_id': receipt_id, **deepcopy(body)}
        except InstallerError:
            raise
        except Exception as error:
            _verification_failure(error, 'recovery_receipt_issuance_failed')

    def verify(self, receipt_id, *, expected_binding, consistency_scope):
        try:
            expected = _binding(expected_binding)
            record = self._read(receipt_id)
            body = record['body']
            required = {'schema_version', 'kind', *_BINDING, 'archive_sha256', 'archive_manifest_sha256',
                'encrypted', 'includes_recovery_payload', 'migration_eligible', 'verification_scope', 'consistency_scope'}
            if (consistency_scope not in ('owned_components_stopped', 'owned_components_stopped_and_adapter_quiesced')
                    or set(body) != required or type(body['schema_version']) is not int or body['schema_version'] != 1
                    or body['kind'] != 'before_recovery_backup' or body['encrypted'] is not True
                    or body['includes_recovery_payload'] is not True or body['migration_eligible'] is not False
                    or body['verification_scope'] != _VERIFIED or body['consistency_scope'] != consistency_scope
                    or any(body[key] != expected[key] for key in _BINDING)):
                _fail('recovery_receipt_binding_mismatch')
            verified = self._verified(record['archive_path'], expected, body['archive_sha256'])
            if verified['archive_manifest_sha256'] != body['archive_manifest_sha256']:
                _fail('recovery_receipt_manifest_mismatch')
            return {'receipt_id': receipt_id, **deepcopy(body)}
        except InstallerError:
            raise
        except Exception as error:
            _verification_failure(error, 'recovery_receipt_verification_failed')
