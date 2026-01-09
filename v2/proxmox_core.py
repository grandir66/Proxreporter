#!/usr/bin/env python3
"""
Proxmox Core Reporter (cron-ready edition)

Script pensato per girare senza file di configurazione, sia in locale sul nodo Proxmox
sia interrogando un host remoto via API/SSH. Le credenziali SFTP restano hardcoded,
mentre host/utente/password per l'accesso remoto vengono passati da riga di comando.

Esempi:
  • solo locale: python3 proxmox_core.py --codcli 99999 --nomecliente RG
  • remoto:      python3 proxmox_core.py --codcli 99999 --nomecliente RG --host 192.168.40.11 --username root@pam --password ********
"""

import argparse
import csv
import json
import re
import socket
import subprocess
import sys
import os
import shutil
import importlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set
import logging
import time
import fcntl
import functools

# Custom modules
try:
    from html_generator import HTMLReporter
    from email_sender import EmailSender
except ImportError:
    # Gestione import se eseguiti come script standalone o modulo
    if __name__ == "__main__":
        # Aggiungi current dir e riprova
        sys.path.append(str(Path(__file__).resolve().parent))
        from html_generator import HTMLReporter
        from email_sender import EmailSender
    else:
        raise

# Logging Configuration
LOG_FILE_PATH = Path("/var/log/proxreporter/app.log")
logger = logging.getLogger("proxreporter")

def setup_logging(debug: bool = False, log_file: Path = LOG_FILE_PATH):
    """Configura logging su file e console"""
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Console Handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    # File Handler (Rotating manually or simple append for now, can use RotatingFileHandler)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG if debug else logging.INFO)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except PermissionError:
        logger.info(f"⚠ W: Impossibile scrivere log in {log_file} (Permessi). Logging solo su console.")

def retry(times=3, delay=5, backoff=2, exceptions=(Exception,)):
    """Decorator per retry automatico con backoff esponenziale"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            mtries, mdelay = times, delay
            while mtries > 1:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    logger.warning(f"Errore in {func.__name__}: {e}. Riprovo tra {mdelay}s... ({mtries-1} tentativi rimasti)")
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return func(*args, **kwargs)
        return wrapper
    return decorator

def acquire_lock(lock_file="/var/run/proxreporter.lock"):
    """Acquisisce lock esclusivo per evitare esecuzioni multiple"""
    lock_fd = open(lock_file, 'a+')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except IOError:
        return None

# Garantisci che si possa importare dal file principale
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))


def ensure_pre_import_dependencies() -> None:
    """Ensure required system packages are available before importing dependencies."""
    missing_packages: List[str] = []

    try:
        importlib.import_module("paramiko")
    except ModuleNotFoundError:
        missing_packages.append("python3-paramiko")

    if shutil.which("lshw") is None:
        missing_packages.append("lshw")

    if not missing_packages:
        return

    if os.geteuid() != 0:
        print(
            "⚠ Impossibile installare automaticamente i pacchetti: "
            + ", ".join(missing_packages)
        )
        print('  Eseguire manualmente: apt install ' + " ".join(missing_packages))
        return

    print("→ Installazione dipendenze di sistema: " + ", ".join(missing_packages))
    try:
        subprocess.run(
            ["apt-get", "update"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["apt-get", "install", "-y", *missing_packages],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print(
            "⚠ Comando apt-get non disponibile. Installare manualmente: apt install "
            + " ".join(missing_packages)
        )
    except Exception as exc:
        print(f"⚠ Installazione dipendenze fallita: {exc}")

    # Ritenta import paramiko se necessario
    try:
        importlib.import_module("paramiko")
    except ModuleNotFoundError:
        print(
            "⚠ Libreria 'paramiko' ancora mancante. Installare manualmente: apt install python3-paramiko"
        )


ensure_pre_import_dependencies()

from proxmox_report import (  # type: ignore
    ProxmoxLocalExtractor,
    feature_enabled,
    generate_filename,
    rotate_files,
    ProxmoxBackupIntegrated,
    SFTPUploader,
)


# ---------------------------------------------------------------------------
# Configurazione SFTP Default
# ---------------------------------------------------------------------------

SFTP_ENABLED_DEFAULT = True
SFTP_HOST_DEFAULT = "sftp.domarc.it"
SFTP_PORT_DEFAULT = 11122
SFTP_USERNAME_DEFAULT = "proxmox"
# SFTP_PASSWORD rimosso - deve essere caricato da config.json
SFTP_BASE_PATH_DEFAULT = "/home/proxmox/uploads"
SFTP_FALLBACK_HOST = "192.168.20.14"

FEATURES_DEFAULT = {
    "collect_cluster": True,
    "collect_host": True,
    "collect_host_details": True,
    "collect_storage": True,
    "collect_network": True,
    "collect_vms": True,
    "collect_containers": False,
    "collect_backup": True,
    "collect_perf": True,
}

NULL_TOKEN = ""

def format_decimal(value: Any, digits: int = 1) -> str:
    if value in (None, "", "N/A"):
        return NULL_TOKEN
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return NULL_TOKEN


def human_bytes(value: Any, digits: int = 1) -> str:
    if value in (None, "", "N/A"):
        return NULL_TOKEN
    try:
        value = float(value)
    except (TypeError, ValueError):
        return NULL_TOKEN
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.{digits}f} {units[idx]}"


def seconds_to_human(seconds: Any) -> str:
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return NULL_TOKEN
    if seconds < 0:
        seconds = 0
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def format_speed(value: Any) -> str:
    if value in (None, "", "N/A"):
        return NULL_TOKEN
    try:
        return f"{float(value):.1f} Mbps"
    except (TypeError, ValueError):
        return str(value)


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# Configurazione SMTP Domarc (valori predefiniti - stessi di configure_smtp.py)
DEFAULT_SMTP_SERVER = "esva.domarc.it"
DEFAULT_SMTP_PORT = 25
DEFAULT_SMTP_USER = "smtp.domarc"
DEFAULT_SMTP_ENCRYPTION = "starttls"
DEFAULT_FROM_ADDRESS = "px-@domarc.it"
DEFAULT_RECIPIENT = "domarcsrl+pxbackup@mycheckcentral.cc"
# Password SMTP - lasciare vuoto se non disponibile (verrà chiesta o saltata)
DEFAULT_SMTP_PASSWORD = "***REMOVED***"


def create_notification_template(codcli: str, nomecliente: str, execution_mode: str, executor=None) -> None:
    """
    Crea la cartella /etc/pve/notification-templates/default e il file vzdump-subject.txt.hbs
    con il template personalizzato per le notifiche di backup.
    
    Il template viene creato con i parametri nomecliente e codcli ricevuti:
    - Durante setup.py: usa i parametri inseriti dall'utente
    - Durante esecuzione: usa i parametri dal crontab (--nomecliente, --codcli)
    
    Il template viene SEMPRE aggiornato per riflettere i parametri corretti del cliente.
    """
    template_dir = "/etc/pve/notification-templates/default"
    template_file = f"{template_dir}/vzdump-subject.txt.hbs"
    
    # Template personalizzato con i parametri del cliente
    # I parametri codcli e nomecliente vengono presi da:
    # 1. Durante setup.py: dall'input utente
    # 2. Durante esecuzione: dagli argomenti --codcli e --nomecliente del comando in crontab
    template_content = f"{nomecliente} - {codcli} - Backup: {{{{hostname}}}} - {{{{status-text}}}} - Node: {{{{node}}}} - Job: {{{{job-id}}}}\n"
    
    try:
        if execution_mode == "ssh" and executor:
            # Modalità remota via SSH
            logger.info(f"→ Aggiornamento template notifica remoto: {template_file}")
            # Crea la directory
            mkdir_result = executor(f"mkdir -p {template_dir} 2>/dev/null")
            # Crea il file usando heredoc per evitare problemi di escape
            # Usiamo un delimiter unico per evitare conflitti
            delimiter = "PROXREPORTER_TEMPLATE_EOF"
            cmd = f'''cat > "{template_file}" << '{delimiter}'
{template_content}{delimiter}'''
            result = executor(cmd)
            # Verifica che il file sia stato creato
            check_cmd = f'test -f "{template_file}" && echo "OK" || echo "FAIL"'
            check_result = executor(check_cmd)
            if check_result and "OK" in check_result:
                logger.info(f"  ✓ Template notifica aggiornato con parametri cliente")
                logger.info(f"     Cliente: {nomecliente}")
                logger.info(f"     Codice: {codcli}")
            else:
                logger.info(f"  ⚠ Impossibile creare template remoto (verifica permessi root)")
        elif execution_mode == "local":
            # Modalità locale
            template_path = Path(template_dir)
            template_file_path = template_path / "vzdump-subject.txt.hbs"
            
            logger.info(f"→ Aggiornamento template notifica: {template_file}")
            try:
                # Crea la directory
                template_path.mkdir(parents=True, exist_ok=True)
                # Crea/aggiorna il file
                with open(template_file_path, 'w', encoding='utf-8') as f:
                    f.write(template_content)
                logger.info(f"  ✓ Template notifica aggiornato con parametri cliente")
                logger.info(f"     Cliente: {nomecliente}")
                logger.info(f"     Codice: {codcli}")
            except PermissionError:
                logger.info(f"  ⚠ Impossibile creare template (richiesti permessi root per {template_dir})")
                logger.info(f"     Eseguire manualmente:")
                logger.info(f"     sudo mkdir -p {template_dir}")
                logger.info(f"     sudo tee {template_file} << 'EOF'")
                logger.info(template_content)
                logger.info("EOF")
            except Exception as e:
                logger.info(f"  ⚠ Errore creazione template: {e}")
        else:
            # Modalità API - non possiamo creare file localmente
            logger.info(f"  ℹ Template notifica non creato (modalità API, richiede accesso locale/SSH)")
    except Exception as e:
        logger.info(f"  ⚠ Errore durante creazione template notifica: {e}")


def configure_smtp_notification(
    smtp_password: Optional[str],
    codcli: str,
    execution_mode: str,
    executor=None,
    config: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Configura il target di notifica SMTP in Proxmox usando pvesh.
    Usa i parametri predefiniti Domarc.
    Aggiunge un nuovo server SMTP dedicato senza sovrascrivere configurazioni esistenti.
    
    Args:
        smtp_password: Password SMTP (può essere None, verrà cercata nel config o richiesta)
        codcli: Codice cliente per costruire il from-address
        execution_mode: Modalità di esecuzione (local, ssh, api)
        executor: Funzione per eseguire comandi remoti
        config: Dizionario di configurazione (opzionale, per cercare la password)
    """
    # Cerca la password nel config se non fornita
    if not smtp_password and config:
        smtp_config = config.get("smtp", {})
        smtp_password = smtp_config.get("password") or smtp_config.get("smtp_password")
    
    # Se ancora non presente, usa DEFAULT_SMTP_PASSWORD se disponibile
    if not smtp_password:
        smtp_password = DEFAULT_SMTP_PASSWORD
    
    # Se ancora non presente, chiedi all'utente
    if not smtp_password:
        try:
            import getpass
            logger.info("  ℹ Password SMTP non disponibile")
            logger.info("     Inserire la password SMTP (premere Invio per saltare):")
            smtp_password = getpass.getpass("  Password SMTP: ").strip()
            if not smtp_password:
                logger.info("  ℹ Password SMTP non inserita, salto configurazione SMTP")
                return False
        except (KeyboardInterrupt, EOFError):
            logger.info("\n  ℹ Configurazione SMTP annullata")
            return False
    
    # Costruisci il from-address con il codice cliente
    from_address = f"da-px-{codcli}@domarc.it"
    
    # Nome del target di notifica con codice cliente
    target_name = f"da-alert-{codcli}"
    
    # Verifica se esiste già una configurazione con questo nome usando pvesh
    def check_existing_config(exec_mode: str, exec_func) -> bool:
        if exec_mode == "ssh" and exec_func:
            check_cmd = f'pvesh get /cluster/notifications/endpoints/{target_name} 2>/dev/null && echo "EXISTS" || echo "NOT_EXISTS"'
            result = exec_func(check_cmd)
            return result and "EXISTS" in result
        elif exec_mode == "local":
            try:
                result = subprocess.run(
                    ["pvesh", "get", f"/cluster/notifications/endpoints/{target_name}"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return result.returncode == 0
            except Exception:
                return False
        return False
    
    if check_existing_config(execution_mode, executor):
        logger.info(f"  ℹ Notification target 'da-alert' già esistente")
        logger.info("     Non sovrascritto per preservare le impostazioni")
        return True
    
    # Costruisci comando pvesh
    # Escape della password per shell (sostituisci ' con '\''')
    password_escaped = smtp_password.replace("'", "'\"'\"'")
    
    # NOTA: Non costruiamo più un comando shell con stringhe
    # Usiamo parametri diretti per evitare problemi con i tipi
    
    try:
        if execution_mode == "ssh" and executor:
            logger.info(f"→ Configurazione notification target 'da-alert' remota (pvesh)...")
            logger.info(f"  Comando: pvesh create /cluster/notifications/endpoints/smtp --name {target_name}")
            
            # Costruisci comando con parametri corretti (port come numero)
            pvesh_cmd = (
                f"pvesh create /cluster/notifications/endpoints/smtp "
                f"--name '{target_name}' "
                f"--mailto '{DEFAULT_RECIPIENT}' "
                f"--server '{DEFAULT_SMTP_SERVER}' "
                f"--port {DEFAULT_SMTP_PORT} "  # Numero senza virgolette
                f"--user '{DEFAULT_SMTP_USER}' "
                f"--password '{smtp_password}' "  # Password diretta, escape gestito da shell
                f"--from-address '{from_address}' "
                f"--mode insecure"
            )
            
            result = executor(pvesh_cmd)
            if result:
                logger.info(f"  pvesh output: {result}")
            
            # Verifica che il target sia stato creato
            check_cmd = f'pvesh get /cluster/notifications/endpoints/{target_name} 2>/dev/null && echo "OK" || echo "FAIL"'
            check_result = executor(check_cmd)
            logger.info(f"  Verifica target: {check_result}")
            
            if check_result and "OK" in check_result:
                logger.info(f"  ✓ Notification target 'da-alert' creato con successo")
                logger.info(f"     (Target dedicato, non sovrascrive configurazioni esistenti)")
                return True
            else:
                # Controlla se l'errore è "already exists"
                if result and ("already exists" in result.lower() or "duplicate" in result.lower()):
                    logger.info(f"  ℹ Notification target 'da-alert' già esistente")
                    return True
                logger.info(f"  ✗ Impossibile verificare creazione target")
                logger.info(f"     Verifica permessi e che pvesh sia disponibile")
                return False
        elif execution_mode == "local":
            logger.info(f"→ Configurazione notification target 'da-alert' locale (pvesh)...")
            logger.info(f"  Comando: pvesh create /cluster/notifications/endpoints/smtp --name {target_name}")
            
            try:
                # Esegui senza shell per passare correttamente i parametri come array
                result = subprocess.run([
                    "pvesh", "create", "/cluster/notifications/endpoints/smtp",
                    "--name", target_name,
                    "--mailto", DEFAULT_RECIPIENT,
                    "--server", DEFAULT_SMTP_SERVER,
                    "--port", str(DEFAULT_SMTP_PORT),
                    "--user", DEFAULT_SMTP_USER,
                    "--password", smtp_password,
                    "--from-address", from_address,
                    "--mode", "insecure"
                ],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode == 0:
                    logger.info(f"  ✓ Comando pvesh eseguito con successo")
                    
                    # Verifica che il target sia stato creato
                    verify_result = subprocess.run(
                        ["pvesh", "get", f"/cluster/notifications/endpoints/{target_name}"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    
                    if verify_result.returncode == 0:
                        logger.info(f"  ✓ Notification target 'da-alert' verificato")
                        logger.info(f"     (Target dedicato, non sovrascrive configurazioni esistenti)")
                        return True
                    else:
                        logger.info(f"  ⚠ Target creato ma verifica fallita")
                        return True  # Considera comunque successo se il comando è andato a buon fine
                else:
                    error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                    if "already exists" in error_msg.lower() or "duplicate" in error_msg.lower():
                        logger.info(f"  ℹ Notification target 'da-alert' già esistente")
                        logger.info("     Non sovrascritto per preservare le impostazioni")
                        return True
                    else:
                        logger.info(f"  ✗ Errore pvesh: {error_msg}")
                        return False
            except FileNotFoundError:
                logger.info(f"  ✗ pvesh non trovato (Proxmox non installato o non in PATH)")
                return False
            except subprocess.TimeoutExpired:
                logger.info(f"  ✗ Timeout durante esecuzione pvesh")
                return False
            except Exception as e:
                logger.info(f"  ✗ Errore esecuzione pvesh: {e}")
                return False
        else:
            logger.info(f"  ℹ Configurazione SMTP non disponibile (modalità API)")
            return False
    except Exception as e:
        logger.info(f"  ⚠ Errore durante configurazione SMTP: {e}")
        return False


def configure_backup_jobs_notification(
    target_name: str,
    codcli: str,
    execution_mode: str,
    executor=None
) -> bool:
    """
    Crea un notification matcher che collega tutti i job di backup al target creato.
    Preserva eventuali email già configurate nei job aggiungendole al target.
    
    Args:
        target_name: Nome del target di notifica (es. da-alert-12345)
        codcli: Codice cliente per il nome del matcher
        execution_mode: Modalità di esecuzione (local, ssh, api)
        executor: Funzione per eseguire comandi remoti
    
    Returns:
        True se la configurazione è andata a buon fine, False altrimenti
    """
    try:
        logger.info("")
        logger.info("=" * 70)
        logger.info("  CONFIGURAZIONE NOTIFICATION MATCHER PER BACKUP")
        logger.info("=" * 70)
        logger.info("")
        
        # Nome del matcher
        matcher_name = f"backup-matcher-{codcli}"
        
        logger.info(f"  Creazione notification matcher: {matcher_name}")
        logger.info(f"  Target: {target_name}")
        logger.info("")
        
        # Verifica se il matcher esiste già
        matcher_exists = False
        if execution_mode == "ssh" and executor:
            check_cmd = f"pvesh get /cluster/notifications/matchers/{matcher_name} 2>/dev/null && echo 'EXISTS' || echo 'NOT_EXISTS'"
            check_result = executor(check_cmd)
            matcher_exists = check_result and "EXISTS" in check_result
        else:
            try:
                result = subprocess.run(
                    ["pvesh", "get", f"/cluster/notifications/matchers/{matcher_name}"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                matcher_exists = result.returncode == 0
            except Exception:
                matcher_exists = False
        
        if matcher_exists:
            logger.info(f"  ℹ Notification matcher '{matcher_name}' già esistente")
            logger.info(f"    Eliminazione per ricrearlo...")
            
            if execution_mode == "ssh" and executor:
                delete_cmd = f"pvesh delete /cluster/notifications/matchers/{matcher_name} 2>&1"
                executor(delete_cmd)
            else:
                try:
                    subprocess.run(
                        ["pvesh", "delete", f"/cluster/notifications/matchers/{matcher_name}"],
                        capture_output=True,
                        timeout=5
                    )
                except Exception:
                    pass
        
        # Crea il notification matcher per i backup job
        # Match field format: exact:type=vzdump per catturare tutti i backup
        logger.info(f"  → Creazione matcher per tutti i backup job...")
        
        if execution_mode == "ssh" and executor:
            # Crea matcher che cattura tutti i backup vzdump
            # Sintassi corretta: --match-field exact:type=vzdump
            create_matcher_cmd = f"""pvesh create /cluster/notifications/matchers \
                --name '{matcher_name}' \
                --target '{target_name}' \
                --match-field 'exact:type=vzdump' \
                --mode 'all' \
                --comment 'Auto-generated matcher for backup notifications' \
                2>&1"""
            
            create_result = executor(create_matcher_cmd)
            success = create_result is not None and "error" not in (create_result.lower() if create_result else "")
        else:
            try:
                result = subprocess.run([
                    "pvesh", "create", "/cluster/notifications/matchers",
                    "--name", matcher_name,
                    "--target", target_name,
                    "--match-field", "exact:type=vzdump",
                    "--mode", "all",
                    "--comment", "Auto-generated matcher for backup notifications"
                ],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                success = result.returncode == 0
                if not success and result.stderr:
                    logger.info(f"    Output: {result.stderr}")
            except Exception as e:
                logger.info(f"    Errore: {e}")
                success = False
        
        if not success:
            logger.info(f"  ✗ Errore nella creazione del matcher")
            return False
        
        logger.info(f"  ✓ Notification matcher creato con successo")
        logger.info("")
        logger.info(f"  Il matcher cattura automaticamente:")
        logger.info(f"    • Tutti i job di tipo 'vzdump' (backup)")
        logger.info(f"    • Invia notifiche al target: {target_name}")
        logger.info("")
        
        # Raccoglie email dai backup job esistenti per aggiungerle al target
        logger.info(f"  → Raccolta email dai backup job esistenti...")
        all_emails = set()
        
        if execution_mode == "ssh" and executor:
            jobs_cmd = "pvesh get /cluster/backup --output-format json 2>/dev/null"
            jobs_result = executor(jobs_cmd)
            if jobs_result:
                try:
                    jobs = json.loads(jobs_result)
                    for job in jobs:
                        mailto = job.get("mailto", "")
                        if mailto:
                            for email in mailto.split(","):
                                email = email.strip()
                                if email:
                                    all_emails.add(email)
                except Exception:
                    pass
        else:
            try:
                result = subprocess.run(
                    ["pvesh", "get", "/cluster/backup", "--output-format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    jobs = json.loads(result.stdout)
                    for job in jobs:
                        mailto = job.get("mailto", "")
                        if mailto:
                            for email in mailto.split(","):
                                email = email.strip()
                                if email:
                                    all_emails.add(email)
            except Exception:
                pass
        
        # Aggiorna il target con tutte le email raccolte
        if all_emails:
            logger.info(f"  Email trovate nei backup jobs: {', '.join(sorted(all_emails))}")
            logger.info(f"  → Aggiunta al notification target...")
            
            # Recupera mailto attuale del target
            if execution_mode == "ssh" and executor:
                get_mailto_cmd = f"pvesh get /cluster/notifications/endpoints/{target_name} --output-format json 2>/dev/null"
                mailto_result = executor(get_mailto_cmd)
                if mailto_result:
                    try:
                        target_config = json.loads(mailto_result)
                        current_target_mailto = target_config.get("mailto", "")
                        if isinstance(current_target_mailto, list):
                            current_target_mailto = ",".join(current_target_mailto)
                    except json.JSONDecodeError:
                        current_target_mailto = ""
                else:
                    current_target_mailto = ""
            else:
                try:
                    result = subprocess.run(
                        ["pvesh", "get", f"/cluster/notifications/endpoints/{target_name}", "--output-format", "json"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if result.returncode == 0:
                        target_config = json.loads(result.stdout)
                        current_target_mailto = target_config.get("mailto", "")
                        if isinstance(current_target_mailto, list):
                            current_target_mailto = ",".join(current_target_mailto)
                    else:
                        current_target_mailto = ""
                except Exception:
                    current_target_mailto = ""
            
            # Aggiungi le nuove email
            if current_target_mailto:
                for email in current_target_mailto.split(","):
                    email = email.strip()
                    if email:
                        all_emails.add(email)
            
            # Aggiorna il target
            final_mailto = ",".join(sorted(all_emails))
            
            if execution_mode == "ssh" and executor:
                update_mailto_cmd = f"pvesh set /cluster/notifications/endpoints/{target_name} --mailto '{final_mailto}' 2>&1"
                update_mailto_result = executor(update_mailto_cmd)
                mailto_success = update_mailto_result is not None and "error" not in (update_mailto_result.lower() if update_mailto_result else "")
            else:
                try:
                    result = subprocess.run(
                        ["pvesh", "set", f"/cluster/notifications/endpoints/{target_name}", "--mailto", final_mailto],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    mailto_success = result.returncode == 0
                except Exception:
                    mailto_success = False
            
            if mailto_success:
                logger.info(f"  ✓ Email aggiunte al target")
                for email in sorted(all_emails):
                    logger.info(f"    • {email}")
            else:
                logger.info(f"  ⚠ Possibile errore nell'aggiornamento delle email")
            
            logger.info("")
        else:
            logger.info(f"  ℹ Nessuna email trovata nei backup job esistenti")
            logger.info("")
        
        logger.info("=" * 70)
        logger.info(f"  ✓ CONFIGURAZIONE COMPLETATA")
        logger.info(f"    Notification matcher: {matcher_name}")
        logger.info(f"    Target: {target_name}")
        if all_emails:
            logger.info(f"    Email nel target: {len(all_emails)}")
        logger.info("=" * 70)
        logger.info("")
        
        return True
        
    except Exception as e:
        logger.info(f"  ⚠ Errore durante configurazione backup jobs: {e}")
        import traceback
        traceback.print_exc()
        return False


def attempt_sftp_upload(uploader: SFTPUploader, files: List[str]) -> bool:
    sftp_conf = uploader.config.get("sftp", {})
    base_path = sftp_conf.get("base_path", "/tmp")

    original_host = sftp_conf.get("host")
    original_port = sftp_conf.get("port")

    attempts: List[Tuple[str, int]] = []
    if original_host and original_port:
        attempts.append((original_host, original_port))
        if original_port != 22:
            attempts.append((original_host, 22))

    if original_host != SFTP_FALLBACK_HOST:
        if original_port:
            attempts.append((SFTP_FALLBACK_HOST, original_port))
            if original_port != 22:
                attempts.append((SFTP_FALLBACK_HOST, 22))
        else:
            attempts.append((SFTP_FALLBACK_HOST, 22))

    tried: Set[Tuple[str, int]] = set()

    for host_try, port_try in attempts:
        if (host_try, port_try) in tried:
            continue
        tried.add((host_try, port_try))
        logger.info(f"→ Tentativo upload SFTP {host_try}:{port_try}")
        sftp_conf["host"] = host_try
        sftp_conf["port"] = port_try
        try:
            if uploader.connect():
                uploader.upload_files(files, base_path)
                uploader.close()
                logger.info("✓ Upload SFTP completato")
                return True
        except Exception as exc:
            logger.info(f"   ⚠ Upload fallito su {host_try}:{port_try}: {exc}")
        finally:
            try:
                uploader.close()
            except Exception:
                pass

    logger.info("✗ Tutti i tentativi di upload SFTP sono falliti")
    if original_host is not None:
        sftp_conf["host"] = original_host
    if original_port is not None:
        sftp_conf["port"] = original_port
    return False



# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------


def build_config(
    codcli: str,
    nomecliente: str,
    server_identifier: str,
    output_dir: Path,
    remote_enabled: bool,
    api_host: str,
    api_username: Optional[str],
    api_password: Optional[str],
    ssh_host: Optional[str],
    ssh_port: int,
    sftp_host_override: Optional[str],
    sftp_port_override: Optional[int],
    sftp_username_override: Optional[str],
    sftp_password_override: Optional[str],
    sftp_base_path_override: Optional[str],
) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "proxmox": {
            "enabled": True,
            "host": api_host,
            "username": api_username or "",
            "password": api_password or "",
            "password_encrypted": False,
            "verify_ssl": False,
            "client_mapping": {},
        },
        "ssh": {
            "enabled": remote_enabled,
            "host": ssh_host or "",
            "port": ssh_port,
            "username": "",
            "password": api_password if remote_enabled else "",
            "password_encrypted": False,
        },
        "client": {
            "codcli": codcli,
            "nomecliente": nomecliente,
            "server_identifier": server_identifier,
        },
        "sftp": {
            "enabled": True,
            "host": sftp_host_override or SFTP_HOST_DEFAULT,
            "port": sftp_port_override or SFTP_PORT_DEFAULT,
            "username": sftp_username_override or SFTP_USERNAME_DEFAULT,
            "password": sftp_password_override, # Password passed from args or config
            "base_path": sftp_base_path_override or SFTP_BASE_PATH_DEFAULT,
        },
        "features": FEATURES_DEFAULT.copy(),
        "system": {
            "output_directory": str(output_dir),
            "csv_directory": str(output_dir / "csv"),
            "backup_directory": str(output_dir / "backup"),
            "cleanup_days": 30,
            "max_file_size_mb": 10,
            "auto_cleanup": True,
            "max_file_copies": 5,
        },
    }

    if remote_enabled and api_username:
        ssh_user = api_username.split("@")[0] if "@" in api_username else api_username
        config["ssh"]["username"] = ssh_user
    else:
        config["ssh"]["username"] = ""
        config["ssh"]["password"] = ""

    return config


# ---------------------------------------------------------------------------
# Esecuzione principale (derivata da proxmox_report.main)
# ---------------------------------------------------------------------------


def run_report(config: Dict[str, Any], codcli: str, nomecliente: str, server_identifier: str, output_dir: Path, no_upload: bool) -> None:
    features_config = config.get("features", {})

    extractor = ProxmoxLocalExtractor(config, features_config)
    execution_mode = extractor.detect_execution_mode()
    logger.info("")

    if execution_mode == "ssh":
        if not extractor.connect_ssh():
            logger.info("✗ Connessione SSH fallita, impossibile procedere")
            sys.exit(1)

    # Crea il template di notifica per i backup
    executor = extractor.execute_command if execution_mode in ("local", "ssh") else None
    create_notification_template(codcli, nomecliente, execution_mode, executor)
    
    # Configura SMTP per le notifiche (se password disponibile)
    if execution_mode in ("local", "ssh") and executor:
        logger.info("")
        logger.info("→ Configurazione SMTP automatica...")
        # Cerca password nel config prima di usare DEFAULT_SMTP_PASSWORD
        smtp_password = None
        if config:
            smtp_config = config.get("smtp", {})
            smtp_password = smtp_config.get("password") or smtp_config.get("smtp_password")
        if not smtp_password:
            smtp_password = DEFAULT_SMTP_PASSWORD
        
        if smtp_password:
            logger.info(f"  Password SMTP: {'*' * len(smtp_password)} (configurata)")
        else:
            logger.info(f"  Password SMTP: non configurata (verrà richiesta)")
        
        # Crea il notification target
        target_created = configure_smtp_notification(smtp_password, codcli, execution_mode, executor, config)
        
        # Se il target è stato creato con successo, crea il notification matcher
        if target_created:
            target_name = f"da-alert-{codcli}"
            configure_backup_jobs_notification(target_name, codcli, execution_mode, executor)
    else:
        logger.info("")
        logger.info("  ℹ Configurazione SMTP saltata (modalità API o executor non disponibile)")
    
    logger.info("")

    logger.info("→ Estrazione informazioni nodo...")
    extractor.get_node_info()
    detected_identifier = extractor.node_info.get("hostname") or server_identifier
    server_identifier = detected_identifier
    logger.info("")

    collect_cluster = feature_enabled(features_config, "collect_cluster", True)
    if collect_cluster:
        extractor.get_cluster_info()
    else:
        logger.info("→ Raccolta informazioni cluster disabilitata")
    logger.info("")

    system_conf = config.get("system", {})
    csv_dir = Path(system_conf.get("csv_directory", output_dir / "csv"))
    backup_dir = Path(system_conf.get("backup_directory", output_dir / "backup"))
    ensure_directory(csv_dir)
    ensure_directory(backup_dir)

    all_hosts_info: List[Dict[str, Any]] = []
    collect_host = feature_enabled(features_config, "collect_host", True)
    collect_storage = feature_enabled(features_config, "collect_storage", True)
    collect_network = feature_enabled(features_config, "collect_network", True)
    collect_host_details = feature_enabled(features_config, "collect_host_details", True)

    if collect_host or collect_storage or collect_network or collect_host_details:
        all_hosts_info = extractor.get_all_hosts_info()
        current_hostname = extractor.node_info.get("hostname")
        if current_hostname and all_hosts_info:
            filtered = [host for host in all_hosts_info if host.get("hostname") == current_hostname]
            if filtered:
                all_hosts_info = filtered
        logger.info("")
        if all_hosts_info and execution_mode != "api" and (collect_host_details or collect_network):
            for host_info in all_hosts_info:
                if host_info.get("hostname") == current_hostname:
                    extractor.enrich_host_info_with_commands(host_info, extractor.execute_command)
                    if collect_host_details:
                        augment_local_host_details(host_info, extractor)
                    break
    else:
        logger.info("→ Raccolta informazioni host disabilitata")
        logger.info("")

    if collect_storage and all_hosts_info:
        current_hostname = extractor.node_info.get("hostname")
        if current_hostname:
            for host_info in all_hosts_info:
                if host_info.get("hostname") == current_hostname and not host_info.get("storage"):
                    if extractor.execution_mode in ("local", "ssh"):
                        populate_storage_via_pvesm(host_info, extractor.execute_command)
                    else:
                        logger.info("  ℹ Impossibile usare pvesm in modalità API pura")

    detailed_host_info = all_hosts_info[0] if all_hosts_info else {}
    if not detailed_host_info and (collect_host or collect_host_details):
        detailed_host_info = extractor.get_detailed_host_info_for_node(None)
        if detailed_host_info:
            all_hosts_info = [detailed_host_info]

    collect_vms = feature_enabled(features_config, "collect_vms", True)
    vms: List[Dict[str, Any]] = []
    csv_file: Optional[str] = None
    if collect_vms:
        vms = get_full_vm_details(extractor, config, execution_mode)
        logger.info("")
        csv_file_path = write_vms_csv(
            vms,
            csv_dir,
            codcli,
            nomecliente,
            server_identifier,
            int(system_conf.get("max_file_copies", 5)),
        )
        if csv_file_path:
            logger.info(f"✓ CSV VM salvato: {csv_file_path}")
            csv_file = str(csv_file_path)
        else:
            logger.info("✗ Errore salvataggio CSV VM")
            sys.exit(1)
        logger.info("")
    else:
        extractor.vms_data = []
        logger.info("→ Raccolta VM disabilitata")
        logger.info("")

    host_csv_file: Optional[str] = None
    storage_csv_file: Optional[str] = None
    network_csv_file: Optional[str] = None
    if collect_host or collect_storage or collect_network:
        max_copies = int(system_conf.get("max_file_copies", 5))
        if collect_host:
            host_csv_file = write_host_csv(all_hosts_info, csv_dir, codcli, nomecliente, server_identifier, max_copies)
        if collect_storage:
            storage_csv_file = write_storage_csv(all_hosts_info, csv_dir, codcli, nomecliente, server_identifier, max_copies)
        if collect_network:
            network_csv_file = write_network_csv(all_hosts_info, csv_dir, codcli, nomecliente, server_identifier, max_copies)

        if collect_host and host_csv_file and Path(host_csv_file).exists():
            logger.info(f"✓ CSV host salvato: {host_csv_file}")
        if collect_storage and storage_csv_file and storage_csv_file and Path(storage_csv_file).exists():
            logger.info(f"✓ CSV storage salvato: {storage_csv_file}")
        if collect_network and network_csv_file and network_csv_file and Path(network_csv_file).exists():
            logger.info(f"✓ CSV network salvato: {network_csv_file}")
        logger.info("")
    else:
        logger.info("→ Raccolta CSV host/storage/network disabilitata")
        logger.info("")

    backup_file: Optional[str] = None
    collect_backup = feature_enabled(features_config, "collect_backup", True)
    if collect_backup and execution_mode in ("local", "ssh"):
        backup_manager = ProxmoxBackupIntegrated(config)
        backup_manager.execution_mode = execution_mode
        backup_manager.ssh_client = extractor.ssh_client
        if backup_manager.create_backup(str(backup_dir), codcli, nomecliente, int(system_conf.get("max_file_copies", 5)), server_identifier):
            backup_file = backup_manager.backup_file
        logger.info("")
    else:
        if not collect_backup:
            logger.info("→ Backup disabilitato dalle feature")
        else:
            logger.info("→ Backup non disponibile in modalità API")
        logger.info("")

    if extractor.ssh_client:
        extractor.ssh_client.close()
        extractor.ssh_client = None
        logger.info("✓ Connessione SSH chiusa")
        logger.info("")

    if not no_upload and config.get("sftp", {}).get("enabled"):
        uploader = SFTPUploader(config)
        files: List[str] = []
        if csv_file and Path(csv_file).exists():
            files.append(csv_file)
        if collect_host and host_csv_file and Path(host_csv_file).exists():
            files.append(host_csv_file)
        if collect_storage and storage_csv_file and Path(storage_csv_file).exists():
            files.append(storage_csv_file)
        if collect_network and network_csv_file and Path(network_csv_file).exists():
            files.append(network_csv_file)
        if collect_backup and backup_file and Path(backup_file).exists():
            files.append(backup_file)
        if files:
            attempt_sftp_upload(uploader, files)
        logger.info("")
    else:
        logger.info("→ Upload SFTP disabilitato")
        logger.info("")

    # -----------------------------------------------------------------------
    # HTML REPORT & EMAIL
    # -----------------------------------------------------------------------
    logger.info("=" * 70)
    logger.info("GENERAZIONE REPORT HTML")
    logger.info("=" * 70)
    
    html_file = output_dir / f"{codcli}_{nomecliente}_report.html"
    
    # Prepare data for template
    report_data = {
        "client": {
            "codcli": codcli,
            "nomecliente": nomecliente,
            "server_identifier": server_identifier
        },
        "cluster": extractor.cluster_info,
        "hosts": all_hosts_info,
        "vms": vms
    }
    
    html_reporter = HTMLReporter(template_dir=CURRENT_DIR / "templates")
    html_generated = html_reporter.generate_report(report_data, str(html_file))
    
    if html_generated:
        logger.info(f"  📄 HTML Report: {html_file}")
        
        # Email Sending
        smtp_config = config.get("smtp", {})
        if smtp_config.get("enabled"):
            logger.info("=" * 70)
            logger.info("INVIO EMAIL")
            logger.info("=" * 70)
            
            email_sender = EmailSender(config)
            subject = f"Proxmox Report - {nomecliente} ({codcli})"
            
            # Attachments list
            attachments = []
            if html_file.exists():
                attachments.append(str(html_file))
            
            # Read HTML content for body
            try:
                with open(html_file, "r") as f:
                    html_body = f.read()
                    
                if email_sender.send_report(html_body, subject, attachments=attachments):
                     pass # Logged inside sender
            except Exception as e:
                logger.error(f"Errore preparazione email: {e}")
        else:
             logger.info("→ Invio email disabilitato (smtp.enabled=false o assente)")
    else:
        logger.warning("⚠ impossibile generare report HTML")

    logger.info("=" * 70)
    logger.info("✓ REPORT COMPLETATO")
    logger.info("=" * 70)
    logger.info("File generati:")
    if csv_file and Path(csv_file).exists():
        logger.info(f"  📄 CSV VM:  {csv_file}")
    if collect_host and host_csv_file and Path(host_csv_file).exists():
        logger.info(f"  📄 CSV Host:  {host_csv_file}")
    if collect_storage and storage_csv_file and storage_csv_file and Path(storage_csv_file).exists():
        logger.info(f"  📄 CSV Storage:  {storage_csv_file}")
    if collect_network and network_csv_file and network_csv_file and Path(network_csv_file).exists():
        logger.info(f"  📄 CSV Network:  {network_csv_file}")
    if collect_backup and backup_file and Path(backup_file).exists():
        logger.info(f"  📦 Backup: {backup_file}")
    logger.info("")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Proxmox local reporter (cron edition)")
    parser.add_argument("--codcli", help="Codice cliente (opzionale se presente config.json)")
    parser.add_argument("--nomecliente", help="Nome cliente (opzionale se presente config.json)")
    parser.add_argument("--output-dir", default="reports", help="Directory di output (default: reports)")
    parser.add_argument("--no-upload", action="store_true", help="Non eseguire l'upload SFTP")
    parser.add_argument("--host", help="Hostname/IP Proxmox remoto (senza porta, useremo 8006 per API)")
    parser.add_argument("--username", help="Utente API/SSH (es. root@pam) per accesso remoto")
    parser.add_argument("--password", help="Password API/SSH per accesso remoto")
    parser.add_argument("--ssh-port", type=int, default=22, help="Porta SSH remota (default 22)")
    parser.add_argument("--local", action="store_true", help="Forza modalità locale (ignora --host)")
    parser.add_argument("--sftp-host", help="Host SFTP (override)")
    parser.add_argument("--sftp-port", type=int, help="Porta SFTP (override)")
    parser.add_argument("--sftp-user", help="Username SFTP (override)")
    parser.add_argument("--sftp-password", help="Password SFTP (override)")
    parser.add_argument("--sftp-base-path", help="Percorso base SFTP (override)")
    parser.add_argument("--config", help="Percorso file di configurazione JSON (opzionale)")
    parser.add_argument("--auto-update", action="store_true", help="Verifica e applica aggiornamenti prima dell'esecuzione")
    parser.add_argument("--skip-update", action="store_true", help="Salta la verifica aggiornamenti automatica")
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args()
    return args


def populate_storage_via_pvesm(host_info: Dict[str, Any], executor) -> None:
    """
    Estrae informazioni storage via pvesm status (solo formato tabellare).
    pvesm status NON supporta JSON, quindi viene sempre parsato il testo.
    """
    try:
        raw_output = executor("pvesm status 2>/dev/null")
        if not raw_output:
            logger.info("      ℹ pvesm status non ha restituito output (verifica PATH e permessi)")
            return
        
        storage_data = _parse_pvesm_text(raw_output)
        if not storage_data:
            logger.info("      ℹ Nessun dato storage restituito da pvesm")
            return
        
        storages: List[Dict[str, Any]] = []
        for storage in storage_data:
            storage_info = {
                "hostname": host_info.get("hostname"),
                "name": storage.get("name"),
                "type": storage.get("type"),
                "status": storage.get("status"),
                "total_gb": storage.get("total_gb"),
                "used_gb": storage.get("used_gb"),
                "available_gb": storage.get("available_gb"),
                "used_percent": storage.get("used_percent"),
                "content": storage.get("content"),
            }
            storages.append(storage_info)
        
        if storages:
            host_info["storage"] = storages
            logger.info(f"  → Storage locale via pvesm: {len(storages)} elementi")
    except Exception as exc:
        logger.info(f"      ⚠ Errore raccolta storage via pvesm: {exc}")


def _parse_pvesm_text(raw_output: str) -> List[Dict[str, Any]]:
    lines = [line.rstrip() for line in raw_output.splitlines() if line.strip()]
    if len(lines) <= 1:
        return []
    entries: List[Dict[str, Any]] = []
    for line in lines[1:]:
        columns = re.split(r"\s+", line.strip())
        if len(columns) < 7:
            continue
        name, stype, status, total_raw, used_raw, avail_raw, percent_raw = (
            columns[0],
            columns[1],
            columns[2],
            columns[3],
            columns[4],
            columns[5],
            columns[6],
        )
        total_gb = _safe_parse_size(total_raw)
        used_gb = _safe_parse_size(used_raw)
        avail_gb = _safe_parse_size(avail_raw)
        used_percent = None
        percent_match = re.search(r"([\d.,]+)", percent_raw)
        if percent_match:
            try:
                used_percent = float(percent_match.group(1).replace(",", "."))
            except ValueError:
                used_percent = None
        if used_percent is None and total_gb not in (None, 0) and used_gb is not None:
            used_percent = (used_gb / total_gb) * 100.0
        entries.append(
            {
                "name": name,
                "type": stype,
                "status": status,
                "total_gb": total_gb,
                "used_gb": used_gb,
                "available_gb": avail_gb,
                "used_percent": used_percent,
                "content": None,
            }
        )
    return entries


def _safe_parse_size(value: Optional[str]) -> Optional[float]:
    """
    Converte byte in GiB.
    pvesm status restituisce valori in byte (senza unità), quindi convertiamo direttamente.
    """
    if not value:
        return None
    value = value.strip()
    try:
        numeric = float(value)
        return numeric / (1024**3)
    except ValueError:
        return None


def _run_pvesh_json(extractor: ProxmoxLocalExtractor, endpoint: str, timeout: int = 30) -> Optional[Any]:
    """
    Esegue pvesh (locale o via SSH) e restituisce il risultato JSON.
    Restituisce None in caso di errore o output vuoto.
    """
    cmd = f"pvesh get {endpoint} --output-format json"
    try:
        if extractor.execution_mode == "ssh":
            output = extractor.execute_command(f"{cmd} 2>/dev/null")
            if not output:
                return None
            return json.loads(output)
        else:
            result = subprocess.run(
                ["pvesh", "get", endpoint, "--output-format", "json"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                return None
            stdout = result.stdout.strip()
            if not stdout:
                return None
            return json.loads(stdout)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, subprocess.SubprocessError) as exc:
        logger.info(f"  ⚠ Errore pvesh {endpoint}: {exc}")
        return None
    except Exception as exc:
        logger.info(f"  ⚠ Errore imprevisto pvesh {endpoint}: {exc}")
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if "=" in lowered or "," in lowered:
            parts = re.split(r"[,\s]+", lowered)
            for part in parts:
                if part in {"1", "true", "yes", "on", "enabled"}:
                    return True
                if "=" in part:
                    key, val = part.split("=", 1)
                    if val in {"1", "true", "yes", "on", "enabled"}:
                        return True
    return False


def _clean_string(value: Any) -> str:
    if value is None:
        return NULL_TOKEN
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.upper() == "N/A":
            return NULL_TOKEN
        return stripped
    return str(value)


def _flatten_field_value(value: Any) -> Optional[str]:
    if value in (None, "", "N/A", NULL_TOKEN):
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        parts: List[str] = []
        for key, sub_value in value.items():
            flattened = _flatten_field_value(sub_value)
            if flattened:
                parts.append(f"{key}={flattened}")
        return " | ".join(parts) if parts else None
    if isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            flattened = _flatten_field_value(item)
            if flattened:
                parts.append(flattened)
        return " | ".join(parts) if parts else None
    return _clean_string(value)


def _prepare_csv_value(value: Any) -> str:
    formatted_blocks = _format_detail_blocks(value)
    if formatted_blocks is not None:
        return formatted_blocks
    flattened = _flatten_field_value(value)
    return flattened if flattened is not None else NULL_TOKEN


def _format_detail_blocks(value: Any) -> Optional[str]:
    items: List[str] = []

    def format_dict(entry: Dict[str, Any]) -> Optional[str]:
        parts: List[str] = []
        for key, val in entry.items():
            if val in (None, "", "N/A", NULL_TOKEN):
                continue
            formatted = _flatten_field_value(val)
            if formatted in (None, "", NULL_TOKEN):
                continue
            parts.append(f"{key}={formatted}")
        if not parts:
            return None
        return "{ " + ", ".join(parts) + " }"

    if isinstance(value, dict) and "id" in value:
        formatted = format_dict(value)
        return formatted if formatted is not None else None

    if isinstance(value, (list, tuple, set)):
        for element in value:
            if isinstance(element, dict) and "id" in element:
                formatted = format_dict(element)
                if formatted:
                    items.append(formatted)
            else:
                return None
        if items:
            return "\n".join(items)

    return None


def _format_bytes(value: Any) -> str:
    human = human_bytes(value)
    return NULL_TOKEN if human == NULL_TOKEN else human


def _format_percent(value: Any) -> str:
    if value in (None, "", "N/A"):
        return NULL_TOKEN
    try:
        numeric = float(value)
        if numeric <= 1.0:
            numeric *= 100.0
        return f"{numeric:.1f}%"
    except (TypeError, ValueError):
        return _clean_string(value)


def _format_duration(value: Any) -> str:
    human = seconds_to_human(value)
    return NULL_TOKEN if human == NULL_TOKEN else human


def _parse_disk_entries(config: Dict[str, Any]) -> (List[str], List[Dict[str, Any]]):
    disk_ids: List[str] = []
    disk_details: List[Dict[str, Any]] = []
    for key, raw_value in (config or {}).items():
        if not isinstance(raw_value, str):
            continue
        if key.startswith(("scsi", "sata", "ide", "virtio", "mp", "unused")):
            detail: Dict[str, Any] = {"id": key}
            if key.startswith("mp"):
                detail["type"] = "mountpoint"
            elif key.startswith("unused"):
                detail["type"] = "unused"
            else:
                detail["type"] = "disk"
                if key not in disk_ids:
                    disk_ids.append(key)
            segments = [seg.strip() for seg in raw_value.split(",") if seg.strip()]
            if segments:
                first = segments.pop(0)
                if ":" in first and not first.startswith("/"):
                    storage, volume = first.split(":", 1)
                    detail["storage"] = storage
                    detail["volume"] = volume
                else:
                    detail["volume"] = first
            for segment in segments:
                if "=" in segment:
                    param, param_value = segment.split("=", 1)
                    detail[param.strip()] = param_value.strip()
            disk_details.append(detail)
    return disk_ids, disk_details


def _parse_network_entries(config: Dict[str, Any]) -> (List[str], List[Dict[str, Any]]):
    net_ids: List[str] = []
    net_details: List[Dict[str, Any]] = []
    for key, raw_value in (config or {}).items():
        if not isinstance(raw_value, str) or not key.startswith("net"):
            continue
        detail: Dict[str, Any] = {"id": key}
        segments = [seg.strip() for seg in raw_value.split(",") if seg.strip()]
        if segments:
            first = segments.pop(0)
            if "=" in first:
                model, mac = first.split("=", 1)
                detail["model"] = model.strip()
                detail["mac"] = mac.strip()
        for segment in segments:
            if "=" in segment:
                param, param_value = segment.split("=", 1)
                detail[param.strip()] = param_value.strip()
        net_ids.append(key)
        net_details.append(detail)
    return net_ids, net_details


def _collect_agent_interfaces(agent_data: Any) -> List[Dict[str, Any]]:
    interfaces: List[Dict[str, Any]] = []
    if not agent_data:
        return interfaces
    if isinstance(agent_data, dict) and "result" in agent_data:
        raw_list = agent_data.get("result")
    else:
        raw_list = agent_data
    if not isinstance(raw_list, list):
        return interfaces
    for iface in raw_list:
        if not isinstance(iface, dict):
            continue
        name = iface.get("name")
        mac = iface.get("hardware-address") or iface.get("hardware_address")
        ip_entries = iface.get("ip-addresses") or iface.get("ip_addresses") or []
        ipv4_list: List[str] = []
        ipv6_list: List[str] = []
        ips_all: List[str] = []
        if isinstance(ip_entries, list):
            for ip_entry in ip_entries:
                if not isinstance(ip_entry, dict):
                    continue
                ip_value = ip_entry.get("ip-address") or ip_entry.get("ip") or ""
                if not ip_value:
                    continue
                ip_value = ip_value.strip()
                if ip_value.startswith(("127.", "::1", "fe80:", "169.254.")):
                    continue
                ips_all.append(ip_value)
                if ":" in ip_value:
                    ipv6_list.append(ip_value)
                else:
                    ipv4_list.append(ip_value)
        interfaces.append(
            {
                "name": name,
                "mac": mac,
                "ips": ips_all,
                "ipv4": ipv4_list,
                "ipv6": ipv6_list,
            }
        )
    return interfaces


def _merge_agent_with_network(
    network_details: List[Dict[str, Any]], agent_interfaces: List[Dict[str, Any]]
) -> Dict[str, Any]:
    aggregated_ipv4: List[str] = []
    aggregated_ipv6: List[str] = []
    aggregated_ips: List[str] = []
    matched_macs: set[str] = set()

    for detail in network_details:
        mac = (detail.get("mac") or "").lower()
        matched = None
        if mac:
            for agent_iface in agent_interfaces:
                agent_mac = (agent_iface.get("mac") or "").lower()
                if agent_mac and agent_mac == mac:
                    matched = agent_iface
                    matched_macs.add(agent_mac)
                    break
        if matched:
            if matched.get("name"):
                detail["interface_name"] = matched["name"]
            if matched.get("ips"):
                detail["ips"] = matched["ips"]
                aggregated_ips.extend(matched["ips"])
            if matched.get("ipv4"):
                detail["ipv4"] = matched["ipv4"]
                aggregated_ipv4.extend(matched["ipv4"])
            if matched.get("ipv6"):
                detail["ipv6"] = matched["ipv6"]
                aggregated_ipv6.extend(matched["ipv6"])

    for agent_iface in agent_interfaces:
        agent_mac = (agent_iface.get("mac") or "").lower()
        if agent_mac and agent_mac in matched_macs:
            continue
        supplemental = {
            "id": agent_iface.get("name") or (agent_iface.get("mac") and f"agent-{agent_iface.get('mac')}"),
            "model": agent_iface.get("model", "guest"),
            "mac": agent_iface.get("mac"),
            "ips": agent_iface.get("ips", []),
            "ipv4": agent_iface.get("ipv4", []),
            "ipv6": agent_iface.get("ipv6", []),
        }
        network_details.append(supplemental)
        aggregated_ips.extend(agent_iface.get("ips", []))
        aggregated_ipv4.extend(agent_iface.get("ipv4", []))
        aggregated_ipv6.extend(agent_iface.get("ipv6", []))

    return {
        "all_ips": sorted({ip for ip in aggregated_ips}),
        "ipv4": sorted({ip for ip in aggregated_ipv4}),
        "ipv6": sorted({ip for ip in aggregated_ipv6}),
    }


def _parse_snapshot_info(snapshot_data: Any) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    if not snapshot_data:
        return snapshots
    raw_list = snapshot_data
    if isinstance(snapshot_data, dict) and "data" in snapshot_data:
        raw_list = snapshot_data.get("data")
    if not isinstance(raw_list, list):
        return snapshots
    for snap in raw_list:
        if not isinstance(snap, dict):
            continue
        snapshots.append(
            {
                "name": snap.get("name"),
                "snaptime": snap.get("snaptime"),
                "description": snap.get("description"),
                "parent": snap.get("parent"),
                "vmstate": snap.get("vmstate"),
            }
        )
    return snapshots


def _collect_temperature_readings(executor) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    readings: List[Dict[str, Any]] = []
    highest: Optional[float] = None

    try:
        sensors_json = executor("sensors -Aj 2>/dev/null")
        if sensors_json:
            try:
                data = json.loads(sensors_json)
                for chip_name, chip_data in data.items():
                    if not isinstance(chip_data, dict):
                        continue
                    adapter = chip_data.get("Adapter") or chip_data.get("adapter")
                    for sensor_name, sensor_values in chip_data.items():
                        if not isinstance(sensor_values, dict):
                            continue
                        for key, value in sensor_values.items():
                            if not key.endswith("_input"):
                                continue
                            try:
                                temp_val = float(value)
                            except (TypeError, ValueError):
                                continue
                            entry = {
                                "chip": chip_name,
                                "sensor": sensor_name,
                                "adapter": adapter,
                                "temperature_c": round(temp_val, 1),
                            }
                            readings.append(entry)
                            highest = temp_val if highest is None else max(highest, temp_val)
                if readings:
                    return readings, highest
            except (json.JSONDecodeError, TypeError):
                pass

        sensors_output = executor("sensors 2>/dev/null")
        if sensors_output:
            current_chip = None
            for line in sensors_output.splitlines():
                line = line.rstrip()
                if not line:
                    continue
                if ":" not in line and line.endswith(":"):
                    current_chip = line.rstrip(":")
                    continue
                match = re.search(r"([A-Za-z0-9_./+\- ]+?):\s*([+\-]?\d+(?:\.\d+)?)°C", line)
                if match:
                    label = match.group(1).strip()
                    temp = float(match.group(2))
                    entry = {
                        "chip": current_chip,
                        "sensor": label,
                        "adapter": None,
                        "temperature_c": round(temp, 1),
                    }
                    readings.append(entry)
                    highest = temp if highest is None else max(highest, temp)
        if not readings:
            zone_base = "/sys/class/thermal"
            thermal_type = executor("for f in /sys/class/thermal/thermal_zone*/type; do cat \"$f\" 2>/dev/null; done")
            zone_outputs = executor("for f in /sys/class/thermal/thermal_zone*/temp; do cat \"$f\" 2>/dev/null; done")
            if thermal_type and zone_outputs:
                types = [t.strip() for t in thermal_type.splitlines() if t.strip()]
                temps = [t.strip() for t in zone_outputs.splitlines() if t.strip()]
                for idx, temp_str in enumerate(temps):
                    try:
                        milli = float(temp_str)
                        if milli > 1000:
                            value = milli / 1000.0
                        else:
                            value = milli
                    except (TypeError, ValueError):
                        continue
                    entry = {
                        "chip": types[idx] if idx < len(types) else None,
                        "sensor": f"thermal_zone{idx}",
                        "adapter": None,
                        "temperature_c": round(value, 1),
                    }
                    readings.append(entry)
                    highest = value if highest is None else max(highest, value)
    except Exception:
        pass

    return readings, highest


def _collect_boot_devices(executor) -> List[Dict[str, Any]]:
    try:
        lsblk_output = executor(
            "lsblk --json -o NAME,TYPE,SIZE,MOUNTPOINT,MODEL,SERIAL,FSTYPE,TRAN,ROTA,RM,PARTFLAGS 2>/dev/null"
        )
        if not lsblk_output:
            return []
        data = json.loads(lsblk_output)
    except Exception:
        return []

    devices: List[Dict[str, Any]] = []

    def visit(nodes, parent_name=None):
        for node in nodes or []:
            name = node.get("name")
            dev_type = node.get("type")
            entry = {
                "name": name,
                "type": dev_type,
                "size": node.get("size"),
                "model": node.get("model"),
                "serial": node.get("serial"),
                "mountpoint": node.get("mountpoint"),
                "fstype": node.get("fstype"),
                "transport": node.get("tran"),
                "rotational": node.get("rota"),
                "removable": node.get("rm"),
                "parent": parent_name,
                "partflags": node.get("partflags"),
            }
            flags = str(node.get("partflags") or "").lower()
            entry["is_boot"] = bool(
                flags and any(flag in flags for flag in ("boot", "esp", "legacy_boot", "bios_grub"))
            )
            devices.append(entry)
            children = node.get("children") or node.get("children".upper())
            if children:
                visit(children, name)

    visit(data.get("blockdevices"))
    return devices


def _collect_pci_devices(executor, limit: int = 30) -> List[str]:
    try:
        lspci_output = executor("lspci 2>/dev/null")
        if not lspci_output:
            return []
        lines = [line.strip() for line in lspci_output.splitlines() if line.strip()]
        return lines[:limit]
    except Exception:
        return []


def _collect_usb_devices(executor, limit: int = 30) -> List[str]:
    try:
        lsusb_output = executor("lsusb 2>/dev/null")
        if not lsusb_output:
            return []
        lines = [line.strip() for line in lsusb_output.splitlines() if line.strip()]
        return lines[:limit]
    except Exception:
        return []


def _collect_lshw_summary(executor, limit: int = 40) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    system_product: Optional[str] = None

    def try_command(cmds: List[str]) -> str:
        for cmd in cmds:
            output = executor(f"LANG=C {cmd} 2>/dev/null")
            if output:
                if "command not found" in output.lower():
                    continue
                return output
        return ""

    short_output = try_command(["lshw -short", "/usr/sbin/lshw -short"])

    allowed_keywords = {"system", "bus", "memory", "processor", "storage", "disk", "volume", "network"}

    if short_output:
        for raw_line in short_output.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            if line.startswith("H/W path") or line.startswith("="):
                continue
            columns = re.split(r"\s{2,}", line.strip())
            if not columns:
                continue
            path = device = description = ""
            clazz = ""
            if len(columns) >= 4:
                path, device, clazz, description = columns[0], columns[1], columns[2], " ".join(columns[3:])
            elif len(columns) == 3:
                path, clazz, description = columns[0], columns[1], columns[2]
            elif len(columns) == 2:
                clazz, description = columns[0], columns[1]
            else:
                continue
            clazz = clazz.strip().lower()
            if clazz == "system" and not system_product and not path and not device:
                system_product = description
                continue
            if clazz not in allowed_keywords:
                continue
            entry_parts = []
            if device:
                entry_parts.append(device)
            entry_parts.append(description)
            entry = " - ".join(part for part in entry_parts if part)
            sections.setdefault(clazz, []).append(f"[{entry.strip()}]")
        for key, entries in list(sections.items()):
            sections[key] = entries[:limit]
        return sections

    json_output = try_command(["lshw -quiet -json", "/usr/sbin/lshw -quiet -json"])

    if json_output:
        try:
            data = json.loads(json_output)
        except json.JSONDecodeError:
            return {}
        nodes = data if isinstance(data, list) else [data]

        def walk(node, depth: int = 0, parent_path: str = "") -> None:
            if not isinstance(node, dict):
                return
            node_class = (node.get("class") or node.get("type") or "").lower()
            if node_class and node_class in allowed_keywords:
                path = node.get("id") or node.get("handle") or parent_path
                device = node.get("logicalname") or node.get("dev") or ""
                description = node.get("description") or node.get("product") or ""
                entry = " - ".join(part for part in [path, device, description] if part)
                if entry:
                    sections.setdefault(node_class, []).append(f"[{entry}]")
            for child in node.get("children", []) or []:
                child_path = parent_path
                if node.get("id"):
                    child_path = node.get("id")
                walk(child, depth + 1, child_path)

        for node in nodes:
            walk(node)
        for key, entries in list(sections.items()):
            sections[key] = entries[:limit]
        if system_product:
            sections.setdefault("system", []).insert(0, f"[{system_product}]")
        return sections

    return {}


def _collect_bios_info(executor) -> Dict[str, str]:
    info: Dict[str, str] = {}
    sys_paths = {
        "bios_vendor": "/sys/class/dmi/id/bios_vendor",
        "bios_version": "/sys/class/dmi/id/bios_version",
        "bios_release_date": "/sys/class/dmi/id/bios_date",
        "system_manufacturer": "/sys/class/dmi/id/sys_vendor",
        "system_product": "/sys/class/dmi/id/product_name",
        "system_serial": "/sys/class/dmi/id/product_serial",
        "board_vendor": "/sys/class/dmi/id/board_vendor",
        "board_name": "/sys/class/dmi/id/board_name",
    }

    for key, path in sys_paths.items():
        try:
            value = executor(f"cat {path} 2>/dev/null")
            if value:
                info[key] = value.strip()
        except Exception:
            continue

    if not info.get("bios_vendor") or not info.get("bios_version"):
        try:
            dmidecode_output = executor("dmidecode -t bios 2>/dev/null")
            if dmidecode_output:
                vendor_match = re.search(r"Vendor:\s*(.+)", dmidecode_output)
                version_match = re.search(r"Version:\s*(.+)", dmidecode_output)
                date_match = re.search(r"Release Date:\s*(.+)", dmidecode_output)
                if vendor_match:
                    info.setdefault("bios_vendor", vendor_match.group(1).strip())
                if version_match:
                    info.setdefault("bios_version", version_match.group(1).strip())
                if date_match:
                    info.setdefault("bios_release_date", date_match.group(1).strip())
        except Exception:
            pass

    return info


def augment_local_host_details(host_info: Dict[str, Any], extractor: ProxmoxLocalExtractor) -> None:
    def run(cmd: str) -> str:
        try:
            if extractor.execution_mode == "ssh" and getattr(extractor, "execute_command", None):
                return extractor.execute_command(cmd) or ""
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return result.stdout
        except Exception:
            return ""
        return ""

    lscpu_output = run("lscpu 2>/dev/null")
    if lscpu_output:
        pass

    meminfo_output = run("cat /proc/meminfo 2>/dev/null")
    if meminfo_output:
        pass

    temperature_readings, highest_temp = _collect_temperature_readings(run)
    if temperature_readings and not host_info.get("temperature_summary"):
        formatted = []
        for entry in temperature_readings[:20]:
            chip = entry.get("chip") or "sensor"
            sensor = entry.get("sensor") or chip
            temp_val = entry.get("temperature_c")
            adapter = entry.get("adapter")
            label = f"{sensor}"
            if chip and chip != sensor:
                label = f"{chip} - {sensor}"
            if adapter:
                label = f"{label} [{adapter}]"
            if temp_val is not None:
                label = f"{label}: {temp_val:.1f}°C"
            formatted.append(label)
        host_info["temperature_summary"] = formatted
    if highest_temp is not None and not host_info.get("temperature_highest_c"):
        host_info["temperature_highest_c"] = round(highest_temp, 1)

    bios_info = _collect_bios_info(run)
    for key, value in bios_info.items():
        if value and not host_info.get(key):
            host_info[key] = value

    boot_devices = _collect_boot_devices(run)
    if boot_devices:
        host_info.setdefault("boot_devices_details", boot_devices)
        summaries: List[str] = []
        for device in boot_devices[:20]:
            name = device.get("name") or "?"
            dev_type = device.get("type") or "?"
            size = device.get("size") or "?"
            model = device.get("model") or ""
            serial = device.get("serial") or ""
            mountpoint = device.get("mountpoint") or ""
            boot_flag = " (boot)" if device.get("is_boot") else ""
            summary_parts = [f"{name}", f"{dev_type}", size]
            if model:
                summary_parts.append(model)
            if serial:
                summary_parts.append(serial)
            if mountpoint:
                summary_parts.append(f"mnt:{mountpoint}")
            summaries.append(" | ".join(part for part in summary_parts if part) + boot_flag)
        host_info["boot_devices"] = summaries

    if not host_info.get("boot_entries"):
        boot_entries_output = run("efibootmgr -v 2>/dev/null")
        if boot_entries_output:
            entries = [line.strip() for line in boot_entries_output.splitlines() if line.strip()]
            if entries:
                host_info["boot_entries"] = entries[:40]

    lshw_info = _collect_lshw_summary(run)
    for key in ("boot_entries", "boot_devices", "boot_devices_details"):
        host_info.pop(key, None)
    if lshw_info:
        host_info["hardware_system"] = lshw_info.get("system") or []
        host_info["hardware_bus"] = lshw_info.get("bus") or []
        host_info["hardware_memory"] = lshw_info.get("memory") or []
        host_info["hardware_processor"] = lshw_info.get("processor") or []
        host_info["hardware_storage"] = lshw_info.get("storage") or []
        host_info["hardware_disk"] = lshw_info.get("disk") or []
        host_info["hardware_volume"] = lshw_info.get("volume") or []
        host_info["hardware_network"] = lshw_info.get("network") or []
        if lshw_info.get("system"):
            host_info["hardware_product"] = lshw_info.get("system")[0]
        host_info.pop("hardware_summary", None)

    host_info.pop("pci_devices", None)
    host_info.pop("usb_devices", None)


def _finalize_vm_record(vm: Dict[str, Any]) -> Dict[str, Any]:
    record = dict(vm)

    record["cpu_percent"] = _format_percent(record.pop("cpu_usage", None))
    record["mem_used"] = _format_bytes(record.get("mem_used"))
    record["mem_total"] = _format_bytes(record.get("mem_total"))
    record["memory_assigned"] = _format_bytes(record.get("memory_assigned"))
    record["disk_used"] = _format_bytes(record.get("disk_used"))
    record["disk_size"] = _format_bytes(record.get("disk_size"))
    record["diskread"] = _format_bytes(record.get("diskread"))
    record["diskwrite"] = _format_bytes(record.get("diskwrite"))
    record["netin"] = _format_bytes(record.get("netin"))
    record["netout"] = _format_bytes(record.get("netout"))
    record["balloon_actual"] = _format_bytes(record.get("balloon_actual"))

    if record.get("balloon_target"):
        try:
            record["balloon_target"] = _format_bytes(int(record["balloon_target"]) * 1024 * 1024)
        except (ValueError, TypeError):
            record["balloon_target"] = _clean_string(record["balloon_target"])
    else:
        record["balloon_target"] = ""

    record["uptime"] = _format_duration(record.get("uptime"))

    record["agent_enabled"] = "Yes" if _truthy(record.get("agent_enabled")) else "No"
    record["template"] = "Yes" if _truthy(record.get("template")) else ""
    record["numa"] = "Yes" if _truthy(record.get("numa")) else ""
    record["onboot"] = "Yes" if _truthy(record.get("onboot")) else ""
    record["protection"] = "Yes" if _truthy(record.get("protection")) else ""
    record["ha_managed"] = "Yes" if _truthy(record.get("ha_managed")) else ""
    record["kvm"] = "Yes" if _truthy(record.get("kvm")) else ""

    disk_list = record.get("disks_details") or []
    record["disks_details"] = _prepare_csv_value(disk_list)
    net_list = record.get("networks_details") or []
    record["networks_details"] = _prepare_csv_value(net_list)

    snapshot_list = record.get("snapshots_details") or []
    record["snapshots_count"] = len(snapshot_list)
    record["snapshots_details"] = _prepare_csv_value(snapshot_list)
    record["snapshots_present"] = "Yes" if snapshot_list else ""

    all_ips = record.get("ip_addresses") or []
    if isinstance(all_ips, list):
        record["ip_addresses"] = " | ".join(sorted(all_ips))
    else:
        record["ip_addresses"] = _clean_string(all_ips)

    ipv4_list = record.get("ipv4") or []
    ipv6_list = record.get("ipv6") or []
    if isinstance(ipv4_list, list):
        record["ipv4"] = " | ".join(sorted(ipv4_list))
    else:
        record["ipv4"] = _clean_string(ipv4_list)
    if isinstance(ipv6_list, list):
        record["ipv6"] = " | ".join(sorted(ipv6_list))
    else:
        record["ipv6"] = _clean_string(ipv6_list)

    primary_ip = ""
    if record["ipv4"] and record["ipv4"] != NULL_TOKEN:
        primary_ip = record["ipv4"].split("|")[0].strip()
    elif record["ipv6"] and record["ipv6"] != NULL_TOKEN:
        primary_ip = record["ipv6"].split("|")[0].strip()
    record["primary_ip"] = primary_ip if primary_ip else NULL_TOKEN

    if record.get("creation_time"):
        try:
            timestamp = float(record["creation_time"])
            record["creation_time"] = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            record["creation_time"] = _clean_string(record["creation_time"])
    else:
        record["creation_time"] = NULL_TOKEN

    for key, value in list(record.items()):
        if value in (None, "N/A", [], {}, "[]", "{}", ""):
            record[key] = NULL_TOKEN
        elif isinstance(value, str):
            stripped = value.strip()
            record[key] = stripped if stripped else NULL_TOKEN

    return record


VMS_EXPORT_FIELDS: List[str] = [
    "node",
    "vmid",
    "name",
    "status",
    "vm_type",
    "cores",
    "sockets",
    "mem_used",
    "mem_total",
    "disk_size",
    "diskread",
    "diskwrite",
    "netin",
    "netout",
    "uptime",
    "primary_ip",
    "ipv4",
    "bios",
    "ostype",
    "boot",
    "startup",
    "agent_enabled",
    "agent_version",
    "agent_options",
    "num_disks",
    "disks",
    "disks_details",
    "num_networks",
    "networks",
    "networks_details",
    "primary_bridge",
    "snapshots_count",
    "tags",
    "pid",
]


HOST_EXPORT_FIELDS: List[Tuple[str, str]] = [
    ("server_identifier", "srv_id"),
    ("uptime_human", "uptime"),
    ("manager_version", "prox_ver"),
    ("kernel_version", "prox_kern"),
    ("cpu_model", "cpu"),
    ("cpu_cores", "cpu_cores"),
    ("cpu_sockets", "cpu_sockets"),
    ("cpu_threads", "cpu_threads"),
    ("load_average_15m", "load_15m"),
    ("memory_total_gb", "mem_tot"),
    ("memory_used_gb", "mem_used"),
    ("swap_total_gb", "swap_tot"),
    ("swap_used_gb", "swap_used"),
    ("temperature_highest_c", "temp_max"),
    ("temperature_summary", "temp"),
    ("hardware_memory", "HW_mem"),
    ("hardware_storage", "HW_stor"),
    ("hardware_disk", "HW_disk"),
    ("hardware_volume", "HW_vol"),
    ("hardware_network", "HW_net"),
    ("bios_vendor", "HW_bios"),
    ("bios_version", "HW_bios_ver"),
    ("bios_release_date", "HW_bios_date"),
    ("system_manufacturer", "HW_prod"),
    ("system_product", "HW_model"),
    ("system_serial", "HW_serial"),
    ("board_vendor", "HW_manuf"),
    ("board_name", "HW_board"),
    ("license_status", "lic_status"),
    ("license_level", "lic_level"),
    ("subscription_type", "lic_type"),
    ("subscription_key", "lic_key"),
    ("subscription_server_id", "lic_subs"),
    ("subscription_sockets", "lic_sock"),
    ("subscription_next_due", "lic_scad"),
]


def _format_host_value(key: str, value: Any) -> str:
    if value in (None, "", "N/A", NULL_TOKEN):
        return NULL_TOKEN
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple, set, dict)):
        flattened = _flatten_field_value(value)
        return flattened if flattened is not None else NULL_TOKEN
    if isinstance(value, (int, float)):
        if key.endswith("_gb"):
            return format_decimal(value)
        if key.endswith("_percent") or key.startswith("load_average") or key in {"io_delay_percent", "cpu_usage_percent"}:
            return format_decimal(value, digits=2)
        if key.endswith("_c"):
            return format_decimal(value, digits=1)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return format_decimal(value, digits=2)
        return str(value)
    return _clean_string(value)


def _finalize_host_record(host: Dict[str, Any], server_identifier: str) -> Dict[str, Any]:
    record: Dict[str, Any] = {}
    record["server_identifier"] = server_identifier or host.get("server_identifier") or NULL_TOKEN
    skip_keys = {"storage", "network_interfaces"}
    for key, value in host.items():
        if key in skip_keys:
            continue
        record[key] = _format_host_value(key, value)
    return record


def _legacy_vm_to_record(vm: Dict[str, Any]) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "node": vm.get("node"),
        "vmid": vm.get("vmid"),
        "name": vm.get("name"),
        "status": vm.get("status"),
        "cpu_usage": vm.get("cpu"),
        "maxcpu": vm.get("maxcpu"),
        "cores": vm.get("cores"),
        "sockets": vm.get("sockets"),
        "mem_used": vm.get("mem"),
        "mem_total": vm.get("maxmem"),
        "balloon_actual": vm.get("balloon_actual") or vm.get("balloon"),
        "disk_used": vm.get("disk"),
        "disk_size": vm.get("maxdisk"),
        "diskread": vm.get("diskread"),
        "diskwrite": vm.get("diskwrite"),
        "netin": vm.get("netin"),
        "netout": vm.get("netout"),
        "uptime": vm.get("uptime"),
        "bios": vm.get("bios"),
        "machine": vm.get("machine"),
        "ostype": vm.get("ostype"),
        "hotplug": vm.get("hotplug"),
        "numa": vm.get("numa"),
        "onboot": vm.get("onboot"),
        "protection": vm.get("protection"),
        "boot": vm.get("boot"),
        "bootdisk": vm.get("bootdisk"),
        "scsi_hw": vm.get("scsi_hw"),
        "vmgenid": vm.get("vmgenid"),
        "tags": vm.get("tags"),
        "description": vm.get("description"),
        "agent_enabled": vm.get("agent"),
        "disks": vm.get("disks"),
        "num_disks": vm.get("num_disks"),
        "networks": vm.get("networks"),
        "num_networks": vm.get("num_networks"),
        "ip_addresses": vm.get("ip_addresses"),
    }
    disks_details = vm.get("disks_details")
    if isinstance(disks_details, str):
        try:
            record["disks_details"] = json.loads(disks_details)
        except Exception:
            record["disks_details"] = disks_details
    else:
        record["disks_details"] = disks_details

    networks_details = vm.get("networks_details")
    if isinstance(networks_details, str):
        try:
            record["networks_details"] = json.loads(networks_details)
        except Exception:
            record["networks_details"] = networks_details
    else:
        record["networks_details"] = networks_details

    return record


def _collect_vm_record(extractor: ProxmoxLocalExtractor, node_name: str, vm_summary: Dict[str, Any]) -> Dict[str, Any]:
    vmid = vm_summary.get("vmid")
    config = _run_pvesh_json(extractor, f"/nodes/{node_name}/qemu/{vmid}/config") or {}
    status_current = _run_pvesh_json(extractor, f"/nodes/{node_name}/qemu/{vmid}/status/current") or {}

    status_value = vm_summary.get("status") or status_current.get("status")

    agent_data = None
    if status_value and status_value.lower() == "running":
        agent_data = _run_pvesh_json(
            extractor, f"/nodes/{node_name}/qemu/{vmid}/agent/network-get-interfaces"
        )

    snapshots_data = _run_pvesh_json(extractor, f"/nodes/{node_name}/qemu/{vmid}/snapshot")

    disk_ids, disk_details = _parse_disk_entries(config)
    net_ids, net_details = _parse_network_entries(config)
    agent_interfaces = _collect_agent_interfaces(agent_data)
    ip_summary = _merge_agent_with_network(net_details, agent_interfaces)

    snapshots = _parse_snapshot_info(snapshots_data)

    try:
        cores_int = int(config.get("cores", 0))
    except (TypeError, ValueError):
        cores_int = 0
    try:
        sockets_int = int(config.get("sockets", 0))
    except (TypeError, ValueError):
        sockets_int = 0
    computed_maxcpu = cores_int * sockets_int if cores_int and sockets_int else None

    memory_assigned_bytes: Optional[int] = None
    if config.get("memory") not in (None, "", "N/A"):
        try:
            memory_assigned_bytes = int(config["memory"]) * 1024 * 1024
        except (TypeError, ValueError):
            memory_assigned_bytes = None

    record: Dict[str, Any] = {
        "node": node_name,
        "vmid": vmid,
        "name": _clean_string(config.get("name") or vm_summary.get("name") or f"VM-{vmid}"),
        "status": _clean_string(status_value),
        "qmpstatus": _clean_string(status_current.get("qmpstatus")),
        "ha_state": _clean_string(vm_summary.get("ha") or status_current.get("ha")),
        "ha_managed": status_current.get("ha-managed") or vm_summary.get("ha-managed"),
        "template": config.get("template"),
        "cpu_usage": vm_summary.get("cpu") if vm_summary.get("cpu") is not None else status_current.get("cpu"),
        "maxcpu": vm_summary.get("maxcpu") or computed_maxcpu,
        "cores": cores_int or config.get("cores"),
        "sockets": sockets_int or config.get("sockets"),
        "memory_assigned": memory_assigned_bytes,
        "mem_used": vm_summary.get("mem") or status_current.get("mem"),
        "mem_total": vm_summary.get("maxmem") or status_current.get("maxmem"),
        "balloon_actual": status_current.get("balloon"),
        "balloon_target": config.get("balloon"),
        "disk_used": vm_summary.get("disk") or status_current.get("disk"),
        "disk_size": vm_summary.get("maxdisk") or status_current.get("maxdisk"),
        "diskread": vm_summary.get("diskread") or status_current.get("diskread"),
        "diskwrite": vm_summary.get("diskwrite") or status_current.get("diskwrite"),
        "netin": vm_summary.get("netin") or status_current.get("netin"),
        "netout": vm_summary.get("netout") or status_current.get("netout"),
        "uptime": vm_summary.get("uptime") or status_current.get("uptime"),
        "ostype": config.get("ostype"),
        "bios": config.get("bios"),
        "machine": config.get("machine"),
        "hotplug": config.get("hotplug"),
        "numa": config.get("numa"),
        "onboot": config.get("onboot"),
        "protection": config.get("protection"),
        "boot": config.get("boot"),
        "bootdisk": config.get("bootdisk"),
        "scsi_hw": config.get("scsi_hw"),
        "vmgenid": config.get("vmgenid"),
        "tags": config.get("tags"),
        "description": config.get("description"),
        "agent_enabled": config.get("agent"),
        "agent_version": status_current.get("agent", {}).get("version")
        if isinstance(status_current.get("agent"), dict)
        else None,
        "agent_options": config.get("agent") if isinstance(config.get("agent"), str) else "",
        "cpulimit": config.get("cpulimit"),
        "cpuunits": config.get("cpuunits"),
        "shares": config.get("shares"),
        "startup": config.get("startup"),
        "tablet": config.get("tablet"),
        "keyboard": config.get("keyboard"),
        "kvm": config.get("kvm"),
        "hookscript": config.get("hookscript"),
        "watchdog": config.get("watchdog"),
        "reboot": config.get("reboot"),
        "lock": status_current.get("lock"),
        "pid": status_current.get("pid"),
        "ha_group": status_current.get("ha-group"),
        "creation_time": status_current.get("starttime"),
        "vm_type": "qemu",
        "disks": ", ".join(disk_ids),
        "disks_details": disk_details,
        "num_disks": len([d for d in disk_details if d.get("type") != "unused"]),
        "networks": ", ".join(net_ids),
        "networks_details": net_details,
        "num_networks": len(net_details),
        "ip_addresses": ip_summary["all_ips"],
        "ipv4": ip_summary["ipv4"],
        "ipv6": ip_summary["ipv6"],
        "primary_bridge": net_details[0].get("bridge") if net_details else "",
        "snapshots_details": snapshots,
        "snapshots_count": len(snapshots),
    }

    return record


def _normalize_node_aliases(name: Optional[str]) -> Set[str]:
    aliases: Set[str] = set()
    if not name:
        return aliases
    normalized = str(name).strip().lower()
    if not normalized:
        return aliases
    aliases.add(normalized)
    if "." in normalized:
        aliases.add(normalized.split(".", 1)[0])
    return aliases


def _node_matches_target(node_name: Optional[str], target_aliases: Set[str]) -> bool:
    if not target_aliases:
        return True
    node_aliases = _normalize_node_aliases(node_name)
    return bool(node_aliases & target_aliases)


def get_full_vm_details(extractor: ProxmoxLocalExtractor, config: Dict[str, Any], execution_mode: str) -> List[Dict[str, Any]]:
    """
    Recupera informazioni dettagliate sulle VM replicando il comportamento di proxmox_report:
    - se API abilitate: usa pvesh per config/status/agent/snapshot
    - altrimenti usa qm config/status e file locali
    """
    logger.info("→ Estrazione dettagliata VM (pvesh)")
    vms: List[Dict[str, Any]] = []

    target_node_aliases: Set[str] = set()
    if execution_mode in ("local", "ssh"):
        target_node_aliases |= _normalize_node_aliases(extractor.node_info.get("hostname"))
        if hasattr(extractor, "hostname"):
            target_node_aliases |= _normalize_node_aliases(getattr(extractor, "hostname", None))

    nodes_data = _run_pvesh_json(extractor, "/nodes")
    if not nodes_data:
        logger.info("  ⚠ pvesh non disponibile, uso fallback dati base")
        try:
            base_vms = extractor.get_vms_from_local()
        except Exception as exc:
            logger.info(f"✗ Errore raccolta VM: {exc}")
            return []
        filtered_base_vms = [
            vm
            for vm in base_vms
            if _node_matches_target(vm.get("node"), target_node_aliases)
            and str(vm.get("status") or "").strip().lower() == "running"
        ]
        return [_finalize_vm_record(_legacy_vm_to_record(vm)) for vm in filtered_base_vms]

    for node_entry in nodes_data or []:
        if not isinstance(node_entry, dict):
            continue
        node_name = node_entry.get("node")
        if not node_name:
            continue
        if target_node_aliases and not _node_matches_target(node_name, target_node_aliases):
            logger.info(f"  → Nodo {node_name}: ignorato (fuori dall'host corrente)")
            continue

        vm_list = _run_pvesh_json(extractor, f"/nodes/{node_name}/qemu") or []
        logger.info(f"  → Nodo {node_name}: {len(vm_list or [])} VM")
        for vm_summary in vm_list or []:
            if not isinstance(vm_summary, dict):
                continue
            vmid = vm_summary.get("vmid")
            if vmid is None:
                continue
            status_value = str(vm_summary.get("status") or "").strip().lower()
            if status_value != "running":
                continue
            try:
                vm_record = _collect_vm_record(extractor, node_name, vm_summary)
                vms.append(_finalize_vm_record(vm_record))
            except Exception as exc:
                logger.info(f"    ⚠ Errore dettagli VM {node_name}/{vmid}: {exc}")

    if not vms:
        logger.info("  ⚠ Nessuna VM trovata via pvesh, uso fallback")
        try:
            base_vms = extractor.get_vms_from_local()
        except Exception as exc:
            logger.info(f"✗ Errore fallback VM: {exc}")
            return []
        filtered_base_vms = [
            vm
            for vm in base_vms
            if _node_matches_target(vm.get("node"), target_node_aliases)
            and str(vm.get("status") or "").strip().lower() == "running"
        ]
        return [_finalize_vm_record(_legacy_vm_to_record(vm)) for vm in filtered_base_vms]

    return vms
def write_vms_csv(
    vms: List[Dict[str, Any]],
    output_path: Path,
    codcli: str,
    nomecliente: str,
    server_identifier: str,
    max_copies: int,
) -> Optional[Path]:
    ensure_directory(output_path)
    filename = generate_filename(codcli, nomecliente, "vms", "csv", server_identifier)
    filepath = output_path / filename
    rotate_files(str(output_path), filename, max_copies)
    fieldnames = VMS_EXPORT_FIELDS

    try:
        with open(filepath, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=';')
            writer.writeheader()
            for vm in vms:
                row = {field: _prepare_csv_value(vm.get(field)) for field in fieldnames}
                writer.writerow(row)
        return filepath
    except Exception as exc:
        logger.info(f"✗ Errore salvataggio CSV VM: {exc}")
        return None


def write_host_csv(
    hosts: List[Dict[str, Any]],
    output_path: Path,
    codcli: str,
    nomecliente: str,
    server_identifier: str,
    max_copies: int,
) -> Optional[Path]:
    if not hosts:
        return None
    
    # IMPORTANTE: Estrai licenza per ogni host prima di finalizzare (se non già estratta)
    for host in hosts:
        hostname = host.get('hostname', 'unknown')
        # Se non ha già i dati licenza, prova ad estrarli
        if not host.get("license_status"):
            try:
                logger.info(f"  → [CSV] Tentativo estrazione licenza per {hostname}")
                # Esegui comando pvesubscription get
                result = subprocess.run(
                    ["pvesubscription", "get"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout:
                    logger.info(f"  → [CSV] Output pvesubscription ricevuto ({len(result.stdout)} caratteri)")
                    sub_data = {}
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if ':' in line:
                            key, value = line.split(':', 1)
                            key = key.strip().lower().replace(' ', '_')
                            value = value.strip()
                            sub_data[key] = value
                    
                    logger.info(f"  → [CSV] Parsed {len(sub_data)} campi subscription per {hostname}")
                    if sub_data.get('status'):
                        host['license_status'] = sub_data['status']
                        logger.info(f"  → [CSV] license_status: {sub_data['status']}")
                    if sub_data.get('level'):
                        host['license_level'] = sub_data['level']
                    if sub_data.get('productname'):
                        host['subscription_type'] = sub_data['productname']
                    if sub_data.get('key'):
                        host['subscription_key'] = sub_data['key']
                    if sub_data.get('serverid'):
                        host['subscription_server_id'] = sub_data['serverid']
                    if sub_data.get('sockets'):
                        host['subscription_sockets'] = sub_data['sockets']
                    if sub_data.get('nextduedate'):
                        host['subscription_next_due'] = sub_data['nextduedate']
                else:
                    logger.info(f"  ⚠ [CSV] pvesubscription get non ha restituito output per {hostname}")
            except Exception as e:
                logger.info(f"  ⚠ [CSV] Errore estrazione licenza per {hostname}: {e}")
        else:
            logger.info(f"  ✓ [CSV] Licenza già estratta per {hostname}: {host.get('license_status')}")
    
    records = [_finalize_host_record(host, server_identifier) for host in hosts]
    if not records:
        return None

    ensure_directory(output_path)
    filename = generate_filename(codcli, nomecliente, "hosts", "csv", server_identifier)
    filepath = output_path / filename
    rotate_files(str(output_path), filename, max_copies)

    fieldnames: List[str] = [new_key for _, new_key in HOST_EXPORT_FIELDS]

    try:
        with open(filepath, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=';')
            writer.writeheader()
            for record in records:
                row: Dict[str, Any] = {}
                for old_key, new_key in HOST_EXPORT_FIELDS:
                    row[new_key] = record.get(old_key, NULL_TOKEN)
                writer.writerow(row)
        return filepath
    except Exception as exc:
        logger.info(f"✗ Errore salvataggio CSV host: {exc}")
        return None


def write_storage_csv(
    hosts: List[Dict[str, Any]],
    output_path: Path,
    codcli: str,
    nomecliente: str,
    server_identifier: str,
    max_copies: int,
) -> Optional[Path]:
    rows: List[Dict[str, Any]] = []
    for host in hosts:
        hostname = host.get("hostname")
        for storage in host.get("storage", []) or []:
            row = dict(storage) if isinstance(storage, dict) else {}
            row["hostname"] = hostname
            rows.append(row)
    if not rows:
        return None
    ensure_directory(output_path)
    filename = generate_filename(codcli, nomecliente, "storage", "csv", server_identifier)
    filepath = output_path / filename
    rotate_files(str(output_path), filename, max_copies)
    fieldnames = [
        "server_identifier",
        "hostname",
        "name",
        "type",
        "status",
        "total_gb",
        "used_gb",
        "available_gb",
        "used_percent",
        "content",
    ]
    try:
        with open(filepath, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=';')
            writer.writeheader()
            for storage in rows:
                writer.writerow(
                    {
                        "server_identifier": server_identifier or NULL_TOKEN,
                        "hostname": _prepare_csv_value(storage.get("hostname")),
                        "name": _prepare_csv_value(storage.get("name")),
                        "type": _prepare_csv_value(storage.get("type")),
                        "status": _prepare_csv_value(storage.get("status")),
                        "total_gb": format_decimal(storage.get("total_gb")),
                        "used_gb": format_decimal(storage.get("used_gb")),
                        "available_gb": format_decimal(storage.get("available_gb")),
                        "used_percent": (
                            (
                                f"{format_decimal(storage.get('used_percent'), digits=2)}%"
                                if storage.get("used_percent") not in (None, "")
                                else NULL_TOKEN
                            )
                            if storage.get("used_percent") not in (None, "")
                            else (
                                (
                                    f"{format_decimal((storage.get('used_gb') / storage.get('total_gb')) * 100.0, digits=2)}%"
                                )
                                if storage.get("used_gb") not in (None, 0)
                                and storage.get("total_gb") not in (None, 0)
                                else NULL_TOKEN
                            )
                        ),
                        "content": _prepare_csv_value(storage.get("content")),
                    }
                )
        return filepath
    except Exception as exc:
        logger.info(f"✗ Errore salvataggio CSV storage: {exc}")
        return None


def write_network_csv(
    hosts: List[Dict[str, Any]],
    output_path: Path,
    codcli: str,
    nomecliente: str,
    server_identifier: str,
    max_copies: int,
) -> Optional[Path]:
    rows: List[Dict[str, Any]] = []
    for host in hosts:
        hostname = host.get("hostname")
        for iface in host.get("network_interfaces", []) or []:
            state = iface.get("state") or ("up" if iface.get("active") else "down")
            category = iface.get("category") or "other"
            if category == "physical" and str(state).lower() != "up":
                continue
            members_value = (
                iface.get("members")
                or iface.get("bridge_ports")
                or iface.get("ports_slaves")
                or iface.get("slaves")
            )
            if isinstance(members_value, (list, tuple, set)):
                members_value = ", ".join(str(v) for v in members_value if v)
            row = {
                "hostname": hostname,
                "category": category,
                "name": iface.get("name"),
                "type": iface.get("type"),
                "state": state,
                "mac_address": iface.get("mac_address"),
                "ip4": iface.get("ip"),
                "ip6": iface.get("ip6"),
                "ip_addresses": iface.get("ip_addresses"),
                "gateway": iface.get("gateway"),
                "gateway6": iface.get("gateway6"),
                "netmask": iface.get("netmask"),
                "bridge": iface.get("bridge"),
                "members": members_value,
                "vlan_id": iface.get("vlan_id") or iface.get("vlan") or iface.get("tag"),
                "bond_mode": iface.get("bond_mode"),
                "speed_mbps": iface.get("speed_mbps"),
                "comment": iface.get("comment"),
            }
            rows.append(row)
    if not rows:
        return None
    ensure_directory(output_path)
    filename = generate_filename(codcli, nomecliente, "network", "csv", server_identifier)
    filepath = output_path / filename
    rotate_files(str(output_path), filename, max_copies)
    fieldnames = [
        "server_identifier",
        "hostname",
        "category",
        "name",
        "type",
        "state",
        "mac_address",
        "ip4",
        "ip6",
        "ip_addresses",
        "gateway",
        "gateway6",
        "netmask",
        "bridge",
        "members",
        "vlan_id",
        "bond_mode",
        "speed_mbps",
        "comment",
    ]
    try:
        with open(filepath, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=';')
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "server_identifier": server_identifier or NULL_TOKEN,
                        "hostname": _prepare_csv_value(row.get("hostname")),
                        "category": _prepare_csv_value(row.get("category")),
                        "name": _prepare_csv_value(row.get("name")),
                        "type": _prepare_csv_value(row.get("type")),
                        "state": _prepare_csv_value(row.get("state")),
                        "mac_address": _prepare_csv_value(row.get("mac_address")),
                        "ip4": _prepare_csv_value(row.get("ip4")),
                        "ip6": _prepare_csv_value(row.get("ip6")),
                        "ip_addresses": _prepare_csv_value(row.get("ip_addresses")),
                        "gateway": _prepare_csv_value(row.get("gateway")),
                        "gateway6": _prepare_csv_value(row.get("gateway6")),
                        "netmask": _prepare_csv_value(row.get("netmask")),
                        "bridge": _prepare_csv_value(row.get("bridge")),
                        "members": _prepare_csv_value(row.get("members")),
                        "vlan_id": _prepare_csv_value(row.get("vlan_id")),
                        "bond_mode": _prepare_csv_value(row.get("bond_mode")),
                        "speed_mbps": format_speed(row.get("speed_mbps")),
                        "comment": _prepare_csv_value(row.get("comment")),
                    }
                )
        return filepath
    except Exception as exc:
        logger.info(f"✗ Errore salvataggio CSV network: {exc}")
        return None


def main() -> None:
    args = parse_args()
    local_hostname = socket.gethostname()
    output_dir = Path(args.output_dir).resolve()
    
    # Crea tutte le directory necessarie
    ensure_directory(output_dir)
    ensure_directory(output_dir / "csv")
    ensure_directory(output_dir / "backup")
    
    # Assicura permessi corretti
    try:
        os.chmod(output_dir, 0o755)
        os.chmod(output_dir / "csv", 0o755)
        os.chmod(output_dir / "backup", 0o755)
    except Exception:
        pass  # Ignora errori permessi se non root

    remote_host_arg = args.host
    forced_local = args.local
    remote_enabled = bool(remote_host_arg) and not forced_local

    api_username = args.username
    api_password = args.password
    ssh_port = args.ssh_port
    sftp_host_override = args.sftp_host
    sftp_port_override = args.sftp_port
    sftp_username_override = args.sftp_user
    sftp_password_override = args.sftp_password
    sftp_base_path_override = args.sftp_base_path

    if remote_enabled:
        if not api_username or not api_password:
            logger.info("✗ Per l'accesso remoto specifica sia --username che --password")
            sys.exit(1)
        api_host = remote_host_arg if ":" in remote_host_arg else f"{remote_host_arg}:8006"
        ssh_host = remote_host_arg.split(":")[0]
        server_identifier = ssh_host
    else:
        api_host = "localhost:8006"
        ssh_host = "localhost"
        api_username = ""
        api_password = ""
        server_identifier = local_hostname

    config = build_config(
        args.codcli,
        args.nomecliente,
        server_identifier,
        output_dir,
        remote_enabled,
        api_host,
        api_username,
        api_password,
        ssh_host,
        ssh_port,
        sftp_host_override,
        sftp_port_override,
        sftp_username_override,
        sftp_password_override,
        sftp_base_path_override,
    )

    # Carica configurazione da file se specificato o se presente config.json nella stessa directory
    file_config = {}
    config_path = args.config
    
    # Se non specificato, cerca config.json nella stessa cartella dello script
    if not config_path:
        default_config = Path(__file__).parent / "config.json"
        if default_config.exists():
            config_path = str(default_config)
    
    if config_path and os.path.exists(config_path):
        try:
            logger.info(f"→ Caricamento configurazione da: {config_path}")
            with open(config_path, "r") as f:
                file_config = json.load(f)
            
            # Merge smart: la CLI ha precedenza, ma il file riempie i buchi (specialmente le password)
            # Merge SFTP
            if "sftp" in file_config:
                if not config["sftp"]["password"] and file_config["sftp"].get("password"):
                    config["sftp"]["password"] = file_config["sftp"]["password"]
                if not sftp_host_override and file_config["sftp"].get("host"):
                    config["sftp"]["host"] = file_config["sftp"]["host"]
                if not sftp_username_override and file_config["sftp"].get("username"):
                    config["sftp"]["username"] = file_config["sftp"]["username"]
            
            # Merge Client Info (se non passati da CLI, ma CLI è required per ora)
            # Merge Client Info (se non passati da CLI)
            if "client" in file_config:
                client_cfg = file_config["client"]
                if not args.codcli and client_cfg.get("codcli"):
                    config["client"]["codcli"] = client_cfg["codcli"]
                    # Aggiorniamo anche le variabili locali usate dopo
                    args.codcli = client_cfg["codcli"] 
                if not args.nomecliente and client_cfg.get("nomecliente"):
                    config["client"]["nomecliente"] = client_cfg["nomecliente"]
                    args.nomecliente = client_cfg["nomecliente"]
            
            # Update build_config vars since they were immutable strings in function scope, 
            # but config dict is mutable.
            # However, run_report uses args.codcli directly.


             # Merge SMTP
            if "smtp" in file_config:
                config["smtp"] = file_config["smtp"]

        except Exception as e:
            logger.info(f"⚠ Errore caricamento config file: {e}")

    
    # Validation
    if not args.codcli or not args.nomecliente:
        # Se stiamo facendo solo auto-update, va bene ignorare
        args_prelim = sys.argv[1:]
        if "--auto-update" in args_prelim or "--skip-update" in args_prelim:
             if not args.codcli: args.codcli = "UPDATE"
             if not args.nomecliente: args.nomecliente = "UPDATE"
        else:
            logger.error("✗ Errore: --codcli e --nomecliente sono obbligatori (o devono essere nel config.json)")
            sys.exit(1)
    logger.info("=" * 70)
    logger.info("PROXMOX LOCAL REPORTER (CRON)")
    logger.info("=" * 70)
    
    # Init Logging
    setup_logging(debug=False, log_file=Path(args.output_dir) / "proxreporter.log") # Use output dir for log if var log not writable or argument based?
    # Better: default to /var/log/proxreporter/app.log, fallback to output_dir/app.log?
    # For now let's use the function default but respect permissions.
    
    # Lock Check
    lock_fd = acquire_lock()
    if not lock_fd:
        logger.error("Altra istanza in esecuzione. Esco.")
        sys.exit(1)
        
    logger.info("=== Start Execution ===")
    
    target_desc = ssh_host if remote_enabled else local_hostname
    logger.info(f"Target Proxmox: {target_desc} ({'remoto' if remote_enabled else 'locale'})")
    logger.info(f"Output directory: {output_dir}")
    logger.info("") # Keep some visual separation in stdout? Or just rely on logger

    try:
        run_report(config, args.codcli, args.nomecliente, server_identifier, output_dir, args.no_upload)
    except Exception as e:
        logger.exception("Errore fatale durante l'esecuzione")
        sys.exit(1)
    finally:
        logger.info("=== End Execution ===")



if __name__ == "__main__":
    # Verifica auto-update se richiesto
    args_prelim = sys.argv[1:]
    if "--auto-update" in args_prelim and "--skip-update" not in args_prelim:
        update_script = Path(__file__).resolve().parent / "update_scripts.py"
        if update_script.exists():
            logger.info("→ Verifica aggiornamenti script...")
            try:
                result = subprocess.run([sys.executable, str(update_script)], check=False)
                if result.returncode == 0:
                    logger.info("→ Riavvio script con versione aggiornata...\n")
                    # Ri-esegui lo stesso comando (senza --auto-update per evitare loop)
                    new_args = [arg for arg in sys.argv if arg != "--auto-update"]
                    new_args.append("--skip-update")
                    os.execv(sys.executable, [sys.executable] + new_args)
            except Exception as e:
                logger.info(f"⚠ Errore durante auto-update: {e}")
                logger.info("→ Continuo con la versione corrente...\n")
    
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n✋ Operazione interrotta dall'utente")
        sys.exit(0)
    except Exception as exc:
        logger.info(f"\n✗ Errore critico: {exc}")
        raise

