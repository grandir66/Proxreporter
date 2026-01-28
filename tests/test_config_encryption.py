"""
Tests for configuration encryption.

Verifies that remote Proxmox credentials and other passwords
are properly encrypted when saved to config.json.
"""

import pytest
import json
import tempfile
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from proxreporter.security import SecurityManager
from proxreporter.config import Config


class TestConfigEncryption:
    """Tests for configuration password encryption."""
    
    def test_encrypt_proxmox_password(self):
        """Test that Proxmox password is encrypted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            # Simulate config with plain password
            config = {
                "proxmox": {
                    "enabled": True,
                    "host": "192.168.1.100:8006",
                    "username": "root@pam",
                    "password": "my_secret_password",
                },
                "client": {
                    "codcli": "TEST",
                    "nomecliente": "Test Client",
                }
            }
            
            # Encrypt passwords
            encrypted_config = sm.encrypt_config_passwords(config)
            
            # Verify password is encrypted
            assert encrypted_config["proxmox"]["password"].startswith("ENC:")
            # Verify other fields are not encrypted
            assert encrypted_config["proxmox"]["username"] == "root@pam"
            assert encrypted_config["proxmox"]["host"] == "192.168.1.100:8006"
    
    def test_encrypt_ssh_password(self):
        """Test that SSH password is encrypted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            config = {
                "ssh": {
                    "enabled": True,
                    "host": "192.168.1.100",
                    "port": 22,
                    "username": "root",
                    "password": "ssh_secret",
                }
            }
            
            encrypted_config = sm.encrypt_config_passwords(config)
            
            assert encrypted_config["ssh"]["password"].startswith("ENC:")
            assert encrypted_config["ssh"]["username"] == "root"
    
    def test_encrypt_sftp_password(self):
        """Test that SFTP password is encrypted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            config = {
                "sftp": {
                    "enabled": True,
                    "host": "sftp.example.com",
                    "port": 22,
                    "username": "user",
                    "password": "sftp_password",
                    "fallback_password": "fallback_pass",
                }
            }
            
            encrypted_config = sm.encrypt_config_passwords(config)
            
            assert encrypted_config["sftp"]["password"].startswith("ENC:")
            assert encrypted_config["sftp"]["fallback_password"].startswith("ENC:")
    
    def test_encrypt_smtp_password(self):
        """Test that SMTP password is encrypted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            config = {
                "smtp": {
                    "enabled": True,
                    "host": "smtp.gmail.com",
                    "port": 587,
                    "user": "user@gmail.com",
                    "password": "app_password",
                    "sender": "user@gmail.com",
                    "recipients": "admin@example.com",
                }
            }
            
            encrypted_config = sm.encrypt_config_passwords(config)
            
            assert encrypted_config["smtp"]["password"].startswith("ENC:")
            assert encrypted_config["smtp"]["user"] == "user@gmail.com"
    
    def test_full_config_encrypt_decrypt_roundtrip(self):
        """Test that full config can be encrypted and decrypted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            original_config = {
                "proxmox": {
                    "enabled": True,
                    "host": "192.168.1.100:8006",
                    "username": "root@pam",
                    "password": "proxmox_pass",
                },
                "ssh": {
                    "enabled": True,
                    "host": "192.168.1.100",
                    "password": "ssh_pass",
                },
                "sftp": {
                    "enabled": True,
                    "host": "sftp.example.com",
                    "password": "sftp_pass",
                    "fallback_password": "fallback_pass",
                },
                "smtp": {
                    "enabled": True,
                    "host": "smtp.gmail.com",
                    "password": "smtp_pass",
                },
                "client": {
                    "codcli": "TEST",
                    "nomecliente": "Test Client",
                }
            }
            
            # Encrypt
            encrypted = sm.encrypt_config_passwords(original_config)
            
            # Verify all passwords are encrypted
            assert encrypted["proxmox"]["password"].startswith("ENC:")
            assert encrypted["ssh"]["password"].startswith("ENC:")
            assert encrypted["sftp"]["password"].startswith("ENC:")
            assert encrypted["sftp"]["fallback_password"].startswith("ENC:")
            assert encrypted["smtp"]["password"].startswith("ENC:")
            
            # Decrypt
            decrypted = sm.decrypt_config(encrypted)
            
            # Verify all passwords are decrypted correctly
            assert decrypted["proxmox"]["password"] == "proxmox_pass"
            assert decrypted["ssh"]["password"] == "ssh_pass"
            assert decrypted["sftp"]["password"] == "sftp_pass"
            assert decrypted["sftp"]["fallback_password"] == "fallback_pass"
            assert decrypted["smtp"]["password"] == "smtp_pass"
    
    def test_config_save_and_load_with_encryption(self):
        """Test saving and loading config with encrypted passwords."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            key_file = Path(tmpdir) / ".secret.key"
            
            # Create security manager and generate key
            sm = SecurityManager(key_file)
            sm.load_or_generate_key()
            
            # Create config with passwords
            config_data = {
                "proxmox": {
                    "enabled": True,
                    "host": "localhost:8006",
                    "username": "root@pam",
                    "password": "test_password",
                },
                "client": {
                    "codcli": "TEST",
                    "nomecliente": "Test",
                },
                "sftp": {
                    "enabled": True,
                    "host": "sftp.example.com",
                    "port": 22,
                    "username": "user",
                    "password": "sftp_password",
                    "base_path": "/uploads",
                },
                "smtp": {
                    "enabled": False,
                }
            }
            
            # Encrypt and save
            encrypted_config = sm.encrypt_config_passwords(config_data)
            with open(config_file, 'w') as f:
                json.dump(encrypted_config, f, indent=4)
            
            # Load and verify encryption in file
            with open(config_file, 'r') as f:
                saved_config = json.load(f)
            
            assert saved_config["proxmox"]["password"].startswith("ENC:")
            assert saved_config["sftp"]["password"].startswith("ENC:")
            
            # Load with Config class (should auto-decrypt)
            loaded = Config(str(config_file))
            
            assert loaded.get("proxmox.password") == "test_password"
            assert loaded.get("sftp.password") == "sftp_password"
    
    def test_empty_password_not_encrypted(self):
        """Test that empty passwords are not encrypted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            config = {
                "proxmox": {
                    "password": "",
                },
                "ssh": {
                    "password": None,
                }
            }
            
            encrypted = sm.encrypt_config_passwords(config)
            
            # Empty/None should remain unchanged
            assert encrypted["proxmox"]["password"] == ""
            assert encrypted["ssh"]["password"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
