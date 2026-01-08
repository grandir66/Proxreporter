# Proxreporter V2

Automated reporting tool for Proxmox VE environments. Generates CSV reports (VMs, Hosts, Storage, Network) and backups, uploads them via SFTP, and supports email notifications.

## Features
- **Automated Reporting**: Collects extensive data on VMs, containers, hosts, and storage.
- **Secure SFTP Upload**: Uploads reports to a centralized server.
- **Configuration Management**: Uses `config.json` for secure credential storage.
- **Email Notifications**: Integrates with Proxmox notification system.
- **Auto-Update**: Can automatically update itself from the repository.

## Installation

### Method 1: Quick Install (Recommended)

Run the following command on your Proxmox host:

```bash
wget -O - https://raw.githubusercontent.com/grandir66/Proxreporter/main/v2/install.sh | bash
```

*Note: Replace the URL with the actual path to your raw `install.sh` file.*

This script will:
1. Install necessary dependencies (git, python3).
2. Clone the repository to `/opt/proxreport`.
3. Launch the interactive configuration wizard.

### Method 2: Manual Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/grandir66/Proxreporter.git
   cd Proxreporter/v2
   ```

2. Run the setup script:
   ```bash
   sudo python3 setup.py
   ```

3. Follow the interactive prompts to configure:
   - Client Code & Name
   - SFTP Password
   - Remote/Local execution mode

## Configuration

The configuration is stored in `config.json` in the installation directory.
To modify settings after installation, you can edit this file or re-run `setup.py`.

## Usage

The tool is designed to run via cron (configured automatically by `setup.py`).
To run manually:

```bash
python3 proxmox_core.py --config config.json
```
