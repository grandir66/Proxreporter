"""
SSH connection management for Proxreporter.

Provides secure SSH connections with connection pooling,
proper host key verification, and secure credential handling.
"""

import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Union
from contextlib import contextmanager

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    paramiko = None

from .exceptions import SSHConnectionError, AuthenticationError
from .security import mask_password

logger = logging.getLogger("proxreporter.ssh")


class HostKeyPolicy:
    """SSH host key verification policies."""
    
    REJECT = "reject"       # Reject unknown hosts (most secure)
    WARN = "warn"           # Warn but accept unknown hosts
    AUTO_ADD = "auto_add"   # Automatically accept (least secure, but practical)


class SSHConnection:
    """
    Secure SSH connection with proper resource management.
    
    Features:
    - Configurable host key verification
    - Connection pooling support
    - Secure password handling (not exposed in process list)
    - Automatic reconnection on failure
    - Context manager support
    """
    
    DEFAULT_TIMEOUT = 30
    DEFAULT_PORT = 22
    
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        username: str = "root",
        password: Optional[str] = None,
        key_file: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        host_key_policy: str = HostKeyPolicy.WARN,
        known_hosts_file: Optional[str] = None,
    ):
        """
        Initialize SSH connection parameters.
        
        Args:
            host: Remote host address.
            port: SSH port.
            username: SSH username.
            password: SSH password (optional if using key).
            key_file: Path to private key file.
            timeout: Connection timeout in seconds.
            host_key_policy: How to handle unknown host keys.
            known_hosts_file: Path to known_hosts file.
        """
        if not PARAMIKO_AVAILABLE:
            raise SSHConnectionError(
                "paramiko library not installed. Install with: pip install paramiko"
            )
        
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.key_file = key_file
        self.timeout = timeout
        self.host_key_policy = host_key_policy
        self.known_hosts_file = known_hosts_file
        
        self._client: Optional[paramiko.SSHClient] = None
        self._lock = threading.Lock()
        self._connected = False
    
    @property
    def is_connected(self) -> bool:
        """Check if connection is active."""
        if not self._client:
            return False
        try:
            transport = self._client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False
    
    def _setup_host_key_policy(self, client: paramiko.SSHClient) -> None:
        """Configure host key verification policy."""
        # Load known hosts if available
        if self.known_hosts_file:
            known_hosts = Path(self.known_hosts_file)
            if known_hosts.exists():
                client.load_host_keys(str(known_hosts))
        else:
            # Try default locations
            for path in ['~/.ssh/known_hosts', '/etc/ssh/ssh_known_hosts']:
                known_hosts = Path(path).expanduser()
                if known_hosts.exists():
                    try:
                        client.load_host_keys(str(known_hosts))
                        break
                    except Exception:
                        pass
        
        # Set policy for unknown hosts
        if self.host_key_policy == HostKeyPolicy.REJECT:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        elif self.host_key_policy == HostKeyPolicy.WARN:
            client.set_missing_host_key_policy(paramiko.WarningPolicy())
        else:  # AUTO_ADD
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            logger.warning(
                f"Auto-accepting host key for {self.host}. "
                "This is insecure for production use."
            )
    
    def connect(self) -> None:
        """
        Establish SSH connection.
        
        Raises:
            SSHConnectionError: If connection fails.
            AuthenticationError: If authentication fails.
        """
        with self._lock:
            if self.is_connected:
                return
            
            try:
                self._client = paramiko.SSHClient()
                self._setup_host_key_policy(self._client)
                
                connect_kwargs = {
                    'hostname': self.host,
                    'port': self.port,
                    'username': self.username,
                    'timeout': self.timeout,
                    'allow_agent': False,
                    'look_for_keys': False,
                }
                
                # Use key file if provided
                if self.key_file:
                    key_path = Path(self.key_file).expanduser()
                    if key_path.exists():
                        connect_kwargs['key_filename'] = str(key_path)
                        connect_kwargs['look_for_keys'] = True
                    else:
                        logger.warning(f"Key file not found: {self.key_file}")
                
                # Use password if provided and no key
                if self._password and 'key_filename' not in connect_kwargs:
                    connect_kwargs['password'] = self._password
                
                logger.info(f"Connecting to {self.host}:{self.port} as {self.username}")
                self._client.connect(**connect_kwargs)
                self._connected = True
                logger.info(f"Connected to {self.host}")
                
            except paramiko.AuthenticationException as e:
                self._cleanup()
                raise AuthenticationError(
                    f"Authentication failed for {self.username}@{self.host}",
                    host=self.host,
                    username=self.username,
                    details=str(e)
                )
            except paramiko.SSHException as e:
                self._cleanup()
                raise SSHConnectionError(
                    f"SSH error connecting to {self.host}",
                    host=self.host,
                    port=self.port,
                    details=str(e)
                )
            except Exception as e:
                self._cleanup()
                raise SSHConnectionError(
                    f"Failed to connect to {self.host}",
                    host=self.host,
                    port=self.port,
                    details=str(e)
                )
    
    def _cleanup(self) -> None:
        """Clean up connection resources."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._connected = False
    
    def disconnect(self) -> None:
        """Close SSH connection."""
        with self._lock:
            self._cleanup()
            logger.debug(f"Disconnected from {self.host}")
    
    def execute(self, command: str, timeout: Optional[int] = None) -> tuple:
        """
        Execute a command on the remote host.
        
        Args:
            command: Command to execute.
            timeout: Command timeout (uses connection timeout if None).
            
        Returns:
            Tuple of (exit_code, stdout, stderr).
            
        Raises:
            SSHConnectionError: If not connected or execution fails.
        """
        if not self.is_connected:
            self.connect()
        
        timeout = timeout or self.timeout
        
        try:
            stdin, stdout, stderr = self._client.exec_command(
                command, timeout=timeout
            )
            
            exit_code = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode('utf-8', errors='replace')
            stderr_text = stderr.read().decode('utf-8', errors='replace')
            
            return exit_code, stdout_text, stderr_text
            
        except Exception as e:
            raise SSHConnectionError(
                f"Command execution failed on {self.host}",
                host=self.host,
                details=str(e)
            )
    
    def execute_or_fail(self, command: str, timeout: Optional[int] = None) -> str:
        """
        Execute a command and raise on non-zero exit.
        
        Args:
            command: Command to execute.
            timeout: Command timeout.
            
        Returns:
            Command stdout.
            
        Raises:
            SSHConnectionError: If command fails.
        """
        exit_code, stdout, stderr = self.execute(command, timeout)
        
        if exit_code != 0:
            raise SSHConnectionError(
                f"Command failed with exit code {exit_code}",
                host=self.host,
                details=f"stderr: {stderr}"
            )
        
        return stdout
    
    def open_sftp(self) -> 'paramiko.SFTPClient':
        """
        Open SFTP channel on this connection.
        
        Returns:
            SFTP client.
            
        Raises:
            SSHConnectionError: If SFTP open fails.
        """
        if not self.is_connected:
            self.connect()
        
        try:
            return self._client.open_sftp()
        except Exception as e:
            raise SSHConnectionError(
                f"Failed to open SFTP channel to {self.host}",
                host=self.host,
                details=str(e)
            )
    
    def __enter__(self) -> 'SSHConnection':
        """Context manager entry."""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.disconnect()


class SSHConnectionPool:
    """
    Pool of SSH connections for reuse.
    
    Maintains a pool of connections to avoid the overhead
    of creating new connections for each operation.
    """
    
    def __init__(self, max_connections: int = 5):
        """
        Initialize connection pool.
        
        Args:
            max_connections: Maximum connections per host.
        """
        self.max_connections = max_connections
        self._pools: Dict[str, list] = {}
        self._lock = threading.Lock()
    
    def _get_pool_key(self, host: str, port: int, username: str) -> str:
        """Generate unique key for connection pool."""
        return f"{username}@{host}:{port}"
    
    @contextmanager
    def get_connection(
        self,
        host: str,
        port: int = 22,
        username: str = "root",
        password: Optional[str] = None,
        **kwargs
    ):
        """
        Get a connection from the pool.
        
        Args:
            host: Remote host.
            port: SSH port.
            username: SSH username.
            password: SSH password.
            **kwargs: Additional SSHConnection arguments.
            
        Yields:
            SSHConnection instance.
        """
        pool_key = self._get_pool_key(host, port, username)
        conn = None
        
        with self._lock:
            pool = self._pools.get(pool_key, [])
            
            # Try to get existing connection
            while pool:
                candidate = pool.pop(0)
                if candidate.is_connected:
                    conn = candidate
                    break
                else:
                    # Connection died, clean up
                    candidate.disconnect()
        
        # Create new connection if needed
        if conn is None:
            conn = SSHConnection(
                host=host,
                port=port,
                username=username,
                password=password,
                **kwargs
            )
            conn.connect()
        
        try:
            yield conn
        finally:
            # Return connection to pool if still valid
            with self._lock:
                if pool_key not in self._pools:
                    self._pools[pool_key] = []
                
                if len(self._pools[pool_key]) < self.max_connections and conn.is_connected:
                    self._pools[pool_key].append(conn)
                else:
                    conn.disconnect()
    
    def close_all(self) -> None:
        """Close all pooled connections."""
        with self._lock:
            for pool in self._pools.values():
                for conn in pool:
                    conn.disconnect()
            self._pools.clear()


# Global connection pool instance
_connection_pool: Optional[SSHConnectionPool] = None


def get_connection_pool() -> SSHConnectionPool:
    """Get the global connection pool instance."""
    global _connection_pool
    if _connection_pool is None:
        _connection_pool = SSHConnectionPool()
    return _connection_pool


def create_executor(
    ssh_connection: Optional[SSHConnection] = None,
    local: bool = False
) -> Callable[[str], str]:
    """
    Create a command executor function.
    
    Args:
        ssh_connection: SSH connection for remote execution.
        local: If True, execute commands locally.
        
    Returns:
        Executor function that takes a command string.
    """
    import subprocess
    
    if local or ssh_connection is None:
        def local_executor(cmd: str) -> str:
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                return result.stdout
            except Exception:
                return ""
        return local_executor
    else:
        def remote_executor(cmd: str) -> str:
            try:
                _, stdout, _ = ssh_connection.execute(cmd)
                return stdout
            except Exception:
                return ""
        return remote_executor
