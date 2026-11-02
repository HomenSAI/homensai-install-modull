"""Compatibility import for the shared cooperative writer-lock protocol."""
from homensai_installer_client.writer_guard import CooperativeWriterLock, WriterGuardError

__all__ = ['CooperativeWriterLock', 'WriterGuardError']
