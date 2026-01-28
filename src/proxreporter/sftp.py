"""
SFTP upload management for Proxreporter.

Provides secure file uploads with retry logic, connection pooling,
and proper error handling.
"""

import os
import logging
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from .exceptions import SFTPConnectionError, UploadError, AuthenticationError
from .ssh import SSHConnection, HostKeyPolicy
from .utils import ensure_directory

logger = logging.getLogger("proxreporter.sftp")


class SFTPUploader:
    """
    Secure SFTP file uploader with retry logic and failover.
    
    Features:
    - Automatic retry on failure with exponential backoff
    - Fallback to secondary server
    - Connection reuse
    - Progress tracking
    - Secure credential handling
    """
    
    DEFAULT_TIMEOUT = 30
    DEFAULT_RETRIES = 3
    DEFAULT_BACKOFF = 5
    
    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "root",
        password: Optional[str] = None,
        base_path: str = "/tmp",
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        fallback_host: Optional[str] = None,
        fallback_port: Optional[int] = None,
        fallback_username: Optional[str] = None,
        fallback_password: Optional[str] = None,
        host_key_policy: str = HostKeyPolicy.WARN,
    ):
        """
        Initialize SFTP uploader.
        
        Args:
            host: Primary SFTP server host.
            port: SFTP port.
            username: SFTP username.
            password: SFTP password.
            base_path: Base path on remote server.
            timeout: Connection timeout.
            retries: Number of retry attempts.
            fallback_host: Fallback server host.
            fallback_port: Fallback server port.
            fallback_username: Fallback server username.
            fallback_password: Fallback server password.
            host_key_policy: How to handle unknown host keys.
        """
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.base_path = base_path
        self.timeout = timeout
        self.retries = retries
        self.host_key_policy = host_key_policy
        
        # Fallback configuration
        self.fallback_host = fallback_host
        self.fallback_port = fallback_port or port
        self.fallback_username = fallback_username or username
        self._fallback_password = fallback_password or password
        
        self._connection: Optional[SSHConnection] = None
        self._sftp = None
        self._using_fallback = False
    
    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'SFTPUploader':
        """
        Create uploader from configuration dictionary.
        
        Args:
            config: Configuration dictionary with 'sftp' section.
            
        Returns:
            Configured SFTPUploader instance.
        """
        sftp_config = config.get('sftp', {})
        
        return cls(
            host=sftp_config.get('host', ''),
            port=sftp_config.get('port', 22),
            username=sftp_config.get('username', ''),
            password=sftp_config.get('password'),
            base_path=sftp_config.get('base_path', '/tmp'),
            timeout=sftp_config.get('timeout', cls.DEFAULT_TIMEOUT),
            retries=sftp_config.get('retries', cls.DEFAULT_RETRIES),
            fallback_host=sftp_config.get('fallback_host'),
            fallback_port=sftp_config.get('fallback_port'),
            fallback_username=sftp_config.get('fallback_username'),
            fallback_password=sftp_config.get('fallback_password'),
        )
    
    def _try_connect(self, host: str, port: int, 
                     username: str, password: str) -> bool:
        """
        Attempt to establish connection to a specific host.
        
        Returns:
            True if connection successful.
        """
        try:
            self._connection = SSHConnection(
                host=host,
                port=port,
                username=username,
                password=password,
                timeout=self.timeout,
                host_key_policy=self.host_key_policy,
            )
            self._connection.connect()
            self._sftp = self._connection.open_sftp()
            logger.info(f"Connected to SFTP server {host}:{port}")
            return True
            
        except AuthenticationError as e:
            logger.warning(f"Authentication failed for {username}@{host}: {e}")
            self._cleanup()
            return False
            
        except SFTPConnectionError as e:
            logger.warning(f"Connection failed to {host}:{port}: {e}")
            self._cleanup()
            return False
            
        except Exception as e:
            logger.warning(f"Unexpected error connecting to {host}: {e}")
            self._cleanup()
            return False
    
    def connect(self) -> bool:
        """
        Connect to SFTP server with retry and failover.
        
        Returns:
            True if connection established.
            
        Raises:
            SFTPConnectionError: If all connection attempts fail.
        """
        if self._sftp:
            return True
        
        # Validate configuration
        if not self.host or not self.username:
            raise SFTPConnectionError(
                "SFTP configuration incomplete (missing host or username)"
            )
        
        if not self._password:
            raise SFTPConnectionError(
                "SFTP password not configured"
            )
        
        # Try primary server with retries
        delay = self.DEFAULT_BACKOFF
        for attempt in range(1, self.retries + 1):
            logger.info(f"Connection attempt {attempt}/{self.retries} to {self.host}")
            
            if self._try_connect(self.host, self.port, 
                                self.username, self._password):
                self._using_fallback = False
                return True
            
            if attempt < self.retries:
                logger.info(f"Waiting {delay}s before retry...")
                time.sleep(delay)
                delay *= 2  # Exponential backoff
        
        logger.error(f"All {self.retries} attempts to {self.host} failed")
        
        # Try fallback server
        if self.fallback_host and self.fallback_host != self.host:
            logger.info(f"Attempting failover to {self.fallback_host}")
            
            if self._try_connect(
                self.fallback_host, 
                self.fallback_port,
                self.fallback_username, 
                self._fallback_password
            ):
                self._using_fallback = True
                logger.info("Failover successful")
                return True
            
            logger.error(f"Failover to {self.fallback_host} also failed")
        
        raise SFTPConnectionError(
            "Failed to connect to any SFTP server",
            host=self.host
        )
    
    def _cleanup(self) -> None:
        """Clean up connection resources."""
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        
        if self._connection:
            self._connection.disconnect()
            self._connection = None
    
    def disconnect(self) -> None:
        """Close SFTP connection."""
        self._cleanup()
        logger.debug("SFTP connection closed")
    
    def _ensure_remote_directory(self, remote_path: str) -> bool:
        """
        Ensure remote directory exists, creating if necessary.
        
        Args:
            remote_path: Remote directory path.
            
        Returns:
            True if directory exists or was created.
        """
        if not self._sftp:
            return False
        
        path = remote_path.replace('\\', '/')
        if not path or path == '.':
            return True
        
        parts = [p for p in path.split('/') if p]
        is_absolute = path.startswith('/')
        
        current = '/' if is_absolute else ''
        
        for part in parts:
            current = f"{current}/{part}" if current else part
            if is_absolute and not current.startswith('/'):
                current = '/' + current
            
            try:
                self._sftp.stat(current)
            except FileNotFoundError:
                try:
                    self._sftp.mkdir(current)
                    logger.debug(f"Created remote directory: {current}")
                except Exception as e:
                    logger.error(f"Failed to create directory {current}: {e}")
                    return False
            except Exception as e:
                logger.warning(f"Error checking directory {current}: {e}")
        
        return True
    
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """
        Upload a single file with retry logic.
        
        Args:
            local_path: Path to local file.
            remote_path: Destination path on server.
            
        Returns:
            True if upload successful.
            
        Raises:
            UploadError: If upload fails after retries.
        """
        local_file = Path(local_path)
        
        if not local_file.exists():
            raise UploadError(
                f"Local file not found",
                local_path=str(local_path)
            )
        
        delay = self.DEFAULT_BACKOFF
        
        for attempt in range(1, self.retries + 1):
            try:
                if not self._sftp:
                    self.connect()
                
                # Ensure parent directory exists
                remote_dir = os.path.dirname(remote_path)
                if remote_dir:
                    self._ensure_remote_directory(remote_dir)
                
                # Upload file
                self._sftp.put(str(local_file), remote_path)
                
                file_size = local_file.stat().st_size / (1024 * 1024)
                logger.info(
                    f"Uploaded: {local_file.name} ({file_size:.2f} MB) "
                    f"-> {remote_path}"
                )
                return True
                
            except Exception as e:
                logger.warning(
                    f"Upload attempt {attempt}/{self.retries} failed: {e}"
                )
                
                if attempt < self.retries:
                    # Try to reconnect
                    self._cleanup()
                    try:
                        self.connect()
                    except Exception:
                        pass
                    
                    logger.info(f"Waiting {delay}s before retry...")
                    time.sleep(delay)
                    delay *= 2
        
        raise UploadError(
            f"Failed to upload after {self.retries} attempts",
            local_path=str(local_path),
            remote_path=remote_path
        )
    
    def upload_files(self, files: List[str], 
                     remote_base_path: Optional[str] = None) -> Dict[str, bool]:
        """
        Upload multiple files.
        
        Args:
            files: List of local file paths.
            remote_base_path: Base path on remote server (uses self.base_path if None).
            
        Returns:
            Dictionary mapping file paths to upload success status.
        """
        results = {}
        base_path = remote_base_path or self.base_path
        
        logger.info(f"Uploading {len(files)} files to {base_path}")
        
        if not self._sftp:
            try:
                self.connect()
            except SFTPConnectionError as e:
                logger.error(f"Failed to connect: {e}")
                return {f: False for f in files}
        
        for file_path in files:
            local_file = Path(file_path)
            
            if not local_file.exists():
                logger.warning(f"File not found, skipping: {file_path}")
                results[file_path] = False
                continue
            
            remote_path = f"{base_path}/{local_file.name}"
            
            try:
                self.upload_file(str(local_file), remote_path)
                results[file_path] = True
            except UploadError as e:
                logger.error(f"Failed to upload {file_path}: {e}")
                results[file_path] = False
        
        success_count = sum(results.values())
        logger.info(f"Upload complete: {success_count}/{len(files)} files")
        
        return results
    
    def __enter__(self) -> 'SFTPUploader':
        """Context manager entry."""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.disconnect()
