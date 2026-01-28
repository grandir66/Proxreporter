"""
Custom exceptions for Proxreporter.

Provides a hierarchy of exceptions for better error handling and debugging.
"""

from typing import Optional, Any


class ProxreporterError(Exception):
    """Base exception for all Proxreporter errors."""
    
    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.details = details
    
    def __str__(self) -> str:
        if self.details:
            return f"{self.message} (details: {self.details})"
        return self.message


class ConfigurationError(ProxreporterError):
    """Raised when there's a configuration problem."""
    pass


class ConnectionError(ProxreporterError):
    """Raised when a connection (SSH, SFTP, API) fails."""
    
    def __init__(self, message: str, host: Optional[str] = None, 
                 port: Optional[int] = None, details: Optional[Any] = None):
        super().__init__(message, details)
        self.host = host
        self.port = port
    
    def __str__(self) -> str:
        location = ""
        if self.host:
            location = f" to {self.host}"
            if self.port:
                location += f":{self.port}"
        return f"{self.message}{location}"


class AuthenticationError(ConnectionError):
    """Raised when authentication fails."""
    
    def __init__(self, message: str, host: Optional[str] = None,
                 username: Optional[str] = None, details: Optional[Any] = None):
        super().__init__(message, host, details=details)
        self.username = username


class SSHConnectionError(ConnectionError):
    """Raised when SSH connection fails."""
    pass


class SFTPConnectionError(ConnectionError):
    """Raised when SFTP connection fails."""
    pass


class APIConnectionError(ConnectionError):
    """Raised when Proxmox API connection fails."""
    pass


class UploadError(ProxreporterError):
    """Raised when file upload fails."""
    
    def __init__(self, message: str, local_path: Optional[str] = None,
                 remote_path: Optional[str] = None, details: Optional[Any] = None):
        super().__init__(message, details)
        self.local_path = local_path
        self.remote_path = remote_path


class ExtractionError(ProxreporterError):
    """Raised when data extraction fails."""
    
    def __init__(self, message: str, source: Optional[str] = None,
                 details: Optional[Any] = None):
        super().__init__(message, details)
        self.source = source


class EncryptionError(ProxreporterError):
    """Raised when encryption/decryption fails."""
    pass


class DecryptionError(EncryptionError):
    """Raised when decryption specifically fails."""
    pass


class LockError(ProxreporterError):
    """Raised when file locking fails."""
    pass


class ValidationError(ProxreporterError):
    """Raised when validation fails."""
    
    def __init__(self, message: str, field: Optional[str] = None,
                 value: Optional[Any] = None, details: Optional[Any] = None):
        super().__init__(message, details)
        self.field = field
        self.value = value
