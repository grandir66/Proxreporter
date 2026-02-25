# Proxreporter

Proxreporter is an automated reporting and auditing tool designed for Proxmox VE environments. It efficiently collects detailed information about virtual machines, containers, hosts, storage, and networking configuration, generating comprehensive CSV reports.

The system is designed to run directly on the Proxmox host (or remotely via SSH/API), supports secure configurations, generates backups of the collected data, and uploads everything to a central SFTP server.

## Features

-   **Deep Analysis**: Collects extensive details including VM states, hardware capabilities (CPU/RAM/Disk), network configurations (VLANs, Bridges, Bonds), and storage usage.
-   **Security First**: Uses `config.json` to store sensitive credentials (SFTP passwords), avoiding hardcoded secrets in scripts.
-   **Automated Delivery**: Compresses reports and configuration backups into an archive and uploads it via secure SFTP.
-   **HTML Reporting**: Generates modern, responsive HTML reports summarizing the cluster status, VM usage, and storage.
-   **Email Alerts**: Sends the HTML report directly via email (SMTP) to configured recipients.
-   **Syslog/Graylog Integration**: Sends alerts in GELF format to centralized logging systems (Graylog, Syslog).
-   **Hardware Monitoring**: Monitors disk health (SMART), RAID status, memory ECC errors, CPU temperatures, and kernel errors.
-   **PVE Monitor**: Monitors Proxmox backup jobs, storage status, services, and sends summaries to Syslog.
-   **Centralized Configuration**: Downloads configuration from SFTP server, enabling centralized management of all installations.
-   **Heartbeat System**: Sends hourly status messages to Syslog with system health and hardware diagnostics.
-   **Auto-Update**: Capable of self-updating from the repository to ensure the latest features and fixes are applied.
-   **Low Footprint**: Written in Python 3 with minimal external dependencies.

---

## Installation, Migration & Updates

### Quick Reference

| Scenario | Command |
|----------|---------|
| **New installation** | `bash <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/install.sh)` |
| **Migrate from old SFTP version** | `python3 <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/migrate.py)` |
| **Manual update** | `cd /opt/proxreport && git fetch origin && git reset --hard origin/main` |
| **Force update + reconfigure** | `python3 /opt/proxreport/update_scripts.py` |
| **Test heartbeat** | `python3 /opt/proxreport/heartbeat.py -v` |

---

### New Installation

To install Proxreporter on your Proxmox host, run as `root`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/install.sh)
```

Or with wget:

```bash
wget -qO- https://raw.githubusercontent.com/grandir66/Proxreporter/main/install.sh | bash
```

**What the installer does:**
1. Checks dependencies (`git`, `python3`, `python3-venv`)
2. Clones repository to `/opt/proxreport`
3. Preserves existing `config.json` and `.secret.key` during updates
4. Launches interactive `setup.py` wizard

---

### Migration from Old Versions (SFTP-based)

If you have an old installation that was deployed via SFTP (not Git), use the migration script:

```bash
# Preview what will be done (no changes)
python3 <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/migrate.py) --dry-run

# Execute migration
python3 <(curl -fsSL https://raw.githubusercontent.com/grandir66/Proxreporter/main/migrate.py)
```

Or download and run:

```bash
wget https://raw.githubusercontent.com/grandir66/Proxreporter/main/migrate.py
python3 migrate.py
```

**Migration options:**

| Option | Description |
|--------|-------------|
| `--dry-run` / `-n` | Show what would be done without making changes |
| `--force` / `-f` | Force migration even if already on Git |
| `--verbose` / `-v` | Detailed output |

**What the migration does:**
1. Detects old installations in `/opt/proxreport`, `/opt/proxreport/v2`, `/opt/proxreporter`, etc.
2. Backs up old installation with timestamp
3. Migrates configuration from legacy format to new format
4. Preserves encryption keys (`.secret.key`, `.encryption_key`)
5. Removes old cron jobs
6. Installs new Git-based version
7. Configures daily report + hourly heartbeat cron jobs

---

### Manual Update

To manually update an existing installation:

```bash
cd /opt/proxreport && git fetch origin && git reset --hard origin/main
```

To also run post-update tasks (reconfigure cron, sync remote config):

```bash
python3 /opt/proxreport/update_scripts.py
```

---

### Automatic Updates

Proxreporter includes **automatic update** capability:

1. **Daily** (via `proxmox_core.py --auto-update`): Checks for updates before generating report
2. **Hourly** (via `heartbeat.py`): Lightweight version check, updates if new version available

Both methods:
- Compare local version with remote `version.py`
- Download and apply updates via `git reset --hard`
- Preserve local `config.json` and encryption keys

**Cron jobs created automatically:**

```
# Daily report at 6:00 AM
0 6 * * * root /usr/bin/python3 /opt/proxreport/proxmox_core.py --config /opt/proxreport/config.json --auto-update

# Hourly heartbeat
0 * * * * root /usr/bin/python3 /opt/proxreport/heartbeat.py --config /opt/proxreport/config.json
```

---

### Version Check

To see installed version:

```bash
python3 -c "from version import __version__; print(__version__)"
```

Or check heartbeat output:

```bash
python3 /opt/proxreport/heartbeat.py -v
```

---

## Before You Start (Requirements)

Before running the installation, ensure you have:

1. **SFTP Password**: Password for the `proxmox` user on `sftp.domarc.it`
2. **Client Code**: Short identifier for this installation (e.g., `CUST01`)
3. **Client Name**: Full name of the client
4. **(Optional) SMTP Details**: Host, Port, User, Password for email reports

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

The system is automatically scheduled via cron (daily at 6:00 AM + hourly heartbeat). You can run it manually for testing:

```bash
# Generate report (local mode, no upload)
python3 /opt/proxreport/proxmox_core.py --config /opt/proxreport/config.json --local --no-upload

# Generate report and upload
python3 /opt/proxreport/proxmox_core.py --config /opt/proxreport/config.json --local

# Test heartbeat + hardware + PVE monitor
python3 /opt/proxreport/heartbeat.py -c /opt/proxreport/config.json -v

# Test PVE monitor only
python3 /opt/proxreport/pve_monitor.py --test

# Test alerts (SMTP + Syslog)
python3 /opt/proxreport/test_alerts.py --config /opt/proxreport/config.json
```

### Command Line Arguments

| Argument | Description |
|----------|-------------|
| `--config <path>` | Specify configuration file path |
| `--no-upload` | Generate reports without SFTP upload |
| `--skip-update` | Skip auto-update check |
| `--local` | Force local execution mode |
| `--auto-update` | Check and apply updates before execution |
| `-v` / `--verbose` | Verbose output |

## Troubleshooting

| Issue | Solution |
|-------|----------|
| **Permission denied** | Run as `root` |
| **SFTP connection failed** | Check `sftp.domarc.it:11122` connectivity |
| **Syslog not received** | Verify port 8514 (TCP) is open, format is GELF |
| **Config not synced** | Run `python3 /opt/proxreport/update_scripts.py` |
| **Old version** | Run `cd /opt/proxreport && git fetch origin && git reset --hard origin/main` |

**Log files:**
- `/var/log/proxreporter/cron.log` - Execution logs
- `/var/log/proxreporter/*.csv` - Generated reports

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

### Testing Alerts

```bash
python3 /opt/proxreport/test_alerts.py --config /opt/proxreport/config.json
```

---

## Hardware Monitoring

The system monitors hardware health and sends diagnostics to Syslog every hour:

| Component | What is Monitored |
|-----------|-------------------|
| **Disks (SMART)** | Health status, reallocated sectors, pending sectors, temperature, model, serial |
| **Memory (ECC)** | Corrected and uncorrected errors via EDAC |
| **RAID** | mdadm array status, ZFS pool health/capacity |
| **Temperature** | CPU and component temperatures via `sensors` or `/sys/class/thermal` |
| **Kernel** | MCE errors, I/O errors, hardware failures from dmesg |

### GELF Message Example (Syslog port 8514)

```json
{
  "_app": "proxreporter",
  "_module": "hardware_monitor",
  "_message_type": "HARDWARE_STATUS",
  "_status": "ok",
  "_disk_count": 2,
  "_disk_0_device": "/dev/sda",
  "_disk_0_smart": "PASSED",
  "_disk_0_temp": 38,
  "_temp_max": 52,
  "_mem_total_gb": 64.0,
  "_raid_count": 1,
  "_raid_0_type": "zfs",
  "_raid_0_status": "online"
}
```

### Configuration

```json
{
  "hardware_monitoring": {
    "enabled": true,
    "check_disks": true,
    "check_memory": true,
    "check_raid": true,
    "check_temperature": true,
    "check_kernel": true
  },
  "hardware_thresholds": {
    "disk_temp_warning": 45,
    "disk_temp_critical": 55,
    "cpu_temp_warning": 75,
    "cpu_temp_critical": 90,
    "reallocated_sectors_warning": 1,
    "reallocated_sectors_critical": 10
  }
}
```

---

## PVE Monitor

PVE Monitor provides detailed Proxmox VE health checks, sent to Syslog on a dedicated port (default: 4514).

### What is Monitored

| Check | Description |
|-------|-------------|
| **Node Status** | CPU, memory, uptime, load average |
| **Storage Status** | Backup storage usage, warning/critical thresholds |
| **Backup Results** | vzdump task results from last 24h |
| **Backup Jobs** | Scheduled backup jobs configuration |
| **Backup Coverage** | VMs/CTs without scheduled backups |
| **Service Status** | Critical PVE services (pvedaemon, pveproxy, etc.) |

### Summary Message (port 8514)

A summary is also sent to the main Syslog port for unified monitoring:

```json
{
  "_app": "proxreporter",
  "_module": "pve_monitor",
  "_message_type": "PVE_MONITOR_SUMMARY",
  "_status": "success",
  "_backup_tasks_24h": 5,
  "_backup_failed_count": 0,
  "_storage_count": 2,
  "_storage_0_name": "local-zfs",
  "_storage_0_used_percent": 45.2,
  "_not_covered_vms": 1,
  "_failed_services": 0
}
```

### Configuration

```json
{
  "pve_monitor": {
    "enabled": true,
    "lookback_hours": 24,
    "syslog_port": 4514,
    "check_node_status": true,
    "check_storage_status": true,
    "check_backup_results": true,
    "check_backup_jobs": true,
    "check_backup_coverage": true,
    "check_service_status": true
  }
}
```

### Manual Test

```bash
python3 /opt/proxreport/pve_monitor.py --test
```

---

## Heartbeat System

The heartbeat sends an hourly status message to Syslog indicating system presence:

### Message Content

| Field | Description |
|-------|-------------|
| `hostname` | System hostname |
| `proxreporter_version` | Installed version |
| `pve_version` | Proxmox VE version |
| `kernel_version` | Linux kernel version |
| `uptime_days` | System uptime in days |
| `uptime_formatted` | Human-readable uptime |

### What Runs Hourly

1. **Version check**: Downloads remote `version.py`, updates if newer
2. **Heartbeat**: Sends presence message to Syslog
3. **Hardware check**: Collects and sends hardware diagnostics
4. **PVE Monitor**: Runs backup/storage/service checks (if enabled)

### Manual Test

```bash
python3 /opt/proxreport/heartbeat.py -c /opt/proxreport/config.json -v
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
