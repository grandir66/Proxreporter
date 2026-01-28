"""
Command-line interface for Proxreporter.

Provides argument parsing and main entry point.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .config import Config
from .exceptions import (
    ProxreporterError,
    ConfigurationError,
    ConnectionError,
    UploadError,
)
from .extractor import ProxmoxExtractor, ProxmoxAPIClient
from .csv_writer import CSVWriter
from .backup import ProxmoxBackup
from .sftp import SFTPUploader
from .ssh import SSHConnection, HostKeyPolicy
from .utils import file_lock, ensure_directory, get_hostname

logger = logging.getLogger("proxreporter")


def setup_logging(level: str = "INFO", log_file: Optional[Path] = None) -> None:
    """
    Configure logging.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Optional log file path.
    """
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    handlers = [logging.StreamHandler(sys.stdout)]
    
    if log_file:
        ensure_directory(log_file.parent)
        handlers.append(logging.FileHandler(log_file))
    
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        handlers=handlers,
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=f"Proxreporter v{__version__} - Proxmox Configuration Reporter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --config config.json --local
  %(prog)s --config config.json --host 192.168.1.100
  %(prog)s --config config.json --no-upload
        """
    )
    
    # Configuration
    parser.add_argument(
        '--config', '-c',
        default='config.json',
        help='Configuration file path (default: config.json)'
    )
    
    # Client identification (override config)
    parser.add_argument(
        '--codcli',
        help='Client code (overrides config.json)'
    )
    parser.add_argument(
        '--nomecliente',
        help='Client name (overrides config.json)'
    )
    
    # Execution mode
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--local', '-l',
        action='store_true',
        help='Run locally on Proxmox host'
    )
    mode_group.add_argument(
        '--host', '-H',
        help='Remote Proxmox host (hostname or IP)'
    )
    
    # Remote connection options
    parser.add_argument(
        '--username', '-u',
        default='root@pam',
        help='Proxmox API username (default: root@pam)'
    )
    parser.add_argument(
        '--password', '-p',
        help='Proxmox API password'
    )
    parser.add_argument(
        '--ssh-port',
        type=int,
        default=22,
        help='SSH port for remote connection (default: 22)'
    )
    
    # Output options
    parser.add_argument(
        '--output-dir', '-o',
        help='Output directory (overrides config.json)'
    )
    parser.add_argument(
        '--no-upload',
        action='store_true',
        help='Skip SFTP upload'
    )
    parser.add_argument(
        '--no-backup',
        action='store_true',
        help='Skip configuration backup'
    )
    
    # Feature flags
    parser.add_argument(
        '--no-vms',
        action='store_true',
        help='Skip VM collection'
    )
    parser.add_argument(
        '--no-hosts',
        action='store_true',
        help='Skip host collection'
    )
    parser.add_argument(
        '--no-storage',
        action='store_true',
        help='Skip storage collection'
    )
    parser.add_argument(
        '--no-network',
        action='store_true',
        help='Skip network collection'
    )
    
    # Other options
    parser.add_argument(
        '--verify-ssl',
        action='store_true',
        help='Verify SSL certificates (default: disabled)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    parser.add_argument(
        '--version', '-v',
        action='version',
        version=f'%(prog)s {__version__}'
    )
    
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    """
    Main execution logic.
    
    Args:
        args: Parsed command-line arguments.
        
    Returns:
        Exit code (0 = success).
    """
    # Load configuration
    try:
        config = Config(args.config)
    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    
    # Override config with CLI args
    if args.codcli:
        config._config.setdefault('client', {})['codcli'] = args.codcli
    if args.nomecliente:
        config._config.setdefault('client', {})['nomecliente'] = args.nomecliente
    if args.output_dir:
        config._config.setdefault('system', {})['output_directory'] = args.output_dir
    
    # Get key values
    codcli = config.codcli
    nomecliente = config.nomecliente
    server_identifier = config.server_identifier or get_hostname()
    output_dir = config.output_directory
    
    # Validate required fields
    if not codcli or not nomecliente:
        logger.error("codcli and nomecliente are required (via config.json or CLI)")
        return 1
    
    logger.info("=" * 70)
    logger.info(f"PROXREPORTER v{__version__}")
    logger.info("=" * 70)
    logger.info(f"Client: {codcli} - {nomecliente}")
    logger.info(f"Server: {server_identifier}")
    logger.info(f"Output: {output_dir}")
    logger.info("")
    
    # Ensure output directories
    csv_dir = output_dir / 'csv'
    backup_dir = output_dir / 'backup'
    ensure_directory(csv_dir)
    ensure_directory(backup_dir)
    
    # Initialize extractor
    api_client = None
    ssh_connection = None
    
    if args.local:
        logger.info("Mode: Local execution")
    elif args.host:
        logger.info(f"Mode: Remote ({args.host})")
        
        # Get password
        password = args.password or config.get('proxmox.password')
        if not password:
            logger.error("Password required for remote connection")
            return 1
        
        # Initialize API client
        api_client = ProxmoxAPIClient(
            host=args.host,
            username=args.username,
            password=password,
            verify_ssl=args.verify_ssl,
        )
        
        # Initialize SSH connection
        ssh_connection = SSHConnection(
            host=args.host.split(':')[0],
            port=args.ssh_port,
            username=args.username.split('@')[0],
            password=password,
            host_key_policy=HostKeyPolicy.WARN,
        )
    else:
        logger.info("Mode: Local execution (default)")
    
    extractor = ProxmoxExtractor(
        api_client=api_client,
        ssh_connection=ssh_connection,
        local_mode=args.local or not args.host,
    )
    
    # Initialize CSV writer
    csv_writer = CSVWriter(
        output_dir=str(csv_dir),
        codcli=codcli,
        nomecliente=nomecliente,
        server_identifier=server_identifier,
    )
    
    # Collect data
    files_to_upload = []
    
    # Host info
    if not args.no_hosts:
        logger.info("Collecting host information...")
        try:
            host_info = extractor.extract_host_info()
            host_file = csv_writer.write_hosts([host_info])
            if host_file:
                files_to_upload.append(str(host_file))
        except Exception as e:
            logger.error(f"Failed to collect host info: {e}")
    
    # VMs
    if not args.no_vms:
        logger.info("Collecting VM information...")
        try:
            vms = extractor.extract_all_vms()
            if vms:
                vm_file = csv_writer.write_vms(vms)
                if vm_file:
                    files_to_upload.append(str(vm_file))
        except Exception as e:
            logger.error(f"Failed to collect VMs: {e}")
    
    # Storage
    if not args.no_storage:
        logger.info("Collecting storage information...")
        try:
            storage = extractor.extract_all_storage()
            if storage:
                storage_file = csv_writer.write_storage(storage)
                if storage_file:
                    files_to_upload.append(str(storage_file))
        except Exception as e:
            logger.error(f"Failed to collect storage: {e}")
    
    # Network
    if not args.no_network:
        logger.info("Collecting network information...")
        try:
            network = extractor.extract_all_network()
            if network:
                network_file = csv_writer.write_network(network)
                if network_file:
                    files_to_upload.append(str(network_file))
        except Exception as e:
            logger.error(f"Failed to collect network: {e}")
    
    # Backup
    if not args.no_backup:
        logger.info("Creating configuration backup...")
        try:
            backup = ProxmoxBackup(
                output_dir=str(backup_dir),
                codcli=codcli,
                nomecliente=nomecliente,
                server_identifier=server_identifier,
            )
            backup_file = backup.create_backup()
            if backup_file:
                files_to_upload.append(str(backup_file))
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
    
    # SFTP Upload
    if not args.no_upload and files_to_upload:
        sftp_config = config.sftp
        
        if sftp_config.get('enabled', True):
            logger.info("")
            logger.info("=" * 70)
            logger.info("SFTP UPLOAD")
            logger.info("=" * 70)
            
            try:
                uploader = SFTPUploader.from_config(config.to_dict())
                
                with uploader:
                    results = uploader.upload_files(files_to_upload)
                    
                success_count = sum(results.values())
                logger.info(f"Upload complete: {success_count}/{len(files_to_upload)} files")
                
            except ConnectionError as e:
                logger.error(f"SFTP connection failed: {e}")
            except UploadError as e:
                logger.error(f"Upload failed: {e}")
            except Exception as e:
                logger.error(f"Unexpected upload error: {e}")
    else:
        if args.no_upload:
            logger.info("SFTP upload skipped (--no-upload)")
        elif not files_to_upload:
            logger.info("No files to upload")
    
    # Cleanup SSH connection
    if ssh_connection:
        ssh_connection.disconnect()
    
    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Files generated: {len(files_to_upload)}")
    for f in files_to_upload:
        logger.info(f"  - {Path(f).name}")
    
    return 0


def main() -> None:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging
    log_level = "DEBUG" if args.debug else "INFO"
    setup_logging(level=log_level)
    
    # Run with lock to prevent concurrent execution
    lock_file = Path("/var/run/proxreporter.lock")
    
    try:
        with file_lock(lock_file):
            exit_code = run(args)
    except Exception as e:
        if "Could not acquire lock" in str(e):
            logger.error("Another instance is already running")
            exit_code = 1
        else:
            logger.exception("Unexpected error")
            exit_code = 1
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
