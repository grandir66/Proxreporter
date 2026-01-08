#!/usr/bin/env python3
"""
Script per configurare i parametri SMTP di Proxmox per le notifiche.
Configura il server SMTP, i destinatari e aggiunge destinatari aggiuntivi.

Utilizzo:
    python3 configure_smtp.py --smtp-server smtp.example.com --smtp-port 587 \\
        --smtp-user user@example.com --smtp-password password \\
        --from-address noreply@example.com \\
        --recipient admin@example.com \\
        --additional-recipients user1@example.com,user2@example.com

    python3 configure_smtp.py --host 192.168.1.100 --username root@pam --password pass \\
        --smtp-server smtp.example.com --smtp-port 587 \\
        --recipient admin@example.com
"""

import argparse
import json
import sys
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import paramiko
except ImportError:
    print("⚠ Libreria paramiko non disponibile. Installare: apt install python3-paramiko")
    paramiko = None


def execute_command_local(cmd: str) -> Optional[str]:
    """Esegue un comando in locale"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception as e:
        print(f"  ⚠ Errore esecuzione comando: {e}")
        return None


def execute_command_ssh(ssh_client: paramiko.SSHClient, cmd: str) -> Optional[str]:
    """Esegue un comando via SSH"""
    try:
        stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=30)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            return stdout.read().decode('utf-8').strip()
        return None
    except Exception as e:
        print(f"  ⚠ Errore esecuzione comando SSH: {e}")
        return None


def connect_ssh(host: str, port: int, username: str, password: str) -> Optional[paramiko.SSHClient]:
    """Connette al server Proxmox via SSH"""
    if not paramiko:
        print("✗ Paramiko non disponibile, impossibile connettersi via SSH")
        return None
    
    try:
        print(f"→ Connessione SSH a {host}:{port}...")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=port, username=username, password=password, timeout=10)
        print(f"  ✓ Connesso a {host}")
        return ssh
    except Exception as e:
        print(f"  ✗ Connessione SSH fallita: {e}")
        return None


def configure_smtp_via_pvesh(
    smtp_config: Dict[str, Any],
    execution_mode: str,
    executor
) -> bool:
    """
    Configura SMTP tramite pvesh (CLI Proxmox).
    Usa i comandi pvesh per configurare le notifiche.
    """
    try:
        # Proxmox 7.0+ usa un sistema di notifiche basato su target
        # Possiamo provare a configurare tramite pvesh
        
        # Nota: pvesh potrebbe non avere comandi diretti per SMTP in tutte le versioni
        # Quindi questo è un tentativo, con fallback al metodo file-based
        
        if execution_mode == "ssh" and executor:
            # Prova con pvesh se disponibile
            pvesh_check = executor("which pvesh 2>/dev/null")
            if not pvesh_check:
                return False
            
            # Per ora, usiamo il metodo file-based che è più affidabile
            return False
        else:
            # Locale
            result = execute_command_local("which pvesh 2>/dev/null")
            if not result:
                return False
            
            # Per ora, usiamo il metodo file-based
            return False
    except Exception as e:
        print(f"  ⚠ Errore configurazione pvesh: {e}")
        return False


def configure_smtp_via_file(
    smtp_config: Dict[str, Any],
    execution_mode: str,
    executor
) -> bool:
    """
    Configura SMTP modificando i file di configurazione Proxmox.
    Crea/modifica il file di configurazione per le notifiche.
    """
    config_dir = "/etc/pve/notifications"
    config_file = f"{config_dir}/smtp.conf"
    
    try:
        if execution_mode == "ssh" and executor:
            # Modalità remota
            print(f"→ Configurazione SMTP remota: {config_file}")
            executor(f"mkdir -p {config_dir} 2>/dev/null")
            
            # Crea il file di configurazione JSON
            config_data = {
                "server": smtp_config.get("server"),
                "port": smtp_config.get("port", 587),
                "username": smtp_config.get("username"),
                "password": smtp_config.get("password"),
                "from_address": smtp_config.get("from_address"),
                "encryption": smtp_config.get("encryption", "starttls"),
                "recipients": smtp_config.get("recipients", []),
                "additional_recipients": smtp_config.get("additional_recipients", [])
            }
            
            config_json = json.dumps(config_data, indent=2)
            delimiter = "PROXREPORTER_SMTP_CONFIG_EOF"
            cmd = f'''cat > "{config_file}" << '{delimiter}'
{config_json}
{delimiter}'''
            result = executor(cmd)
            
            # Verifica
            check_cmd = f'test -f "{config_file}" && echo "OK" || echo "FAIL"'
            check_result = executor(check_cmd)
            if check_result and "OK" in check_result:
                print(f"  ✓ Configurazione SMTP salvata: {config_file}")
                return True
            else:
                print(f"  ⚠ Impossibile verificare il file (verifica permessi root)")
                return False
        else:
            # Modalità locale
            print(f"→ Configurazione SMTP locale: {config_file}")
            config_path = Path(config_dir)
            try:
                config_path.mkdir(parents=True, exist_ok=True)
                
                config_data = {
                    "server": smtp_config.get("server"),
                    "port": smtp_config.get("port", 587),
                    "username": smtp_config.get("username"),
                    "password": smtp_config.get("password"),
                    "from_address": smtp_config.get("from_address"),
                    "encryption": smtp_config.get("encryption", "starttls"),
                    "recipients": smtp_config.get("recipients", []),
                    "additional_recipients": smtp_config.get("additional_recipients", [])
                }
                
                config_file_path = config_path / "smtp.conf"
                with open(config_file_path, 'w', encoding='utf-8') as f:
                    json.dump(config_data, f, indent=2)
                
                print(f"  ✓ Configurazione SMTP salvata: {config_file}")
                return True
            except PermissionError:
                print(f"  ⚠ Impossibile creare configurazione (richiesti permessi root)")
                print(f"     Eseguire manualmente: sudo mkdir -p {config_dir}")
                return False
            except Exception as e:
                print(f"  ⚠ Errore creazione configurazione: {e}")
                return False
    except Exception as e:
        print(f"  ⚠ Errore durante configurazione SMTP: {e}")
        return False


def configure_proxmox_notification_target(
    smtp_config: Dict[str, Any],
    execution_mode: str,
    executor,
    smtp_password: Optional[str] = None
) -> bool:
    """
    Configura il target di notifica SMTP in Proxmox usando pvesm o pvenotify.
    Questo è il metodo preferito per Proxmox 7.0+.
    Aggiunge un nuovo server SMTP dedicato senza sovrascrivere configurazioni esistenti.
    """
    server = smtp_config.get("server")
    port = smtp_config.get("port", 587)
    username = smtp_config.get("username")
    password = smtp_config.get("password")
    from_addr = smtp_config.get("from_address")
    encryption = smtp_config.get("encryption", "starttls")
    
    recipients = smtp_config.get("recipients", [])
    additional = smtp_config.get("additional_recipients", [])
    all_recipients = recipients + additional
    
    try:
        target_name = "da-alert"
        
        # Prepara i destinatari (primo principale, poi aggiuntivi)
        recipients_list = recipients + additional
        mailto_value = ", ".join(recipients_list) if recipients_list else recipients[0] if recipients else ""
        
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
            print(f"  ℹ Notification target 'da-alert' già esistente")
            print("     Non sovrascritto per preservare le impostazioni")
            return True
        
        # Costruisci comando pvesh
        # Escape della password per shell (sostituisci ' con '\''')
        password_escaped = password.replace("'", "'\"'\"'") if password else ""
        
        # Costruisci comando base
        # Nota: pvesh usa --user invece di --username
        pvesh_cmd_parts = [
            "pvesh create /cluster/notifications/endpoints/smtp",
            f"--name {target_name}",
            f"--mailto '{mailto_value}'",
            f"--server '{server}'",
            f"--port {port}",
            f"--from-address '{from_addr}'",
            "--mode insecure"
        ]
        
        if username:
            pvesh_cmd_parts.append(f"--user '{username}'")
        if password:
            pvesh_cmd_parts.append(f"--password '{password_escaped}'")
        
        pvesh_cmd = " ".join(pvesh_cmd_parts)
        
        if execution_mode == "ssh" and executor:
            print(f"→ Configurazione notification target 'da-alert' remota (pvesh)...")
            print(f"  Comando: pvesh create /cluster/notifications/endpoints/smtp --name {target_name}")
            
            result = executor(pvesh_cmd)
            if result:
                print(f"  pvesh output: {result}")
            
            # Verifica che il target sia stato creato
            check_cmd = f'pvesh get /cluster/notifications/endpoints/{target_name} 2>/dev/null && echo "OK" || echo "FAIL"'
            check_result = executor(check_cmd)
            print(f"  Verifica target: {check_result}")
            
            if check_result and "OK" in check_result:
                print(f"  ✓ Notification target 'da-alert' creato con successo")
                print(f"     (Target dedicato, non sovrascrive configurazioni esistenti)")
                return True
            else:
                # Controlla se l'errore è "already exists"
                if result and ("already exists" in result.lower() or "duplicate" in result.lower()):
                    print(f"  ℹ Notification target 'da-alert' già esistente")
                    return True
                print(f"  ✗ Impossibile verificare creazione target")
                print(f"     Verifica permessi e che pvesh sia disponibile")
                return False
        elif execution_mode == "local":
            print(f"→ Configurazione notification target 'da-alert' locale (pvesh)...")
            print(f"  Comando: pvesh create /cluster/notifications/endpoints/smtp --name {target_name}")
            
            try:
                result = subprocess.run(
                    pvesh_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode == 0:
                    print(f"  ✓ Comando pvesh eseguito con successo")
                    
                    # Verifica che il target sia stato creato
                    verify_result = subprocess.run(
                        ["pvesh", "get", f"/cluster/notifications/endpoints/{target_name}"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    
                    if verify_result.returncode == 0:
                        print(f"  ✓ Notification target 'da-alert' verificato")
                        print(f"     (Target dedicato, non sovrascrive configurazioni esistenti)")
                        return True
                    else:
                        print(f"  ⚠ Target creato ma verifica fallita")
                        return True  # Considera comunque successo se il comando è andato a buon fine
                else:
                    error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                    if "already exists" in error_msg.lower() or "duplicate" in error_msg.lower():
                        print(f"  ℹ Notification target 'da-alert' già esistente")
                        print("     Non sovrascritto per preservare le impostazioni")
                        return True
                    else:
                        print(f"  ✗ Errore pvesh: {error_msg}")
                        return False
            except FileNotFoundError:
                print(f"  ✗ pvesh non trovato (Proxmox non installato o non in PATH)")
                return False
            except subprocess.TimeoutExpired:
                print(f"  ✗ Timeout durante esecuzione pvesh")
                return False
            except Exception as e:
                print(f"  ✗ Errore esecuzione pvesh: {e}")
                return False
        else:
            print(f"  ℹ Configurazione SMTP non disponibile (modalità non supportata)")
            return False
    except Exception as e:
        print(f"  ⚠ Errore configurazione target: {e}")
        return False


def apply_smtp_configuration_to_proxmox(
    smtp_config: Dict[str, Any],
    execution_mode: str,
    executor
) -> bool:
    """
    Applica la configurazione SMTP a Proxmox usando il metodo più appropriato.
    Prova prima con pvesm/pvenotify, poi con file di configurazione.
    """
    # Metodo 1: Configurazione tramite file (metodo più universale)
    success = configure_proxmox_notification_target(smtp_config, execution_mode, executor)
    
    if success:
        print("\n→ Configurazione SMTP completata.")
        print("  Nota: Per applicare completamente le modifiche in Proxmox:")
        print("        1. Accedi all'interfaccia web: Datacenter > Notifications")
        print("        2. Verifica che il target SMTP sia configurato correttamente")
        print("        3. Crea un Notification Matcher per i backup vzdump")
        print("        4. Riavvia i servizi se necessario: systemctl reload pveproxy")
        return True
    
    return False


# Configurazione SMTP Domarc (valori predefiniti)
DEFAULT_SMTP_SERVER = "esva.domarc.it"
DEFAULT_SMTP_PORT = 25
DEFAULT_SMTP_USER = "smtp.domarc"
DEFAULT_SMTP_ENCRYPTION = "starttls"
DEFAULT_FROM_ADDRESS = "bk_px_clienti@domarc.it"
DEFAULT_RECIPIENT = "domarcsrl+pxbackup@mycheckcentral.cc"

# ============================================================================
# PASSWORD SMTP - INSERIRE QUI LA PASSWORD MANUALMENTE
# ============================================================================
# Inserisci la password SMTP qui sotto tra le virgolette:
DEFAULT_SMTP_PASSWORD = "***REMOVED***"
# ============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configura i parametri SMTP di Proxmox per le notifiche (Domarc)"
    )
    
    # Parametri per connessione remota
    parser.add_argument("--host", help="Hostname/IP Proxmox remoto (per SSH)")
    parser.add_argument("--username", help="Utente SSH (es. root)")
    parser.add_argument("--password", help="Password SSH")
    parser.add_argument("--ssh-port", type=int, default=22, help="Porta SSH (default: 22)")
    
    # Parametri SMTP (con valori predefiniti Domarc)
    parser.add_argument("--smtp-server", default=DEFAULT_SMTP_SERVER,
                       help=f"Server SMTP (default: {DEFAULT_SMTP_SERVER})")
    parser.add_argument("--smtp-port", type=int, default=DEFAULT_SMTP_PORT,
                       help=f"Porta SMTP (default: {DEFAULT_SMTP_PORT})")
    parser.add_argument("--smtp-user", default=DEFAULT_SMTP_USER,
                       help=f"Username SMTP (default: {DEFAULT_SMTP_USER})")
    parser.add_argument("--smtp-password", help="Password SMTP (richiesta)")
    parser.add_argument("--smtp-encryption", choices=["none", "starttls", "ssl"], 
                       default=DEFAULT_SMTP_ENCRYPTION,
                       help=f"Tipo di crittografia (default: {DEFAULT_SMTP_ENCRYPTION})")
    parser.add_argument("--from-address", default=DEFAULT_FROM_ADDRESS,
                       help=f"Indirizzo email mittente (default: {DEFAULT_FROM_ADDRESS})")
    
    # Destinatari (con valore predefinito)
    parser.add_argument("--recipient", default=DEFAULT_RECIPIENT,
                       help=f"Destinatario principale (default: {DEFAULT_RECIPIENT})")
    parser.add_argument("--additional-recipients", 
                       help="Destinatari aggiuntivi separati da virgola (es. user1@example.com,user2@example.com)")
    
    return parser.parse_args()


def main():
    args = parse_args()
    
    print("=" * 70)
    print("CONFIGURAZIONE SMTP PROXMOX - DOMARC")
    print("=" * 70)
    
    # Usa password SMTP: prima da argomento, poi da variabile DEFAULT, infine chiedi all'utente
    smtp_password = args.smtp_password
    if not smtp_password:
        smtp_password = DEFAULT_SMTP_PASSWORD
    if not smtp_password:
        import getpass
        smtp_password = getpass.getpass(f"Password SMTP per {args.smtp_user}: ")
        if not smtp_password:
            print("✗ Password SMTP obbligatoria")
            print("   Inserirla nel file configure_smtp.py nella variabile DEFAULT_SMTP_PASSWORD")
            sys.exit(1)
    
    # Determina modalità di esecuzione
    remote_enabled = bool(args.host)
    execution_mode = "ssh" if remote_enabled else "local"
    executor = None
    ssh_client = None
    
    if remote_enabled:
        if not args.username:
            print("✗ Per l'accesso remoto specifica --username")
            sys.exit(1)
        
        # Chiedi password SSH se non fornita
        ssh_password = args.password
        if not ssh_password:
            import getpass
            ssh_password = getpass.getpass(f"Password SSH per {args.username}@{args.host}: ")
            if not ssh_password:
                print("✗ Password SSH obbligatoria per accesso remoto")
                sys.exit(1)
        
        ssh_client = connect_ssh(args.host, args.ssh_port, args.username, ssh_password)
        if not ssh_client:
            print("✗ Impossibile connettersi via SSH")
            sys.exit(1)
        
        def ssh_executor(cmd: str) -> Optional[str]:
            return execute_command_ssh(ssh_client, cmd)
        executor = ssh_executor
    else:
        executor = execute_command_local
    
    # Prepara configurazione SMTP
    recipients = [args.recipient]
    if args.additional_recipients:
        additional = [r.strip() for r in args.additional_recipients.split(",") if r.strip()]
        recipients.extend(additional)
    
    smtp_config = {
        "server": args.smtp_server,
        "port": args.smtp_port,
        "username": args.smtp_user,
        "password": smtp_password,
        "from_address": args.from_address,
        "encryption": args.smtp_encryption,
        "recipients": [args.recipient],
        "additional_recipients": [r.strip() for r in args.additional_recipients.split(",")] if args.additional_recipients else []
    }
    
    print(f"\n→ Configurazione SMTP:")
    print(f"  Server: {smtp_config['server']}:{smtp_config['port']}")
    print(f"  From: {smtp_config['from_address']}")
    print(f"  Encryption: {smtp_config['encryption']}")
    print(f"  Recipients: {', '.join(smtp_config['recipients'])}")
    if smtp_config['additional_recipients']:
        print(f"  Additional: {', '.join(smtp_config['additional_recipients'])}")
    print()
    
    # Applica configurazione
    success = apply_smtp_configuration_to_proxmox(smtp_config, execution_mode, executor)
    
    if ssh_client:
        ssh_client.close()
        print("\n✓ Connessione SSH chiusa")
    
    if success:
        print("\n" + "=" * 70)
        print("✓ CONFIGURAZIONE SMTP COMPLETATA")
        print("=" * 70)
        print("\nNota: Potrebbe essere necessario riavviare i servizi Proxmox")
        print("      per applicare completamente le modifiche.")
        sys.exit(0)
    else:
        print("\n✗ CONFIGURAZIONE SMTP FALLITA")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n✋ Operazione interrotta dall'utente")
        sys.exit(0)
    except Exception as exc:
        print(f"\n✗ Errore critico: {exc}")
        raise

