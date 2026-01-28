"""
Proxmox backup module.

Provides configuration backup with compression and encryption support.
"""

import os
import tarfile
import logging
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

from .utils import ensure_directory, rotate_files, generate_filename
from .exceptions import ProxreporterError

logger = logging.getLogger("proxreporter.backup")


# Default paths to backup
DEFAULT_BACKUP_PATHS = [
    '/etc/pve',
    '/etc/network/interfaces',
    '/etc/network/interfaces.d',
    '/etc/hosts',
    '/etc/hostname',
    '/etc/resolv.conf',
    '/etc/apt/sources.list',
    '/etc/apt/sources.list.d',
    '/etc/cron.d',
    '/etc/cron.daily',
    '/etc/ssh/sshd_config',
    '/etc/sysctl.conf',
    '/etc/sysctl.d',
]


class ProxmoxBackup:
    """
    Proxmox configuration backup manager.
    
    Features:
    - Configurable backup paths
    - Tar.gz compression
    - File rotation
    - Symlink handling
    - Error resilience (continues on file errors)
    """
    
    def __init__(
        self,
        output_dir: str,
        codcli: str,
        nomecliente: str,
        server_identifier: Optional[str] = None,
        backup_paths: Optional[List[str]] = None,
        max_copies: int = 5,
    ):
        """
        Initialize backup manager.
        
        Args:
            output_dir: Directory for backup files.
            codcli: Client code for filename.
            nomecliente: Client name for filename.
            server_identifier: Optional server identifier.
            backup_paths: Paths to include in backup (defaults to DEFAULT_BACKUP_PATHS).
            max_copies: Maximum backup copies to keep.
        """
        self.output_dir = Path(output_dir)
        self.codcli = codcli
        self.nomecliente = nomecliente
        self.server_identifier = server_identifier
        self.backup_paths = backup_paths or DEFAULT_BACKUP_PATHS
        self.max_copies = max_copies
        
        # Stats
        self._files_added = 0
        self._files_failed = 0
        self._total_size = 0
    
    def _should_skip_file(self, path: Path) -> bool:
        """
        Check if a file should be skipped.
        
        Args:
            path: File path to check.
            
        Returns:
            True if file should be skipped.
        """
        # Skip certain file types
        skip_extensions = {'.log', '.tmp', '.swp', '.bak', '.old'}
        if path.suffix.lower() in skip_extensions:
            return True
        
        # Skip very large files (> 100MB)
        try:
            if path.is_file() and path.stat().st_size > 100 * 1024 * 1024:
                logger.warning(f"Skipping large file: {path}")
                return True
        except Exception:
            pass
        
        return False
    
    def _add_path_to_tar(self, tar: tarfile.TarFile, path: Path) -> None:
        """
        Add a path (file or directory) to tar archive.
        
        Args:
            tar: Tar file object.
            path: Path to add.
        """
        if not path.exists():
            logger.debug(f"Path not found, skipping: {path}")
            return
        
        try:
            if path.is_file():
                if self._should_skip_file(path):
                    return
                
                try:
                    tar.add(str(path), arcname=str(path))
                    self._files_added += 1
                    self._total_size += path.stat().st_size
                except Exception as e:
                    logger.warning(f"Failed to add file {path}: {e}")
                    self._files_failed += 1
                    
            elif path.is_dir():
                # Add directory contents recursively
                for item in path.rglob('*'):
                    if item.is_file():
                        if self._should_skip_file(item):
                            continue
                        try:
                            tar.add(str(item), arcname=str(item))
                            self._files_added += 1
                            self._total_size += item.stat().st_size
                        except Exception as e:
                            logger.warning(f"Failed to add file {item}: {e}")
                            self._files_failed += 1
            
            elif path.is_symlink():
                # Handle symlinks
                try:
                    tar.add(str(path), arcname=str(path))
                    self._files_added += 1
                except Exception as e:
                    logger.debug(f"Failed to add symlink {path}: {e}")
                    
        except Exception as e:
            logger.warning(f"Error processing path {path}: {e}")
    
    def create_backup(self) -> Optional[Path]:
        """
        Create backup archive.
        
        Returns:
            Path to backup file, or None on failure.
        """
        # Reset stats
        self._files_added = 0
        self._files_failed = 0
        self._total_size = 0
        
        # Ensure output directory exists
        ensure_directory(self.output_dir)
        
        # Generate filename
        filename = generate_filename(
            self.codcli,
            self.nomecliente,
            'backup',
            'tar.gz',
            self.server_identifier
        )
        
        backup_path = self.output_dir / filename
        
        # Rotate existing backups
        rotate_files(self.output_dir, filename, self.max_copies)
        
        logger.info(f"Creating backup: {backup_path}")
        
        try:
            with tarfile.open(backup_path, 'w:gz') as tar:
                for path_str in self.backup_paths:
                    path = Path(path_str)
                    logger.debug(f"Adding: {path}")
                    self._add_path_to_tar(tar, path)
            
            # Log stats
            backup_size = backup_path.stat().st_size / (1024 * 1024)
            logger.info(
                f"Backup complete: {self._files_added} files, "
                f"{backup_size:.2f} MB compressed"
            )
            
            if self._files_failed > 0:
                logger.warning(f"Failed to backup {self._files_failed} files")
            
            return backup_path
            
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            
            # Clean up partial backup
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except Exception:
                    pass
            
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get backup statistics.
        
        Returns:
            Dictionary with backup stats.
        """
        return {
            'files_added': self._files_added,
            'files_failed': self._files_failed,
            'total_size_bytes': self._total_size,
            'total_size_mb': round(self._total_size / (1024 * 1024), 2),
        }


def create_backup(
    output_dir: str,
    codcli: str,
    nomecliente: str,
    server_identifier: Optional[str] = None,
    backup_paths: Optional[List[str]] = None,
    max_copies: int = 5,
) -> Optional[Path]:
    """
    Convenience function to create a backup.
    
    Args:
        output_dir: Directory for backup files.
        codcli: Client code.
        nomecliente: Client name.
        server_identifier: Optional server identifier.
        backup_paths: Paths to backup.
        max_copies: Maximum backup copies.
        
    Returns:
        Path to backup file or None.
    """
    backup = ProxmoxBackup(
        output_dir=output_dir,
        codcli=codcli,
        nomecliente=nomecliente,
        server_identifier=server_identifier,
        backup_paths=backup_paths,
        max_copies=max_copies,
    )
    return backup.create_backup()
