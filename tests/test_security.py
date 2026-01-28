"""
Tests for security module.
"""

import pytest
import tempfile
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from proxreporter.security import (
    SecurityManager,
    mask_password,
    escape_shell_arg,
)
from proxreporter.exceptions import EncryptionError, DecryptionError


class TestSecurityManager:
    """Tests for SecurityManager class."""
    
    def test_encrypt_decrypt_roundtrip(self):
        """Test that encrypt->decrypt returns original value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            original = "my_secret_password"
            encrypted = sm.encrypt(original)
            
            # Should have ENC: prefix
            assert encrypted.startswith("ENC:")
            
            # Should decrypt to original
            decrypted = sm.decrypt(encrypted)
            assert decrypted == original
    
    def test_encrypt_empty_string(self):
        """Test encrypting empty string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            assert sm.encrypt("") == ""
    
    def test_decrypt_without_prefix(self):
        """Test decrypting value without ENC: prefix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            # Encrypt first
            encrypted = sm.encrypt("test")
            # Remove prefix
            without_prefix = encrypted[4:]
            
            # Should still decrypt
            decrypted = sm.decrypt(without_prefix)
            assert decrypted == "test"
    
    def test_is_encrypted(self):
        """Test is_encrypted detection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            encrypted = sm.encrypt("test")
            
            assert sm.is_encrypted(encrypted) is True
            assert sm.is_encrypted("plain_text") is False
            assert sm.is_encrypted("") is False
    
    def test_decrypt_config(self):
        """Test recursive config decryption."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            config = {
                'sftp': {
                    'host': 'example.com',
                    'password': sm.encrypt('secret123'),
                },
                'plain': 'not_encrypted',
            }
            
            decrypted = sm.decrypt_config(config)
            
            assert decrypted['sftp']['host'] == 'example.com'
            assert decrypted['sftp']['password'] == 'secret123'
            assert decrypted['plain'] == 'not_encrypted'
    
    def test_encrypt_config_passwords(self):
        """Test config password encryption."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            sm = SecurityManager(key_file)
            
            config = {
                'sftp': {
                    'host': 'example.com',
                    'password': 'secret123',
                },
            }
            
            encrypted = sm.encrypt_config_passwords(config)
            
            assert encrypted['sftp']['host'] == 'example.com'
            assert encrypted['sftp']['password'].startswith('ENC:')
    
    def test_key_persistence(self):
        """Test that key persists across instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".secret.key"
            
            # Create first instance and encrypt
            sm1 = SecurityManager(key_file)
            encrypted = sm1.encrypt("test")
            
            # Create second instance
            sm2 = SecurityManager(key_file)
            decrypted = sm2.decrypt(encrypted)
            
            assert decrypted == "test"


class TestPasswordMasking:
    """Tests for password masking."""
    
    def test_mask_password_empty(self):
        assert mask_password("") == "<empty>"
        assert mask_password(None) == "<empty>"
    
    def test_mask_password_no_visible(self):
        # Should return fixed length mask
        assert mask_password("password") == "********"
        assert mask_password("short") == "********"
    
    def test_mask_password_with_visible(self):
        result = mask_password("password123", visible_chars=2)
        assert result.startswith("pa")
        assert result.endswith("23")
        assert "****" in result


class TestShellEscape:
    """Tests for shell argument escaping."""
    
    def test_escape_simple(self):
        # shlex.quote only adds quotes when necessary
        result = escape_shell_arg("hello")
        assert result == "hello" or result == "'hello'"
    
    def test_escape_with_spaces(self):
        assert escape_shell_arg("hello world") == "'hello world'"
    
    def test_escape_with_quotes(self):
        result = escape_shell_arg("it's a test")
        # Should be properly escaped
        assert "'" in result or '"' in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
