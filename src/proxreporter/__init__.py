"""
Proxreporter - Proxmox Configuration Reporter and Backup Tool

A modular, secure tool for extracting Proxmox configuration data,
generating reports, and uploading backups via SFTP.
"""

__version__ = "3.0.0"
__author__ = "Proxreporter Team"

from .exceptions import (
    ProxreporterError,
    ConfigurationError,
    ConnectionError,
    AuthenticationError,
    UploadError,
    ExtractionError,
)
from .security import SecurityManager
from .config import Config

__all__ = [
    "__version__",
    "ProxreporterError",
    "ConfigurationError", 
    "ConnectionError",
    "AuthenticationError",
    "UploadError",
    "ExtractionError",
    "SecurityManager",
    "Config",
]
