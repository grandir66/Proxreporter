"""
Tests for CSV writer module.
"""

import pytest
import tempfile
import csv
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from proxreporter.csv_writer import CSVWriter, write_csv_simple


class TestCSVWriter:
    """Tests for CSVWriter class."""
    
    def test_format_value_none(self):
        assert CSVWriter.format_value(None) == 'N/A'
    
    def test_format_value_bool(self):
        assert CSVWriter.format_value(True) == 'Yes'
        assert CSVWriter.format_value(False) == 'No'
    
    def test_format_value_float(self):
        assert CSVWriter.format_value(3.14159) == '3.14'
    
    def test_format_value_list(self):
        assert CSVWriter.format_value([1, 2, 3]) == '1, 2, 3'
        # Empty list returns empty string which gets formatted
        result = CSVWriter.format_value([])
        assert result in ['', 'N/A']  # Implementation may vary
    
    def test_format_value_string(self):
        assert CSVWriter.format_value("hello") == 'hello'
        assert CSVWriter.format_value("  spaced  ") == 'spaced'
        assert CSVWriter.format_value("") == 'N/A'
    
    def test_write_basic(self):
        """Test basic CSV writing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = CSVWriter(
                output_dir=tmpdir,
                codcli="TEST",
                nomecliente="Client",
            )
            
            fieldnames = ['id', 'name', 'value']
            rows = [
                {'id': 1, 'name': 'test1', 'value': 100},
                {'id': 2, 'name': 'test2', 'value': 200},
            ]
            
            filepath = writer.write('test', fieldnames, rows)
            
            assert filepath is not None
            assert filepath.exists()
            
            # Verify content
            with open(filepath, 'r') as f:
                reader = csv.DictReader(f, delimiter=';')
                read_rows = list(reader)
                
            assert len(read_rows) == 2
            assert read_rows[0]['id'] == '1'
            assert read_rows[0]['name'] == 'test1'
    
    def test_write_empty_rows(self):
        """Test writing with empty rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = CSVWriter(
                output_dir=tmpdir,
                codcli="TEST",
                nomecliente="Client",
            )
            
            filepath = writer.write('empty', ['id', 'name'], [])
            
            assert filepath is None
    
    def test_write_vms(self):
        """Test VM CSV writing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = CSVWriter(
                output_dir=tmpdir,
                codcli="TEST",
                nomecliente="Client",
                server_identifier="pve1",
            )
            
            vms = [
                {
                    'vmid': 100,
                    'name': 'vm-test',
                    'type': 'qemu',
                    'status': 'running',
                    'node': 'pve1',
                    'cpus': 4,
                    'memory_gb': 8.0,
                },
            ]
            
            filepath = writer.write_vms(vms)
            
            assert filepath is not None
            assert 'vms' in filepath.name
    
    def test_write_with_rotation(self):
        """Test file rotation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = CSVWriter(
                output_dir=tmpdir,
                codcli="TEST",
                nomecliente="Client",
                max_copies=3,
            )
            
            fieldnames = ['id']
            
            # Write multiple times
            for i in range(5):
                rows = [{'id': i}]
                writer.write('rotation', fieldnames, rows)
            
            # Check rotated files exist
            output_dir = Path(tmpdir)
            files = list(output_dir.glob('*rotation*'))
            
            # Should have main file + rotated copies (up to max_copies)
            assert len(files) <= 3


class TestWriteCSVSimple:
    """Tests for simple CSV writer function."""
    
    def test_write_simple(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.csv"
            
            success = write_csv_simple(
                str(filepath),
                ['a', 'b'],
                [{'a': 1, 'b': 2}],
            )
            
            assert success is True
            assert filepath.exists()
    
    def test_write_simple_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "subdir" / "test.csv"
            
            success = write_csv_simple(
                str(filepath),
                ['a'],
                [{'a': 1}],
            )
            
            assert success is True
            assert filepath.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
