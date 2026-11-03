"""Compatibility import for the shared cooperative writer-lock protocol."""
from homensai_installer_client.writer_guard import CooperativeWriterLock, WriterGuardError
import hashlib


def writer_lock_digest(path):
    """Bind a canonical cooperative lock path into a durable update plan."""
    if path is None:
        return None
    lock = CooperativeWriterLock(path)
    return hashlib.sha256(str(lock.path).encode('utf-8')).hexdigest()

__all__ = ['CooperativeWriterLock', 'WriterGuardError', 'writer_lock_digest']
