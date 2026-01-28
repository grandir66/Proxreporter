#!/usr/bin/env python3
import sys
import os
import json
import logging
from pathlib import Path

# Setup logging minimal
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("debug_auth")

# Add current dir to path to import proxmox_core
current_dir = Path(__file__).resolve().parent
sys.path.append(str(current_dir))

try:
    from proxmox_core import SecurityManager
    import paramiko
except ImportError as e:
    print(f"Errore import: {e}")
    sys.exit(1)

def mask(s):
    if not s: return "<VUOTO>"
    if len(s) < 4: return "***"
    return s[:2] + "*" * (len(s)-4) + s[-2:]

def main():
    print("=== DEBUG AUTH SFTP ===")
    
    config_file = current_dir / "config.json"
    key_file = current_dir / ".secret.key"

    if not config_file.exists():
        print(f"ERROR: Config file non trovato in {config_file}")
        sys.exit(1)
        
    print(f"Config file: {config_file}")
    print(f"Key file: {key_file} (Esiste? {key_file.exists()})")

    with open(config_file, 'r') as f:
        config = json.load(f)

    sftp_conf = config.get("sftp", {})
    raw_pass = sftp_conf.get("password", "")
    
    print(f"\n[Configurazione RAW]")
    print(f"Host: {sftp_conf.get('host')}")
    print(f"User: {sftp_conf.get('username')}")
    print(f"Pass (Raw): {mask(raw_pass)} (Inizia con ENC:? {'Sì' if raw_pass.startswith('ENC:') else 'No'})")

    final_pass = raw_pass
    
    # Decryption Logic Check
    if raw_pass.startswith("ENC:"):
        print("\nRilevata password cifrata. Tento decifratura...")
        try:
            sec_manager = SecurityManager(key_file=key_file)
            final_pass = sec_manager.decrypt(raw_pass)
            print(f"Pass (Decrypted): {mask(final_pass)}")
            if final_pass.startswith("ENC:"):
                print("ATTENZIONE: La password decifrata inizia ancora con ENC:! Qualcosa non va.")
        except Exception as e:
            print(f"ERRORE DECIFRATURA: {e}")
            final_pass = raw_pass # Fallback? No, fail.
    
    print(f"\n[Test Connessione Paramiko]")
    host = sftp_conf.get("host")
    port = sftp_conf.get("port", 22)
    user = sftp_conf.get("username")
    
    print(f"Connessione a {host}:{port} utente {user}...")
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Force password auth similar to my fix
        client.connect(
            host, 
            port=port, 
            username=user, 
            password=final_pass, 
            timeout=10,
            look_for_keys=False,
            allow_agent=False
        )
        print("\n✅ CONNESSIONE RIUSCITA!")
        client.close()
    except Exception as e:
        print(f"\n❌ CONNESSIONE FALLITA: {e}")
        # Print exception type
        print(f"Tipo Errore: {type(e)}")

if __name__ == "__main__":
    main()
