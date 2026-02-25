#!/usr/bin/env python3
"""
Proxreporter - PVE Monitor Module

Monitora e invia a Syslog/Graylog:
- Stato del nodo (CPU, memoria, uptime)
- Stato degli storage di backup
- Risultati task di backup (vzdump)
- Job di backup schedulati
- Copertura backup (VM/CT senza backup)
- Stato servizi PVE

Integrato in Proxreporter - può essere abilitato/disabilitato via config.

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import json
import logging
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Version info
try:
    from version import __version__
except ImportError:
    __version__ = "unknown"

logger = logging.getLogger("proxreporter")


class PVESyslogSender:
    """Invia messaggi syslog RFC 5424 o GELF via TCP/UDP per PVE Monitor"""

    FACILITY_MAP = {
        "local0": 16, "local1": 17, "local2": 18, "local3": 19,
        "local4": 20, "local5": 21, "local6": 22, "local7": 23
    }

    def __init__(self, config: Dict[str, Any], client_info: Dict[str, Any]):
        """
        Inizializza il sender syslog usando la configurazione di Proxreporter.
        
        Args:
            config: Configurazione completa di Proxreporter (con sezione 'syslog')
            client_info: Info cliente (codcli, nomecliente)
        """
        syslog_config = config.get("syslog", {})
        pve_config = config.get("pve_monitor", {})
        
        self.enabled = syslog_config.get("enabled", False)
        self.server = syslog_config.get("host", "")
        # PVE Monitor usa una porta dedicata (default 4514), diversa da Proxreporter (default 8514)
        self.port = pve_config.get("syslog_port", 4514)
        self.protocol = syslog_config.get("protocol", "tcp").lower()
        self.facility = self.FACILITY_MAP.get(syslog_config.get("facility", "local0"), 16)
        # PVE Monitor usa SEMPRE formato json su porta 4514 (compatibilità con versione funzionante)
        # Il formato GELF viene inviato come copia separata alla porta 8514
        self.format = pve_config.get("syslog_format", "json").lower()
        self.app_name = "pve-monitor"  # App name specifico per PVE Monitor
        
        # Porta principale per copia GELF (8514)
        self.main_port = syslog_config.get("port", 8514)
        # Abilita invio copia anche a porta principale in formato GELF
        self.send_to_main = pve_config.get("send_to_main_syslog", True)
        
        # Client info per i messaggi
        self.client = {
            "code": client_info.get("codcli", ""),
            "name": client_info.get("nomecliente", ""),
            "site": client_info.get("site", "default")
        }
        
        self.hostname = socket.gethostname()

    def send(self, message_type: str, data: Dict, test_mode: bool = False) -> bool:
        """Invia messaggio syslog con payload JSON"""
        if not self.enabled and not test_mode:
            logger.debug(f"Syslog disabilitato, messaggio {message_type} non inviato")
            return False
        
        if not self.server:
            logger.warning("Server syslog non configurato")
            return False

        status = data.get("status", "success")
        severity = {"success": 6, "warning": 4, "failed": 3}.get(status, 6)
        priority = (self.facility * 8) + severity

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        payload = {
            "message_type": message_type,
            "version": __version__,
            "timestamp": timestamp,
            "client": self.client,
            "agent_hostname": self.hostname,
            **data
        }

        if self.format == "gelf":
            # Formato GELF per Graylog - struttura piatta
            syslog_msg = self._build_gelf_message(message_type, payload, severity)
        elif self.format == "json" or self.format == "raw":
            # Formato RFC 5424 con JSON payload - come usato dalla versione funzionante
            # Formato: <priority>1 timestamp hostname app_name pid message_type - {json}
            json_payload = json.dumps(payload, separators=(",", ":"), default=str)
            syslog_msg = f"<{priority}>1 {timestamp} {self.hostname} {self.app_name} {os.getpid()} {message_type} - {json_payload}"
        else:
            # Formato RFC 5424 standard (stesso di json)
            json_payload = json.dumps(payload, separators=(",", ":"), default=str)
            syslog_msg = f"<{priority}>1 {timestamp} {self.hostname} {self.app_name} {os.getpid()} {message_type} - {json_payload}"

        if test_mode:
            logger.info(f"\n=== SYSLOG MESSAGE ({len(syslog_msg)} bytes) ===\n{syslog_msg[:500]}...\n")
            return True

        try:
            # Invio alla porta PVE (4514) - formato JSON raw
            if self.protocol == "tcp":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((self.server, self.port))
                if isinstance(syslog_msg, str):
                    sock.sendall(syslog_msg.encode("utf-8") + b"\n")
                else:
                    sock.sendall(syslog_msg + b"\n")
                sock.close()
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                if isinstance(syslog_msg, str):
                    sock.sendto(syslog_msg.encode("utf-8"), (self.server, self.port))
                else:
                    sock.sendto(syslog_msg, (self.server, self.port))
                sock.close()
            
            logger.info(f"  ✓ PVE Syslog inviato: {message_type} -> porta {self.port}")
            
            # Invio copia anche alla porta principale (8514) in formato GELF
            if self.send_to_main and self.main_port != self.port:
                self._send_to_main_port(message_type, payload, severity)
            
            return True
        except Exception as e:
            logger.error(f"  ✗ Errore invio syslog PVE: {e}")
            return False
    
    def _send_to_main_port(self, message_type: str, payload: Dict, severity: int) -> bool:
        """Invia copia del messaggio alla porta principale (8514) in formato GELF"""
        try:
            gelf_msg = self._build_gelf_message(message_type, payload, severity)
            
            if self.protocol == "tcp":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((self.server, self.main_port))
                sock.sendall(gelf_msg)
                sock.close()
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(gelf_msg, (self.server, self.main_port))
                sock.close()
            
            logger.debug(f"  ✓ Copia GELF inviata a porta {self.main_port}")
            return True
        except Exception as e:
            logger.debug(f"  ⚠ Errore copia GELF a porta {self.main_port}: {e}")
            return False

    def _build_gelf_message(self, message_type: str, payload: Dict, severity: int) -> bytes:
        """Costruisce messaggio GELF con campi comuni standardizzati."""
        import time
        
        status = payload.get("status", "info")
        event = message_type.lower().replace("pve_", "pve.")
        
        gelf_msg = {
            "version": "1.1",
            "host": self.hostname,
            "short_message": f"{message_type}: {status}",
            "timestamp": time.time(),
            "level": severity,
            # Campi comuni standard (presenti in tutti i messaggi GELF)
            "_app": "proxreporter",
            "_module": "pve_monitor",
            "_app_version": __version__,
            "_event": event,
            "_message_type": message_type,
            "_client_code": self.client.get("code", ""),
            "_client_name": self.client.get("name", ""),
            "_hostname": self.hostname,
            "_status": status,
        }
        
        # Espandi campi specifici del payload (skip quelli già in campi comuni)
        skip_keys = {"message_type", "version", "timestamp", "client", "agent_hostname", "status"}
        filtered_payload = {k: v for k, v in payload.items() if k not in skip_keys}
        self._flatten_to_gelf(gelf_msg, filtered_payload, "")
        
        return (json.dumps(gelf_msg, default=str) + '\0').encode('utf-8')
    
    def _flatten_to_gelf(self, gelf_msg: Dict, data: Any, prefix: str) -> None:
        """
        Appiattisce ricorsivamente in campi GELF con prefisso _.
        Liste di dict vengono espanse: services -> _services_0_name, _services_0_state, ...
        storages -> _storages_0_name, _storages_0_type, _storages_0_used_percent, ...
        """
        if isinstance(data, dict):
            for key, value in data.items():
                new_prefix = f"{prefix}_{key}" if prefix else key
                if isinstance(value, dict):
                    self._flatten_to_gelf(gelf_msg, value, new_prefix)
                elif isinstance(value, list):
                    if len(value) > 0 and isinstance(value[0], dict):
                        gelf_msg[f"_{new_prefix}_count"] = len(value)
                        for i, item in enumerate(value[:25]):  # max 25 (services ~12, storages ~10)
                            if isinstance(item, dict):
                                self._flatten_to_gelf(gelf_msg, item, f"{new_prefix}_{i}")
                    else:
                        gelf_msg[f"_{new_prefix}"] = json.dumps(value, default=str)
                else:
                    gelf_msg[f"_{new_prefix}"] = value


def pvesh_get(path: str, **params) -> Any:
    """Esegue pvesh get e ritorna il risultato JSON"""
    cmd = ["pvesh", "get", path, "--output-format", "json"]
    for k, v in params.items():
        cmd.extend([f"--{k.replace('_', '-')}", str(v)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"pvesh error: {result.stderr.strip()}")
    return json.loads(result.stdout)


def get_node_name() -> str:
    """Rileva il nome del nodo PVE locale"""
    return socket.gethostname()


def get_pve_version() -> str:
    """Ottiene la versione di Proxmox VE"""
    try:
        result = subprocess.run(
            ["pveversion"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            match = re.match(r"pve-manager/([\d.]+)", result.stdout.strip())
            if match:
                return match.group(1)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def read_proc_uptime() -> float:
    """Legge uptime in ore da /proc/uptime"""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0]) / 3600
    except:
        return 0.0


def read_proc_loadavg() -> List[float]:
    """Legge load average da /proc/loadavg"""
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            return [float(parts[0]), float(parts[1]), float(parts[2])]
    except:
        return [0.0, 0.0, 0.0]


def read_proc_meminfo() -> Dict[str, int]:
    """Legge informazioni memoria da /proc/meminfo (valori in bytes)"""
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("MemTotal:", "MemAvailable:")):
                    key, value = line.split(":")
                    mem[key.strip()] = int(value.strip().split()[0]) * 1024
    except:
        pass
    return mem


def read_proc_cpu() -> float:
    """Legge utilizzo CPU da /proc/stat (media su 0.5 secondi)"""
    import time
    
    def read_stat():
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
                idle = int(parts[4]) + int(parts[5])
                total = sum(int(p) for p in parts[1:])
                return idle, total
        except:
            return 0, 1

    idle1, total1 = read_stat()
    time.sleep(0.5)
    idle2, total2 = read_stat()

    idle_delta = idle2 - idle1
    total_delta = total2 - total1
    if total_delta == 0:
        return 0.0
    return round((1 - idle_delta / total_delta) * 100, 1)


# Cache globali per evitare chiamate API ripetute
_storage_content_cache: Dict[str, List] = {}
_vzdump_tasks_cache: Dict[str, List] = {}
_cluster_resources_cache: Optional[List] = None


def clear_caches():
    """Resetta tutte le cache globali"""
    global _storage_content_cache, _vzdump_tasks_cache, _cluster_resources_cache
    _storage_content_cache = {}
    _vzdump_tasks_cache = {}
    _cluster_resources_cache = None


def get_storage_content_cached(node: str, storage: str) -> List:
    """Ottiene il contenuto dello storage con cache"""
    cache_key = f"{node}:{storage}"
    if cache_key not in _storage_content_cache:
        try:
            _storage_content_cache[cache_key] = pvesh_get(f"/nodes/{node}/storage/{storage}/content")
        except:
            _storage_content_cache[cache_key] = []
    return _storage_content_cache[cache_key]


def get_cluster_resources_cached() -> List:
    """Ottiene le risorse del cluster con cache"""
    global _cluster_resources_cache
    if _cluster_resources_cache is None:
        try:
            _cluster_resources_cache = pvesh_get("/cluster/resources")
        except:
            _cluster_resources_cache = []
    return _cluster_resources_cache


def get_latest_backup_info(node: str, storage: str, vmid: str, vm_type: str) -> Dict:
    """Ottiene informazioni sull'ultimo backup di una VM/CT dal repository PBS (con cache)"""
    try:
        content = get_storage_content_cached(node, storage)
        
        vm_backups = []
        for backup in content:
            if backup.get("vmid") == int(vmid) and backup.get("content") == "backup":
                if vm_type == "qemu" and backup.get("subtype") == "qemu":
                    vm_backups.append(backup)
                elif vm_type == "lxc" and backup.get("subtype") == "lxc":
                    vm_backups.append(backup)
        
        if vm_backups:
            latest = sorted(vm_backups, key=lambda x: x.get("ctime", 0), reverse=True)[0]
            backup_time = latest.get("ctime", 0)
            
            verification = latest.get("verification", {})
            if isinstance(verification, dict):
                verify_state = verification.get("state", "")
            else:
                verify_state = ""
            
            if verify_state == "ok" or backup_time > 0:
                backup_status = "success"
            elif verify_state == "failed":
                backup_status = "failed"
            else:
                backup_status = "unknown"
            
            return {
                "backup_status": backup_status,
                "backup_date": datetime.fromtimestamp(backup_time, tz=timezone.utc).isoformat() if backup_time else None,
                "backup_size_bytes": latest.get("size", 0),
                "backup_size_gb": round(latest.get("size", 0) / (1024**3), 2) if latest.get("size", 0) > 0 else None,
                "backup_volid": latest.get("volid", ""),
                "verification_state": verify_state if verify_state else None
            }
    except Exception as e:
        logger.debug(f"Errore info backup VM {vmid} da storage {storage}: {e}")
    
    return {}


class PVEMonitor:
    """
    Monitora lo stato di Proxmox VE e invia alert via Syslog.
    Integrato in Proxreporter.
    """

    STATE_FILE = "/var/lib/proxreporter/pve_monitor_state.json"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        pve_config = config.get("pve_monitor", {})
        
        self.enabled = pve_config.get("enabled", False)
        self.lookback_hours = pve_config.get("lookback_hours", 24)
        self.check_node_status = pve_config.get("check_node_status", True)
        self.check_storage_status = pve_config.get("check_storage_status", True)
        self.check_backup_results = pve_config.get("check_backup_results", True)
        self.check_backup_jobs = pve_config.get("check_backup_jobs", True)
        self.send_backup_result_on_success = pve_config.get("send_backup_result_on_success", False)
        self.check_backup_coverage = pve_config.get("check_backup_coverage", True)
        self.check_service_status = pve_config.get("check_service_status", True)
        # Frequenza check backup in ore (default: ogni 6 ore)
        self.backup_check_interval_hours = pve_config.get("backup_check_interval_hours", 6)
        
        client_section = config.get("client", {})
        self.client_info = {
            "codcli": config.get("codcli") or client_section.get("codcli", ""),
            "nomecliente": config.get("nomecliente") or client_section.get("nomecliente", ""),
            "site": pve_config.get("site") or client_section.get("site", "default")
        }
        
        self.node = get_node_name()
        self.syslog: Optional[PVESyslogSender] = None

    def _load_state(self) -> Dict:
        """Carica stato persistente (ultimo run backup check, ecc.)"""
        try:
            with open(self.STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}

    def _save_state(self, state: Dict):
        """Salva stato persistente"""
        try:
            state_dir = os.path.dirname(self.STATE_FILE)
            os.makedirs(state_dir, exist_ok=True)
            with open(self.STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.debug(f"Impossibile salvare stato: {e}")

    def _should_run_backup_checks(self) -> bool:
        """Verifica se è il momento di eseguire i check backup (pesanti)"""
        state = self._load_state()
        last_backup_check = state.get("last_backup_check", 0)
        elapsed_hours = (datetime.now(timezone.utc).timestamp() - last_backup_check) / 3600
        return elapsed_hours >= self.backup_check_interval_hours

    def run(self, test_mode: bool = False) -> Dict[str, Any]:
        """Esegue il monitoraggio PVE completo."""
        if not self.enabled and not test_mode:
            logger.info("  ℹ PVE Monitor disabilitato")
            return {"enabled": False}
        
        logger.info("→ Avvio PVE Monitor...")
        logger.info(f"  Nodo: {self.node}")
        logger.info(f"  Lookback: {self.lookback_hours}h")
        
        run_backup_checks = test_mode or self._should_run_backup_checks()
        if not run_backup_checks:
            logger.info(f"  ℹ Check backup saltati (intervallo: ogni {self.backup_check_interval_hours}h)")
        
        self.syslog = PVESyslogSender(self.config, self.client_info)
        
        # Resetta cache
        clear_caches()
        
        results = {
            "enabled": True,
            "node": self.node,
            "pve_version": get_pve_version(),
            "checks": {}
        }
        
        # Check leggeri - eseguiti sempre (ogni ora)
        if self.check_node_status:
            results["checks"]["node_status"] = self._collect_node_status(test_mode)
        
        if self.check_storage_status:
            results["checks"]["storage_status"] = self._collect_storage_status(test_mode)
        
        if self.check_service_status:
            results["checks"]["service_status"] = self._collect_service_status(test_mode)
        
        # Check pesanti (backup) - eseguiti solo a intervalli configurabili
        if run_backup_checks:
            if self.check_backup_results:
                results["checks"]["backup_results"] = self._collect_backup_results(test_mode)
            
            if self.check_backup_jobs:
                results["checks"]["backup_jobs"] = self._collect_backup_jobs(test_mode)
            
            if self.check_backup_coverage:
                results["checks"]["backup_coverage"] = self._collect_backup_coverage(test_mode)
            
            # Aggiorna timestamp ultimo check backup
            state = self._load_state()
            state["last_backup_check"] = datetime.now(timezone.utc).timestamp()
            self._save_state(state)
        else:
            results["checks"]["backup_skipped"] = f"Next check in {self.backup_check_interval_hours}h interval"
        
        logger.info("  ✓ PVE Monitor completato")
        
        # Invia riepilogo solo se ci sono check backup (evita summary vuoti ogni ora)
        if run_backup_checks:
            self._send_summary_to_main_syslog(results, test_mode)
        
        return results
    
    def _send_summary_to_main_syslog(self, results: Dict, test_mode: bool) -> bool:
        """
        Invia un riepilogo dell'esecuzione PVE Monitor sulla porta 8514 (syslog principale).
        Questo permette di avere tutti i messaggi di sistema su un unico stream.
        """
        import time
        
        syslog_config = self.config.get("syslog", {})
        if not syslog_config.get("enabled", False):
            return False
        
        host = syslog_config.get("host", "")
        port = syslog_config.get("port", 8514)  # Porta principale Proxreporter
        
        if not host:
            return False
        
        # Costruisci riepilogo
        checks = results.get("checks", {})
        
        # Conta problemi
        backup_coverage = checks.get("backup_coverage", {})
        not_covered = backup_coverage.get("not_covered", 0) if isinstance(backup_coverage, dict) else 0
        
        service_status = checks.get("service_status", {})
        services_failed = service_status.get("failed", 0) if isinstance(service_status, dict) else 0
        
        backup_results = checks.get("backup_results", {})
        backup_jobs = backup_results.get("jobs", 0) if isinstance(backup_results, dict) else 0
        backup_tasks = backup_results.get("tasks", 0) if isinstance(backup_results, dict) else 0
        
        # Conta backup falliti (status != success nei risultati)
        backup_failed = 0
        node_status = checks.get("node_status", {})
        
        # Determina severità - solo problemi reali, non VM escluse dal backup
        # Le VM senza backup schedulato sono informative (potrebbero essere escluse intenzionalmente)
        if services_failed > 0:
            status = "failed"
            level = 3  # ERROR
        elif backup_failed > 0:
            status = "warning"
            level = 4  # WARNING
        else:
            status = "success"
            level = 6  # INFO
        
        gelf_msg = {
            "version": "1.1",
            "host": self.node,
            "short_message": f"PVE_MONITOR_SUMMARY: {status} - {self.node}",
            "full_message": f"PVE Monitor completato su {self.node}: {backup_tasks} task backup, {not_covered} VM senza backup, {services_failed} servizi failed",
            "timestamp": time.time(),
            "level": level,
            # Campi comuni standard
            "_app": "proxreporter",
            "_module": "pve_monitor",
            "_app_version": __version__,
            "_event": "pve.monitor.summary",
            "_message_type": "PVE_MONITOR_SUMMARY",
            "_client_code": self.client_info.get("codcli", ""),
            "_client_name": self.client_info.get("nomecliente", ""),
            "_hostname": self.node,
            "_status": status,
            # Campi specifici
            "_pve_version": results.get("pve_version", ""),
            "_backup_tasks": backup_tasks,
            "_backup_jobs": backup_jobs,
            "_vms_not_covered": not_covered,
            "_services_failed": services_failed,
        }
        
        # Aggiungi info nodo se disponibile
        node_status = checks.get("node_status", {})
        if isinstance(node_status, dict) and "data" in node_status:
            node_data = node_status["data"]
            gelf_msg["_cpu_percent"] = node_data.get("cpu_percent", 0)
            gelf_msg["_memory_used_percent"] = node_data.get("memory_used_percent", 0)
            gelf_msg["_uptime_hours"] = node_data.get("uptime_hours", 0)
        
        # Aggiungi info storage
        storage_status = checks.get("storage_status", {})
        if isinstance(storage_status, dict):
            storages = storage_status.get("storages", [])
            gelf_msg["_storage_count"] = len(storages)
            
            # Aggiungi dettagli per ogni storage (max 10 per evitare messaggi troppo grandi)
            for i, stor in enumerate(storages[:10]):
                prefix = f"_storage_{i}"
                gelf_msg[f"{prefix}_name"] = stor.get("name", "")
                gelf_msg[f"{prefix}_type"] = stor.get("type", "")
                gelf_msg[f"{prefix}_total_gb"] = stor.get("total_gb", 0)
                gelf_msg[f"{prefix}_used_gb"] = stor.get("used_gb", 0)
                gelf_msg[f"{prefix}_free_gb"] = stor.get("free_gb", 0)
                gelf_msg[f"{prefix}_used_percent"] = stor.get("used_percent", 0)
                gelf_msg[f"{prefix}_status"] = stor.get("status", "")
            
            # Segnala storage con problemi
            storage_warning = sum(1 for s in storages if s.get("status") == "warning")
            storage_critical = sum(1 for s in storages if s.get("status") == "failed")
            gelf_msg["_storage_warning_count"] = storage_warning
            gelf_msg["_storage_critical_count"] = storage_critical
            
            # Aggiorna status se storage ha problemi
            if storage_critical > 0 and status == "success":
                status = "failed"
                level = 3
                gelf_msg["_status"] = status
                gelf_msg["level"] = level
                gelf_msg["short_message"] = f"PVE_MONITOR_SUMMARY: {status} - {self.node}"
            elif storage_warning > 0 and status == "success":
                status = "warning"
                level = 4
                gelf_msg["_status"] = status
                gelf_msg["level"] = level
                gelf_msg["short_message"] = f"PVE_MONITOR_SUMMARY: {status} - {self.node}"
        
        message = (json.dumps(gelf_msg) + '\0').encode('utf-8')
        
        if test_mode:
            logger.info(f"\n=== SUMMARY to 8514 ({len(message)} bytes) ===\n{message[:500]}...\n")
            return True
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            sock.sendall(message)
            sock.close()
            logger.info(f"  ✓ Riepilogo PVE inviato a {host}:{port}")
            return True
        except Exception as e:
            logger.debug(f"Errore invio riepilogo PVE: {e}")
            return False

    def _collect_node_status(self, test_mode: bool) -> Dict:
        """Raccoglie e invia stato del nodo PVE"""
        logger.info("  → Raccolta stato nodo...")
        
        try:
            meminfo = read_proc_meminfo()
            mem_total = meminfo.get("MemTotal", 0)
            mem_available = meminfo.get("MemAvailable", 0)
            mem_used = mem_total - mem_available
            mem_used_pct = round((mem_used / max(mem_total, 1)) * 100, 1)

            data = {
                "status": "success",
                "server_name": self.node,
                "pve_version": get_pve_version(),
                "uptime_hours": round(read_proc_uptime(), 1),
                "cpu_percent": read_proc_cpu(),
                "memory_total_bytes": mem_total,
                "memory_used_bytes": mem_used,
                "memory_total_gb": round(mem_total / (1024 ** 3), 2),
                "memory_used_percent": mem_used_pct,
                "load_average": read_proc_loadavg(),
            }

            if self.syslog:
                self.syslog.send("PVE_NODE_STATUS", data, test_mode)
            
            return {"sent": True, "data": data}
        except Exception as e:
            logger.error(f"    ✗ Errore raccolta stato nodo: {e}")
            return {"sent": False, "error": str(e)}

    def _collect_storage_status(self, test_mode: bool) -> Dict:
        """Raccoglie e invia stato degli storage di backup"""
        logger.info("  → Raccolta stato storage backup...")
        
        try:
            resources = pvesh_get("/cluster/resources", type="storage")
            
            seen = set()
            backup_storages = []
            
            for res in resources:
                storage_name = res.get("storage", "")
                if storage_name in seen:
                    continue
                
                content = res.get("content", "")
                if "backup" not in content:
                    continue
                
                seen.add(storage_name)
                
                total = res.get("maxdisk", 0)
                used = res.get("disk", 0)
                avail = total - used if total > 0 else 0
                used_percent = round((used / total * 100), 1) if total > 0 else 0
                
                status = "success"
                if used_percent > 95:
                    status = "failed"
                elif used_percent > 90:
                    status = "warning"
                
                backup_storages.append({
                    "name": storage_name,
                    "type": res.get("plugintype", "unknown"),
                    "total_gb": round(total / (1024 ** 3), 2) if total > 0 else None,
                    "used_gb": round(used / (1024 ** 3), 2) if used > 0 else None,
                    "free_gb": round(avail / (1024 ** 3), 2) if avail > 0 else None,
                    "used_percent": used_percent,
                    "status": status
                })
            
            if backup_storages:
                overall_status = "success"
                for s in backup_storages:
                    if s["status"] == "failed":
                        overall_status = "failed"
                        break
                    elif s["status"] == "warning":
                        overall_status = "warning"
                
                data = {
                    "status": overall_status,
                    "storage_count": len(backup_storages),
                    "storages": backup_storages
                }
                
                if self.syslog:
                    self.syslog.send("PVE_STORAGE_STATUS", data, test_mode)
                
                return {"sent": True, "count": len(backup_storages), "storages": backup_storages, "overall_status": overall_status}
            
            return {"sent": False, "count": 0, "storages": []}
        except Exception as e:
            logger.error(f"    ✗ Errore raccolta storage: {e}")
            return {"sent": False, "error": str(e)}

    def _collect_backup_results(self, test_mode: bool) -> Dict:
        """Raccoglie e invia risultati task vzdump"""
        logger.info("  → Raccolta risultati backup vzdump...")
        
        try:
            since = int((datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)).timestamp())
            tasks = pvesh_get(f"/nodes/{self.node}/tasks", typefilter="vzdump", since=str(since),
                              limit="500", source="all")

            completed = [t for t in tasks if t.get("status") != "running"]
            logger.info(f"    Trovati {len(completed)} task vzdump completati")

            # Pre-carica nomi VM dal cluster (una sola chiamata API) per evitare N chiamate individuali
            vm_names = {}
            try:
                for res in get_cluster_resources_cached():
                    if res.get("type") in ["qemu", "lxc"]:
                        vm_names[str(res.get("vmid", ""))] = {
                            "name": res.get("name", ""),
                            "type": res.get("type", "unknown")
                        }
            except:
                pass

            jobs_dict = {}
            
            for task in completed:
                starttime = task.get("starttime", 0)
                endtime = task.get("endtime", 0)
                duration = endtime - starttime if endtime and starttime else 0
                vmid = task.get("id", "")
                upid = task.get("upid", "")
                user = task.get("user", "")
                exitstatus = task.get("exitstatus", "")
                
                if exitstatus == "OK":
                    status = "success"
                elif "error" in str(exitstatus).lower():
                    status = "failed"
                else:
                    status = "warning"
                
                vm_info = vm_names.get(str(vmid), {})
                vm_name = vm_info.get("name", f"VM-{vmid}")
                vm_type = vm_info.get("type", "unknown")
                
                time_key = int(starttime / 300) * 300
                job_key = f"{user}_{time_key}"
                
                if job_key not in jobs_dict:
                    jobs_dict[job_key] = {
                        "user": user,
                        "start_time": starttime,
                        "end_time": endtime,
                        "vms": [],
                        "task_ids": []
                    }
                
                if endtime > jobs_dict[job_key]["end_time"]:
                    jobs_dict[job_key]["end_time"] = endtime
                
                jobs_dict[job_key]["vms"].append({
                    "vmid": vmid,
                    "name": vm_name,
                    "type": vm_type,
                    "status": status,
                    "exit_status": exitstatus,
                    "start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                    "end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                    "duration_seconds": duration,
                    "duration_minutes": round(duration / 60, 1),
                    "task_id": upid,
                })
                jobs_dict[job_key]["task_ids"].append(upid)
            
            jobs_sent = 0
            success_jobs = []
            
            for job_key, job_data in jobs_dict.items():
                vms = job_data["vms"]
                start_time = job_data["start_time"]
                end_time = job_data["end_time"]
                job_duration = end_time - start_time if end_time and start_time else 0
                
                vms_success = sum(1 for v in vms if v["status"] == "success")
                vms_warning = sum(1 for v in vms if v["status"] == "warning")
                vms_failed = sum(1 for v in vms if v["status"] == "failed")
                
                if vms_failed > 0:
                    job_status = "failed"
                elif vms_warning > 0:
                    job_status = "warning"
                else:
                    job_status = "success"
                
                if job_status in ("failed", "warning") or self.send_backup_result_on_success or test_mode:
                    data = {
                        "status": job_status,
                        "job_start_time": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat() if start_time else None,
                        "job_end_time": datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat() if end_time else None,
                        "job_duration_seconds": job_duration,
                        "job_duration_minutes": round(job_duration / 60, 1),
                        "user": job_data["user"],
                        "vm_count": len(vms),
                        "vms_success": vms_success,
                        "vms_warning": vms_warning,
                        "vms_failed": vms_failed,
                        "vms": vms,
                        "task_ids": job_data["task_ids"]
                    }
                    if self.syslog:
                        self.syslog.send("PVE_BACKUP_RESULT", data, test_mode)
                        jobs_sent += 1
                elif job_status == "success":
                    success_jobs.append({
                        "user": job_data["user"],
                        "vm_count": len(vms),
                        "start_time": start_time,
                        "end_time": end_time,
                    })
            
            # Un messaggio accumulativo per tutti i successi
            if success_jobs and self.syslog:
                total_vms = sum(j["vm_count"] for j in success_jobs)
                first_start = min(j["start_time"] for j in success_jobs)
                last_end = max(j["end_time"] for j in success_jobs)
                users = list({j["user"] for j in success_jobs})
                summary_data = {
                    "status": "success",
                    "job_count": len(success_jobs),
                    "vm_count": total_vms,
                    "first_start": datetime.fromtimestamp(first_start, tz=timezone.utc).isoformat() if first_start else None,
                    "last_end": datetime.fromtimestamp(last_end, tz=timezone.utc).isoformat() if last_end else None,
                    "users": users,
                }
                self.syslog.send("PVE_BACKUP_RESULT_SUMMARY", summary_data, test_mode)
                jobs_sent += 1
            
            logger.info(f"    Processati {len(jobs_dict)} job di backup ({jobs_sent} messaggi inviati)")
            return {"sent": True, "jobs": len(jobs_dict), "tasks": len(completed)}
        except Exception as e:
            logger.error(f"    ✗ Errore raccolta task backup: {e}")
            return {"sent": False, "error": str(e)}

    def _collect_backup_jobs(self, test_mode: bool) -> Dict:
        """Raccoglie informazioni sui job di backup schedulati con dettaglio VM"""
        logger.info("  → Raccolta job di backup schedulati...")
        
        clear_caches()
        
        try:
            cluster_jobs = []
            try:
                cluster_jobs = pvesh_get("/cluster/backup")
                logger.info(f"    Trovati {len(cluster_jobs)} job via API")
            except:
                pass
            
            if not cluster_jobs or all(not job.get("vms") and not job.get("all") for job in cluster_jobs):
                try:
                    with open("/etc/pve/jobs.cfg", "r") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            if line.startswith("backup:"):
                                parts = line.split(":")
                                if len(parts) >= 2:
                                    job_id = parts[1]
                                    job_data = {"id": job_id}
                                    for part in parts[2:]:
                                        if "=" in part:
                                            key, value = part.split("=", 1)
                                            job_data[key] = value
                                    cluster_jobs.append(job_data)
                    logger.info(f"    Letti {len(cluster_jobs)} job da /etc/pve/jobs.cfg")
                except:
                    pass
            
            backup_jobs = []
            
            for job in cluster_jobs:
                job_id = job.get("id", "")
                if not job_id:
                    continue
                
                try:
                    try:
                        job_details = pvesh_get(f"/cluster/backup/{job_id}")
                        job.update(job_details)
                    except:
                        pass
                    
                    vms_value = job.get("vms") or job.get("vmid", "")
                    all_flag = job.get("all", False) or job.get("all") == 1
                    exclude_value = job.get("exclude", "")
                    
                    vm_ids = []
                    
                    if all_flag:
                        all_vms = []
                        nodes_str = job.get("nodes", "")
                        nodes_list = [n.strip() for n in nodes_str.split(",") if n.strip()] if nodes_str else []
                        
                        if not nodes_list:
                            try:
                                resources = get_cluster_resources_cached()
                                for res in resources:
                                    if res.get("type") in ["qemu", "lxc"] and res.get("template", 0) == 0:
                                        all_vms.append({"vmid": str(res.get("vmid", "")), "node": res.get("node", ""), "type": res.get("type", "qemu")})
                            except:
                                try:
                                    cluster_nodes = pvesh_get("/nodes")
                                    nodes_list = [n.get("node", "") for n in cluster_nodes if n.get("node")]
                                except:
                                    nodes_list = [self.node]
                        
                        if nodes_list:
                            for n in nodes_list:
                                try:
                                    for vm in pvesh_get(f"/nodes/{n}/qemu"):
                                        if vm.get("template", 0) == 0:
                                            all_vms.append({"vmid": str(vm.get("vmid", "")), "node": n, "type": "qemu"})
                                except:
                                    pass
                                try:
                                    for ct in pvesh_get(f"/nodes/{n}/lxc"):
                                        if ct.get("template", 0) == 0:
                                            all_vms.append({"vmid": str(ct.get("vmid", "")), "node": n, "type": "lxc"})
                                except:
                                    pass
                        
                        seen = set()
                        unique_vms = [v for v in all_vms if v["vmid"] and v["vmid"] not in seen and not seen.add(v["vmid"])]
                        
                        exclude_ids = [v.strip() for v in re.split(r'[,;\s]+', str(exclude_value)) if v.strip() and v.strip().isdigit()] if exclude_value else []
                        vm_ids = [v["vmid"] for v in unique_vms if v["vmid"] not in exclude_ids]
                        
                    elif isinstance(vms_value, list):
                        vm_ids = [str(v) for v in vms_value if v]
                    elif isinstance(vms_value, str) and vms_value.strip():
                        for v in re.split(r'[,;\s]+', vms_value.strip()):
                            v = v.strip()
                            if not v:
                                continue
                            match = re.match(r'(?:vm|lxc|qemu|ct)[/:]?(\d+)', v, re.IGNORECASE)
                            if match:
                                vm_ids.append(match.group(1))
                            elif v.isdigit():
                                vm_ids.append(v)
                    
                    if not vm_ids:
                        continue
                    
                    # Determina nodi per la raccolta info VM
                    nodes_str = job.get("nodes", "")
                    nodes_list = [n.strip() for n in nodes_str.split(",") if n.strip()] if nodes_str else []
                    if not nodes_list:
                        try:
                            cluster_nodes = pvesh_get("/nodes")
                            nodes_list = [n.get("node", "") for n in cluster_nodes if n.get("node")]
                        except:
                            nodes_list = [self.node]
                    
                    # Raccogli info VM dal cluster
                    cluster_vms = {}
                    for n in nodes_list:
                        try:
                            for vm in pvesh_get(f"/nodes/{n}/qemu"):
                                vm_id = str(vm.get("vmid", ""))
                                if vm_id:
                                    cluster_vms[vm_id] = {"name": vm.get("name", f"VM-{vm_id}"), "type": "qemu", "node": n, "status": vm.get("status", "unknown"), "maxdisk": vm.get("maxdisk", 0), "maxmem": vm.get("maxmem", 0)}
                        except:
                            pass
                        try:
                            for ct in pvesh_get(f"/nodes/{n}/lxc"):
                                ct_id = str(ct.get("vmid", ""))
                                if ct_id:
                                    cluster_vms[ct_id] = {"name": ct.get("name", f"CT-{ct_id}"), "type": "lxc", "node": n, "status": ct.get("status", "unknown"), "maxdisk": ct.get("maxdisk", 0), "maxmem": ct.get("maxmem", 0)}
                        except:
                            pass
                    
                    job_storage = job.get("storage", "")
                    vm_list = []
                    
                    for vmid in vm_ids:
                        vm_info = cluster_vms.get(vmid)
                        
                        if vm_info:
                            vm_data = {
                                "vmid": vmid,
                                "name": vm_info["name"],
                                "type": vm_info["type"],
                                "node": vm_info["node"],
                                "status": vm_info.get("status", "unknown"),
                                "disk_size_bytes": vm_info.get("maxdisk", 0) or None,
                                "disk_size_gb": round(vm_info["maxdisk"] / (1024**3), 2) if vm_info.get("maxdisk", 0) > 0 else None,
                                "memory_bytes": vm_info.get("maxmem", 0) or None,
                                "memory_gb": round(vm_info["maxmem"] / (1024**3), 2) if vm_info.get("maxmem", 0) > 0 else None,
                            }
                        else:
                            # Cerca in cluster/resources
                            vm_name, vm_type, vm_node, vm_disk, vm_mem, vm_status = f"VM-{vmid}", "unknown", nodes_list[0], 0, 0, "unknown"
                            try:
                                for res in get_cluster_resources_cached():
                                    if str(res.get("vmid", "")) == vmid:
                                        vm_name = res.get("name", vm_name)
                                        vm_type = res.get("type", vm_type)
                                        vm_node = res.get("node", vm_node)
                                        vm_disk = res.get("maxdisk", 0)
                                        vm_mem = res.get("maxmem", 0)
                                        vm_status = res.get("status", vm_status)
                                        break
                            except:
                                pass
                            vm_data = {
                                "vmid": vmid, "name": vm_name, "type": vm_type, "node": vm_node, "status": vm_status,
                                "disk_size_bytes": vm_disk or None, "disk_size_gb": round(vm_disk / (1024**3), 2) if vm_disk > 0 else None,
                                "memory_bytes": vm_mem or None, "memory_gb": round(vm_mem / (1024**3), 2) if vm_mem > 0 else None,
                            }
                        
                        # Aggiungi info backup dal repository
                        if job_storage and vm_data.get("type") != "unknown":
                            backup_info = get_latest_backup_info(self.node, job_storage, vmid, vm_data.get("type", "qemu"))
                            if backup_info:
                                vm_data.update(backup_info)
                        
                        vm_list.append(vm_data)
                    
                    # Filtra VM con backup recente
                    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
                    vms_with_recent = []
                    for vm in vm_list:
                        backup_date_str = vm.get("backup_date")
                        if backup_date_str:
                            try:
                                backup_dt = datetime.fromisoformat(backup_date_str.replace("Z", "+00:00"))
                                if backup_dt >= cutoff_time:
                                    vms_with_recent.append(vm)
                            except:
                                pass
                    
                    if vms_with_recent:
                        backup_jobs.append({
                            "job_id": job_id,
                            "nodes": job.get("nodes", self.node),
                            "storage": job.get("storage", "unknown"),
                            "schedule": job.get("schedule", ""),
                            "enabled": job.get("enabled", True) if "enabled" in job else True,
                            "mode": job.get("mode", "snapshot"),
                            "compress": job.get("compress", ""),
                            "all": job.get("all", False),
                            "vms": vms_with_recent,
                            "vm_count": len(vms_with_recent),
                        })
                except Exception as e:
                    logger.debug(f"    Errore elaborazione job {job_id}: {e}")
                    continue
            
            # Invia ogni job al syslog
            jobs_sent = 0
            for job in backup_jobs:
                data = {
                    "status": "success" if job.get("enabled", True) else "warning",
                    "job_id": job.get("job_id", ""),
                    "nodes": job.get("nodes", ""),
                    "storage": job.get("storage", ""),
                    "schedule": job.get("schedule", ""),
                    "enabled": job.get("enabled", True),
                    "mode": job.get("mode", ""),
                    "compress": job.get("compress", ""),
                    "all": job.get("all", False),
                    "vm_count": job.get("vm_count", 0),
                    "vms": job.get("vms", []),
                }
                if self.syslog:
                    self.syslog.send("PVE_BACKUP_JOB", data, test_mode)
                    jobs_sent += 1
            
            total_vms = sum(j.get("vm_count", 0) for j in backup_jobs)
            logger.info(f"    Trovati {len(backup_jobs)} job con {total_vms} VM/CT totali")
            return {"sent": True, "jobs": len(backup_jobs), "vms": total_vms}
        except Exception as e:
            logger.error(f"    ✗ Errore raccolta job backup: {e}")
            return {"sent": False, "error": str(e)}

    def _collect_backup_coverage(self, test_mode: bool) -> Dict:
        """Verifica VM/CT senza copertura backup"""
        logger.info("  → Verifica copertura backup...")
        
        try:
            not_backed_up = pvesh_get("/cluster/backup-info/not-backed-up")
            
            guests = []
            for item in not_backed_up:
                guests.append({
                    "vmid": item.get("vmid", ""),
                    "name": item.get("name", "unknown"),
                    "type": item.get("type", "unknown"),
                })
            
            count = len(guests)
            # Status info (non warning) - le VM escluse sono intenzionali
            status = "info" if count > 0 else "success"
            
            data = {
                "status": status,
                "not_backed_up_count": count,
                "guests": guests,
            }
            
            if self.syslog:
                self.syslog.send("PVE_BACKUP_COVERAGE", data, test_mode)
            
            if count > 0:
                logger.info(f"    ℹ {count} VM/CT senza backup schedulato (potrebbero essere escluse)")
            else:
                logger.info("    ✓ Tutte le VM/CT hanno backup schedulato")
            
            return {"sent": True, "not_covered": count}
        except Exception as e:
            logger.error(f"    ✗ Errore verifica copertura: {e}")
            return {"sent": False, "error": str(e)}

    def _collect_service_status(self, test_mode: bool) -> Dict:
        """Verifica stato servizi systemd PVE"""
        logger.info("  → Raccolta stato servizi PVE...")
        
        try:
            important_services = [
                "pve-cluster",
                "pvedaemon",
                "pveproxy",
                "pvestatd",
                "pve-firewall",
                "corosync",
                "pve-ha-crm",
                "pve-ha-lrm",
            ]
            
            services = []
            services_running = 0
            services_stopped = 0
            services_failed = 0
            
            for service_name in important_services:
                try:
                    result = subprocess.run(
                        ["systemctl", "is-active", service_name],
                        capture_output=True, text=True, timeout=5
                    )
                    state = result.stdout.strip() if result.returncode == 0 else "inactive"
                    
                    enabled_result = subprocess.run(
                        ["systemctl", "is-enabled", service_name],
                        capture_output=True, text=True, timeout=5
                    )
                    startup_type = enabled_result.stdout.strip() if enabled_result.returncode == 0 else "unknown"
                    
                    services.append({
                        "name": service_name,
                        "state": state,
                        "startup_type": startup_type,
                    })
                    
                    if state == "active":
                        services_running += 1
                    elif state == "failed":
                        services_failed += 1
                    else:
                        services_stopped += 1
                except:
                    services.append({
                        "name": service_name,
                        "state": "unknown",
                        "startup_type": "unknown",
                    })
            
            # Determina status complessivo
            status = "success"
            for svc in services:
                if svc["state"] == "failed":
                    status = "failed"
                    break
                elif svc["startup_type"] == "enabled" and svc["state"] != "active":
                    status = "warning"
            
            data = {
                "status": status,
                "services_total": len(services),
                "services_running": services_running,
                "services_stopped": services_stopped,
                "services_failed": services_failed,
                "services": services,
            }
            
            if self.syslog:
                self.syslog.send("PVE_SERVICE_STATUS", data, test_mode)
            
            logger.info(f"    Servizi: {services_running} running, {services_stopped} stopped, {services_failed} failed")
            return {"sent": True, "running": services_running, "failed": services_failed}
        except Exception as e:
            logger.error(f"    ✗ Errore raccolta servizi: {e}")
            return {"sent": False, "error": str(e)}

    def run_daily_report(self, test_mode: bool = False) -> Dict:
        """Genera e invia report giornaliero completo"""
        logger.info("→ Generazione report giornaliero PVE...")
        
        results = self.run(test_mode)
        
        # Genera riepilogo
        try:
            since = int((datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)).timestamp())
            tasks = pvesh_get(f"/nodes/{self.node}/tasks", typefilter="vzdump", since=str(since),
                              limit="500", source="all")
            
            completed = [t for t in tasks if t.get("status") == "stopped"]
            
            success_count = sum(1 for t in completed if t.get("exitstatus") == "OK")
            failed_count = sum(1 for t in completed if "error" in str(t.get("exitstatus", "")).lower())
            warning_count = len(completed) - success_count - failed_count
            
            overall = "failed" if failed_count > 0 else "warning" if warning_count > 0 else "success"
            
            data = {
                "status": overall,
                "report_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "lookback_hours": self.lookback_hours,
                "tasks_total": len(completed),
                "tasks_success": success_count,
                "tasks_warning": warning_count,
                "tasks_failed": failed_count,
            }
            
            if self.syslog:
                self.syslog.send("PVE_DAILY_REPORT", data, test_mode)
            
            results["daily_report"] = data
            logger.info(f"  ✓ Report giornaliero: {len(completed)} task ({success_count} ok, {warning_count} warning, {failed_count} failed)")
        except Exception as e:
            logger.error(f"  ✗ Errore report giornaliero: {e}")
            results["daily_report"] = {"error": str(e)}
        
        return results


def run_pve_monitor(config: Dict[str, Any], test_mode: bool = False) -> Dict[str, Any]:
    """
    Funzione principale per eseguire PVE Monitor.
    
    Args:
        config: Configurazione Proxreporter
        test_mode: Se True, non invia realmente i messaggi
    
    Returns:
        Risultati del monitoraggio
    """
    monitor = PVEMonitor(config)
    return monitor.run(test_mode)


if __name__ == "__main__":
    # Test standalone
    import argparse
    
    parser = argparse.ArgumentParser(description="PVE Monitor - Standalone test")
    parser.add_argument("-c", "--config", default="/opt/proxreport/config.json", help="Config file")
    parser.add_argument("--test", action="store_true", help="Test mode (no real send)")
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    try:
        with open(args.config) as f:
            config = json.load(f)
    except Exception as e:
        print(f"Errore caricamento config: {e}")
        exit(1)
    
    monitor = PVEMonitor(config)
    results = monitor.run(test_mode=args.test)
    
    print(json.dumps(results, indent=2, default=str))
