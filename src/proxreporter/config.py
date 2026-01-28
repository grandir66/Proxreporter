"""
Configuration management for Proxreporter.

Provides centralized configuration loading, validation, and access.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from .exceptions import ConfigurationError
from .security import SecurityManager

logger = logging.getLogger("proxreporter.config")


# Default values
DEFAULTS = {
    'sftp': {
        'host': 'sftp.domarc.it',
        'port': 11122,
        'username': 'proxmox',
        'base_path': '/home/proxmox/uploads',
    },
    'system': {
        'output_directory': '/var/log/proxreporter',
        'max_file_copies': 5,
        'log_level': 'INFO',
    },
    'features': {
        'collect_cluster': True,
        'collect_host': True,
        'collect_host_details': True,
        'collect_storage': True,
        'collect_network': True,
        'collect_vms': True,
        'collect_backup': True,
        'collect_containers': False,
        'collect_perf': False,
    },
    'proxmox': {
        'verify_ssl': False,
    },
}


class Config:
    """
    Configuration manager with validation and encryption support.
    
    Features:
    - Load from JSON file
    - Environment variable overrides
    - Automatic password decryption
    - Validation
    - Default values
    """
    
    def __init__(self, config_file: Optional[str] = None):
        """
        Initialize configuration.
        
        Args:
            config_file: Path to config.json file.
        """
        self._config: Dict[str, Any] = {}
        self._config_file: Optional[Path] = None
        self._security: Optional[SecurityManager] = None
        
        if config_file:
            self.load(config_file)
    
    def load(self, config_file: str) -> None:
        """
        Load configuration from file.
        
        Args:
            config_file: Path to config.json.
            
        Raises:
            ConfigurationError: If loading fails.
        """
        self._config_file = Path(config_file)
        
        if not self._config_file.exists():
            raise ConfigurationError(
                f"Configuration file not found: {config_file}"
            )
        
        try:
            with open(self._config_file, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
            logger.info(f"Loaded configuration from {config_file}")
        except json.JSONDecodeError as e:
            raise ConfigurationError(
                f"Invalid JSON in {config_file}: {e}"
            )
        except Exception as e:
            raise ConfigurationError(
                f"Failed to load {config_file}: {e}"
            )
        
        # Initialize security manager if key file exists
        key_file = self._config_file.parent / ".secret.key"
        if key_file.exists():
            try:
                self._security = SecurityManager(key_file)
                self._config = self._security.decrypt_config(self._config)
                logger.info("Configuration decrypted successfully")
            except Exception as e:
                logger.warning(f"Failed to decrypt configuration: {e}")
        else:
            # Check for encrypted values without key
            self._check_encrypted_without_key()
        
        # Apply defaults
        self._apply_defaults()
        
        # Apply environment overrides
        self._apply_env_overrides()
        
        # Validate
        self._validate()
    
    def _check_encrypted_without_key(self) -> None:
        """Warn if encrypted values found but no key available."""
        def check_recursive(obj: Any, path: str = "") -> List[str]:
            found = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    found.extend(check_recursive(v, f"{path}.{k}"))
            elif isinstance(obj, str) and obj.startswith("ENC:"):
                found.append(path)
            return found
        
        encrypted_fields = check_recursive(self._config)
        if encrypted_fields:
            logger.warning(
                f"Encrypted values found but no .secret.key file: "
                f"{', '.join(encrypted_fields)}"
            )
    
    def _apply_defaults(self) -> None:
        """Apply default values for missing settings."""
        def merge_defaults(config: dict, defaults: dict) -> None:
            for key, value in defaults.items():
                if key not in config:
                    config[key] = value
                elif isinstance(value, dict) and isinstance(config.get(key), dict):
                    merge_defaults(config[key], value)
        
        merge_defaults(self._config, DEFAULTS)
    
    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides."""
        env_mappings = {
            'PROXREPORTER_SFTP_HOST': ('sftp', 'host'),
            'PROXREPORTER_SFTP_PORT': ('sftp', 'port'),
            'PROXREPORTER_SFTP_USER': ('sftp', 'username'),
            'PROXREPORTER_SFTP_PASSWORD': ('sftp', 'password'),
            'PROXREPORTER_CODCLI': ('client', 'codcli'),
            'PROXREPORTER_NOMECLIENTE': ('client', 'nomecliente'),
            'PROXREPORTER_OUTPUT_DIR': ('system', 'output_directory'),
            'PROXREPORTER_LOG_LEVEL': ('system', 'log_level'),
        }
        
        for env_var, (section, key) in env_mappings.items():
            value = os.environ.get(env_var)
            if value:
                if section not in self._config:
                    self._config[section] = {}
                
                # Convert port to int
                if key == 'port':
                    try:
                        value = int(value)
                    except ValueError:
                        continue
                
                self._config[section][key] = value
                logger.debug(f"Applied env override: {env_var}")
    
    def _validate(self) -> None:
        """Validate configuration."""
        errors = []
        
        # Validate client section
        client = self._config.get('client', {})
        if not client.get('codcli'):
            errors.append("client.codcli is required")
        if not client.get('nomecliente'):
            errors.append("client.nomecliente is required")
        
        # Validate SFTP if enabled
        sftp = self._config.get('sftp', {})
        if sftp.get('enabled', True):
            if not sftp.get('host'):
                errors.append("sftp.host is required when SFTP is enabled")
            if not sftp.get('username'):
                errors.append("sftp.username is required when SFTP is enabled")
            if not sftp.get('password'):
                errors.append("sftp.password is required when SFTP is enabled")
        
        if errors:
            raise ConfigurationError(
                "Configuration validation failed:\n  - " + 
                "\n  - ".join(errors)
            )
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value.
        
        Args:
            key: Configuration key (supports dot notation: 'sftp.host').
            default: Default value if key not found.
            
        Returns:
            Configuration value.
        """
        parts = key.split('.')
        value = self._config
        
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        
        return value
    
    def get_section(self, section: str) -> Dict[str, Any]:
        """
        Get entire configuration section.
        
        Args:
            section: Section name (e.g., 'sftp', 'client').
            
        Returns:
            Section dictionary (empty if not found).
        """
        return self._config.get(section, {})
    
    @property
    def sftp(self) -> Dict[str, Any]:
        """Get SFTP configuration."""
        return self.get_section('sftp')
    
    @property
    def client(self) -> Dict[str, Any]:
        """Get client configuration."""
        return self.get_section('client')
    
    @property
    def system(self) -> Dict[str, Any]:
        """Get system configuration."""
        return self.get_section('system')
    
    @property
    def features(self) -> Dict[str, Any]:
        """Get features configuration."""
        return self.get_section('features')
    
    @property
    def proxmox(self) -> Dict[str, Any]:
        """Get Proxmox API configuration."""
        return self.get_section('proxmox')
    
    @property
    def ssh(self) -> Dict[str, Any]:
        """Get SSH configuration."""
        return self.get_section('ssh')
    
    @property
    def codcli(self) -> str:
        """Get client code."""
        return self.get('client.codcli', '')
    
    @property
    def nomecliente(self) -> str:
        """Get client name."""
        return self.get('client.nomecliente', '')
    
    @property
    def server_identifier(self) -> str:
        """Get server identifier."""
        return self.get('client.server_identifier', '')
    
    @property
    def output_directory(self) -> Path:
        """Get output directory path."""
        return Path(self.get('system.output_directory', '/var/log/proxreporter'))
    
    def is_feature_enabled(self, feature: str) -> bool:
        """
        Check if a feature is enabled.
        
        Args:
            feature: Feature name (e.g., 'collect_vms').
            
        Returns:
            True if feature is enabled.
        """
        return bool(self.get(f'features.{feature}', False))
    
    def save(self, encrypt_passwords: bool = True) -> None:
        """
        Save configuration to file.
        
        Args:
            encrypt_passwords: Whether to encrypt password fields.
            
        Raises:
            ConfigurationError: If saving fails.
        """
        if not self._config_file:
            raise ConfigurationError("No configuration file path set")
        
        config_to_save = self._config.copy()
        
        if encrypt_passwords and self._security:
            config_to_save = self._security.encrypt_config_passwords(config_to_save)
        
        try:
            with open(self._config_file, 'w', encoding='utf-8') as f:
                json.dump(config_to_save, f, indent=4)
            
            # Set restrictive permissions
            self._config_file.chmod(0o600)
            
            logger.info(f"Configuration saved to {self._config_file}")
            
        except Exception as e:
            raise ConfigurationError(f"Failed to save configuration: {e}")
    
    def to_dict(self) -> Dict[str, Any]:
        """Return configuration as dictionary."""
        return self._config.copy()
