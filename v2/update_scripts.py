#!/usr/bin/env python3
"""
Script di auto-aggiornamento per Proxmox Reporter V2.
Scarica gli script aggiornati da GitHub, confronta versioni e sostituisce se più recenti.
"""

import hashlib
import os
import sys
import shutil
import tempfile
import urllib.request
import urllib.error
import time
import subprocess
from pathlib import Path
from typing import Optional, List, Tuple

# Configurazione GitHub
GITHUB_REPO_URL = "https://raw.githubusercontent.com/grandir66/Proxreporter/main"
GITHUB_BASE_PATH = "v2"  # Directory nel repo

# Script da aggiornare (relativi alla directory di installazione)
SCRIPTS_TO_UPDATE = [
    "proxmox_core.py",
    "proxmox_report.py",
    "html_generator.py",
    "email_sender.py",
    "setup.py",
    "update_scripts.py",
    "templates/report.html.j2",
    "install.sh",
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
        remote_url = f"{GITHUB_REPO_URL}/{GITHUB_BASE_PATH}/{script_rel_path}?t={timestamp}"
        
        # Gestione path template nel repo (che sono dentro v2/templates)
        # Lo script assume che SCRIPTS_TO_UPDATE rifletta la struttura locale in v2/ e remota in v2/
        
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


def update_via_git(install_dir: Path) -> int:
    """
    Tenta aggiornamento via git se disponibile.
    Return codes: 0 (aggiornato), 2 (nessun agg.), 1 (errore/non git)
    """
    # Check parent dir for .git (since install_dir is .../v2, usually repo root is parent)
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


def main():
    # Determina directory di installazione
    # Se eseguito come script, siamo in v2/update_scripts.py, quindi install_dir è v2/
    install_dir = Path(__file__).resolve().parent
    
    # Verifica permessi scrittura
    if not os.access(install_dir, os.W_OK):
        print(f"⚠ W: Nessun permesso scrittura su {install_dir}. Salto aggiornamento.")
        return # Non uscire con errore, semplicemente salta update nel cron

    # Tentativo 1: Git Pull
    git_result = update_via_git(install_dir)
    if git_result == 0:
        print("✓ Aggiornamento git completato.")
        sys.exit(0) # Restart
    elif git_result == 2:
        # Git rilevato ma nessun aggiornamento
        sys.exit(2)
    
    # Tentativo 2: Download File (Fallback o Non-Git)
    updated_files = check_and_download_updates(install_dir)
    
    if updated_files:
        if apply_updates(install_dir, updated_files):
            print("✓ Aggiornamento completato con successo.")
            sys.exit(0) # Restart required
        else:
            print("⚠ Aggiornamento parziale o fallito.")
            sys.exit(1) # Error
    else:
        # Silenzioso se non ci sono aggiornamenti
        sys.exit(2) # No updates, no restart needed

if __name__ == "__main__":
    main()
