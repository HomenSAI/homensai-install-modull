"""Experimental local JSON client for the HomenS.AI installer API."""
import json
from importlib import resources

from .client import InstallerClient, InstallerClientError
from .writer_guard import CooperativeWriterLock, WriterGuardError

__version__ = '0.3.0'
CONTRACT_API_MAJOR = 1
CONTRACT_API_MINOR = 43


def contract_schema(kind: str) -> dict:
    """Read the bundled API 1.43 request or response-envelope schema."""
    if kind not in ('request', 'response') or type(kind) is not str:
        raise ValueError('expected request or response')
    data = resources.files(__package__).joinpath('schemas', 'api-v1-' + kind + '.schema.json').read_text(encoding='utf-8')
    return json.loads(data)


__all__ = ['InstallerClient', 'InstallerClientError', 'CooperativeWriterLock', 'WriterGuardError',
           'CONTRACT_API_MAJOR', 'CONTRACT_API_MINOR',
           'contract_schema', '__version__']
