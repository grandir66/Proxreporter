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
from pathlib import Path
from typing import List, Optional, Tuple


def prompt(label: str, default: Optional[str] = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            value = default
        if value or not required:
            return value
        print("Valore obbligatorio, riprova.")


def prompt_password(label: str, default: Optional[str] = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = getpass.getpass(f"{label}{suffix}: ")
        if not value and default is not None:
            value = default
        if value or not required:
            return value
        print("Valore obbligatorio, riprova.")


def prompt_yes_no(label: str, default: bool = False) -> bool:
    mapping = {"y": True, "yes": True, "n": False, "no": False}
    default_char = "y" if default else "n"
    while True:
        choice = input(f"{label} [y/n] (default {default_char}): ").strip().lower()
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
    print("  1) Ogni giorno alle 02:00")
    print("  2) Ogni ora (minuto 0)")
    print("  3) Personalizzata (inserisci espressione cron)")
    choice = prompt("Scelta", default="1")
    if choice == "1":
        return "0 2 * * *"
    if choice == "2":
        return "0 * * * *"
    print("Inserisci espressione cron (es. 30 3 * * 1-5):")
    return prompt("Espressione cron personalizzata")


def deploy_scripts(target_dir: Path) -> Path:
    source_dir = Path(__file__).resolve().parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(f"✗ Permessi insufficienti per creare {target_dir}. Esegui lo script come root.")
        raise SystemExit(1)

    files_to_copy = ["proxmox_core.py", "proxmox_report.py", "update_scripts.py"]
    for filename in files_to_copy:
        src = source_dir / filename
        if not src.exists():
            print(f"⚠ File sorgente mancante: {src}")
            continue
        dst = target_dir / filename
        shutil.copy2(src, dst)
        if dst.suffix == ".py":
            dst.chmod(0o755)
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


def setup_custom(script_path: str) -> Tuple[str, str]:
    """
    Configura setup personalizzato con domande complete.
    Ritorna (comando_cron, directory_output).
    """
    print("\n=== SETUP PERSONALIZZATO ===\n")
    
    default_output = str(Path("/var/log/proxreporter"))
    output_dir = prompt("Directory output dei report", default=default_output)

    codcli = prompt("Codice cliente (codcli)")
    nomecliente = prompt("Nome cliente (nomecliente)")

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

    use_remote = prompt_yes_no("Interrogare un host Proxmox remoto?")
    if use_remote:
        host = prompt("Host/IP remoto (Proxmox API/SSH)")
        username = prompt("Username API/SSH (es. root@pam)")
        password = prompt_password("Password API/SSH")
        ssh_port = prompt("Porta SSH", default="22")
        args += ["--host", host, "--username", username, "--password", password, "--ssh-port", ssh_port]
    else:
        args.append("--local")

    if prompt_yes_no("Vuoi sovrascrivere i parametri SFTP?"):
        sftp_host = prompt("SFTP host", default="sftp.domarc.it")
        sftp_port = prompt("SFTP port", default=str(SFTP_PORT_DEFAULT := 11122))
        sftp_user = prompt("SFTP username", default="proxmox")
        sftp_password = prompt_password("SFTP password", default="PX!h03f257m")
        sftp_base_path = prompt("SFTP base path", default="/home/proxmox/uploads")
        args += [
            "--sftp-host",
            sftp_host,
        ]
        if sftp_port:
            args += ["--sftp-port", sftp_port]
        if sftp_user:
            args += ["--sftp-user", sftp_user]
        if sftp_password:
            args += ["--sftp-password", sftp_password]
        if sftp_base_path:
            args += ["--sftp-base-path", sftp_base_path]

    if prompt_yes_no("Disabilitare l'upload SFTP?", default=False):
        args.append("--no-upload")
    
    if prompt_yes_no("Abilitare auto-aggiornamento script prima di ogni esecuzione?", default=True):
        args.append("--auto-update")

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
    print("\nModalità setup disponibili:\n")
    print("  1. SETUP RAPIDO (consigliato)")
    print("     → Configurazione standard con valori ottimali")
    print("     → Esecuzione locale, SFTP sftp.domarc.it:11122")
    print("     → Auto-update attivo, schedulazione ore 11:00")
    print()
    print("  2. SETUP PERSONALIZZATO")
    print("     → Configurazione completa con tutte le opzioni")
    print("     → Permette di modificare ogni parametro")
    print()
    
    while True:
        choice = input("Seleziona modalità [1/2] (default 1): ").strip()
        if not choice:
            choice = "1"
        if choice in ("1", "2"):
            break
        print("Scelta non valida, usa 1 o 2.")
    
    use_default = (choice == "1")

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
    if use_default:
        command = setup_default(script_path)
        custom_output_dir = None
    else:
        command, custom_output_dir = setup_custom(script_path)
    
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

