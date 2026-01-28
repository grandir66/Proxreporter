"""
Tests for utility functions.
"""

import pytest
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from proxreporter.utils import (
    safe_round,
    safe_int,
    safe_float,
    safe_divide,
    calculate_percentage,
    bytes_to_gib,
    bytes_to_mib,
    format_size,
    seconds_to_human,
    clean_string,
    sanitize_filename,
    truncate_string,
    validate_cron_expression,
    generate_filename,
    is_valid_hostname,
    is_valid_ip,
)


class TestNumericUtilities:
    """Tests for numeric utility functions."""
    
    def test_safe_round_valid(self):
        assert safe_round(3.14159, 2) == 3.14
        assert safe_round(10.0, 0) == 10.0
        
    def test_safe_round_none(self):
        assert safe_round(None) is None
        
    def test_safe_round_invalid(self):
        assert safe_round("abc") is None
        assert safe_round([1, 2, 3]) is None
    
    def test_safe_int_valid(self):
        assert safe_int(42) == 42
        assert safe_int("42") == 42
        assert safe_int(42.9) == 42
        
    def test_safe_int_invalid(self):
        assert safe_int(None) == 0
        assert safe_int("abc") == 0
        assert safe_int("abc", default=-1) == -1
    
    def test_safe_float_valid(self):
        assert safe_float(3.14) == 3.14
        assert safe_float("3.14") == 3.14
        
    def test_safe_float_invalid(self):
        assert safe_float(None) == 0.0
        assert safe_float("abc") == 0.0
        assert safe_float("abc", default=-1.0) == -1.0
    
    def test_safe_divide_valid(self):
        assert safe_divide(10, 2) == 5.0
        assert safe_divide(10, 3) == pytest.approx(3.333, rel=0.01)
        
    def test_safe_divide_by_zero(self):
        assert safe_divide(10, 0) == 0.0
        assert safe_divide(10, 0, default=-1) == -1
    
    def test_calculate_percentage_valid(self):
        assert calculate_percentage(50, 100) == 50.0
        assert calculate_percentage(1, 3, decimals=1) == pytest.approx(33.3, rel=0.01)
        
    def test_calculate_percentage_zero_total(self):
        assert calculate_percentage(10, 0) is None


class TestSizeConversions:
    """Tests for size conversion functions."""
    
    def test_bytes_to_gib(self):
        assert bytes_to_gib(1073741824) == 1.0  # 1 GiB
        assert bytes_to_gib(2147483648) == 2.0  # 2 GiB
        assert bytes_to_gib(None) is None
        
    def test_bytes_to_mib(self):
        assert bytes_to_mib(1048576) == 1.0  # 1 MiB
        assert bytes_to_mib(None) is None
    
    def test_format_size(self):
        assert format_size(1024) == "1.00 KiB"
        assert format_size(1048576) == "1.00 MiB"
        assert format_size(1073741824) == "1.00 GiB"
        assert format_size(None) == "N/A"


class TestTimeConversions:
    """Tests for time conversion functions."""
    
    def test_seconds_to_human(self):
        assert seconds_to_human(60) == "1m"
        # Implementation may omit zero values
        assert "1h" in seconds_to_human(3600)
        assert "1d" in seconds_to_human(86400)
        assert "1d" in seconds_to_human(90061) and "1h" in seconds_to_human(90061)
        
    def test_seconds_to_human_invalid(self):
        assert seconds_to_human(None) == "N/A"
        assert seconds_to_human(-1) == "N/A"
        assert seconds_to_human("abc") == "N/A"


class TestStringUtilities:
    """Tests for string utility functions."""
    
    def test_clean_string(self):
        assert clean_string("  hello  ") == "hello"
        assert clean_string(None) == ""
        assert clean_string("") == ""
        assert clean_string("", default="N/A") == "N/A"
    
    def test_sanitize_filename(self):
        assert sanitize_filename("hello world") == "hello_world"
        assert sanitize_filename("test/file:name") == "test_file_name"
        assert sanitize_filename("__test__") == "test"
        
    def test_truncate_string(self):
        assert truncate_string("hello", 10) == "hello"
        assert truncate_string("hello world", 8) == "hello..."
        # With 2-char suffix, 8 chars means 6 content + ".."
        assert truncate_string("hello world", 8, suffix="..") == "hello .."


class TestValidation:
    """Tests for validation functions."""
    
    def test_validate_cron_expression_valid(self):
        assert validate_cron_expression("* * * * *") is True
        assert validate_cron_expression("0 0 * * *") is True
        assert validate_cron_expression("30 4 * * 0") is True
        assert validate_cron_expression("0 0 1 1 *") is True
        
    def test_validate_cron_expression_invalid(self):
        assert validate_cron_expression("* * *") is False  # Too few fields
        assert validate_cron_expression("60 * * * *") is False  # Invalid minute
        assert validate_cron_expression("* 25 * * *") is False  # Invalid hour
    
    def test_is_valid_hostname(self):
        assert is_valid_hostname("server1") is True
        assert is_valid_hostname("server-1.example.com") is True
        assert is_valid_hostname("") is False
        assert is_valid_hostname("-invalid") is False
        
    def test_is_valid_ip(self):
        assert is_valid_ip("192.168.1.1") is True
        assert is_valid_ip("::1") is True
        assert is_valid_ip("invalid") is False


class TestFilenameGeneration:
    """Tests for filename generation."""
    
    def test_generate_filename_basic(self):
        filename = generate_filename("CLI001", "TestClient", "vms")
        assert filename == "CLI001_TestClient_prox_vms.csv"
        
    def test_generate_filename_with_server(self):
        filename = generate_filename("CLI001", "TestClient", "hosts", 
                                     server_identifier="pve-node1")
        assert filename == "CLI001_TestClient_pve-node1_prox_hosts.csv"
        
    def test_generate_filename_with_extension(self):
        filename = generate_filename("CLI001", "TestClient", "backup", 
                                     extension="tar.gz")
        assert filename == "CLI001_TestClient_prox_backup.tar.gz"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
