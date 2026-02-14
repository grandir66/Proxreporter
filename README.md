# Proxreporter

Proxreporter is an automated reporting and auditing tool designed for Proxmox VE environments. It efficiently collects detailed information about virtual machines, containers, hosts, storage, and networking configuration, generating comprehensive CSV reports.

The system is designed to run directly on the Proxmox host (or remotely via SSH/API), supports secure configurations, generates backups of the collected data, and uploads everything to a central SFTP server.

## Versions

- **V3 (Modular)**: New architecture in `src/proxreporter/` with improved security, error handling, and performance. [See V3 README](src/README.md)
- **V2 (Legacy)**: Original scripts in root directory, still fully functional.

## Features

-   **Deep Analysis**: Collects extensive details including VM states, hardware capabilities (CPU/RAM/Disk), network configurations (VLANs, Bridges, Bonds), and storage usage.
-   **Security First**: Uses `config.json` to store sensitive credentials (SFTP passwords), avoiding hardcoded secrets in scripts.
-   **Automated Delivery**: Compresses reports and configuration backups into an archive and uploads it via secure SFTP.
-   **HTML Reporting**: Generates modern, responsive HTML reports summarizing the cluster status, VM usage, and storage.
-   **Email Alerts**: Sends the HTML report directly via email (SMTP) to configured recipients.
-   **Syslog/Graylog Integration**: Sends alerts in GELF format to centralized logging systems (Graylog, Syslog).
-   **Hardware Monitoring**: Monitors disk health (SMART), RAID status, memory ECC errors, CPU temperatures, and kernel errors.
-   **Centralized Configuration**: Downloads configuration from SFTP server, enabling centralized management of all installations.
-   **Notification System**: Integrates with Proxmox's notification system to alert on backup status or errors.
-   **Auto-Update**: Capable of self-updating from the repository to ensure the latest features and fixes are applied.
-   **Low Footprint**: Written in Python 3 with minimal external dependencies.

## Before You Start (Requirements)

Before running the installation, ensure you have the following information ready:

1.  **SFTP Password**: The password for the `proxmox` user on `sftp.domarc.it`.
2.  **Client Code**: A short code to identify this installation (e.g., `CUST01`).
3.  **Client Name**: The full name of the client.
4.  **(Optional) SMTP Details**: If you want email reports, you'll need the Host, Port, User, and Password for the email account.

## Installation

### Quick Install (Recommended)

To install Proxreporter on your Proxmox host, run the following command as `root`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/install.sh)
```

Or with wget:

```bash
wget -qO- https://raw.githubusercontent.com/grandir66/Proxreporter/main/install.sh | bash
```

### Install V3 (Modular Architecture)

For the new modular version with improved security and performance:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/install_v3.sh)
```

### What the Installer Does
1.  **Checks Dependencies**: Ensures `git`, `python3`, and required packages are installed.
2.  **Clones Repository**: Downloads the latest version of the code to `/opt/proxreport`.
3.  **Preserves Configuration**: Keeps existing `config.json` and `.secret.key` during updates.
4.  **Launches Setup**: Starts the interactive `setup.py` wizard to configure the system.

## Setup & Configuration Guide

During the installation, the interactive wizard will ask you to configure the system. Here is a guide to the requested parameters:

### 1. General Settings
*   **Output Directory**: The local folder where CSV reports, logs, and backup archives will be stored.
    *   *Default*: `/var/log/proxreporter` (Recommended)
*   **Client Code (codcli)**: A short, unique identifier for the customer or site (e.g., `CUST01`, `ACMECORP`). This code is used in the report filenames.
*   **Client Name**: The full human-readable name of the customer (e.g., `Acme Corp International`).

### 2. SFTP Configuration (Uploads)
*   **SFTP Password**: **(Required)** You must enter the password for the centralized SFTP server (`sftp.domarc.it`).
    *   The scripts authenticate as the `proxmox` user.
    *   This password is saved securely in `config.json` and is never printed in logs.

### 3. Connection Mode (Local vs Remote)
*   **Remote Host?**:
    *   Answer `n` (No) if you are installing this **directly on the Proxmox host** you want to monitor (Recommended for most cases). The script will use local system commands.
    *   Answer `y` (Yes) if you are installing this on a separate management server and want to monitor a Proxmox node over the network. You will be asked for the remote **IP/Hostname**, **SSH Port**, **User**, and **Password**.

### 4. Updates & Maintenance
*   **Auto-Update?**:
    *   Answer `y` (Yes) to allow the script to pull the latest version from GitHub before every execution. This ensures you always have the latest bug fixes and features without manual intervention.

### 5. Email Reporting (Optional)
*   **Configure Email?**:
    *   Answer `y` (Yes) to enable sending HTML reports via email.
    *   You will need to provide SMTP details: **Host** (e.g., smtp.gmail.com), **Port** (e.g., 587), **Username**, **Password**, and a comma-separated list of **Recipients**.

---

### Configuration File (`config.json`)

The setup process generates a secured `config.json` file in `/opt/proxreport/config.json`.
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
    },
    "smtp": {
        "enabled": false
    }
}
```

## Manual Usage

The system is automatically scheduled via `crond` (typically 11:00 AM daily). However, you can run it manually for testing or on-demand reports.

### V3 (Modular - Recommended)

```bash
python3 /opt/proxreport/src/proxreporter_cli.py --config /opt/proxreport/config.json --local
```

Or using the symlink (if installed with install_v3.sh):

```bash
proxreporter --config /opt/proxreport/config.json --local
```

### V2 (Legacy)

```bash
python3 /opt/proxreport/proxmox_core.py --config /opt/proxreport/config.json --local
```

### Command Line Arguments

-   `--config <path>`: Specify a JSON configuration file.
-   `--no-upload`: Generate reports but do *not* upload to SFTP.
-   `--skip-update`: Skip the auto-update check (V2 only).
-   `--local`: Force local execution mode even if config specifies remote.
-   `--debug`: Enable debug logging (V3 only).

## Troubleshooting

-   **Logs**: Check `/var/log/proxreporter/cron.log` for execution logs.
-   **Permissions**: Ensure the script runs as `root` so it can access system files (`/etc/pve`, `/etc/network/interfaces`).
-   **SFTP Errors**: Verify the `sftp.domarc.it` connection on port `11122`.

## V3 Architecture

The V3 modular version provides:
- Custom exception hierarchy for better error handling
- SSH connection pooling for performance
- Parallel data extraction with ThreadPoolExecutor
- Secure password handling (not exposed in process list)
- Comprehensive unit tests

See [src/README.md](src/README.md) for detailed V3 documentation.

---

## Alert System

Proxreporter includes a comprehensive alert system that sends notifications via **Email (SMTP)** and **Syslog/Graylog (GELF format)**.

### Alert Types

| Alert Type | Description | Email | Syslog |
|------------|-------------|-------|--------|
| `backup_success` | Backup completed successfully | No | Yes |
| `backup_failure` | Backup failed | Yes | Yes |
| `upload_success` | SFTP upload completed | No | Yes |
| `upload_failure` | SFTP upload failed | Yes | Yes |
| `storage_warning` | Storage usage above threshold | Yes | Yes |
| `report_generated` | HTML report generated | No | Yes |
| `hardware_warning` | Hardware issue detected (warning) | Yes | Yes |
| `hardware_critical` | Hardware issue detected (critical) | Yes | Yes |

### Hardware Monitoring

The system monitors:
- **Disks (SMART)**: Health status, reallocated sectors, pending sectors, temperature
- **Memory (ECC)**: Corrected and uncorrected errors via EDAC
- **RAID**: mdadm array status, ZFS pool status
- **Temperature**: CPU and component temperatures
- **Kernel**: MCE errors, I/O errors, hardware failures from dmesg

### Testing Alerts

```bash
python3 /opt/proxreport/test_alerts.py --config /opt/proxreport/config.json
```

---

## Centralized Configuration

Proxreporter supports **centralized configuration management** via SFTP. This allows administrators to manage settings for all installations from a single location.

### How It Works

1. A master configuration file is stored on the SFTP server at:
   ```
   /home/proxmox/config/proxreporter_defaults.json
   ```

2. At each execution, clients:
   - Download the remote configuration
   - Merge it with the local `config.json`
   - **Save the updated configuration locally**

3. The remote configuration has **priority** for centralized fields:
   - `syslog.*` (host, port, format, etc.)
   - `smtp.*` (host, recipients, credentials)
   - `alerts.*` (thresholds, enabled channels)
   - `hardware_monitoring.*` and `hardware_thresholds.*`

4. Local-only fields are preserved:
   - `client.codcli`, `client.nomecliente` (unique identifiers)
   - `sftp.password` (local credentials)

### Updating All Installations

To change configuration for all clients:

1. Edit the file on the SFTP server:
   ```bash
   scp proxreporter_defaults.json proxmox@sftp-server:/home/proxmox/config/
   ```

2. At the next execution of each client, the changes are automatically applied.

### Example Remote Configuration

```json
{
    "syslog": {
        "enabled": true,
        "host": "syslog.example.com",
        "port": 8514,
        "protocol": "tcp",
        "format": "gelf"
    },
    "smtp": {
        "host": "smtp.example.com",
        "port": 25,
        "user": "smtp-user",
        "password": "smtp-password",
        "sender": "{codcli}_{nomecliente}@example.com",
        "recipients": "alerts@example.com"
    },
    "alerts": {
        "enabled": true,
        "storage_warning_threshold": 85
    },
    "hardware_monitoring": {
        "enabled": true
    }
}
```

---

## License & Credits

**Proxreporter** - Software di monitoraggio e reporting per infrastrutture Proxmox VE

- **Sviluppatore**: Riccardo Grandi
- **Proprietario**: Domarc SRL

© 2024-2026 **Domarc SRL** - Tutti i diritti riservati.

Questo software e il relativo codice sorgente sono di proprietà esclusiva di Domarc SRL.
L'utilizzo, la riproduzione o la distribuzione non autorizzata sono vietati.
