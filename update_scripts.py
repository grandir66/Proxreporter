#!/usr/bin/env python3
"""
Script di auto-aggiornamento per Proxmox Reporter.
Scarica gli script aggiornati da GitHub, confronta versioni e sostituisce se più recenti.

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import hashlib
import json
import os
import sys
import shutil
import tempfile
import urllib.request
import urllib.error
import time
import subprocess
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

# Configurazione GitHub
GITHUB_REPO_URL = "https://raw.githubusercontent.com/grandir66/Proxreporter/main"

# Script da aggiornare (relativi alla directory di installazione)
SCRIPTS_TO_UPDATE = [
    "proxmox_core.py",
    "proxmox_report.py",
    "html_generator.py",
    "email_sender.py",
    "setup.py",
    "update_scripts.py",
    "alert_manager.py",
    "remote_config.py",
    "hardware_monitor.py",
    "pve_monitor.py",
    "heartbeat.py",
    "version.py",
    "test_alerts.py",
    "templates/report.html.j2",
    "install.sh",
    "config.json.example",
    "README.md"
]

def compute_file_hash(filepath: Path) -> Optional[str]:
    """Calcola hash SHA256 di un file."""
    try:
        hasher = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        # Se il file non esiste, hash è None
        return None


def download_file(url: str, dest_path: Path) -> bool:
    """Scarica un file da URL."""
    try:
        with urllib.request.urlopen(url) as response, open(dest_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        return True
    except urllib.error.HTTPError as e:
        print(f"  ✗ Errore HTTP {e.code} per {url}")
        return False
    except Exception as e:
        print(f"  ✗ Errore download {url}: {e}")
        return False


def check_and_download_updates(install_dir: Path) -> List[Tuple[str, Path]]:
    """
    Verifica aggiornamenti disponibili e scarica gli script più recenti.
    Ritorna lista di tuple (nome_script, percorso_temporaneo) degli script aggiornati.
    """
    updated_files: List[Tuple[str, Path]] = []
    
    print("\n→ Verifica aggiornamenti da GitHub...")
    
    for script_rel_path in SCRIPTS_TO_UPDATE:
        local_path = install_dir / script_rel_path
        # Cache protection
        timestamp = int(time.time())
        remote_url = f"{GITHUB_REPO_URL}/{script_rel_path}?t={timestamp}"
        
        # Scarica in file temporaneo
        fd, temp_file_path = tempfile.mkstemp(suffix=f"_{os.path.basename(script_rel_path)}")
        os.close(fd)
        temp_file = Path(temp_file_path)
        
        # print(f"  Checking: {script_rel_path}...")
        
        if download_file(remote_url, temp_file):
            remote_hash = compute_file_hash(temp_file)
            local_hash = compute_file_hash(local_path) if local_path.exists() else None
            
            if local_hash != remote_hash:
                print(f"  found update: {script_rel_path}")
                updated_files.append((script_rel_path, temp_file))
            else:
                temp_file.unlink() # Clean up unchanged
        else:
             temp_file.unlink() # Clean up failed
             
    return updated_files


def apply_updates(install_dir: Path, updated_files: List[Tuple[str, Path]]) -> bool:
    """Applica gli aggiornamenti sostituendo i file locali."""
    if not updated_files:
        return False
    
    print(f"\n→ Applicazione aggiornamenti ({len(updated_files)} file)...")
    
    # Backup directory
    backup_dir = install_dir / ".backup"
    backup_dir.mkdir(exist_ok=True)
    
    success_count = 0
    
    for script_rel_path, temp_path in updated_files:
        local_path = install_dir / script_rel_path
        backup_path = backup_dir / f"{os.path.basename(script_rel_path)}.bak"
        
        # Ensure parent dir exists (e.g. templates/)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Backup versione corrente
            if local_path.exists():
                shutil.copy2(local_path, backup_path)
            
            # Sostituisci con nuova versione
            shutil.move(str(temp_path), str(local_path))
            
            # Set permissions if .py
            if local_path.suffix == ".py":
                os.chmod(local_path, 0o755)
                
            print(f"  ✓ Aggiornato: {script_rel_path}")
            success_count += 1
            
        except Exception as e:
            print(f"  ✗ Errore aggiornamento {script_rel_path}: {e}")
            # Ripristina backup se disponibile
            if backup_path.exists():
                shutil.copy2(backup_path, local_path)
                print(f"    → Ripristinato backup")
    
    return success_count > 0


def auto_enable_syslog(install_dir: Path) -> bool:
    """
    Abilita automaticamente Syslog nella configurazione esistente.
    Viene eseguito dopo ogni aggiornamento per garantire che i sistemi
    già installati ricevano la configurazione Syslog centralizzata.
    
    Returns:
        True se la configurazione è stata modificata, False altrimenti
    """
    config_file = install_dir / "config.json"
    
    if not config_file.exists():
        return False
    
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        modified = False
        
        # Verifica se syslog è già configurato e abilitato
        syslog_config = config.get("syslog", {})
        
        if not syslog_config or not syslog_config.get("enabled"):
            # Abilita syslog con configurazione minima
            # I valori reali verranno scaricati dal server SFTP
            config["syslog"] = {
                "enabled": True,
                "host": "",  # Sarà popolato dal file remoto
                "port": 8514,
                "protocol": "tcp",
                "facility": 16,
                "app_name": "proxreporter"
            }
            modified = True
            print("  → Syslog abilitato (configurazione da server remoto)")
        
        # Verifica se SMTP deve essere abilitato
        smtp_config = config.get("smtp", {})
        if smtp_config.get("host") and smtp_config.get("recipients") and not smtp_config.get("enabled"):
            config["smtp"]["enabled"] = True
            modified = True
            print("  → SMTP abilitato automaticamente")
        
        # Verifica se alerts è configurato
        if "alerts" not in config:
            config["alerts"] = {
                "enabled": True,
                "email_min_severity": "warning",
                "syslog_min_severity": "info",
                "storage_warning_threshold": 85,
                "backup_failure": {"email": True, "syslog": True},
                "upload_failure": {"email": True, "syslog": True},
                "storage_warning": {"email": True, "syslog": True},
                "backup_success": {"email": False, "syslog": True},
                "upload_success": {"email": False, "syslog": True},
                "report_generated": {"email": False, "syslog": True}
            }
            modified = True
            print("  → Alert system configurato")
        elif not config["alerts"].get("enabled"):
            config["alerts"]["enabled"] = True
            modified = True
        
        # Abilita PVE Monitor per l'invio dei report backup a Syslog
        if "pve_monitor" not in config:
            config["pve_monitor"] = {
                "enabled": True,
                "lookback_hours": 24,
                "syslog_port": 4514,
                "syslog_format": "json",
                "send_to_main_syslog": True,
                "check_node_status": True,
                "check_storage_status": True,
                "check_backup_results": True,
                "check_backup_jobs": True,
                "check_backup_coverage": True,
                "check_service_status": True
            }
            modified = True
            print("  → PVE Monitor (backup reports) abilitato")
        else:
            # Assicura che le nuove opzioni siano presenti
            if not config["pve_monitor"].get("enabled"):
                config["pve_monitor"]["enabled"] = True
                modified = True
                print("  → PVE Monitor abilitato")
            if "syslog_format" not in config["pve_monitor"]:
                config["pve_monitor"]["syslog_format"] = "json"
                modified = True
            if "send_to_main_syslog" not in config["pve_monitor"]:
                config["pve_monitor"]["send_to_main_syslog"] = True
                modified = True
        
        # Abilita hardware monitoring
        if "hardware_monitoring" not in config:
            config["hardware_monitoring"] = {
                "enabled": True,
                "check_disks": True,
                "check_memory": True,
                "check_raid": True,
                "check_temperature": True,
                "check_kernel": True
            }
            modified = True
            print("  → Hardware monitoring abilitato")
        
        # Salva se modificato
        if modified:
            # Backup prima di modificare
            backup_file = config_file.with_suffix('.json.bak')
            shutil.copy2(config_file, backup_file)
            
            with open(config_file, 'w') as f:
                json.dump(config, f, indent=4)
            
            # Mantieni permessi restrittivi
            os.chmod(config_file, 0o600)
            
            print("  ✓ Configurazione aggiornata automaticamente")
            return True
        
        return False
        
    except json.JSONDecodeError as e:
        print(f"  ⚠ Errore parsing config.json: {e}")
        return False
    except Exception as e:
        print(f"  ⚠ Errore auto-configurazione syslog: {e}")
        return False


def download_remote_defaults(install_dir: Path, config: Dict[str, Any]) -> bool:
    """
    Scarica i defaults remoti dal server SFTP e li applica alla configurazione.
    Salva le modifiche nel config.json locale per gestione centralizzata.
    """
    try:
        # Import dinamico per evitare errori se il modulo non esiste ancora
        sys.path.insert(0, str(install_dir))
        from remote_config import sync_remote_config
        
        print("  → Sincronizzazione configurazione centralizzata...")
        
        config_file = install_dir / "config.json"
        merged_config = sync_remote_config(config, config_file)
        
        # Mostra info sulla configurazione
        syslog_host = merged_config.get("syslog", {}).get("host", "")
        smtp_enabled = merged_config.get("smtp", {}).get("enabled", False)
        
        if syslog_host:
            print(f"  ✓ Syslog: {syslog_host}:{merged_config.get('syslog', {}).get('port', 514)}")
        if smtp_enabled:
            print(f"  ✓ SMTP: {merged_config.get('smtp', {}).get('host', 'N/A')}")
        
        return True
            
    except ImportError:
        # Il modulo remote_config non esiste ancora
        print("  ℹ Modulo remote_config non disponibile")
        return False
    except Exception as e:
        print(f"  ⚠ Errore sincronizzazione config remota: {e}")
        return False


def update_via_git(install_dir: Path) -> int:
    """
    Tenta aggiornamento via git se disponibile.
    Return codes: 0 (aggiornato), 2 (nessun agg.), 1 (errore/non git)
    """
    # Check if install_dir itself is a git repo
    repo_dir = install_dir
    if not (repo_dir / ".git").exists():
        # Fallback: check parent dir (legacy structure)
        repo_dir = install_dir.parent
        if not (repo_dir / ".git").exists():
            return 1
        
    print(f"→ Rilevato repository Git in {repo_dir}, uso git pull...")
    try:
        # Get current hash
        old_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        
        # Pull
        subprocess.run(["git", "pull"], cwd=repo_dir, check=True)
        
        # Get new hash
        new_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        
        if old_hash != new_hash:
            print(f"  ✓ Aggiornato da {old_hash[:7]} a {new_hash[:7]}")
            return 0
        else:
            print("  ✓ Già aggiornato")
            return 2
    except Exception as e:
        print(f"  ⚠ Errore git pull: {e}, fallback su download diretto")
        return 1


def post_update_tasks(install_dir: Path, was_updated: bool) -> None:
    """
    Esegue task post-aggiornamento:
    - Auto-abilita Syslog nei sistemi esistenti
    - Scarica configurazione remota
    - Applica eventuali migrazioni di configurazione
    """
    print("\n→ Configurazione automatica post-aggiornamento...")
    
    config_file = install_dir / "config.json"
    config = {}
    
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
        except Exception:
            pass
    
    # 1. Auto-abilita Syslog se non configurato
    syslog_modified = auto_enable_syslog(install_dir)
    
    # Ricarica config se modificato
    if syslog_modified and config_file.exists():
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
        except Exception:
            pass
    
    # 2. Scarica e applica configurazione remota
    if config:
        download_remote_defaults(install_dir, config)
    
    # 3. Configura cron heartbeat se non presente
    setup_heartbeat_cron(install_dir)
    
    print("✓ Configurazione automatica completata")


def setup_heartbeat_cron(install_dir: Path) -> bool:
    """
    Configura il cron job per l'heartbeat se non già presente.
    Usa /etc/cron.d/ per evitare conflitti con crontab utente.
    
    Returns:
        True se configurato o già presente
    """
    cron_file = Path("/etc/cron.d/proxreporter-heartbeat")
    heartbeat_script = install_dir / "heartbeat.py"
    config_file = install_dir / "config.json"
    log_dir = Path("/var/log/proxreporter")
    
    # Verifica che lo script heartbeat esista
    if not heartbeat_script.exists():
        print("  ℹ Script heartbeat.py non trovato, skip cron setup")
        return False
    
    # Se il cron esiste già, verifica che sia corretto
    if cron_file.exists():
        try:
            with open(cron_file, 'r') as f:
                content = f.read()
                if "heartbeat.py" in content:
                    print("  ✓ Cron heartbeat già configurato")
                    return True
        except Exception:
            pass
    
    # Crea directory log se non esiste
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    
    # Crea il cron job
    cron_content = f"""# Proxreporter Heartbeat - Invia stato al Syslog ogni ora
# Generato automaticamente da update_scripts.py
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

0 * * * * root /usr/bin/python3 {heartbeat_script} -c {config_file} >> {log_dir}/heartbeat.log 2>&1
"""
    
    try:
        with open(cron_file, 'w') as f:
            f.write(cron_content)
        os.chmod(cron_file, 0o644)
        print(f"  ✓ Cron heartbeat configurato: {cron_file}")
        return True
    except PermissionError:
        print("  ⚠ Permessi insufficienti per creare cron heartbeat")
        return False
    except Exception as e:
        print(f"  ⚠ Errore configurazione cron heartbeat: {e}")
        return False


def main():
    # Determina directory di installazione
    # Lo script risiede direttamente in /opt/proxreport/
    install_dir = Path(__file__).resolve().parent
    
    # Verifica permessi scrittura
    if not os.access(install_dir, os.W_OK):
        print(f"⚠ W: Nessun permesso scrittura su {install_dir}. Salto aggiornamento.")
        return # Non uscire con errore, semplicemente salta update nel cron

    # Tentativo 1: Git Pull
    git_result = update_via_git(install_dir)
    if git_result == 0:
        print("✓ Aggiornamento git completato.")
        # Esegui task post-aggiornamento
        post_update_tasks(install_dir, was_updated=True)
        sys.exit(0) # Restart
    elif git_result == 2:
        # Git rilevato ma nessun aggiornamento
        # Esegui comunque la configurazione automatica (per sistemi esistenti)
        post_update_tasks(install_dir, was_updated=False)
        sys.exit(2)
    
    # Tentativo 2: Download File (Fallback o Non-Git)
    updated_files = check_and_download_updates(install_dir)
    
    if updated_files:
        if apply_updates(install_dir, updated_files):
            print("✓ Aggiornamento completato con successo.")
            # Esegui task post-aggiornamento
            post_update_tasks(install_dir, was_updated=True)
            sys.exit(0) # Restart required
        else:
            print("⚠ Aggiornamento parziale o fallito.")
            sys.exit(1) # Error
    else:
        # Anche senza aggiornamenti, esegui la configurazione automatica
        # Questo garantisce che i sistemi esistenti ricevano Syslog
        post_update_tasks(install_dir, was_updated=False)
        sys.exit(2) # No updates, no restart needed


if __name__ == "__main__":
    main()
