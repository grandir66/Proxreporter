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
    """Legge input da stdin, con fallback su /dev/tty se stdin è chiuso (pipe)."""
    try:
        return input(prompt_text)
    except EOFError:
        # Se siamo in pipe (wget | bash), stdin è chiuso. Usiamo tty.
        try:
            with open("/dev/tty", "r") as tty:
                print(prompt_text, end="", flush=True)
                return tty.readline().rstrip("\n")
        except OSError:
            print("\n✗ Errore: impossibile leggere input (stdin chiuso e tty non disponibile).")
            print("  Esegui lo script interattivamente o senza pipe.")
            sys.exit(1)

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

    files_to_copy = ["proxmox_core.py", "proxmox_report.py", "update_scripts.py", "html_generator.py", "email_sender.py"]
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


def setup_v2(script_path: str) -> Tuple[str, str]:
    """
    Setup V2: Richiede password SFTP, usa default per resto, genera config.json.
    """
    print("\n=== SETUP PROXREPORTER V2 ===\n")
    
    default_output = str(Path("/var/log/proxreporter"))
    output_dir = prompt("Directory output dei report", default=default_output)

    codcli = prompt("Codice cliente (codcli)")
    nomecliente = prompt("Nome cliente (nomecliente)")
    
    # SFTP Configuration
    print("\n[Configurazione SFTP]")
    print(f"  Host: sftp.domarc.it (default)")
    print(f"  Utente: proxmox (nascosto)")
    
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

    # Email Config
    smtp_conf = {"enabled": False}
    if prompt_yes_no("\nConfigurare invio report via email?"):
        smtp_host = prompt("SMTP Host", default="smtp.gmail.com")
        smtp_port = prompt("SMTP Port", default="587")
        smtp_user = prompt("SMTP Username")
        smtp_password = prompt_password("SMTP Password", required=True)
        smtp_sender = prompt("Email Mittente", default=smtp_user)
        smtp_recipients = prompt("Destinatari (separati da virgola)")
        
        smtp_conf = {
            "enabled": True,
            "host": smtp_host,
            "port": int(smtp_port),
            "user": smtp_user,
            "password": smtp_password,
            "sender": smtp_sender,
            "recipients": smtp_recipients
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
    
    config_data = {
        "proxmox": {
            "enabled": True,
            "host": remote_conf.get("host", "localhost") + ":8006" if use_remote else "localhost", # approssimato
            "username": remote_conf.get("username", ""),
            "password": remote_conf.get("password", ""),
            "verify_ssl": False
        },
        "ssh": remote_conf if use_remote else {"enabled": False},
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
            "password": sftp_password,
            "base_path": "/home/proxmox/uploads"
        },
        "smtp": smtp_conf
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


def main() -> None:
    ensure_dependencies()

    print("=" * 70)
    print("PROXMOX REPORTER - SETUP & CONFIGURAZIONE")
    print("=" * 70)

    # Scelta modalità setup
    # Scelta modalità setup
    print("\nModalità setup disponibili:\n")
    print("  1. SETUP PROXREPORTER V2 (Standard)")
    print("     → Configura automaticamente host sftp e utente")
    print("     → Richiede password SFTP")
    print("     → Genera config.json sicuro")
    print()
    
    while True:
        choice = robust_input("Seleziona modalità [1] (default 1): ").strip()
        if not choice:
            choice = "1"
        if choice in ("1"):
            break
        print("Scelta non valida, usa 1.")
    
    use_default = False # Disabilitiamo il default legacy, usiamo setup_v2 come standard

    # Deploy scripts
    default_install_dir = Path("/opt/proxreport")
    if use_default:
        install_dir = default_install_dir
        print(f"\n→ Installazione script in: {install_dir}")
    else:
        install_dir_input = prompt(
            "Directory installazione script",
            default=str(default_install_dir),
        )
        install_dir = Path(install_dir_input).expanduser().resolve()
    
    script_path = str(deploy_scripts(install_dir))

    # Genera comando cron
    # Usiamo sempre setup_v2 (che sostituisce logicamente il custom/default precedente)
    command, custom_output_dir = setup_v2(script_path)
    
    # Crea directory di output e log SUBITO dopo aver definito il path
    # (necessario per il redirect del cron e per l'esecuzione immediata)
    output_dir = Path(custom_output_dir) if custom_output_dir else Path("/var/log/proxreporter")
    
    print(f"\n→ Creazione directory output...")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "csv").mkdir(exist_ok=True)
        (output_dir / "backup").mkdir(exist_ok=True)
        output_dir.chmod(0o755)
        (output_dir / "csv").chmod(0o755)
        (output_dir / "backup").chmod(0o755)
        print(f"  ✓ {output_dir}")
        print(f"  ✓ {output_dir / 'csv'}")
        print(f"  ✓ {output_dir / 'backup'}")
    except PermissionError as e:
        print(f"  ✗ Permessi insufficienti: {e}")
        if os.geteuid() != 0:
            print("\n✗ Errore: il setup richiede privilegi root per creare directory in /var/log/")
            print("  Esegui: sudo python3 setup.py")
            sys.exit(1)
    except Exception as e:
        print(f"  ⚠ Errore durante creazione directory: {e}")
        print("  Le directory verranno create automaticamente al primo avvio dello script.")

    # Mostra e conferma
    print("\n" + "=" * 70)
    print("CRON JOB GENERATO")
    print("=" * 70)
    print(f"\n{command}\n")
    print("=" * 70)

    if not prompt_yes_no("\nInstallare questo job nel crontab?", default=True):
        print("\n✋ Setup annullato. Per installare manualmente:")
        print(f"   crontab -e")
        print(f"   # Aggiungi la riga mostrata sopra")
        return

    # Installa cron job
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        cron_file = existing.stdout if existing.returncode == 0 else ""
        if cron_file and not cron_file.endswith("\n"):
            cron_file += "\n"
        cron_file += command + "\n"
        subprocess.run(["crontab", "-"], input=cron_file, text=True, check=True)
        print("\n✓ Job cron installato correttamente!")
        print("\n→ Verifica con: crontab -l")
        print("→ Test manuale: " + script_path + " --local --codcli TEST --nomecliente TEST")
    except subprocess.CalledProcessError as exc:
        print(f"\n✗ Errore durante l'installazione del crontab: {exc}")
        print("\nPer installare manualmente, esegui:")
        print("  crontab -e")
        print(f"  # Aggiungi: {command}")


if __name__ == "__main__":
    main()

