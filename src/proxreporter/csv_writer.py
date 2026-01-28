"""
CSV writing utilities for Proxreporter.

Provides unified CSV generation with consistent formatting,
file rotation, and error handling.
"""

import csv
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from .utils import (
    ensure_directory, 
    rotate_files, 
    generate_filename,
    clean_string,
    safe_round,
)
from .exceptions import ProxreporterError

logger = logging.getLogger("proxreporter.csv_writer")


class CSVWriter:
    """
    Unified CSV writer with rotation and consistent formatting.
    
    Features:
    - Automatic file rotation
    - Consistent value formatting
    - Delimiter configuration
    - Error handling with logging
    """
    
    DEFAULT_DELIMITER = ';'
    DEFAULT_MAX_COPIES = 5
    DEFAULT_ENCODING = 'utf-8'
    
    def __init__(
        self,
        output_dir: str,
        codcli: str,
        nomecliente: str,
        server_identifier: Optional[str] = None,
        delimiter: str = DEFAULT_DELIMITER,
        max_copies: int = DEFAULT_MAX_COPIES,
    ):
        """
        Initialize CSV writer.
        
        Args:
            output_dir: Output directory for CSV files.
            codcli: Client code for filename.
            nomecliente: Client name for filename.
            server_identifier: Optional server identifier.
            delimiter: CSV field delimiter.
            max_copies: Maximum file copies for rotation.
        """
        self.output_dir = Path(output_dir)
        self.codcli = codcli
        self.nomecliente = nomecliente
        self.server_identifier = server_identifier
        self.delimiter = delimiter
        self.max_copies = max_copies
        
        # Ensure output directory exists
        ensure_directory(self.output_dir)
    
    @staticmethod
    def format_value(value: Any) -> str:
        """
        Format a value for CSV output.
        
        Args:
            value: Value to format.
            
        Returns:
            Formatted string.
        """
        if value is None:
            return 'N/A'
        
        if isinstance(value, bool):
            return 'Yes' if value else 'No'
        
        if isinstance(value, float):
            return str(safe_round(value, 2))
        
        if isinstance(value, (list, tuple)):
            return ', '.join(str(v) for v in value if v is not None)
        
        if isinstance(value, dict):
            return str(value)
        
        result = clean_string(value)
        return result if result else 'N/A'
    
    def write(
        self,
        file_type: str,
        fieldnames: List[str],
        rows: List[Dict[str, Any]],
        transform: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Optional[Path]:
        """
        Write data to CSV file.
        
        Args:
            file_type: Type of file (vms, hosts, storage, etc.).
            fieldnames: List of column names.
            rows: List of row dictionaries.
            transform: Optional function to transform each row.
            
        Returns:
            Path to written file, or None on failure.
        """
        if not rows:
            logger.warning(f"No data to write for {file_type}")
            return None
        
        # Generate filename
        filename = generate_filename(
            self.codcli,
            self.nomecliente,
            file_type,
            'csv',
            self.server_identifier
        )
        
        filepath = self.output_dir / filename
        
        # Rotate existing files
        rotate_files(self.output_dir, filename, self.max_copies)
        
        try:
            with open(filepath, 'w', newline='', encoding=self.DEFAULT_ENCODING) as f:
                writer = csv.DictWriter(
                    f, 
                    fieldnames=fieldnames,
                    delimiter=self.delimiter,
                    extrasaction='ignore'
                )
                writer.writeheader()
                
                for row in rows:
                    # Apply transformation if provided
                    if transform:
                        row = transform(row)
                    
                    # Format all values
                    formatted_row = {
                        field: self.format_value(row.get(field))
                        for field in fieldnames
                    }
                    writer.writerow(formatted_row)
            
            file_size = filepath.stat().st_size / 1024
            logger.info(f"Written {len(rows)} rows to {filename} ({file_size:.1f} KB)")
            
            return filepath
            
        except Exception as e:
            logger.error(f"Failed to write {filename}: {e}")
            return None
    
    def write_vms(self, vms: List[Dict[str, Any]]) -> Optional[Path]:
        """
        Write VM data to CSV.
        
        Args:
            vms: List of VM dictionaries.
            
        Returns:
            Path to written file.
        """
        fieldnames = [
            'vmid', 'name', 'type', 'status', 'node',
            'cpus', 'memory_gb', 'disk_gb',
            'os_type', 'ip_addresses', 'mac_addresses',
            'created', 'uptime', 'description',
            'codcli', 'nomecliente', 'server_identifier',
        ]
        
        def transform(vm: Dict[str, Any]) -> Dict[str, Any]:
            return {
                **vm,
                'codcli': self.codcli,
                'nomecliente': self.nomecliente,
                'server_identifier': self.server_identifier,
            }
        
        return self.write('vms', fieldnames, vms, transform)
    
    def write_hosts(self, hosts: List[Dict[str, Any]]) -> Optional[Path]:
        """
        Write host data to CSV.
        
        Args:
            hosts: List of host dictionaries.
            
        Returns:
            Path to written file.
        """
        fieldnames = [
            'hostname', 'fqdn', 'ip_address',
            'cpu_model', 'cpu_cores', 'cpu_sockets', 'cpu_threads',
            'memory_total_gb', 'memory_used_gb', 'memory_usage_percent',
            'pve_version', 'kernel_version',
            'uptime_seconds', 'uptime_human',
            'lic_status', 'lic_level', 'lic_key',
            'codcli', 'nomecliente', 'server_identifier',
        ]
        
        def transform(host: Dict[str, Any]) -> Dict[str, Any]:
            return {
                **host,
                'codcli': self.codcli,
                'nomecliente': self.nomecliente,
                'server_identifier': self.server_identifier,
            }
        
        return self.write('hosts', fieldnames, hosts, transform)
    
    def write_storage(self, storage_items: List[Dict[str, Any]]) -> Optional[Path]:
        """
        Write storage data to CSV.
        
        Args:
            storage_items: List of storage dictionaries.
            
        Returns:
            Path to written file.
        """
        fieldnames = [
            'server_identifier', 'hostname', 'storage_name', 'storage_type',
            'total_gb', 'used_gb', 'available_gb', 'usage_percent',
            'content', 'shared', 'active',
            'codcli', 'nomecliente',
        ]
        
        def transform(storage: Dict[str, Any]) -> Dict[str, Any]:
            return {
                **storage,
                'codcli': self.codcli,
                'nomecliente': self.nomecliente,
                'server_identifier': self.server_identifier,
            }
        
        return self.write('storage', fieldnames, storage_items, transform)
    
    def write_network(self, interfaces: List[Dict[str, Any]]) -> Optional[Path]:
        """
        Write network interface data to CSV.
        
        Args:
            interfaces: List of interface dictionaries.
            
        Returns:
            Path to written file.
        """
        fieldnames = [
            'server_identifier', 'hostname', 'interface_name', 'interface_type',
            'mac_address', 'ip_addresses', 'gateway',
            'bridge_ports', 'vlan_id',
            'mtu', 'speed_mbps', 'state',
            'codcli', 'nomecliente',
        ]
        
        def transform(iface: Dict[str, Any]) -> Dict[str, Any]:
            return {
                **iface,
                'codcli': self.codcli,
                'nomecliente': self.nomecliente,
                'server_identifier': self.server_identifier,
            }
        
        return self.write('network', fieldnames, interfaces, transform)


def write_csv_simple(
    filepath: str,
    fieldnames: List[str],
    rows: List[Dict[str, Any]],
    delimiter: str = ';',
) -> bool:
    """
    Simple CSV writer without rotation or formatting.
    
    Args:
        filepath: Output file path.
        fieldnames: Column names.
        rows: Row data.
        delimiter: Field delimiter.
        
    Returns:
        True on success.
    """
    try:
        filepath = Path(filepath)
        ensure_directory(filepath.parent)
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(
                f, 
                fieldnames=fieldnames,
                delimiter=delimiter,
                extrasaction='ignore'
            )
            writer.writeheader()
            writer.writerows(rows)
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to write CSV {filepath}: {e}")
        return False
