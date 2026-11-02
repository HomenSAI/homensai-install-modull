"""Experimental local JSON client for the HomenS.AI installer API."""
from .client import InstallerClient, InstallerClientError

__version__ = '0.1.0'
__all__ = ['InstallerClient', 'InstallerClientError', '__version__']
