"""
Utility functions for Proxreporter.

Provides common helper functions used across the application.
"""

import os
import re
import fcntl
import socket
import logging
from pathlib import Path
from datetime import timedelta
from typing import Optional, Any, Union, Callable
from contextlib import contextmanager

from .exceptions import LockError, ValidationError

logger = logging.getLogger("proxreporter.utils")


# ============================================================================
# NUMERIC UTILITIES
# ============================================================================

def safe_round(value: Any, decimals: int = 2) -> Optional[float]:
    """
    Safely round a value, handling None and non-numeric types.
    
    Args:
        value: Value to round.
        decimals: Number of decimal places.
        
    Returns:
        Rounded float or None if value is not numeric.
    """
    if value is None:
        return None
    try:
        return round(float(value), decimals)
    except (ValueError, TypeError):
        return None


def safe_int(value: Any, default: int = 0) -> int:
    """
    Safely convert a value to int.
    
    Args:
        value: Value to convert.
        default: Default value if conversion fails.
        
    Returns:
        Integer value or default.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Safely convert a value to float.
    
    Args:
        value: Value to convert.
        default: Default value if conversion fails.
        
    Returns:
        Float value or default.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """
    Safely divide two numbers, avoiding division by zero.
    
    Args:
        numerator: The numerator.
        denominator: The denominator.
        default: Value to return if denominator is zero.
        
    Returns:
        Result of division or default.
    """
    if denominator == 0:
        return default
    return numerator / denominator


def calculate_percentage(part: float, total: float, decimals: int = 2) -> Optional[float]:
    """
    Calculate percentage safely.
    
    Args:
        part: The part value.
        total: The total value.
        decimals: Number of decimal places.
        
    Returns:
        Percentage value or None if total is zero.
    """
    if total == 0:
        return None
    return safe_round((part / total) * 100, decimals)


# ============================================================================
# SIZE CONVERSIONS
# ============================================================================

def bytes_to_gib(value: Any) -> Optional[float]:
    """
    Convert bytes to GiB (gibibytes).
    
    Args:
        value: Value in bytes.
        
    Returns:
        Value in GiB or None.
    """
    if value is None:
        return None
    try:
        return round(float(value) / (1024 ** 3), 2)
    except (ValueError, TypeError):
        return None


def bytes_to_mib(value: Any) -> Optional[float]:
    """
    Convert bytes to MiB (mebibytes).
    
    Args:
        value: Value in bytes.
        
    Returns:
        Value in MiB or None.
    """
    if value is None:
        return None
    try:
        return round(float(value) / (1024 ** 2), 2)
    except (ValueError, TypeError):
        return None


def gib_to_bytes(value: Any) -> Optional[int]:
    """
    Convert GiB to bytes.
    
    Args:
        value: Value in GiB.
        
    Returns:
        Value in bytes or None.
    """
    if value is None:
        return None
    try:
        return int(float(value) * (1024 ** 3))
    except (ValueError, TypeError):
        return None


def format_size(bytes_value: int, decimals: int = 2) -> str:
    """
    Format bytes to human-readable string.
    
    Args:
        bytes_value: Size in bytes.
        decimals: Number of decimal places.
        
    Returns:
        Human-readable size string (e.g., "1.5 GiB").
    """
    if bytes_value is None:
        return "N/A"
    
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']:
        if abs(bytes_value) < 1024.0:
            return f"{bytes_value:.{decimals}f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.{decimals}f} EiB"


# ============================================================================
# TIME CONVERSIONS
# ============================================================================

def seconds_to_human(seconds: Any) -> str:
    """
    Convert seconds to human-readable duration.
    
    Args:
        seconds: Duration in seconds.
        
    Returns:
        Human-readable string (e.g., "5d 3h 20m").
    """
    if seconds is None:
        return "N/A"
    
    try:
        seconds = int(seconds)
    except (ValueError, TypeError):
        return "N/A"
    
    if seconds < 0:
        return "N/A"
    
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or not parts:
        parts.append(f"{minutes}m")
    
    return " ".join(parts)


# ============================================================================
# STRING UTILITIES
# ============================================================================

def clean_string(value: Any, default: str = "") -> str:
    """
    Clean a string value, handling None and whitespace.
    
    Args:
        value: Value to clean.
        default: Default if value is None/empty.
        
    Returns:
        Cleaned string.
    """
    if value is None:
        return default
    result = str(value).strip()
    return result if result else default


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for use as a filename.
    
    Args:
        name: The name to sanitize.
        
    Returns:
        Safe filename string.
    """
    # Replace spaces and special chars with underscores
    safe = re.sub(r'[^\w\-.]', '_', name)
    # Remove multiple underscores
    safe = re.sub(r'_+', '_', safe)
    # Remove leading/trailing underscores
    return safe.strip('_')


def truncate_string(value: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate a string to max length with suffix.
    
    Args:
        value: String to truncate.
        max_length: Maximum length including suffix.
        suffix: Suffix to add when truncating.
        
    Returns:
        Truncated string.
    """
    if len(value) <= max_length:
        return value
    return value[:max_length - len(suffix)] + suffix


# ============================================================================
# FILE LOCKING
# ============================================================================

@contextmanager
def file_lock(lock_path: Union[str, Path], timeout: Optional[float] = None):
    """
    Context manager for exclusive file locking.
    
    Args:
        lock_path: Path to the lock file.
        timeout: Optional timeout in seconds (None = non-blocking).
        
    Yields:
        The lock file descriptor.
        
    Raises:
        LockError: If lock cannot be acquired.
    """
    lock_path = Path(lock_path)
    lock_fd = None
    
    try:
        # Create lock file if it doesn't exist
        lock_fd = open(lock_path, 'a+')
        
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            raise LockError(
                f"Could not acquire lock on {lock_path}. "
                "Another instance may be running."
            )
        
        yield lock_fd
        
    finally:
        if lock_fd:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass


# ============================================================================
# NETWORK UTILITIES
# ============================================================================

def get_hostname() -> str:
    """Get the local hostname."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def get_fqdn() -> str:
    """Get the fully qualified domain name."""
    try:
        return socket.getfqdn()
    except Exception:
        return get_hostname()


def is_valid_hostname(hostname: str) -> bool:
    """
    Validate a hostname.
    
    Args:
        hostname: The hostname to validate.
        
    Returns:
        True if valid, False otherwise.
    """
    if not hostname or len(hostname) > 253:
        return False
    
    # Remove trailing dot
    if hostname.endswith('.'):
        hostname = hostname[:-1]
    
    # Check each label
    pattern = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$')
    return all(pattern.match(label) for label in hostname.split('.'))


def is_valid_ip(ip: str) -> bool:
    """
    Validate an IP address (v4 or v6).
    
    Args:
        ip: The IP address to validate.
        
    Returns:
        True if valid, False otherwise.
    """
    import ipaddress
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


# ============================================================================
# VALIDATION
# ============================================================================

def validate_cron_expression(cron_expr: str) -> bool:
    """
    Validate a cron expression.
    
    Args:
        cron_expr: The cron expression to validate (5 fields).
        
    Returns:
        True if valid, False otherwise.
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return False
    
    patterns = [
        r'^(\*|[0-5]?\d)(\/\d+)?$',           # Minute (0-59)
        r'^(\*|[01]?\d|2[0-3])(\/\d+)?$',     # Hour (0-23)
        r'^(\*|[12]?\d|3[01])(\/\d+)?$',      # Day of month (1-31)
        r'^(\*|[1-9]|1[0-2])(\/\d+)?$',       # Month (1-12)
        r'^(\*|[0-6])(\/\d+)?$',              # Day of week (0-6)
    ]
    
    for part, pattern in zip(parts, patterns):
        # Handle ranges and lists
        for item in part.split(','):
            if '-' in item:
                # Range
                try:
                    start, end = item.split('-')
                    if not (re.match(pattern, start) and re.match(pattern, end)):
                        return False
                except ValueError:
                    return False
            elif not re.match(pattern, item):
                return False
    
    return True


# ============================================================================
# DIRECTORY UTILITIES  
# ============================================================================

def ensure_directory(path: Union[str, Path], mode: int = 0o755) -> Path:
    """
    Ensure a directory exists, creating it if necessary.
    
    Args:
        path: Directory path.
        mode: Permission mode for created directories.
        
    Returns:
        Path object.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except Exception:
        pass
    return path


def rotate_files(directory: Union[str, Path], base_filename: str, 
                 max_copies: int = 5) -> None:
    """
    Rotate files, keeping at most max_copies.
    
    Files are renamed: file.csv -> file.csv.1 -> file.csv.2 -> ...
    Oldest files beyond max_copies are deleted.
    
    Args:
        directory: Directory containing files.
        base_filename: Base filename to rotate.
        max_copies: Maximum number of copies to keep.
    """
    directory = Path(directory)
    if not directory.exists():
        return
    
    # Find existing files
    existing = {}
    main_file = directory / base_filename
    if main_file.exists():
        existing[0] = main_file
    
    for i in range(1, max_copies + 1):
        numbered = directory / f"{base_filename}.{i}"
        if numbered.exists():
            existing[i] = numbered
    
    if not existing:
        return
    
    # Delete files beyond max_copies
    for num in sorted(existing.keys(), reverse=True):
        if num >= max_copies - 1:
            try:
                existing[num].unlink()
                del existing[num]
            except Exception as e:
                logger.warning(f"Failed to delete {existing[num]}: {e}")
    
    # Rotate remaining files
    for num in sorted(existing.keys(), reverse=True):
        old_path = existing[num]
        new_path = directory / f"{base_filename}.{num + 1}"
        try:
            old_path.rename(new_path)
        except Exception as e:
            logger.warning(f"Failed to rotate {old_path}: {e}")


def generate_filename(codcli: str, nomecliente: str, file_type: str,
                      extension: str = "csv", 
                      server_identifier: Optional[str] = None) -> str:
    """
    Generate a standardized filename.
    
    Args:
        codcli: Client code.
        nomecliente: Client name.
        file_type: Type of file (vms, hosts, storage, etc.).
        extension: File extension.
        server_identifier: Optional server identifier.
        
    Returns:
        Generated filename.
    """
    parts = [
        sanitize_filename(codcli),
        sanitize_filename(nomecliente),
    ]
    
    if server_identifier:
        parts.append(sanitize_filename(server_identifier))
    
    parts.append(f"prox_{file_type}")
    
    return f"{'_'.join(parts)}.{extension}"
