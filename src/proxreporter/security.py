"""
Security module for Proxreporter.

Provides secure password encryption/decryption, key management,
and secure command execution.
"""

import os
import logging
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List, Union

from .exceptions import EncryptionError, DecryptionError, ConfigurationError

logger = logging.getLogger("proxreporter.security")


class SecurityManager:
    """
    Manages encryption and decryption of sensitive data.
    
    Uses Fernet (AES-128-CBC) for symmetric encryption.
    Key is stored in a separate file with restricted permissions.
    """
    
    ENC_PREFIX = "ENC:"
    KEY_FILE_PERMISSIONS = 0o600
    
    def __init__(self, key_file: Optional[Path] = None):
        """
        Initialize SecurityManager.
        
        Args:
            key_file: Path to the encryption key file. 
                      If None, uses .secret.key in current directory.
        """
        self.key_file = Path(key_file) if key_file else Path(".secret.key")
        self._cipher = None
        self._fernet = None
    
    @property
    def cipher(self):
        """Lazy-load the Fernet cipher."""
        if self._cipher is None:
            self.load_or_generate_key()
        return self._cipher
    
    def load_or_generate_key(self) -> None:
        """
        Load existing key or generate a new one.
        
        The key file is created with restricted permissions (0600).
        
        Raises:
            EncryptionError: If key loading/generation fails.
        """
        try:
            from cryptography.fernet import Fernet
            self._fernet = Fernet
            
            if self.key_file.exists():
                # Load existing key
                with open(self.key_file, "rb") as f:
                    key = f.read().strip()
                if not key:
                    raise EncryptionError(f"Key file {self.key_file} is empty")
                self._cipher = Fernet(key)
                logger.debug(f"Loaded encryption key from {self.key_file}")
            else:
                # Generate new key with atomic file creation
                key = Fernet.generate_key()
                
                # Create file with restricted permissions atomically
                fd = os.open(
                    str(self.key_file),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    self.KEY_FILE_PERMISSIONS
                )
                try:
                    os.write(fd, key)
                finally:
                    os.close(fd)
                
                self._cipher = Fernet(key)
                logger.info(f"Generated new encryption key: {self.key_file}")
                
        except ImportError:
            raise EncryptionError(
                "cryptography library not installed. "
                "Install with: pip install cryptography"
            )
        except FileExistsError:
            # Race condition: another process created the file
            # Try loading the existing key
            self.load_or_generate_key()
        except Exception as e:
            raise EncryptionError(f"Failed to initialize encryption: {e}")
    
    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a string.
        
        Args:
            plaintext: The string to encrypt.
            
        Returns:
            Encrypted string with ENC: prefix.
            
        Raises:
            EncryptionError: If encryption fails.
        """
        if not plaintext:
            return ""
        
        # Already encrypted
        if plaintext.startswith(self.ENC_PREFIX):
            return plaintext
        
        try:
            encrypted = self.cipher.encrypt(plaintext.encode())
            return f"{self.ENC_PREFIX}{encrypted.decode()}"
        except Exception as e:
            raise EncryptionError(f"Encryption failed: {e}")
    
    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt a string.
        
        Args:
            ciphertext: The encrypted string (with or without ENC: prefix).
            
        Returns:
            Decrypted plaintext string.
            
        Raises:
            DecryptionError: If decryption fails.
        """
        if not ciphertext:
            return ""
        
        # Remove prefix if present
        if ciphertext.startswith(self.ENC_PREFIX):
            ciphertext = ciphertext[len(self.ENC_PREFIX):]
        
        try:
            decrypted = self.cipher.decrypt(ciphertext.encode())
            return decrypted.decode()
        except Exception as e:
            raise DecryptionError(f"Decryption failed: {e}")
    
    def is_encrypted(self, value: str) -> bool:
        """Check if a value is encrypted."""
        return bool(value) and value.startswith(self.ENC_PREFIX)
    
    def decrypt_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recursively decrypt all encrypted values in a config dictionary.
        
        Args:
            config: Configuration dictionary with potentially encrypted values.
            
        Returns:
            New dictionary with decrypted values.
        """
        def decrypt_recursive(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: decrypt_recursive(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decrypt_recursive(v) for v in obj]
            elif isinstance(obj, str) and self.is_encrypted(obj):
                try:
                    return self.decrypt(obj)
                except DecryptionError as e:
                    logger.warning(f"Failed to decrypt value: {e}")
                    return obj
            return obj
        
        return decrypt_recursive(config)
    
    def encrypt_config_passwords(self, config: Dict[str, Any], 
                                  password_fields: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Encrypt password fields in a config dictionary.
        
        Args:
            config: Configuration dictionary.
            password_fields: List of field names to encrypt. 
                           Defaults to ['password', 'fallback_password'].
                           
        Returns:
            New dictionary with encrypted password fields.
        """
        if password_fields is None:
            password_fields = ['password', 'fallback_password']
        
        def encrypt_recursive(obj: Any) -> Any:
            if isinstance(obj, dict):
                result = {}
                for k, v in obj.items():
                    if k in password_fields and isinstance(v, str) and v:
                        if not self.is_encrypted(v):
                            result[k] = self.encrypt(v)
                        else:
                            result[k] = v
                    else:
                        result[k] = encrypt_recursive(v)
                return result
            elif isinstance(obj, list):
                return [encrypt_recursive(v) for v in obj]
            return obj
        
        return encrypt_recursive(config)


def run_command_secure(
    cmd: Union[str, List[str]],
    password: Optional[str] = None,
    timeout: int = 30,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    capture_output: bool = True
) -> subprocess.CompletedProcess:
    """
    Execute a command securely without exposing passwords in process list.
    
    If a password is provided, it's passed via stdin or environment variable,
    never as a command-line argument.
    
    Args:
        cmd: Command to execute (string or list of arguments).
        password: Password to pass securely (via stdin if needed).
        timeout: Command timeout in seconds.
        env: Additional environment variables.
        cwd: Working directory.
        capture_output: Whether to capture stdout/stderr.
        
    Returns:
        CompletedProcess with return code, stdout, stderr.
        
    Raises:
        subprocess.TimeoutExpired: If command times out.
        subprocess.CalledProcessError: If command fails.
    """
    # Build environment
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    
    # Convert string command to list (safer)
    if isinstance(cmd, str):
        cmd_list = shlex.split(cmd)
    else:
        cmd_list = list(cmd)
    
    # Run command
    result = subprocess.run(
        cmd_list,
        input=password.encode() if password else None,
        capture_output=capture_output,
        timeout=timeout,
        env=run_env,
        cwd=cwd,
        text=not password  # Use text mode only if no password (binary stdin)
    )
    
    return result


def escape_shell_arg(arg: str) -> str:
    """
    Safely escape a string for use in shell commands.
    
    Args:
        arg: The argument to escape.
        
    Returns:
        Shell-escaped string.
    """
    return shlex.quote(arg)


def mask_password(password: str, visible_chars: int = 0) -> str:
    """
    Mask a password for safe logging.
    
    Args:
        password: The password to mask.
        visible_chars: Number of characters to show (0 = show none).
        
    Returns:
        Masked password string.
    """
    if not password:
        return "<empty>"
    
    if visible_chars <= 0:
        return "*" * 8  # Fixed length to not reveal password length
    
    if len(password) <= visible_chars * 2:
        return "*" * len(password)
    
    return password[:visible_chars] + "*" * 4 + password[-visible_chars:]
