# Proxreporter v3 - Modular Architecture

This is the refactored version of Proxreporter with a modular architecture,
improved security, and better error handling.

## Installation

### Quick Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/install_v3.sh)
```

Or with wget:

```bash
wget -qO- https://raw.githubusercontent.com/grandir66/Proxreporter/main/install_v3.sh | bash
```

## Structure

```
src/
├── proxreporter/           # Main package
│   ├── __init__.py         # Package initialization, version
│   ├── exceptions.py       # Custom exception hierarchy
│   ├── security.py         # Encryption, password handling
│   ├── config.py           # Configuration management
│   ├── utils.py            # Utility functions
│   ├── ssh.py              # SSH connection with pooling
│   ├── sftp.py             # SFTP upload with retry
│   ├── extractor.py        # Proxmox data extraction
│   ├── csv_writer.py       # CSV generation
│   ├── backup.py           # Configuration backup
│   └── cli.py              # Command-line interface
├── proxreporter_cli.py     # Entry point script
└── requirements.txt        # Dependencies

tests/
├── test_utils.py           # Utility function tests
├── test_security.py        # Security module tests
└── test_csv_writer.py      # CSV writer tests
```

## Key Improvements

### Security
- Secure password handling (not exposed in process list)
- Proper encryption with Fernet (AES)
- Configurable SSL verification
- SSH host key verification options

### Error Handling
- Custom exception hierarchy
- Specific exceptions for different error types
- Consistent error propagation
- Better logging

### Code Quality
- Modular architecture
- Type hints throughout
- Docstrings for all public functions
- Unit tests

### Performance
- SSH connection pooling
- Parallel data extraction with ThreadPoolExecutor
- Efficient file operations

## Usage

```bash
# Run from source directory
python proxreporter_cli.py --config config.json --local

# With pip install (from src directory)
pip install -e .
proxreporter --config config.json --local
```

## Command-line Options

```
--config, -c       Configuration file path (default: config.json)
--codcli           Client code (overrides config.json)
--nomecliente      Client name (overrides config.json)
--local, -l        Run locally on Proxmox host
--host, -H         Remote Proxmox host
--username, -u     Proxmox API username (default: root@pam)
--password, -p     Proxmox API password
--output-dir, -o   Output directory
--no-upload        Skip SFTP upload
--no-backup        Skip configuration backup
--debug            Enable debug logging
--version, -v      Show version
```

## Configuration

Configuration is loaded from `config.json`:

```json
{
    "client": {
        "codcli": "CLI001",
        "nomecliente": "Client Name",
        "server_identifier": "pve-host"
    },
    "sftp": {
        "enabled": true,
        "host": "sftp.example.com",
        "port": 22,
        "username": "user",
        "password": "ENC:...",
        "base_path": "/uploads"
    },
    "system": {
        "output_directory": "/var/log/proxreporter",
        "max_file_copies": 5
    }
}
```

## Running Tests

```bash
cd tests
pytest -v
```

## Migrating from v2

The v3 modular version is fully compatible with existing config.json files.
Just update the execution command:

```bash
# Old
python proxmox_core.py --config config.json --local

# New
python src/proxreporter_cli.py --config config.json --local
```
