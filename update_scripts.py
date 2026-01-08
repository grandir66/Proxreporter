#!/usr/bin/env python3
"""
Script di auto-aggiornamento per Proxmox Reporter.
Scarica gli script aggiornati dal server SFTP, confronta versioni e sostituisce se più recenti.
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, List, Tuple

try:
    import paramiko
except ImportError:
    print("✗ Libreria paramiko non disponibile. Installare: apt install python3-paramiko")
    sys.exit(1)


# Configurazione SFTP (stessi parametri di proxmox_core.py)
SFTP_HOST = "sftp.domarc.it"
SFTP_PORT = 11122
SFTP_USERNAME = "proxmox"
SFTP_PASSWORD = "PX!h03f257m"
SFTP_SCRIPTS_PATH = "/home/proxmox/proxreport"
SFTP_FALLBACK_HOST = "192.168.20.14"

# Script da aggiornare (relativi alla directory di installazione)
SCRIPTS_TO_UPDATE = [
    "proxmox_core.py",
    "proxmox_report.py",
    "configure_smtp.py",
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
        print(f"  ⚠ Errore calcolo hash {filepath}: {e}")
        return None


def connect_sftp(host: str, port: int, username: str, password: str) -> Optional[paramiko.SFTPClient]:
    """Connessione SFTP con gestione errori."""
    try:
        transport = paramiko.Transport((host, port))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        return sftp
    except Exception as e:
        print(f"  ✗ Connessione SFTP fallita ({host}:{port}): {e}")
        return None


def attempt_sftp_connection() -> Optional[paramiko.SFTPClient]:
    """Tenta connessione SFTP con fallback automatico."""
    print("→ Connessione al server SFTP per verifica aggiornamenti...")
    
    # Tentativo 1: host primario, porta configurata
    sftp = connect_sftp(SFTP_HOST, SFTP_PORT, SFTP_USERNAME, SFTP_PASSWORD)
    if sftp:
        print(f"  ✓ Connesso a {SFTP_HOST}:{SFTP_PORT}")
        return sftp
    
    # Tentativo 2: host primario, porta 22
    if SFTP_PORT != 22:
        sftp = connect_sftp(SFTP_HOST, 22, SFTP_USERNAME, SFTP_PASSWORD)
        if sftp:
            print(f"  ✓ Connesso a {SFTP_HOST}:22")
            return sftp
    
    # Tentativo 3: host fallback, porta configurata
    sftp = connect_sftp(SFTP_FALLBACK_HOST, SFTP_PORT, SFTP_USERNAME, SFTP_PASSWORD)
    if sftp:
        print(f"  ✓ Connesso a {SFTP_FALLBACK_HOST}:{SFTP_PORT}")
        return sftp
    
    # Tentativo 4: host fallback, porta 22
    if SFTP_PORT != 22:
        sftp = connect_sftp(SFTP_FALLBACK_HOST, 22, SFTP_USERNAME, SFTP_PASSWORD)
        if sftp:
            print(f"  ✓ Connesso a {SFTP_FALLBACK_HOST}:22")
            return sftp
    
    print("  ✗ Impossibile connettersi al server SFTP (tutti i tentativi falliti)")
    return None


def check_and_download_updates(install_dir: Path) -> List[Tuple[str, Path]]:
    """
    Verifica aggiornamenti disponibili e scarica gli script più recenti.
    Ritorna lista di tuple (nome_script, percorso_temporaneo) degli script aggiornati.
    """
    sftp = attempt_sftp_connection()
    if not sftp:
        return []
    
    updated_files: List[Tuple[str, Path]] = []
    
    try:
        for script_name in SCRIPTS_TO_UPDATE:
            local_path = install_dir / script_name
            remote_path = f"{SFTP_SCRIPTS_PATH}/{script_name}"
            
            print(f"\n→ Verifica aggiornamento: {script_name}")
            
            # Controlla esistenza remota
            try:
                sftp.stat(remote_path)
            except FileNotFoundError:
                print(f"  ℹ Script non disponibile sul server: {remote_path}")
                continue
            
            # Scarica in file temporaneo
            temp_file = Path(tempfile.mktemp(suffix=f"_{script_name}"))
            try:
                sftp.get(remote_path, str(temp_file))
            except Exception as e:
                print(f"  ✗ Errore download: {e}")
                continue
            
            # Confronta hash
            if local_path.exists():
                local_hash = compute_file_hash(local_path)
                remote_hash = compute_file_hash(temp_file)
                
                if local_hash == remote_hash:
                    print(f"  ✓ Script già aggiornato (hash identici)")
                    temp_file.unlink()
                    continue
                else:
                    print(f"  → Versione diversa rilevata")
                    print(f"    Locale:  {local_hash[:12]}...")
                    print(f"    Remoto:  {remote_hash[:12]}...")
            else:
                print(f"  → Script locale non esistente, verrà installato")
            
            updated_files.append((script_name, temp_file))
        
        sftp.close()
        
    except Exception as e:
        print(f"\n✗ Errore durante verifica aggiornamenti: {e}")
        sftp.close()
        return []
    
    return updated_files


def apply_updates(install_dir: Path, updated_files: List[Tuple[str, Path]]) -> bool:
    """Applica gli aggiornamenti sostituendo i file locali."""
    if not updated_files:
        return False
    
    print(f"\n→ Applicazione aggiornamenti ({len(updated_files)} file)...")
    
    # Backup directory
    backup_dir = install_dir / ".backup"
    backup_dir.mkdir(exist_ok=True)
    
    for script_name, temp_path in updated_files:
        local_path = install_dir / script_name
        backup_path = backup_dir / f"{script_name}.bak"
        
        try:
            # Backup versione corrente
            if local_path.exists():
                import shutil
                shutil.copy2(local_path, backup_path)
                print(f"  → Backup: {script_name} → {backup_path.name}")
            
            # Sostituisci con nuova versione
            import shutil
            shutil.move(str(temp_path), str(local_path))
            os.chmod(local_path, 0o755)
            print(f"  ✓ Aggiornato: {script_name}")
            
        except Exception as e:
            print(f"  ✗ Errore aggiornamento {script_name}: {e}")
            # Ripristina backup se disponibile
            if backup_path.exists():
                import shutil
                shutil.copy2(backup_path, local_path)
                print(f"    → Ripristinato backup")
            return False
    
    return True


def main():
    # Determina directory di installazione
    install_dir = Path(__file__).resolve().parent
    
    print("=" * 70)
    print("PROXMOX REPORTER - AUTO-UPDATE")
    print("=" * 70)
    print(f"Directory installazione: {install_dir}")
    
    # Verifica e installa dipendenze se mancanti
    missing_deps = []
    try:
        import paramiko
    except ImportError:
        missing_deps.append("python3-paramiko")
    
    if missing_deps and os.geteuid() == 0:
        print(f"\n→ Installazione dipendenze mancanti: {', '.join(missing_deps)}")
        import subprocess
        subprocess.run(["apt-get", "update"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["apt-get", "install", "-y", *missing_deps], check=False)
        print("  ✓ Dipendenze installate")
    
    # Verifica permessi scrittura
    if not os.access(install_dir, os.W_OK):
        print(f"\n✗ Errore: nessun permesso di scrittura su {install_dir}")
        print("  Eseguire come root: sudo python3 update_scripts.py")
        sys.exit(1)
    
    # Controlla aggiornamenti
    updated_files = check_and_download_updates(install_dir)
    
    if not updated_files:
        print("\n✓ Tutti gli script sono già aggiornati")
        sys.exit(0)
    
    # Applica aggiornamenti
    if apply_updates(install_dir, updated_files):
        print("\n" + "=" * 70)
        print("✓ AGGIORNAMENTO COMPLETATO")
        print("=" * 70)
        print(f"Script aggiornati: {len(updated_files)}")
        for script_name, _ in updated_files:
            print(f"  • {script_name}")
        print("\nGli script sono pronti per essere rieseguiti.")
        sys.exit(0)
    else:
        print("\n✗ AGGIORNAMENTO FALLITO")
        print("  Alcuni file potrebbero essere stati ripristinati dal backup")
        sys.exit(1)


if __name__ == "__main__":
    main()

