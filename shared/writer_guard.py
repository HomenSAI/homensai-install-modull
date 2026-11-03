"""Compatibility import for the shared cooperative writer-lock protocol."""
from homensai_installer_client.writer_guard import CooperativeWriterLock, WriterGuardError
import hashlib
from shared.datahome import checked_path, contains


def writer_lock_digest(path, *, forbidden_roots=()):
    """Bind a canonical cooperative lock path into a durable update plan."""
    if path is None:
        return None
    lock = CooperativeWriterLock(path)
    if any(contains(checked_path(root), lock.path) for root in forbidden_roots):
        raise WriterGuardError('Блокировка пересекается с установкой или рабочим каталогом')
    return hashlib.sha256(str(lock.path).encode('utf-8')).hexdigest()

__all__ = ['CooperativeWriterLock', 'WriterGuardError', 'writer_lock_digest']
