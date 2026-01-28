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

## Before You Start (Requirements)

Before running the installation, ensure you have the following information ready:

1.  **SFTP Password**: The password for the `proxmox` user on `sftp.domarc.it`.
2.  **Client Code**: A short code to identify this installation (e.g., `CUST01`).
3.  **Client Name**: The full name of the client.
4.  **(Optional) SMTP Details**: If you want email reports, you'll need the Host, Port, User, and Password for the email account.

## Installation

### Quick Install (Recommended)

To install Proxreporter V2 on your Proxmox host, simply run the following command as `root`. This one-liner handles dependencies, downloading, and configuration.

```bash
wget -q -O install.sh "https://raw.githubusercontent.com/grandir66/Proxreporter/main/v2/install.sh?v=$(date +%s)" && bash install.sh
```

### What the Installer Does
1.  **Checks Dependencies**: Ensures `git`, `python3`, and `python3-venv` (if needed) are installed.
2.  **Clones Repository**: Downloads the latest version of the code to `/opt/proxreport`.
3.  **Launches Setup**: Starts the interactive `setup.py` wizard to configure the system.

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
    },
    "smtp": {
        "enabled": false
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
-   `--smtp-password <password>`: Override SMTP password to avoid interactive prompts (useful for cron).

## Troubleshooting

-   **Logs**: Check `/var/log/proxreporter/cron.log` for execution logs.
-   **Permissions**: Ensure the script runs as `root` so it can access system files (`/etc/pve`, `/etc/network/interfaces`).
-   **SFTP Errors**: Verify the `sftp.domarc.it` connection on port `11122`.
