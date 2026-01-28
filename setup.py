#!/usr/bin/env python3
"""
Setup utility per creare un job cron che esegue proxmox_core.py.
Verifica le dipendenze di sistema, copia gli script in ~/proxreporter
(o percorso scelto) e configura il crontab.
"""

import getpass
import os
import shlex
import subprocess
import shutil
import importlib
import sys
import json
from pathlib import Path
from typing import List, Optional, Tuple
import random


def robust_input(prompt_text: str) -> str:
    """Legge input da stdin, con fallback su /dev/tty se stdin non è interattivo (es. pipe)."""
    # Se stdin non è un TTY (es. curl | bash), prova a usare /dev/tty direttamente
    if not sys.stdin.isatty():
        try:
            with open("/dev/tty", "r") as tty:
                # Scrivi il prompt direttamente sul TTY per essere sicuri che l'utente lo veda
                with open("/dev/tty", "w") as out_tty:
                    out_tty.write(prompt_text)
                    out_tty.flush()
                line = tty.readline()
                return line.rstrip("\n")
        except OSError:
            print("\n✗ Errore: impossibile leggere input (stdin chiuso e tty non disponibile).")
            print("  Esegui lo script interattivamente o senza pipe.")
            sys.exit(1)

    # Fallback standard se è un TTY o se sopra ha fallito in modo strano ma non fatale
    try:
        return input(prompt_text)
    except EOFError:
        return ""

def prompt(label: str, default: Optional[str] = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = robust_input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            value = default
        if value or not required:
            return value
        print("Valore obbligatorio, riprova.")


def prompt_password(label: str, default: Optional[str] = None, required: bool = True) -> str:
    # getpass usa /dev/tty di default se disponibile, ma se fallisce gestiamolo
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            value = getpass.getpass(f"{label}{suffix}: ")
        except EOFError:
             # Fallback manuale su tty se getpass fallisce su EOF
             print(f"{label}{suffix}: ", end="", flush=True)
             try:
                 with open("/dev/tty", "r") as tty:
                     # Nota: password sarà visibile qui, ma è un fallback estremo
                     # Purtroppo getpass su /dev/tty dovrebbe funzionare
                     # Se siamo qui, qualcosa è strano.
                     value = tty.readline().rstrip("\n")
             except OSError:
                 sys.exit(1)

        if not value and default is not None:
            value = default
        if value or not required:
            return value
        print("Valore obbligatorio, riprova.")


def prompt_yes_no(label: str, default: bool = False) -> bool:
    mapping = {"y": True, "yes": True, "n": False, "no": False}
    default_char = "y" if default else "n"
    while True:
        choice = robust_input(f"{label} [y/n] (default {default_char}): ").strip().lower()
        if not choice:
            return default
        if choice in mapping:
            return mapping[choice]
        print("Risposta non valida, usare y/n.")


def ensure_dependencies() -> None:
    required_packages: List[str] = []

    if shutil.which("lshw") is None:
        required_packages.append("lshw")

    try:
        importlib.import_module("paramiko")
    except ModuleNotFoundError:
        required_packages.append("python3-paramiko")
    
    try:
        importlib.import_module("jinja2")
    except ModuleNotFoundError:
        required_packages.append("python3-jinja2")
    
    # Verifica presenza cron
    if shutil.which("cron") is None and shutil.which("crond") is None:
        required_packages.append("cron")

    if not required_packages:
        return

    apt_path = shutil.which("apt-get")
    if not apt_path:
        print(
            "⚠ Dipendenze mancanti: "
            + ", ".join(required_packages)
            + ". Installare manualmente (apt non trovato)."
        )
        return

    if os.geteuid() != 0:
        print(
            "⚠ Dipendenze mancanti: "
            + ", ".join(required_packages)
            + ". Eseguire questo setup come root o installare manualmente:\n"
            f"    sudo apt install {' '.join(required_packages)}"
        )
        return

    print("→ Installazione pacchetti richiesti: " + ", ".join(required_packages))
    subprocess.run([apt_path, "update"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([apt_path, "install", "-y", *required_packages], check=False)

    # Ritenta import paramiko per feedback
    try:
        importlib.import_module("paramiko")
    except ModuleNotFoundError:
        print(
            "⚠ La libreria paramiko non è stata installata correttamente. "
            "Installare manualmente con: sudo apt install python3-paramiko"
        )
    
    # Avvia servizio cron se necessario
    if "cron" in required_packages:
        print("→ Avvio servizio cron...")
        for cmd in [
            ["systemctl", "enable", "cron"],
            ["systemctl", "start", "cron"],
            ["systemctl", "enable", "crond"],  # Alcune distro usano crond
            ["systemctl", "start", "crond"],
        ]:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_cron_expression() -> str:
    print("\nSeleziona la frequenza di esecuzione:")
    print("  1) Ogni giorno alle 11:00 (+/- 60 min variabile)")
    print("  2) Ogni giorno alle 02:00 (legacy)")
    print("  3) Personalizzata (inserisci espressione cron)")
    choice = prompt("Scelta", default="1")
    if choice == "1":
        # Genera orario randomico tra 10:00 e 12:59
        rand_min = random.randint(0, 59)
        rand_hour = random.randint(10, 12)
        print(f"  → Orario calcolato randomicamente: {rand_hour:02d}:{rand_min:02d}")
        return f"{rand_min} {rand_hour} * * *"
    if choice == "2":
        return "0 2 * * *"
    print("Inserisci espressione cron (es. 30 3 * * 1-5):")
    return prompt("Espressione cron personalizzata")


def deploy_scripts(target_dir: Path) -> Path:
    source_dir = Path(__file__).resolve().parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(f"✗ Permessi insufficienti per creare {target_dir}. Esegui lo script come root.")
        raise SystemExit(1)

    if source_dir.resolve() == target_dir.resolve():
        print(f"ℹ Directory sorgente e destinazione coincidono ({target_dir}). Salto la copia.")
        return (target_dir / "proxmox_core.py").resolve()

    files_to_copy = ["proxmox_core.py", "proxmox_report.py", "update_scripts.py", "html_generator.py", "email_sender.py", "debug_auth.py", "install.sh", "README.md"]
    for filename in files_to_copy:
        src = source_dir / filename
        if not src.exists():
            print(f"⚠ File sorgente mancante: {src}")
            continue
        dst = target_dir / filename
        shutil.copy2(src, dst)
        if dst.suffix == ".py":
            dst.chmod(0o755)
            
    # Copy templates
    templates_src = source_dir / "templates"
    templates_dst = target_dir / "templates"
    if templates_src.exists():
        if templates_dst.exists():
            shutil.rmtree(templates_dst)
        shutil.copytree(templates_src, templates_dst)
        print(f"✓ Template copiati in {templates_dst}")
    else:
        print(f"⚠ Directory templates mancante: {templates_src}")
        
    print(f"✓ Script copiati in {target_dir}")
    return (target_dir / "proxmox_core.py").resolve()


def build_command(args: List[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def setup_default(script_path: str) -> str:
    """
    Configura setup rapido con valori di default.
    Ritorna il comando cron generato.
    """
    print("\n=== SETUP RAPIDO (Default) ===\n")
    
    # Valori di default
    default_output = "/var/log/proxreporter"
    
    # Chiedi solo le info essenziali
    codcli = prompt("Codice cliente (codcli)")
    nomecliente = prompt("Nome cliente (nomecliente)")
    
    print("\n→ Configurazione automatica:")
    print(f"  • Esecuzione: locale")
    print(f"  • Output: {default_output}")
    print(f"  • SFTP: sftp.domarc.it:11122")
    print(f"  • Auto-update: abilitato")
    print(f"  • Schedulazione: ore 11:00 ogni giorno")
    
    args: List[str] = [
        "python3",
        script_path,
        "--codcli",
        codcli,
        "--nomecliente",
        nomecliente,
        "--output-dir",
        default_output,
        "--local",
        "--auto-update",
    ]
    
    # Cron: ogni giorno alle 11:00
    cron_expr = "0 11 * * *"
    log_file = f"{default_output}/cron.log"
    command = f"{cron_expr} {build_command(args)} >> {shlex.quote(log_file)} 2>&1"
    
    return command


def setup_interactive(script_path: str) -> Tuple[str, str]:
    """
    Setup interattivo: Richiede password SFTP, usa default per resto, genera config.json.
    """
    print("\n=== SETUP PROXREPORTER V2 ===\n")
    
    default_output = str(Path("/var/log/proxreporter"))
    output_dir = prompt("Directory output dei report", default=default_output)

    codcli = prompt("Codice cliente (codcli)")
    nomecliente = prompt("Nome cliente (nomecliente)")
    
    # SFTP Configuration
    print("\n[Configurazione SFTP]")
    print(f"  Host: sftp.domarc.it (default)")
    print(f"  Utente: ***** (nascosto)")
    
    sftp_password = prompt_password("Password SFTP (Obbligatoria)", required=True)
    
    # SSH/Local (Remote check)
    use_remote = prompt_yes_no("Interrogare un host Proxmox remoto?")
    remote_conf = {}
    
    args: List[str] = [
        "python3",
        script_path,
        "--codcli",
        codcli,
        "--nomecliente",
        nomecliente,
        "--output-dir",
        output_dir,
    ]
    
    if use_remote:
        host = prompt("Host/IP remoto (Proxmox API/SSH)")
        username = prompt("Username API/SSH (es. root@pam)")
        password = prompt_password("Password API/SSH")
        ssh_port_val = int(prompt("Porta SSH", default="22"))
        
        remote_conf = {
            "enabled": True,
            "host": host,
            "port": ssh_port_val,
            "username": username,
            "password": password
        }
    else:
        args.append("--local")

    if prompt_yes_no("Abilitare auto-aggiornamento script prima di ogni esecuzione?", default=True):
        args.append("--auto-update")

    # Email Config - Default SMTP credentials (obfuscated)
    def _get_default_smtp_pw():
        """Decode obfuscated default SMTP password."""
        import base64
        _k = "PROXREPORTER"
        _d = base64.b64decode("Yg0FOxd9YikaOQ==")
        return ''.join(chr(b ^ ord(_k[i % len(_k)])) for i, b in enumerate(_d))
    
    smtp_conf = {"enabled": False}
    if prompt_yes_no("\nConfigurare invio report via email?"):
        smtp_host = prompt("SMTP Host", default="esva.domarc.it")
        smtp_port = prompt("SMTP Port", default="25")
        smtp_user = prompt("SMTP Username", default="smtp.domarc")
        print("  Password di default disponibile (premi INVIO per usarla)")
        smtp_password = prompt_password("SMTP Password", required=False)
        if not smtp_password:
            smtp_password = _get_default_smtp_pw()
            print("  → Usando credenziali di default")
        smtp_sender = prompt("Email Mittente", default="proxreporter@domarc.it")
        smtp_recipients = prompt("Destinatari (separati da virgola)")
        
        smtp_conf = {
            "enabled": True,
            "host": smtp_host,
            "port": int(smtp_port),
            "user": smtp_user,
            "password": smtp_password,
            "sender": smtp_sender,
            "recipients": smtp_recipients,
            "use_tls": False,
            "use_ssl": False
        }



    # Generate config.json (prepara dizionario)
    # Nota: installazione avverrà nella directory dove risiede lo script
    # Ma noi dobbiamo sapere dove verrà copiato lo script.
    # setup.py copia IN target_dir. 
    # Quindi config.json deve andare in target_dir.
    
    # Ritorneremo il config dict per salvarlo nel main dopo aver deciso la target dir?
    # No, setup_custom ritorna command.
    # Possiamo salvare config.json adesso se sapessimo la dir, ma la dir è passata solo a deploy_scripts.
    # Il path dello script_path è assoluto (es. /opt/proxreport/proxmox_core.py)
    # Ricaviamo la dir da script_path
    
    install_dir = Path(script_path).parent
    config_file = install_dir / "config.json"
    
    # Cifratura Password
    encrypt_enabled = False
    security_manager = None
    
    # Check if cryptography is available
    try:
        from cryptography.fernet import Fernet
        crypto_available = True
    except ImportError:
        crypto_available = False
        print("\n⚠ Libreria 'cryptography' non presente. Le password saranno salvate in CHIARO.")
        print("  Per abilitare la cifratura: pip3 install cryptography o apt install python3-cryptography")

    if crypto_available:
        if prompt_yes_no("\nCifrare le password nel file di configurazione? (Richiede file .secret.key)", default=True):
            encrypt_enabled = True
            key_file = install_dir / ".secret.key"
            try:
                # Genera/Sovrascrivi chiave
                key = Fernet.generate_key()
                # Scrivi chiave con permessi stretti
                key_file.touch(mode=0o600, exist_ok=True)
                with open(key_file, "wb") as f:
                    f.write(key)
                cipher = Fernet(key)
                print(f"✓ Chiave cifratura generata in {key_file} (NON CANCELLARE!)")
                
                def encrypt_val(val):
                    if not val: return val
                    return "ENC:" + cipher.encrypt(val.encode()).decode()
            except Exception as e:
                print(f"⚠ Errore generazione chiave: {e}. Passa a modalità in chiaro.")
                encrypt_enabled = False

    # Helper per cifrare se abilitato
    def secure(val):
        if encrypt_enabled and val and not val.startswith("ENC:"):
            return encrypt_val(val)
        return val

    config_data = {
        "proxmox": {
            "enabled": True,
            "host": remote_conf.get("host", "localhost") + ":8006" if use_remote else "localhost", 
            "username": remote_conf.get("username", ""),
            "password": secure(remote_conf.get("password", "")),
            "verify_ssl": False
        },
        "ssh": {
            "enabled": bool(remote_conf),
            "host": remote_conf.get("host", ""),
            "port": remote_conf.get("port", 22),
            "username": remote_conf.get("username", ""),
            "password": secure(remote_conf.get("password", ""))
        } if use_remote else {"enabled": False},
        "client": {
            "codcli": codcli,
            "nomecliente": nomecliente,
            "server_identifier": remote_conf.get("host", "local") if use_remote else "local"
        },
        "sftp": {
            "enabled": True,
            "host": "sftp.domarc.it",
            "port": 11122,
            "username": "proxmox",
            "password": secure(sftp_password),
            "base_path": "/home/proxmox/uploads",
            "fallback_host": "192.168.20.14",
            "fallback_port": 22
        },
        "smtp": {
            "enabled": smtp_conf.get("enabled", False),
            "host": smtp_conf.get("host", "esva.domarc.it"),
            "port": smtp_conf.get("port", 25),
            "user": smtp_conf.get("user", "smtp.domarc"),
            "password": secure(smtp_conf.get("password", "")),
            "sender": smtp_conf.get("sender", "proxreporter@domarc.it"),
            "recipients": smtp_conf.get("recipients", ""),
            "use_tls": False,
            "use_ssl": False
        },
        "system": {
            "output_directory": output_dir,
            "max_file_copies": 5,
            "log_level": "INFO"
        },
        "features": {
            "collect_cluster": True,
            "collect_host": True,
            "collect_host_details": True,
            "collect_storage": True,
            "collect_network": True,
            "collect_vms": True,
            "collect_backup": True,
            "collect_containers": False,
            "collect_perf": False
        }
    }
    
    # Scrivi config.json
    try:
        with open(config_file, "w") as f:
            json.dump(config_data, f, indent=4)
        print(f"✓ Configurazione salvata in {config_file}")
        # Imposta permessi restrittivi (contiene password)
        os.chmod(config_file, 0o600)
    except Exception as e:
        print(f"⚠ Errore salvataggio config.json: {e}")

    # Aggiungi --config agli argomenti cron
    args += ["--config", str(config_file)]

    cron_expr = build_cron_expression()

    log_file = prompt("File log per output cron", default=f"{output_dir}/cron.log", required=False)
    if log_file:
        command = f"{cron_expr} {build_command(args)} >> {shlex.quote(log_file)} 2>&1"
    else:
        command = f"{cron_expr} {build_command(args)}"
    
    return command, output_dir


def clean_crontab(marker: str = "proxmox_core.py") -> None:
    """Rimuove dal crontab le linee che contengono il marker."""
    print("→ Verifica pulizia crontab...")
    try:
        # Leggi crontab
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            # Se exit code != 0, probabilmente non c'è crontab per l'utente, nulla da pulire
            return

        current_cron = result.stdout
        if not current_cron:
            return

        new_lines = []
        removed = 0
        for line in current_cron.splitlines():
            # Rimuovi spazio vuoto e commenti per check lasco, ma il marker è affidabile
            if marker in line:
                removed += 1
                continue
            new_lines.append(line)
        
        if removed > 0:
            # Ricostruisci crontab (assicura newline finale)
            new_cron = "\n".join(new_lines) + "\n"
            subprocess.run(["crontab", "-"], input=new_cron, text=True, check=True)
            print(f"  ✓ Rimossi {removed} job obsoleti/duplicati dal crontab.")
        else:
            print("  ✓ Nessun job precedente trovato.")
            
    except Exception as e:
        print(f"  ⚠ Errore durante la pulizia del crontab: {e}")


def update_system(install_dir: Path) -> None:
    print(f"\n→ Aggiornamento sistema in {install_dir}...")
    
    # Cerca root del repo
    repo_dir = None
    if (install_dir / ".git").exists():
        repo_dir = install_dir
        
    if repo_dir:
        print(f"  Rilevato repository Git in {repo_dir}. Eseguo pull...")
        try:
             subprocess.run(["git", "pull"], cwd=repo_dir, check=True)
             print("  ✓ Codice aggiornato via Git.")
        except subprocess.CalledProcessError as e:
             print(f"  ✗ Errore durante git pull: {e}")
             print("  Provo aggiornamento manuale file principali...")
             repo_dir = None # Fallback
    
    if not repo_dir:
        # Fallback manual download (es. installazione curl | bash senza git clone full)
        print("  Aggiornamento file singoli (fallback no-git)...")
        base_url = "https://raw.githubusercontent.com/grandir66/Proxreporter/main"
        files = ["proxmox_core.py", "proxmox_report.py", "setup.py", "update_scripts.py", "install.sh"]
        
        for f in files:
            try:
                dest = install_dir / f
                # Usa curl con timestamp per evitare cache
                import time
                url = f"{base_url}/{f}?t={int(time.time())}"
                subprocess.run(["curl", "-s", "-o", str(dest), url], check=True)
                dest.chmod(0o755)
                print(f"    ✓ Aggiornato {f}")
            except Exception as e:
                print(f"    ✗ Errore aggiornamento {f}: {e}")

    # Re-check dependencies
    print("\n  Verifica dipendenze di sistema...")
    ensure_dependencies()
    print("\n✓ Aggiornamento COMPLETO terminato.")


def uninstall_system(install_dir: Path) -> None:
    print(f"\n→ Disinstallazione sistema...")
    print(f"  Directory rilevata: {install_dir}")
    
    if not prompt_yes_no("⚠ Sei sicuro di voler RIMUOVERE Proxreporter e i job cron associati?", default=False):
        print("Operazione annullata.")
        return

    # 1. Clean Cron
    clean_crontab("proxmox_core.py")
    
    # 2. Remove Directory
    if install_dir.exists():
        if prompt_yes_no(f"Cancellare la directory {install_dir} (inclusi log e config)?", default=False):
            try:
                shutil.rmtree(install_dir)
                print(f"  ✓ Directory {install_dir} rimossa.")
            except Exception as e:
                print(f"  ✗ Errore rimozione directory: {e}")
        else:
            print(f"  Directory {install_dir} mantenuta.")
    else:
        print(f"  Directory {install_dir} non trovata.")

    print("\n✓ Disinstallazione completata.")


def main() -> None:
    ensure_dependencies()

    print("=" * 70)
    print("PROXMOX REPORTER - SETUP & MANAGEMENT UTILITY")
    print("=" * 70)

    while True:
        print("\nSeleziona operazione:")
        print("  1) INSTALLAZIONE / RICONFIGURAZIONE")
        print("     (Installa script, configura parametri e crea job cron)")
        print("  2) UPDATE SISTEMA")
        print("     (Aggiorna gli script all'ultima versione e verifica dipendenze)")
        print("  3) DISINSTALLAZIONE")
        print("     (Rimuove job cron e file del programma)")
        print("  q) Esci")
        
        choice = robust_input("\nScelta [1]: ").strip()
        if not choice: choice = "1"
        
        if choice == "q":
            return
            
        if choice == "3":
            # Uninstall
            default_install_dir = Path("/opt/proxreport")
            install_dir_input = prompt("Directory installazione da rimuovere", default=str(default_install_dir))
            uninstall_system(Path(install_dir_input))
            return # Exit after uninstall
            
        if choice == "2":
            # Update
            default_install_dir = Path("/opt/proxreport")
            if (Path.cwd() / "proxmox_core.py").exists():
                 # Se siamo eseguiti dalla directory di installazione, usa quella come default
                 default_install_dir = Path.cwd()
            
            install_dir_input = prompt("Directory installazione da aggiornare", default=str(default_install_dir))
            update_system(Path(install_dir_input))
            return # Exit after update? Or go back to menu? Usually exit.
            
        if choice == "1":
            # Install (Break loop to proceed with normal setup)
            break
            
        print("Scelta non valida.")

    # --- INIZIO LOGICA INSTALLAZIONE (Ex Main) ---
    
    print("\n" + "-"*50)
    print("AVVIO PROCEDURA INSTALLAZIONE")
    print("-"*50)
    
    # Deploy scripts
    # Deploy scripts (o in-place)
    # Se siamo già nella cartella con i file, proponiamo quella come default
    default_install_dir = Path.cwd() if (Path.cwd() / "proxmox_core.py").exists() else Path("/opt/proxreport")
    
    install_dir_input = prompt(
        "Directory installazione script",
        default=str(default_install_dir),
    )
    install_dir = Path(install_dir_input).expanduser().resolve()
    
    # Check In-Place vs Deploy
    script_path = ""
    
    if (Path.cwd() / "proxmox_core.py").exists() and install_dir == Path.cwd():
         print(f"ℹ Installazione In-Place rilevata in {install_dir}. Salto la copia dei file.")
         script_path = str(install_dir / "proxmox_core.py")
         
         # Assicuriamo i permessi corretti anche in-place
         for pyfile in install_dir.glob("*.py"):
             try:
                 pyfile.chmod(0o755)
             except:
                 pass
    else:
         # Copia file esterna
         script_path = str(deploy_scripts(install_dir))

    # Genera comando cron e config
    command, custom_output_dir = setup_interactive(script_path)
    
    # Crea directory di output e log
    output_dir = Path(custom_output_dir) if custom_output_dir else Path("/var/log/proxreporter")
    
    print(f"\n→ Creazione directory output...")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "csv").mkdir(exist_ok=True)
        (output_dir / "backup").mkdir(exist_ok=True)
        # Permessi 755
        for p in [output_dir, output_dir / "csv", output_dir / "backup"]:
            p.chmod(0o755)
            
        print(f"  ✓ {output_dir}")
    except PermissionError as e:
        print(f"  ✗ Permessi insufficienti: {e}")
        if os.geteuid() != 0:
            print("\n✗ Errore: il setup richiede privilegi root per creare directory in /var/log/")
            sys.exit(1)
    except Exception as e:
        print(f"  ⚠ Errore creazione directory: {e}")

    # Mostra e conferma
    print("\n" + "=" * 70)
    print("CRON JOB GENERATO")
    print("=" * 70)
    print(f"\n{command}\n")
    print("=" * 70)

    if not prompt_yes_no("\nInstallare questo job nel crontab?", default=True):
        print("\n✋ Setup annullato (configurazione salvata ma cron non installato).")
        return

    # Pulizia vecchi job PRIMA di inserire il nuovo
    clean_crontab("proxmox_core.py")

    # Installazione nuovo job
    try:
        # Rileggi per sicurezza post-pulizia
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        cron_file = existing.stdout if existing.returncode == 0 else ""
        
        if cron_file and not cron_file.endswith("\n"):
            cron_file += "\n"
        
        cron_file += command + "\n"
        
        subprocess.run(["crontab", "-"], input=cron_file, text=True, check=True)
        print("\n✓ Job cron installato correttamente!")
        
        print("\n→ Verifica con: crontab -l")
        print(f"→ Test manuale immediato: {script_path} --local --auto-update --config {install_dir}/config.json")
        
    except subprocess.CalledProcessError as exc:
        print(f"\n✗ Errore durante l'installazione del crontab: {exc}")
        print("\nPer installare manualmente, esegui:")
        print("  crontab -e")
        print(f"  # Aggiungi: {command}")



if __name__ == "__main__":
    main()

