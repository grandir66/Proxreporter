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


def download_remote_config(config: Dict[str, Any], install_dir: Path) -> Optional[Dict[str, Any]]:
    """
    Scarica il file di configurazione remota dal server SFTP.
    
    Args:
        config: Configurazione locale (per ottenere credenziali SFTP)
        install_dir: Directory di installazione per il cache locale
    
    Returns:
        Dict con la configurazione remota o None se non disponibile
    """
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


def merge_remote_defaults(local_config: Dict[str, Any], remote_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Unisce la configurazione remota con quella locale.
    I valori locali hanno precedenza su quelli remoti.
    
    Args:
        local_config: Configurazione locale
        remote_config: Configurazione remota (defaults)
    
    Returns:
        Configurazione unita
    """
    if not remote_config:
        return local_config
    
    merged = local_config.copy()
    
    # Sezione Syslog
    if "syslog" in remote_config:
        local_syslog = merged.get("syslog", {})
        remote_syslog = remote_config["syslog"]
        
        # Se syslog non è configurato localmente o è vuoto, usa i defaults remoti
        if not local_syslog.get("host"):
            merged["syslog"] = {
                "enabled": local_syslog.get("enabled", remote_syslog.get("enabled", True)),
                "host": remote_syslog.get("host", ""),
                "port": remote_syslog.get("port", 514),
                "protocol": remote_syslog.get("protocol", "tcp"),
                "facility": remote_syslog.get("facility", 16),
                "app_name": local_syslog.get("app_name", remote_syslog.get("app_name", "proxreporter"))
            }
            logger.debug(f"Syslog configurato da defaults remoti: {merged['syslog']['host']}:{merged['syslog']['port']}")
    
    # Sezione SMTP - applica defaults remoti per campi mancanti
    if "smtp" in remote_config:
        local_smtp = merged.get("smtp", {})
        remote_smtp = remote_config["smtp"]
        
        # Campi da copiare dai defaults remoti se non presenti localmente
        smtp_fields = ["host", "port", "user", "password", "sender", "recipients", "use_tls", "use_ssl"]
        
        for key in smtp_fields:
            # Copia se il campo non esiste o è vuoto localmente
            if key not in local_smtp or not local_smtp.get(key):
                if key in remote_smtp and remote_smtp.get(key):
                    local_smtp[key] = remote_smtp[key]
        
        # Se host è configurato (da remoto o locale), abilita SMTP automaticamente
        if local_smtp.get("host") and local_smtp.get("recipients"):
            local_smtp["enabled"] = True
            logger.debug(f"SMTP abilitato automaticamente: {local_smtp.get('host')}")
        
        merged["smtp"] = local_smtp
    
    # Sezione Alerts (defaults)
    if "alerts" in remote_config:
        local_alerts = merged.get("alerts", {})
        remote_alerts = remote_config["alerts"]
        
        # Merge intelligente: mantieni valori locali, aggiungi defaults remoti
        for key, value in remote_alerts.items():
            if key not in local_alerts:
                local_alerts[key] = value
        merged["alerts"] = local_alerts
    
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
