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
        self.format = syslog_config.get("format", "rfc5424").lower()
        self.app_name = "pve-monitor"  # App name specifico per PVE Monitor
        
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
            syslog_msg = self._build_gelf_message(message_type, payload, severity)
        else:
            json_payload = json.dumps(payload, separators=(",", ":"), default=str)
            syslog_msg = f"<{priority}>1 {timestamp} {self.hostname} {self.app_name} {os.getpid()} {message_type} - {json_payload}"

        if test_mode:
            logger.info(f"\n=== SYSLOG MESSAGE ({len(syslog_msg)} bytes) ===\n{syslog_msg[:500]}...\n")
            return True

        try:
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
            
            logger.info(f"  ✓ PVE Syslog inviato: {message_type} ({len(syslog_msg)} bytes)")
            return True
        except Exception as e:
            logger.error(f"  ✗ Errore invio syslog PVE: {e}")
            return False

    def _build_gelf_message(self, message_type: str, payload: Dict, severity: int) -> bytes:
        """Costruisce messaggio GELF per Graylog"""
        import time
        
        short_message = f"{message_type}: {payload.get('status', 'info')}"
        
        gelf_msg = {
            "version": "1.1",
            "host": self.hostname,
            "short_message": short_message,
            "full_message": json.dumps(payload, default=str),
            "timestamp": time.time(),
            "level": severity,
            "_app": self.app_name,
            "_app_version": __version__,
            "_message_type": message_type,
            "_client_code": self.client.get("code", ""),
            "_client_name": self.client.get("name", ""),
        }
        
        # Aggiungi campi principali dal payload
        for key in ["status", "server_name", "vm_count", "jobs_total"]:
            if key in payload:
                gelf_msg[f"_{key}"] = str(payload[key])
        
        return (json.dumps(gelf_msg) + '\0').encode('utf-8')


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


class PVEMonitor:
    """
    Monitora lo stato di Proxmox VE e invia alert via Syslog.
    Integrato in Proxreporter.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Inizializza il monitor PVE.
        
        Args:
            config: Configurazione completa di Proxreporter
        """
        self.config = config
        pve_config = config.get("pve_monitor", {})
        
        self.enabled = pve_config.get("enabled", False)
        self.lookback_hours = pve_config.get("lookback_hours", 24)
        self.check_node_status = pve_config.get("check_node_status", True)
        self.check_storage_status = pve_config.get("check_storage_status", True)
        self.check_backup_results = pve_config.get("check_backup_results", True)
        self.check_backup_jobs = pve_config.get("check_backup_jobs", True)
        self.check_backup_coverage = pve_config.get("check_backup_coverage", True)
        self.check_service_status = pve_config.get("check_service_status", True)
        
        # Client info
        self.client_info = {
            "codcli": config.get("codcli", ""),
            "nomecliente": config.get("nomecliente", ""),
            "site": pve_config.get("site", "default")
        }
        
        self.node = get_node_name()
        self.syslog: Optional[PVESyslogSender] = None

    def run(self, test_mode: bool = False) -> Dict[str, Any]:
        """
        Esegue il monitoraggio PVE completo.
        
        Returns:
            Dizionario con risultati dei controlli
        """
        if not self.enabled and not test_mode:
            logger.info("  ℹ PVE Monitor disabilitato")
            return {"enabled": False}
        
        logger.info("→ Avvio PVE Monitor...")
        logger.info(f"  Nodo: {self.node}")
        logger.info(f"  Lookback: {self.lookback_hours}h")
        
        # Inizializza syslog sender
        self.syslog = PVESyslogSender(self.config, self.client_info)
        
        # Resetta cache
        clear_caches()
        
        results = {
            "enabled": True,
            "node": self.node,
            "pve_version": get_pve_version(),
            "checks": {}
        }
        
        # Esegui controlli
        if self.check_node_status:
            results["checks"]["node_status"] = self._collect_node_status(test_mode)
        
        if self.check_storage_status:
            results["checks"]["storage_status"] = self._collect_storage_status(test_mode)
        
        if self.check_backup_results:
            results["checks"]["backup_results"] = self._collect_backup_results(test_mode)
        
        if self.check_backup_jobs:
            results["checks"]["backup_jobs"] = self._collect_backup_jobs(test_mode)
        
        if self.check_backup_coverage:
            results["checks"]["backup_coverage"] = self._collect_backup_coverage(test_mode)
        
        if self.check_service_status:
            results["checks"]["service_status"] = self._collect_service_status(test_mode)
        
        logger.info("  ✓ PVE Monitor completato")
        return results

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
                
                return {"sent": True, "count": len(backup_storages)}
            
            return {"sent": False, "count": 0}
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

            # Raggruppa task per job
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
                
                # Prova a ottenere info VM
                vm_name = f"VM-{vmid}"
                vm_type = "unknown"
                try:
                    vm_info = pvesh_get(f"/nodes/{self.node}/qemu/{vmid}")
                    vm_name = vm_info.get("name", f"VM-{vmid}")
                    vm_type = "qemu"
                except:
                    try:
                        ct_info = pvesh_get(f"/nodes/{self.node}/lxc/{vmid}")
                        vm_name = ct_info.get("name", f"CT-{vmid}")
                        vm_type = "lxc"
                    except:
                        pass
                
                # Raggruppa per job (stesso user + timestamp arrotondato a 5 minuti)
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
            
            # Invia un messaggio per ogni job
            jobs_sent = 0
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
            
            logger.info(f"    Processati {len(jobs_dict)} job di backup")
            return {"sent": True, "jobs": len(jobs_dict), "tasks": len(completed)}
        except Exception as e:
            logger.error(f"    ✗ Errore raccolta task backup: {e}")
            return {"sent": False, "error": str(e)}

    def _collect_backup_jobs(self, test_mode: bool) -> Dict:
        """Raccoglie informazioni sui job di backup schedulati"""
        logger.info("  → Raccolta job di backup schedulati...")
        
        try:
            cluster_jobs = []
            try:
                cluster_jobs = pvesh_get("/cluster/backup")
            except:
                pass
            
            if not cluster_jobs:
                # Prova a leggere da file
                try:
                    with open("/etc/pve/jobs.cfg", "r") as f:
                        for line in f:
                            line = line.strip()
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
                except:
                    pass
            
            jobs_sent = 0
            for job in cluster_jobs:
                job_id = job.get("id", "")
                if not job_id:
                    continue
                
                data = {
                    "status": "success" if job.get("enabled", True) else "warning",
                    "job_id": job_id,
                    "nodes": job.get("nodes", self.node),
                    "storage": job.get("storage", "unknown"),
                    "schedule": job.get("schedule", ""),
                    "enabled": job.get("enabled", True),
                    "mode": job.get("mode", "snapshot"),
                    "compress": job.get("compress", ""),
                    "all": job.get("all", False),
                }
                
                if self.syslog:
                    self.syslog.send("PVE_BACKUP_JOB", data, test_mode)
                    jobs_sent += 1
            
            logger.info(f"    Trovati {len(cluster_jobs)} job schedulati")
            return {"sent": True, "jobs": len(cluster_jobs)}
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
            status = "warning" if count > 0 else "success"
            
            data = {
                "status": status,
                "not_backed_up_count": count,
                "guests": guests,
            }
            
            if self.syslog:
                self.syslog.send("PVE_BACKUP_COVERAGE", data, test_mode)
            
            if count > 0:
                logger.warning(f"    ⚠ {count} VM/CT senza backup schedulato")
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
