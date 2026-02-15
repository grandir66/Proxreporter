"""
Proxreporter - Remote Configuration Downloader

Scarica configurazioni centralizzate dal server SFTP per parametri sensibili
che non devono essere visibili nel repository Git.

Il file di configurazione remota contiene:
- Parametri Syslog (server, porta, protocollo)
- Parametri SMTP di default
- Altre configurazioni centralizzate

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("proxreporter")

# Configurazione per il download del file di configurazione remota
REMOTE_CONFIG_SFTP_HOST = "sftp.domarc.it"
REMOTE_CONFIG_SFTP_PORT = 11122
REMOTE_CONFIG_SFTP_USER = "proxmox"
REMOTE_CONFIG_PATH = "/home/proxmox/config/proxreporter_defaults.json"
LOCAL_CACHE_FILENAME = ".remote_defaults.json"


def _get_sftp_password(config: Dict[str, Any]) -> Optional[str]:
    """Recupera la password SFTP dal config locale"""
    sftp_config = config.get("sftp", {})
    password = sftp_config.get("password", "")
    
    # Decripta se necessario
    if password and password.startswith("ENC:"):
        try:
            from proxmox_report import load_config
            # Il config dovrebbe già avere le password decriptate
            pass
        except ImportError:
            pass
    
    return password if password and not password.startswith("ENC:") else None


def download_remote_config(config: Dict[str, Any], install_dir) -> Optional[Dict[str, Any]]:
    """
    Scarica il file di configurazione remota dal server SFTP.
    
    Args:
        config: Configurazione locale (per ottenere credenziali SFTP)
        install_dir: Directory di installazione per il cache locale (Path o str)
    
    Returns:
        Dict con la configurazione remota o None se non disponibile
    """
    # Assicura che install_dir sia un Path
    if isinstance(install_dir, str):
        install_dir = Path(install_dir)
    
    cache_file = install_dir / LOCAL_CACHE_FILENAME
    
    # Prova prima a usare il cache locale se esiste ed è recente (< 24h)
    if cache_file.exists():
        try:
            import time
            file_age = time.time() - cache_file.stat().st_mtime
            if file_age < 86400:  # 24 ore
                with open(cache_file, 'r') as f:
                    cached_config = json.load(f)
                    logger.debug(f"Configurazione remota caricata da cache: {cache_file}")
                    return cached_config
        except Exception as e:
            logger.debug(f"Cache non valido: {e}")
    
    # Scarica dal server SFTP
    try:
        import paramiko
    except ImportError:
        logger.debug("Paramiko non disponibile, impossibile scaricare config remota")
        return _load_cache(cache_file)
    
    password = _get_sftp_password(config)
    if not password:
        # Prova a usare la password SFTP dal config
        sftp_config = config.get("sftp", {})
        password = sftp_config.get("password", "")
        
        # Se ancora criptata, usa il cache
        if not password or password.startswith("ENC:"):
            logger.debug("Password SFTP non disponibile, uso cache locale")
            return _load_cache(cache_file)
    
    try:
        logger.debug(f"Download configurazione remota da {REMOTE_CONFIG_SFTP_HOST}...")
        
        transport = paramiko.Transport((REMOTE_CONFIG_SFTP_HOST, REMOTE_CONFIG_SFTP_PORT))
        transport.connect(username=REMOTE_CONFIG_SFTP_USER, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        
        # Scarica in un file temporaneo
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as tmp:
            tmp_path = tmp.name
        
        try:
            sftp.get(REMOTE_CONFIG_PATH, tmp_path)
            
            with open(tmp_path, 'r') as f:
                remote_config = json.load(f)
            
            # Salva nel cache locale
            with open(cache_file, 'w') as f:
                json.dump(remote_config, f, indent=2)
            os.chmod(cache_file, 0o600)
            
            logger.info(f"✓ Configurazione remota scaricata e salvata in cache")
            
            return remote_config
            
        finally:
            os.unlink(tmp_path)
            sftp.close()
            transport.close()
            
    except FileNotFoundError:
        logger.debug(f"File configurazione remota non trovato: {REMOTE_CONFIG_PATH}")
        return _load_cache(cache_file)
    except Exception as e:
        logger.debug(f"Impossibile scaricare config remota: {e}")
        return _load_cache(cache_file)


def _load_cache(cache_file: Path) -> Optional[Dict[str, Any]]:
    """Carica la configurazione dal cache locale se disponibile"""
    if cache_file.exists():
        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_merged_config(config: Dict[str, Any], config_file: Path) -> bool:
    """
    Salva la configurazione aggiornata nel file config.json locale.
    
    Args:
        config: Configurazione da salvare
        config_file: Percorso del file config.json
    
    Returns:
        True se salvato con successo
    """
    try:
        # Backup prima di sovrascrivere
        backup_file = config_file.with_suffix('.json.bak')
        if config_file.exists():
            import shutil
            shutil.copy2(config_file, backup_file)
        
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=4)
        
        # Mantieni permessi restrittivi
        os.chmod(config_file, 0o600)
        
        logger.info(f"✓ Configurazione locale aggiornata: {config_file}")
        return True
        
    except Exception as e:
        logger.error(f"✗ Errore salvataggio config: {e}")
        return False


def sync_remote_config(config: Dict[str, Any], config_file: Path) -> Dict[str, Any]:
    """
    Scarica la configurazione remota, fa il merge con quella locale,
    e salva le modifiche nel config.json locale.
    
    Questo permette di gestire la configurazione in modo centralizzato:
    le modifiche sul server SFTP vengono applicate a tutti i client.
    
    Args:
        config: Configurazione locale corrente
        config_file: Percorso del file config.json
    
    Returns:
        Configurazione aggiornata (merged)
    """
    if isinstance(config_file, str):
        config_file = Path(config_file)
    
    install_dir = config_file.parent
    
    # Scarica configurazione remota
    remote_config = download_remote_config(config, install_dir)
    
    if not remote_config:
        logger.debug("Nessuna configurazione remota disponibile")
        return config
    
    # Merge
    merged_config = merge_remote_defaults(config, remote_config)
    
    # Verifica se ci sono differenze da salvare
    config_changed = False
    
    # Controlla sezioni chiave per modifiche
    for section in ['syslog', 'smtp', 'alerts', 'hardware_monitoring', 'hardware_thresholds', 'pve_monitor']:
        if merged_config.get(section) != config.get(section):
            config_changed = True
            logger.debug(f"Sezione '{section}' aggiornata dalla config remota")
    
    # Salva se ci sono modifiche
    if config_changed:
        save_merged_config(merged_config, config_file)
    else:
        logger.debug("Nessuna modifica dalla configurazione remota")
    
    return merged_config


def merge_remote_defaults(local_config: Dict[str, Any], remote_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Unisce la configurazione remota con quella locale.
    
    IMPORTANTE: Il server remoto ha SEMPRE precedenza per i campi centralizzati.
    Questo permette di gestire la configurazione in modo centralizzato.
    
    Args:
        local_config: Configurazione locale
        remote_config: Configurazione remota (master)
    
    Returns:
        Configurazione unita
    """
    if not remote_config:
        return local_config
    
    merged = local_config.copy()
    
    # Sezione Syslog - il server remoto ha precedenza
    if "syslog" in remote_config:
        local_syslog = merged.get("syslog", {})
        remote_syslog = remote_config["syslog"]
        
        # Aggiorna tutti i campi dal server remoto (configurazione centralizzata)
        merged["syslog"] = {
            "enabled": remote_syslog.get("enabled", local_syslog.get("enabled", True)),
            "host": remote_syslog.get("host", local_syslog.get("host", "")),
            "port": remote_syslog.get("port", local_syslog.get("port", 514)),
            "protocol": remote_syslog.get("protocol", local_syslog.get("protocol", "tcp")),
            "facility": remote_syslog.get("facility", local_syslog.get("facility", 16)),
            "app_name": remote_syslog.get("app_name", local_syslog.get("app_name", "proxreporter")),
            "format": remote_syslog.get("format", local_syslog.get("format", "gelf"))
        }
        logger.info(f"Syslog sincronizzato: {merged['syslog']['host']}:{merged['syslog']['port']} ({merged['syslog']['format']})")
    
    # Sezione SMTP - il server remoto ha precedenza per i campi centralizzati
    if "smtp" in remote_config:
        local_smtp = merged.get("smtp", {})
        remote_smtp = remote_config["smtp"]
        
        # Campi gestiti centralmente (il server remoto ha precedenza)
        central_fields = ["host", "port", "user", "password", "sender", "recipients", "use_tls", "use_ssl"]
        
        for key in central_fields:
            remote_value = remote_smtp.get(key)
            if remote_value is not None:  # Usa il valore remoto se presente
                local_smtp[key] = remote_value
        
        # Abilita SMTP se configurato
        if local_smtp.get("host") and local_smtp.get("recipients"):
            local_smtp["enabled"] = True
        
        merged["smtp"] = local_smtp
        logger.info(f"SMTP sincronizzato: {local_smtp.get('host')} -> {local_smtp.get('recipients')}")
    
    # Sezione Alerts - il server remoto ha precedenza
    if "alerts" in remote_config:
        local_alerts = merged.get("alerts", {})
        remote_alerts = remote_config["alerts"]
        
        # Il server remoto ha precedenza per tutti i campi
        for key, value in remote_alerts.items():
            local_alerts[key] = value
        
        merged["alerts"] = local_alerts
        logger.debug("Alerts sincronizzati da configurazione remota")
    
    # Sezione Hardware Monitoring - il server remoto ha precedenza
    if "hardware_monitoring" in remote_config:
        merged["hardware_monitoring"] = remote_config["hardware_monitoring"]
    
    if "hardware_thresholds" in remote_config:
        merged["hardware_thresholds"] = remote_config["hardware_thresholds"]
    
    # Sezione PVE Monitor - il server remoto ha precedenza
    if "pve_monitor" in remote_config:
        local_pve = merged.get("pve_monitor", {})
        remote_pve = remote_config["pve_monitor"]
        
        # Il server remoto ha precedenza per tutti i campi
        for key, value in remote_pve.items():
            local_pve[key] = value
        
        merged["pve_monitor"] = local_pve
        logger.debug("PVE Monitor sincronizzato da configurazione remota")
    
    return merged


def get_graylog_config() -> Dict[str, Any]:
    """
    Restituisce la configurazione predefinita per Graylog/Syslog.
    Questi valori vengono sovrascritti dal file remoto se disponibile.
    """
    return {
        "enabled": True,
        "host": "",  # Sarà popolato dal file remoto
        "port": 514,
        "protocol": "tcp",
        "facility": 16,  # LOCAL0
        "app_name": "proxreporter"
    }
