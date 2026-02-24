#!/usr/bin/env python3
"""
Proxreporter - Heartbeat Script

Invia un messaggio di heartbeat al server Syslog per indicare che il sistema è attivo.
Dovrebbe essere eseguito periodicamente via cron (es. ogni ora).

Uso:
    python3 heartbeat.py --config /opt/proxreport/config.json

Cron example (ogni ora):
    0 * * * * /usr/bin/python3 /opt/proxreport/heartbeat.py --config /opt/proxreport/config.json

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import argparse
import json
import logging
import os
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

# Version info
try:
    from version import __version__, get_version_string
except ImportError:
    __version__ = "unknown"
    def get_version_string():
        return "Proxreporter (version unknown)"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("proxreporter")


def load_config(config_path: str) -> Dict[str, Any]:
    """Carica la configurazione da file"""
    with open(config_path, 'r') as f:
        return json.load(f)


def decrypt_password(encrypted: str, install_dir: Path) -> str:
    """Decripta una password se necessario"""
    if not encrypted or not encrypted.startswith("ENC:"):
        return encrypted
    
    try:
        from cryptography.fernet import Fernet
        key_file = install_dir / ".secret.key"
        if key_file.exists():
            with open(key_file, 'rb') as f:
                key = f.read()
            fernet = Fernet(key)
            return fernet.decrypt(encrypted[4:].encode()).decode()
    except Exception:
        pass
    return ""


def get_system_info() -> Dict[str, Any]:
    """Raccoglie informazioni sul sistema"""
    info = {
        'hostname': socket.gethostname(),
        'platform': platform.system(),
        'platform_release': platform.release(),
        'python_version': platform.python_version(),
        'proxreporter_version': __version__,
    }
    
    # Proxmox VE version
    try:
        import subprocess
        import re
        result = subprocess.run(
            ["pveversion"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            # Output: "pve-manager/8.1.3/abc123 (running kernel: 6.5.11-8-pve)"
            match = re.match(r"pve-manager/([\d.]+)", result.stdout.strip())
            if match:
                info['pve_version'] = match.group(1)
            else:
                info['pve_version'] = result.stdout.strip().split()[0]
    except:
        pass
    
    # Kernel version
    try:
        info['kernel_version'] = platform.release()
    except:
        pass
    
    # Uptime (Linux)
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])
            info['uptime_seconds'] = int(uptime_seconds)
            info['uptime_hours'] = round(uptime_seconds / 3600, 1)
            info['uptime_days'] = round(uptime_seconds / 86400, 1)
            # Formato leggibile: "5d 3h 22m"
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            info['uptime_formatted'] = f"{days}d {hours}h {minutes}m"
    except:
        pass
    
    # Load average (Linux)
    try:
        with open('/proc/loadavg', 'r') as f:
            parts = f.read().split()
            info['load_average'] = [float(parts[0]), float(parts[1]), float(parts[2])]
    except:
        pass
    
    # Memory usage (Linux)
    try:
        with open('/proc/meminfo', 'r') as f:
            meminfo = {}
            for line in f:
                if ':' in line:
                    key, value = line.split(':', 1)
                    meminfo[key.strip()] = int(value.strip().split()[0]) * 1024
            
            total = meminfo.get('MemTotal', 0)
            available = meminfo.get('MemAvailable', 0)
            if total > 0:
                used_pct = round((total - available) / total * 100, 1)
                info['memory_total_gb'] = round(total / (1024**3), 2)
                info['memory_used_percent'] = used_pct
    except:
        pass
    
    return info


def send_heartbeat_gelf(config: Dict[str, Any], system_info: Dict[str, Any]) -> bool:
    """Invia heartbeat in formato GELF al server Syslog"""
    import time
    
    syslog_config = config.get("syslog", {})
    
    if not syslog_config.get("enabled", False):
        logger.info("Syslog non abilitato, heartbeat non inviato")
        return False
    
    host = syslog_config.get("host", "")
    port = syslog_config.get("port", 514)
    protocol = syslog_config.get("protocol", "tcp").lower()
    
    if not host:
        logger.warning("Server syslog non configurato")
        return False
    
    # Costruisci messaggio GELF
    hostname = system_info.get('hostname', socket.gethostname())
    codcli = config.get("codcli", "")
    nomecliente = config.get("nomecliente", "")
    
    gelf_msg = {
        "version": "1.1",
        "host": hostname,
        "short_message": f"HEARTBEAT: {hostname} online - Proxreporter v{__version__}",
        "full_message": f"Sistema {hostname} ({codcli} - {nomecliente}) attivo e funzionante",
        "timestamp": time.time(),
        "level": 6,  # INFO
        "_app": "proxreporter",
        "_app_version": __version__,
        "_message_type": "HEARTBEAT",
        "_client_code": codcli,
        "_client_name": nomecliente,
        "_hostname": hostname,
        "_event": "heartbeat",
    }
    
    # Aggiungi info sistema
    for key, value in system_info.items():
        if key != 'hostname':
            gelf_msg[f"_{key}"] = str(value) if not isinstance(value, (int, float)) else value
    
    message = (json.dumps(gelf_msg) + '\0').encode('utf-8')
    
    try:
        if protocol == "tcp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            sock.sendall(message)
            sock.close()
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(message, (host, port))
            sock.close()
        
        logger.info(f"✓ Heartbeat inviato a {host}:{port} ({protocol.upper()})")
        return True
    except Exception as e:
        logger.error(f"✗ Errore invio heartbeat: {e}")
        return False


def check_and_update() -> Optional[str]:
    """
    Verifica se è disponibile una nuova versione e aggiorna se necessario.
    Usa un check leggero confrontando solo la versione senza scaricare tutto.
    
    Returns:
        Nuova versione se aggiornato, None altrimenti
    """
    import subprocess
    import urllib.request
    
    install_dir = Path(__file__).resolve().parent
    version_url = "https://raw.githubusercontent.com/grandir66/Proxreporter/main/version.py"
    
    try:
        # Scarica solo version.py remoto (pochi bytes)
        with urllib.request.urlopen(version_url, timeout=10) as response:
            remote_content = response.read().decode('utf-8')
        
        # Estrai versione remota
        remote_version = None
        for line in remote_content.split('\n'):
            if line.startswith('__version__'):
                remote_version = line.split('"')[1]
                break
        
        if not remote_version:
            return None
        
        # Confronta con versione locale
        if remote_version == __version__:
            logger.debug(f"Versione {__version__} è aggiornata")
            return None
        
        # Versione diversa, verifica se è più recente
        local_parts = [int(x) for x in __version__.split('.')]
        remote_parts = [int(x) for x in remote_version.split('.')]
        
        if remote_parts <= local_parts:
            return None
        
        logger.info(f"→ Nuova versione disponibile: {__version__} -> {remote_version}")
        
        # Esegui aggiornamento tramite git o update_scripts.py
        git_dir = install_dir / ".git"
        
        if git_dir.exists():
            # Aggiornamento via git
            result = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=install_dir,
                capture_output=True,
                timeout=30
            )
            if result.returncode == 0:
                result = subprocess.run(
                    ["git", "reset", "--hard", "origin/main"],
                    cwd=install_dir,
                    capture_output=True,
                    timeout=30
                )
                if result.returncode == 0:
                    logger.info(f"✓ Aggiornato a v{remote_version} via git")
                    
                    # Esegui post-update tasks
                    update_script = install_dir / "update_scripts.py"
                    if update_script.exists():
                        subprocess.run(
                            [sys.executable, str(update_script)],
                            cwd=install_dir,
                            capture_output=True,
                            timeout=120
                        )
                    
                    return remote_version
        else:
            # Aggiornamento via update_scripts.py
            update_script = install_dir / "update_scripts.py"
            if update_script.exists():
                result = subprocess.run(
                    [sys.executable, str(update_script)],
                    cwd=install_dir,
                    capture_output=True,
                    timeout=120
                )
                if result.returncode == 0:
                    logger.info(f"✓ Aggiornato a v{remote_version}")
                    return remote_version
        
        return None
        
    except urllib.error.URLError as e:
        logger.debug(f"Impossibile verificare aggiornamenti: {e}")
        return None
    except Exception as e:
        logger.debug(f"Errore check aggiornamenti: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Proxreporter Heartbeat - Invia stato al Syslog")
    parser.add_argument("-c", "--config", default="/opt/proxreport/config.json",
                        help="Percorso file di configurazione")
    parser.add_argument("-v", "--verbose", action="store_true", help="Output dettagliato")
    parser.add_argument("--no-update", action="store_true", help="Non verificare aggiornamenti")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Verifica aggiornamenti (prima di tutto)
    updated_version = None
    if not args.no_update:
        updated_version = check_and_update()
        if updated_version:
            # Se aggiornato, ricarica il modulo version
            try:
                import importlib
                import version as ver_module
                importlib.reload(ver_module)
            except:
                pass
    
    # Carica configurazione
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"File di configurazione non trovato: {config_path}")
        sys.exit(1)
    
    try:
        config = load_config(str(config_path))
    except Exception as e:
        logger.error(f"Errore caricamento configurazione: {e}")
        sys.exit(1)
    
    # Aggiungi codcli e nomecliente al config root se in sezione client
    if "client" in config:
        config["codcli"] = config.get("codcli", config["client"].get("codcli", ""))
        config["nomecliente"] = config.get("nomecliente", config["client"].get("nomecliente", ""))
    
    # Raccogli info sistema
    system_info = get_system_info()
    
    # Aggiungi info aggiornamento se presente
    if updated_version:
        system_info['updated_to_version'] = updated_version
        system_info['update_event'] = True
    
    if args.verbose:
        logger.info(f"Sistema: {system_info}")
    
    # Invia heartbeat
    success = send_heartbeat_gelf(config, system_info)
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
