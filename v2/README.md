# Proxreporter V2

Proxreporter is an automated reporting audting tool designed for Proxmox VE environments. It efficiently collects detailed information about virtual machines, containers, hosts, storage, and networking configuration, generating comprehensive CSV reports.

The system is designed to run directly on the Proxmox host (or remotely via SSH/API), supports secure configurations, generates backups of the collected data, and uploads everything to a central SFTP server.

## Features

-   **Deep Analysis**: Collects extensive details including VM states, hardware capabilities (CPU/RAM/Disk), network configurations (VLANs, Bridges, Bonds), and storage usage.
-   **Security First**: Uses `config.json` to store sensitive credentials (SFTP passwords), avoiding hardcoded secrets in scripts.
-   **Automated Delivery**: Compresses reports and configuration backups into an archive and uploads it via secure SFTP.
-   **HTML Reporting**: Generates modern, responsive HTML reports summarizing the cluster status, VM usage, and storage.
-   **Email Alerts**: Sends the HTML report directly via email (SMTP) to configured recipients.
-   **Notification System**: Integrates with Proxmox's notification system to alert on backup status or errors.
-   **Auto-Update**: Capable of self-updating from the repository to ensure the latest features and fixes are applied.
-   **Low Footprint**: Written in Python 3 with minimal external dependencies.

## Installation

### Quick Install (Recommended)

To install Proxreporter V2 on your Proxmox host, simply run the following command as `root`. This one-liner handles dependencies, downloading, and configuration.

```bash
wget -q -O install.sh https://raw.githubusercontent.com/grandir66/Proxreporter/main/v2/install.sh && bash install.sh

```

### What the Installer Does
1.  **Checks Dependencies**: Ensures `git`, `python3`, and `python3-venv` (if needed) are installed.
2.  **Clones Repository**: Downloads the latest version of the code to `/opt/proxreport`.
3.  **Launches Setup**: Starts the interactive `setup.py` wizard to configure the system.

## Setup & Configuration

During installation, the interactive setup wizard will prompt you for the following parameters:

| Parameter | Description |
| :--- | :--- |
| **Output Directory** | The folder where reports and logs will be saved. Default: `/var/log/proxreporter`. |
| **Client Code (codcli)** | A unique code to identify the client/site (e.g., `CL001`). Used in filenames. |
| **Client Name** | A human-readable name for the client (e.g., `Acme Corp`). |
| **Email Reporting?** | `y` to enable SMTP reporting. Limits prompts for Host, User, Pass, Recipients. |
| **SFTP Password** | **Required**. The password for the `proxmox` user on the collected SFTP server (`sftp.domarc.it`). |
| **Remote Host?** | `y` if you want to query a *remote* Proxmox server via SSH; `n` to query the *local* host (recommended). |
| **Auto-Update?** | `y` to check for script updates before every run. Recommended for maintenance-free operation. |

### Configuration File (`config.json`)

The setup process generates a secured `config.json` file in `/opt/proxreport/v2/config.json`.
You can manually edit this file to change passwords or settings without reinstalling.

**Example `config.json`**:
```json
{
    "proxmox": {
        "enabled": true,
        "host": "localhost:8006",
        "username": "",
        "password": "",
        "verify_ssl": false
    },
    "client": {
        "codcli": "CL001",
        "nomecliente": "Acme Corp",
        "server_identifier": "local"
    },
    "sftp": {
        "enabled": true,
        "host": "sftp.domarc.it",
        "port": 11122,
        "username": "proxmox",
        "password": "YOUR_SECRET_PASSWORD_HERE",
        "base_path": "/home/proxmox/uploads"
    }
}
```

## Manual Usage

The system is automatically scheduled via `crond` (typically 11:00 AM daily). However, you can run it manually for testing or on-demand reports.

```bash
cd /opt/proxreport/v2
python3 proxmox_core.py --config config.json
```

### Command Line Arguments

-   `--config <path>`: Specify a JSON configuration file.
-   `--no-upload`: Generate reports but do *not* upload to SFTP.
-   `--skip-update`: Skip the auto-update check.
-   `--local`: Force local execution mode even if config specifies remote.

## Troubleshooting

-   **Logs**: Check `/var/log/proxreporter/cron.log` for execution logs.
-   **Permissions**: Ensure the script runs as `root` so it can access system files (`/etc/pve`, `/etc/network/interfaces`).
-   **SFTP Errors**: Verify the `sftp.domarc.it` connection on port `11122`.
