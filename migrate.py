#!/usr/bin/env python3
"""
Proxreporter - Migration Script

Script di migrazione da vecchie installazioni (SFTP-based) alla nuova versione Git.
Recupera configurazioni esistenti, aggiorna il sistema e configura auto-update.

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.

Uso:
    python3 migrate.py [--dry-run] [--force]
    
    --dry-run  Mostra cosa verrebbe fatto senza applicare modifiche
    --force    Forza migrazione anche se già su versione Git
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("migrate")

# Costanti
REPO_URL = "https://github.com/grandir66/Proxreporter.git"
BRANCH = "main"
DEFAULT_INSTALL_DIR = Path("/opt/proxreport")

# Possibili percorsi vecchia installazione
OLD_PATHS = [
    Path("/opt/proxreport"),
    Path("/opt/proxreport/v2"),
    Path("/opt/proxreporter"),
    Path("/opt/proxmox-reporter"),
    Path("/root/proxreport"),
    Path("/root/proxreporter"),
]

# File da preservare durante migrazione
PRESERVE_FILES = [
    "config.json",
    ".secret.key",
    ".encryption_key",
]

# Vecchi cron job da rimuovere
OLD_CRON_PATTERNS = [
    "proxreport",
    "proxreporter",
    "proxmox_core",
    "proxmox-report",
]


class MigrationResult:
    """Risultato della migrazione"""
    def __init__(self):
        self.success = False
        self.old_version: Optional[str] = None
        self.new_version: Optional[str] = None
        self.config_migrated = False
        self.config_backup: Optional[Path] = None
        self.old_path: Optional[Path] = None
        self.new_path: Optional[Path] = None
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.actions: List[str] = []


def run_command(cmd: str, check: bool = False) -> Tuple[int, str, str]:
    """Esegue un comando shell"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=120
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"
    except Exception as e:
        return -1, "", str(e)


def find_old_installation() -> Optional[Path]:
    """Trova la vecchia installazione"""
    for path in OLD_PATHS:
        if path.exists():
            # Verifica se contiene file Proxreporter
            if (path / "config.json").exists() or \
               (path / "proxmox_core.py").exists() or \
               (path / "setup.py").exists():
                return path
    return None


def is_git_installation(path: Path) -> bool:
    """Verifica se è un'installazione Git"""
    return (path / ".git").is_dir()


def get_old_version(path: Path) -> Optional[str]:
    """Recupera la versione dalla vecchia installazione"""
    # Prova version.py
    version_file = path / "version.py"
    if version_file.exists():
        try:
            content = version_file.read_text()
            for line in content.split('\n'):
                if '__version__' in line and '=' in line:
                    version = line.split('=')[1].strip().strip('"\'')
                    return version
        except:
            pass
    
    # Prova config.json
    config_file = path / "config.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            return config.get("_version", config.get("version", "unknown"))
        except:
            pass
    
    return "unknown"


def load_old_config(path: Path) -> Optional[Dict[str, Any]]:
    """Carica configurazione dalla vecchia installazione"""
    config_paths = [
        path / "config.json",
        path / "v2" / "config.json",
        path / "config" / "config.json",
    ]
    
    for config_path in config_paths:
        if config_path.exists():
            try:
                content = config_path.read_text()
                return json.loads(content)
            except Exception as e:
                logger.warning(f"Errore lettura {config_path}: {e}")
    
    return None


def migrate_config(old_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Migra configurazione vecchia al nuovo formato.
    Gestisce rinominazioni campi e nuove sezioni.
    """
    new_config = {}
    
    # Copia campi top-level standard
    direct_copy = ["codcli", "nomecliente", "server_identifier"]
    for key in direct_copy:
        if key in old_config:
            new_config[key] = old_config[key]
    
    # Migra sezione client (vecchio formato -> nuovo)
    if "client" in old_config:
        new_config["client"] = old_config["client"]
    else:
        # Costruisci da campi legacy
        new_config["client"] = {
            "codcli": old_config.get("codcli", old_config.get("cod_cliente", "")),
            "nomecliente": old_config.get("nomecliente", old_config.get("nome_cliente", "")),
            "server_identifier": old_config.get("server_identifier", old_config.get("hostname", ""))
        }
    
    # Migra sezione SFTP
    if "sftp" in old_config:
        new_config["sftp"] = old_config["sftp"]
    elif "upload" in old_config:
        # Vecchio formato
        upload = old_config["upload"]
        new_config["sftp"] = {
            "enabled": upload.get("enabled", True),
            "host": upload.get("host", upload.get("server", "")),
            "port": upload.get("port", 22),
            "username": upload.get("username", upload.get("user", "")),
            "password": upload.get("password", ""),
            "base_path": upload.get("path", upload.get("remote_path", "/home/proxmox/uploads"))
        }
    
    # Migra Proxmox
    if "proxmox" in old_config:
        new_config["proxmox"] = old_config["proxmox"]
    elif "pve" in old_config:
        pve = old_config["pve"]
        new_config["proxmox"] = {
            "enabled": True,
            "host": pve.get("host", "localhost:8006"),
            "username": pve.get("user", pve.get("username", "root@pam")),
            "password": pve.get("password", ""),
            "verify_ssl": pve.get("verify_ssl", False)
        }
    
    # Migra SSH
    if "ssh" in old_config:
        new_config["ssh"] = old_config["ssh"]
    
    # Migra SMTP (nuovo)
    if "smtp" in old_config:
        new_config["smtp"] = old_config["smtp"]
    elif "email" in old_config:
        email = old_config["email"]
        new_config["smtp"] = {
            "enabled": email.get("enabled", False),
            "host": email.get("server", email.get("host", "")),
            "port": email.get("port", 25),
            "user": email.get("user", email.get("username", "")),
            "password": email.get("password", ""),
            "sender": email.get("from", email.get("sender", "")),
            "recipients": email.get("to", email.get("recipients", ""))
        }
    
    # Migra Syslog (nuovo)
    if "syslog" in old_config:
        new_config["syslog"] = old_config["syslog"]
    
    # Migra alerts (nuovo)
    if "alerts" in old_config:
        new_config["alerts"] = old_config["alerts"]
    
    # Migra hardware_monitoring (nuovo)
    if "hardware_monitoring" in old_config:
        new_config["hardware_monitoring"] = old_config["hardware_monitoring"]
    
    # Migra hardware_thresholds
    if "hardware_thresholds" in old_config:
        new_config["hardware_thresholds"] = old_config["hardware_thresholds"]
    
    # Migra pve_monitor (nuovo)
    if "pve_monitor" in old_config:
        new_config["pve_monitor"] = old_config["pve_monitor"]
    
    # Migra system/output settings
    if "system" in old_config:
        new_config["system"] = old_config["system"]
    elif "output" in old_config:
        new_config["system"] = {
            "output_directory": old_config["output"].get("directory", "/var/log/proxreporter"),
            "max_file_copies": old_config["output"].get("max_copies", 5)
        }
    
    # Migra features
    if "features" in old_config:
        new_config["features"] = old_config["features"]
    elif "collectors" in old_config:
        coll = old_config["collectors"]
        new_config["features"] = {
            "collect_cluster": coll.get("cluster", True),
            "collect_host": coll.get("host", True),
            "collect_storage": coll.get("storage", True),
            "collect_network": coll.get("network", True),
            "collect_vms": coll.get("vms", True),
            "collect_backup": coll.get("backup", True)
        }
    
    return new_config


def backup_old_installation(old_path: Path, result: MigrationResult) -> Optional[Path]:
    """Crea backup della vecchia installazione"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = old_path.parent / f"{old_path.name}_backup_{timestamp}"
    
    try:
        shutil.copytree(old_path, backup_path)
        result.actions.append(f"Backup creato: {backup_path}")
        return backup_path
    except Exception as e:
        result.errors.append(f"Errore backup: {e}")
        return None


def remove_old_cron_jobs(result: MigrationResult, dry_run: bool = False) -> None:
    """Rimuove vecchi cron job"""
    cron_files = [
        Path("/etc/cron.d/proxreport"),
        Path("/etc/cron.d/proxreporter"),
        Path("/etc/cron.d/proxmox-reporter"),
    ]
    
    for cron_file in cron_files:
        if cron_file.exists():
            if dry_run:
                result.actions.append(f"[DRY-RUN] Rimuoverei: {cron_file}")
            else:
                try:
                    cron_file.unlink()
                    result.actions.append(f"Rimosso vecchio cron: {cron_file}")
                except Exception as e:
                    result.warnings.append(f"Errore rimozione {cron_file}: {e}")
    
    # Controlla anche crontab utente
    code, stdout, _ = run_command("crontab -l 2>/dev/null")
    if code == 0 and stdout:
        new_crontab = []
        removed = False
        for line in stdout.split('\n'):
            should_remove = any(pattern in line.lower() for pattern in OLD_CRON_PATTERNS)
            if should_remove and "heartbeat" not in line.lower():
                if dry_run:
                    result.actions.append(f"[DRY-RUN] Rimuoverei da crontab: {line}")
                else:
                    result.actions.append(f"Rimosso da crontab: {line}")
                removed = True
            else:
                new_crontab.append(line)
        
        if removed and not dry_run:
            new_content = '\n'.join(new_crontab)
            run_command(f"echo '{new_content}' | crontab -")


def install_new_version(install_dir: Path, result: MigrationResult, dry_run: bool = False) -> bool:
    """Installa nuova versione da Git"""
    if dry_run:
        result.actions.append(f"[DRY-RUN] Clonerei repository in {install_dir}")
        return True
    
    # Se esiste già come Git repo, aggiorna
    if is_git_installation(install_dir):
        result.actions.append("Aggiornamento repository esistente...")
        code, _, stderr = run_command(f"cd {install_dir} && git fetch origin && git reset --hard origin/{BRANCH}")
        if code != 0:
            result.errors.append(f"Errore git update: {stderr}")
            return False
        result.actions.append("Repository aggiornato")
        return True
    
    # Se esiste ma non è Git, rimuovi (dopo backup)
    if install_dir.exists():
        try:
            shutil.rmtree(install_dir)
            result.actions.append(f"Rimossa vecchia directory: {install_dir}")
        except Exception as e:
            result.errors.append(f"Errore rimozione directory: {e}")
            return False
    
    # Clone nuovo repository
    result.actions.append(f"Clonazione repository in {install_dir}...")
    code, _, stderr = run_command(f"git clone -b {BRANCH} {REPO_URL} {install_dir}")
    if code != 0:
        result.errors.append(f"Errore git clone: {stderr}")
        return False
    
    result.actions.append("Repository clonato con successo")
    return True


def restore_config(install_dir: Path, config: Dict[str, Any], result: MigrationResult, dry_run: bool = False) -> bool:
    """Ripristina configurazione migrata"""
    config_path = install_dir / "config.json"
    
    if dry_run:
        result.actions.append(f"[DRY-RUN] Salverei configurazione in {config_path}")
        return True
    
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        result.actions.append(f"Configurazione salvata: {config_path}")
        result.config_migrated = True
        return True
    except Exception as e:
        result.errors.append(f"Errore salvataggio config: {e}")
        return False


def restore_secret_key(old_path: Path, new_path: Path, result: MigrationResult, dry_run: bool = False) -> None:
    """Ripristina chiave di crittografia"""
    for key_file in [".secret.key", ".encryption_key"]:
        old_key = old_path / key_file
        if old_key.exists():
            if dry_run:
                result.actions.append(f"[DRY-RUN] Copierei {key_file}")
            else:
                try:
                    shutil.copy2(old_key, new_path / key_file)
                    result.actions.append(f"Chiave crittografia ripristinata: {key_file}")
                except Exception as e:
                    result.warnings.append(f"Errore copia {key_file}: {e}")


def run_post_migration(install_dir: Path, result: MigrationResult, dry_run: bool = False) -> None:
    """Esegue operazioni post-migrazione"""
    if dry_run:
        result.actions.append("[DRY-RUN] Eseguirei post-update tasks")
        return
    
    # Imposta permessi eseguibili
    run_command(f"chmod +x {install_dir}/*.py 2>/dev/null")
    
    # Esegui update_scripts.py per configurare cron e sincronizzare config
    update_script = install_dir / "update_scripts.py"
    if update_script.exists():
        result.actions.append("Esecuzione post-update tasks...")
        code, stdout, stderr = run_command(f"python3 {update_script}")
        if code == 0:
            result.actions.append("Post-update completato")
        else:
            result.warnings.append(f"Errore post-update: {stderr}")
    
    # Verifica versione installata
    version_file = install_dir / "version.py"
    if version_file.exists():
        try:
            content = version_file.read_text()
            for line in content.split('\n'):
                if '__version__' in line and '=' in line:
                    result.new_version = line.split('=')[1].strip().strip('"\'')
                    break
        except:
            pass


def migrate(dry_run: bool = False, force: bool = False) -> MigrationResult:
    """
    Esegue la migrazione completa.
    
    Args:
        dry_run: Se True, mostra cosa verrebbe fatto senza applicare
        force: Se True, forza migrazione anche se già su Git
    
    Returns:
        MigrationResult con dettagli dell'operazione
    """
    result = MigrationResult()
    result.new_path = DEFAULT_INSTALL_DIR
    
    logger.info("=== Proxreporter Migration Tool ===")
    if dry_run:
        logger.info("MODO DRY-RUN: nessuna modifica verrà applicata")
    
    # 1. Trova vecchia installazione
    logger.info("→ Ricerca installazione esistente...")
    old_path = find_old_installation()
    
    if old_path:
        result.old_path = old_path
        result.old_version = get_old_version(old_path)
        logger.info(f"  Trovata installazione: {old_path}")
        logger.info(f"  Versione: {result.old_version}")
        
        # Verifica se è già Git
        if is_git_installation(old_path) and not force:
            logger.info("  ✓ Installazione già basata su Git")
            result.actions.append("Installazione già su Git, eseguo solo aggiornamento")
            
            # Aggiorna comunque
            if install_new_version(old_path, result, dry_run):
                run_post_migration(old_path, result, dry_run)
                result.success = True
            return result
    else:
        logger.info("  Nessuna installazione esistente trovata")
        result.actions.append("Nuova installazione")
    
    # 2. Carica vecchia configurazione
    old_config = None
    if old_path:
        logger.info("→ Caricamento configurazione...")
        old_config = load_old_config(old_path)
        if old_config:
            logger.info(f"  ✓ Configurazione trovata con {len(old_config)} parametri")
        else:
            result.warnings.append("Configurazione non trovata o non leggibile")
    
    # 3. Migra configurazione
    migrated_config = None
    if old_config:
        logger.info("→ Migrazione configurazione...")
        migrated_config = migrate_config(old_config)
        logger.info(f"  ✓ Configurazione migrata")
    
    # 4. Backup vecchia installazione
    if old_path and old_path.exists():
        logger.info("→ Backup installazione esistente...")
        backup_path = backup_old_installation(old_path, result)
        if backup_path:
            result.config_backup = backup_path
    
    # 5. Rimuovi vecchi cron job
    logger.info("→ Pulizia vecchi cron job...")
    remove_old_cron_jobs(result, dry_run)
    
    # 6. Installa nuova versione
    logger.info("→ Installazione nuova versione...")
    if not install_new_version(DEFAULT_INSTALL_DIR, result, dry_run):
        result.errors.append("Installazione fallita")
        return result
    
    # 7. Ripristina configurazione
    if migrated_config:
        logger.info("→ Ripristino configurazione...")
        restore_config(DEFAULT_INSTALL_DIR, migrated_config, result, dry_run)
    
    # 8. Ripristina chiave crittografia
    if old_path:
        logger.info("→ Ripristino chiavi crittografia...")
        restore_secret_key(old_path, DEFAULT_INSTALL_DIR, result, dry_run)
    
    # 9. Esegui post-migrazione
    logger.info("→ Configurazione post-migrazione...")
    run_post_migration(DEFAULT_INSTALL_DIR, result, dry_run)
    
    result.success = len(result.errors) == 0
    return result


def print_result(result: MigrationResult) -> None:
    """Stampa riepilogo migrazione"""
    print("\n" + "=" * 60)
    print("RIEPILOGO MIGRAZIONE")
    print("=" * 60)
    
    if result.old_path:
        print(f"Percorso precedente: {result.old_path}")
        print(f"Versione precedente: {result.old_version or 'sconosciuta'}")
    
    print(f"Nuovo percorso:      {result.new_path}")
    print(f"Nuova versione:      {result.new_version or 'da verificare'}")
    
    if result.config_backup:
        print(f"Backup salvato in:   {result.config_backup}")
    
    print(f"\nConfigurazione migrata: {'Sì' if result.config_migrated else 'No'}")
    
    if result.actions:
        print(f"\nAzioni eseguite ({len(result.actions)}):")
        for action in result.actions:
            print(f"  • {action}")
    
    if result.warnings:
        print(f"\nAvvisi ({len(result.warnings)}):")
        for warning in result.warnings:
            print(f"  ⚠ {warning}")
    
    if result.errors:
        print(f"\nErrori ({len(result.errors)}):")
        for error in result.errors:
            print(f"  ✗ {error}")
    
    print("\n" + "=" * 60)
    if result.success:
        print("✓ MIGRAZIONE COMPLETATA CON SUCCESSO")
        print("\nProssimi passi:")
        print(f"  1. Verifica configurazione: cat {result.new_path}/config.json")
        print(f"  2. Test heartbeat: python3 {result.new_path}/heartbeat.py -v")
        print(f"  3. Test report: python3 {result.new_path}/proxmox_core.py --local")
    else:
        print("✗ MIGRAZIONE FALLITA")
        print("\nControlla gli errori sopra e riprova.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Migrazione Proxreporter da versione SFTP a Git"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Mostra cosa verrebbe fatto senza applicare modifiche"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Forza migrazione anche se già su versione Git"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Output dettagliato"
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Verifica root
    if os.geteuid() != 0 and not args.dry_run:
        print("Errore: questo script deve essere eseguito come root")
        print("Usa: sudo python3 migrate.py")
        sys.exit(1)
    
    result = migrate(dry_run=args.dry_run, force=args.force)
    print_result(result)
    
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
