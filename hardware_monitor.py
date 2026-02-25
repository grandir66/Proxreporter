#!/usr/bin/env python3
"""
Proxreporter - Hardware Monitor Module

Monitora lo stato hardware del sistema e invia alert per:
- Dischi (SMART errors, settori riallocati, temperature)
- Memoria ECC (errori corretti/non corretti)
- RAID (mdadm, ZFS pool status)
- Temperature CPU/componenti
- Errori kernel (MCE, I/O errors, hardware failures)

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger("proxreporter")


class HardwareStatus(Enum):
    """Stati possibili per i componenti hardware"""
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass
class HardwareAlert:
    """Rappresenta un alert hardware"""
    component: str          # disk, memory, raid, temperature, kernel
    device: str             # /dev/sda, cpu0, md0, etc.
    status: HardwareStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


class HardwareMonitor:
    """
    Monitora lo stato hardware del sistema Proxmox.
    """
    
    # Soglie di default
    DEFAULT_THRESHOLDS = {
        "disk_temp_warning": 45,      # °C
        "disk_temp_critical": 55,     # °C
        "cpu_temp_warning": 75,       # °C
        "cpu_temp_critical": 90,      # °C
        "reallocated_sectors_warning": 1,
        "reallocated_sectors_critical": 10,
        "pending_sectors_warning": 1,
        "ecc_corrected_warning": 10,
        "ecc_uncorrected_critical": 1,
    }
    
    def __init__(self, config: Dict[str, Any] = None, executor: Callable = None):
        """
        Inizializza il monitor hardware.
        
        Args:
            config: Configurazione con soglie personalizzate
            executor: Funzione per eseguire comandi (locale o remoto via SSH)
        """
        self.config = config or {}
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **self.config.get("hardware_thresholds", {})}
        self.executor = executor or self._local_executor
        self.alerts: List[HardwareAlert] = []
    
    def _local_executor(self, cmd: str, silent: bool = True) -> tuple:
        """Esegue un comando localmente"""
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timeout"
        except Exception as e:
            return -1, "", str(e)
    
    def _run_command(self, cmd: str, silent: bool = True) -> Optional[str]:
        """Esegue un comando e ritorna l'output"""
        exit_code, stdout, stderr = self.executor(cmd, silent)
        if exit_code == 0:
            return stdout
        return None
    
    def run_all_checks(self) -> List[HardwareAlert]:
        """
        Esegue tutti i controlli hardware.
        
        Returns:
            Lista di alert rilevati
        """
        self.alerts = []
        
        logger.info("→ Controllo stato hardware...")
        
        # Dischi SMART
        self._check_smart_disks()
        
        # Memoria ECC
        self._check_memory_ecc()
        
        # RAID (mdadm + ZFS)
        self._check_raid_mdadm()
        self._check_raid_zfs()
        
        # Temperature
        self._check_temperatures()
        
        # Errori Kernel
        self._check_kernel_errors()
        
        # Summary
        critical_count = sum(1 for a in self.alerts if a.status == HardwareStatus.CRITICAL)
        warning_count = sum(1 for a in self.alerts if a.status == HardwareStatus.WARNING)
        
        if critical_count > 0:
            logger.warning(f"  ⚠ Rilevati {critical_count} problemi CRITICI, {warning_count} warning")
        elif warning_count > 0:
            logger.info(f"  ⚠ Rilevati {warning_count} warning hardware")
        else:
            logger.info("  ✓ Nessun problema hardware rilevato")
        
        return self.alerts
    
    # =========================================================================
    # SMART DISK CHECKS
    # =========================================================================
    
    def _check_smart_disks(self) -> None:
        """Controlla lo stato SMART di tutti i dischi"""
        # Trova tutti i dischi
        disks = self._get_disk_devices()
        
        for disk in disks:
            self._check_smart_disk(disk)
    
    def _get_disk_devices(self) -> List[str]:
        """Ottiene la lista dei dispositivi disco"""
        devices = []
        
        # Metodo 1: lsblk
        output = self._run_command("lsblk -d -n -o NAME,TYPE 2>/dev/null")
        if output:
            for line in output.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == 'disk':
                    devices.append(f"/dev/{parts[0]}")
        
        # Metodo 2: /sys/block (fallback)
        if not devices:
            output = self._run_command("ls /sys/block/ 2>/dev/null")
            if output:
                for dev in output.strip().split():
                    if dev.startswith(('sd', 'nvme', 'hd', 'vd')):
                        devices.append(f"/dev/{dev}")
        
        return devices
    
    def _check_smart_disk(self, device: str) -> None:
        """Controlla lo stato SMART di un singolo disco"""
        # Verifica se smartctl è disponibile
        output = self._run_command(f"smartctl -H -A {device} 2>/dev/null")
        if not output:
            return
        
        disk_name = device.split('/')[-1]
        
        # Check overall health
        if "PASSED" in output:
            pass  # OK
        elif "FAILED" in output:
            self.alerts.append(HardwareAlert(
                component="disk",
                device=device,
                status=HardwareStatus.CRITICAL,
                message=f"Disco {disk_name}: SMART health test FAILED",
                details={"smart_status": "FAILED"}
            ))
            return
        
        # Parse attributi SMART
        smart_attrs = self._parse_smart_attributes(output)
        
        # Reallocated Sectors (ID 5)
        reallocated = smart_attrs.get("Reallocated_Sector_Ct", 0)
        if reallocated >= self.thresholds["reallocated_sectors_critical"]:
            self.alerts.append(HardwareAlert(
                component="disk",
                device=device,
                status=HardwareStatus.CRITICAL,
                message=f"Disco {disk_name}: {reallocated} settori riallocati (critico)",
                details={"reallocated_sectors": reallocated}
            ))
        elif reallocated >= self.thresholds["reallocated_sectors_warning"]:
            self.alerts.append(HardwareAlert(
                component="disk",
                device=device,
                status=HardwareStatus.WARNING,
                message=f"Disco {disk_name}: {reallocated} settori riallocati",
                details={"reallocated_sectors": reallocated}
            ))
        
        # Pending Sectors (ID 197)
        pending = smart_attrs.get("Current_Pending_Sector", 0)
        if pending >= self.thresholds["pending_sectors_warning"]:
            self.alerts.append(HardwareAlert(
                component="disk",
                device=device,
                status=HardwareStatus.WARNING,
                message=f"Disco {disk_name}: {pending} settori pending",
                details={"pending_sectors": pending}
            ))
        
        # Temperature (ID 194 o 190)
        temp = smart_attrs.get("Temperature_Celsius", smart_attrs.get("Airflow_Temperature_Cel", 0))
        if temp >= self.thresholds["disk_temp_critical"]:
            self.alerts.append(HardwareAlert(
                component="disk",
                device=device,
                status=HardwareStatus.CRITICAL,
                message=f"Disco {disk_name}: temperatura {temp}°C (critica)",
                details={"temperature": temp}
            ))
        elif temp >= self.thresholds["disk_temp_warning"]:
            self.alerts.append(HardwareAlert(
                component="disk",
                device=device,
                status=HardwareStatus.WARNING,
                message=f"Disco {disk_name}: temperatura {temp}°C (elevata)",
                details={"temperature": temp}
            ))
        
        # Offline Uncorrectable (ID 198)
        uncorrectable = smart_attrs.get("Offline_Uncorrectable", 0)
        if uncorrectable > 0:
            self.alerts.append(HardwareAlert(
                component="disk",
                device=device,
                status=HardwareStatus.CRITICAL,
                message=f"Disco {disk_name}: {uncorrectable} settori non correggibili",
                details={"uncorrectable_sectors": uncorrectable}
            ))
    
    def _parse_smart_attributes(self, output: str) -> Dict[str, int]:
        """Parsa gli attributi SMART dall'output di smartctl"""
        attrs = {}
        
        # Pattern per attributi SMART
        # ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
        pattern = r'^\s*(\d+)\s+(\S+)\s+\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)'
        
        for line in output.split('\n'):
            match = re.match(pattern, line)
            if match:
                attr_name = match.group(2)
                raw_value = int(match.group(3))
                attrs[attr_name] = raw_value
        
        return attrs
    
    # =========================================================================
    # MEMORY ECC CHECKS
    # =========================================================================
    
    def _check_memory_ecc(self) -> None:
        """Controlla errori memoria ECC"""
        # Metodo 1: edac-util
        output = self._run_command("edac-util -s 2>/dev/null")
        if output:
            self._parse_edac_util(output)
            return
        
        # Metodo 2: /sys/devices/system/edac/
        self._check_edac_sysfs()
    
    def _parse_edac_util(self, output: str) -> None:
        """Parsa output di edac-util"""
        # Cerca errori corretti e non corretti
        ce_match = re.search(r'(\d+)\s+Corrected', output, re.IGNORECASE)
        ue_match = re.search(r'(\d+)\s+Uncorrected', output, re.IGNORECASE)
        
        corrected = int(ce_match.group(1)) if ce_match else 0
        uncorrected = int(ue_match.group(1)) if ue_match else 0
        
        if uncorrected >= self.thresholds["ecc_uncorrected_critical"]:
            self.alerts.append(HardwareAlert(
                component="memory",
                device="ECC",
                status=HardwareStatus.CRITICAL,
                message=f"Memoria: {uncorrected} errori ECC non corretti (CRITICO)",
                details={"corrected": corrected, "uncorrected": uncorrected}
            ))
        elif corrected >= self.thresholds["ecc_corrected_warning"]:
            self.alerts.append(HardwareAlert(
                component="memory",
                device="ECC",
                status=HardwareStatus.WARNING,
                message=f"Memoria: {corrected} errori ECC corretti",
                details={"corrected": corrected, "uncorrected": uncorrected}
            ))
    
    def _check_edac_sysfs(self) -> None:
        """Controlla errori ECC via sysfs"""
        edac_path = "/sys/devices/system/edac/mc"
        
        # Controlla se esiste
        output = self._run_command(f"ls {edac_path}/ 2>/dev/null")
        if not output:
            return  # No EDAC support
        
        total_ce = 0
        total_ue = 0
        
        for mc in output.strip().split():
            if mc.startswith("mc"):
                ce = self._run_command(f"cat {edac_path}/{mc}/ce_count 2>/dev/null")
                ue = self._run_command(f"cat {edac_path}/{mc}/ue_count 2>/dev/null")
                
                if ce:
                    total_ce += int(ce.strip())
                if ue:
                    total_ue += int(ue.strip())
        
        if total_ue >= self.thresholds["ecc_uncorrected_critical"]:
            self.alerts.append(HardwareAlert(
                component="memory",
                device="ECC",
                status=HardwareStatus.CRITICAL,
                message=f"Memoria: {total_ue} errori ECC non corretti",
                details={"corrected": total_ce, "uncorrected": total_ue}
            ))
        elif total_ce >= self.thresholds["ecc_corrected_warning"]:
            self.alerts.append(HardwareAlert(
                component="memory",
                device="ECC",
                status=HardwareStatus.WARNING,
                message=f"Memoria: {total_ce} errori ECC corretti",
                details={"corrected": total_ce, "uncorrected": total_ue}
            ))
    
    # =========================================================================
    # RAID CHECKS
    # =========================================================================
    
    def _check_raid_mdadm(self) -> None:
        """Controlla stato RAID mdadm"""
        output = self._run_command("cat /proc/mdstat 2>/dev/null")
        if not output or "md" not in output:
            return
        
        # Parse mdstat
        current_md = None
        for line in output.split('\n'):
            # Nuova riga md
            md_match = re.match(r'^(md\d+)\s*:\s*(\w+)\s+(\w+)', line)
            if md_match:
                current_md = md_match.group(1)
                status = md_match.group(2)  # active/inactive
                
                if status != "active":
                    self.alerts.append(HardwareAlert(
                        component="raid",
                        device=current_md,
                        status=HardwareStatus.CRITICAL,
                        message=f"RAID {current_md}: stato {status} (non attivo)",
                        details={"raid_status": status}
                    ))
            
            # Stato dischi [UU] o [U_]
            state_match = re.search(r'\[([U_]+)\]', line)
            if state_match and current_md:
                state = state_match.group(1)
                failed_count = state.count('_')
                
                if failed_count > 0:
                    self.alerts.append(HardwareAlert(
                        component="raid",
                        device=current_md,
                        status=HardwareStatus.CRITICAL,
                        message=f"RAID {current_md}: {failed_count} disco(i) degradato/i [{state}]",
                        details={"state": state, "failed_disks": failed_count}
                    ))
            
            # Rebuild in progress
            if "recovery" in line.lower() or "resync" in line.lower():
                progress_match = re.search(r'(\d+\.?\d*)%', line)
                progress = progress_match.group(1) if progress_match else "?"
                
                self.alerts.append(HardwareAlert(
                    component="raid",
                    device=current_md,
                    status=HardwareStatus.WARNING,
                    message=f"RAID {current_md}: rebuild in corso ({progress}%)",
                    details={"rebuild_progress": progress}
                ))
    
    def _check_raid_zfs(self) -> None:
        """Controlla stato ZFS pool"""
        output = self._run_command("zpool status 2>/dev/null")
        if not output:
            return
        
        current_pool = None
        
        for line in output.split('\n'):
            # Nome pool
            pool_match = re.match(r'^\s*pool:\s*(\S+)', line)
            if pool_match:
                current_pool = pool_match.group(1)
            
            # Stato pool
            state_match = re.match(r'^\s*state:\s*(\S+)', line)
            if state_match and current_pool:
                state = state_match.group(1)
                
                if state == "DEGRADED":
                    self.alerts.append(HardwareAlert(
                        component="raid",
                        device=f"zpool:{current_pool}",
                        status=HardwareStatus.CRITICAL,
                        message=f"ZFS Pool {current_pool}: stato DEGRADED",
                        details={"pool_state": state}
                    ))
                elif state == "FAULTED":
                    self.alerts.append(HardwareAlert(
                        component="raid",
                        device=f"zpool:{current_pool}",
                        status=HardwareStatus.CRITICAL,
                        message=f"ZFS Pool {current_pool}: stato FAULTED (critico)",
                        details={"pool_state": state}
                    ))
                elif state == "OFFLINE":
                    self.alerts.append(HardwareAlert(
                        component="raid",
                        device=f"zpool:{current_pool}",
                        status=HardwareStatus.CRITICAL,
                        message=f"ZFS Pool {current_pool}: OFFLINE",
                        details={"pool_state": state}
                    ))
            
            # Errori nei vdev
            if current_pool and ("DEGRADED" in line or "FAULTED" in line or "UNAVAIL" in line):
                vdev_match = re.match(r'^\s+(\S+)\s+(DEGRADED|FAULTED|UNAVAIL)', line)
                if vdev_match:
                    vdev = vdev_match.group(1)
                    vdev_state = vdev_match.group(2)
                    
                    self.alerts.append(HardwareAlert(
                        component="raid",
                        device=f"zpool:{current_pool}/{vdev}",
                        status=HardwareStatus.CRITICAL,
                        message=f"ZFS {current_pool}: disco {vdev} {vdev_state}",
                        details={"vdev": vdev, "vdev_state": vdev_state}
                    ))
            
            # Scrub errors
            errors_match = re.search(r'(\d+)\s+repaired', line)
            if errors_match and current_pool:
                repaired = int(errors_match.group(1))
                if repaired > 0:
                    self.alerts.append(HardwareAlert(
                        component="raid",
                        device=f"zpool:{current_pool}",
                        status=HardwareStatus.WARNING,
                        message=f"ZFS Pool {current_pool}: {repaired} errori riparati durante scrub",
                        details={"repaired_errors": repaired}
                    ))
    
    # =========================================================================
    # TEMPERATURE CHECKS
    # =========================================================================
    
    def _check_temperatures(self) -> None:
        """Controlla temperature CPU e componenti"""
        # Metodo 1: sensors (lm-sensors)
        output = self._run_command("sensors 2>/dev/null")
        if output:
            self._parse_sensors_output(output)
            return
        
        # Metodo 2: /sys/class/thermal
        self._check_thermal_sysfs()
    
    def _parse_sensors_output(self, output: str) -> None:
        """Parsa output di sensors"""
        # Pattern per temperature
        temp_pattern = r'(\S+):\s*\+?(-?\d+\.?\d*)\s*°?C'
        
        for line in output.split('\n'):
            match = re.search(temp_pattern, line)
            if match:
                sensor_name = match.group(1).lower()
                temp = float(match.group(2))
                
                # Determina soglie in base al tipo di sensore
                if 'core' in sensor_name or 'cpu' in sensor_name or 'tctl' in sensor_name:
                    warning_thresh = self.thresholds["cpu_temp_warning"]
                    critical_thresh = self.thresholds["cpu_temp_critical"]
                    component = "temperature"
                    device = f"CPU ({sensor_name})"
                else:
                    # Altri sensori (chipset, etc.)
                    warning_thresh = 70
                    critical_thresh = 85
                    component = "temperature"
                    device = sensor_name
                
                if temp >= critical_thresh:
                    self.alerts.append(HardwareAlert(
                        component=component,
                        device=device,
                        status=HardwareStatus.CRITICAL,
                        message=f"Temperatura {device}: {temp}°C (CRITICA)",
                        details={"temperature": temp, "threshold": critical_thresh}
                    ))
                elif temp >= warning_thresh:
                    self.alerts.append(HardwareAlert(
                        component=component,
                        device=device,
                        status=HardwareStatus.WARNING,
                        message=f"Temperatura {device}: {temp}°C (elevata)",
                        details={"temperature": temp, "threshold": warning_thresh}
                    ))
    
    def _check_thermal_sysfs(self) -> None:
        """Controlla temperature via sysfs"""
        output = self._run_command("ls /sys/class/thermal/ 2>/dev/null")
        if not output:
            return
        
        for zone in output.strip().split():
            if zone.startswith("thermal_zone"):
                temp_output = self._run_command(f"cat /sys/class/thermal/{zone}/temp 2>/dev/null")
                type_output = self._run_command(f"cat /sys/class/thermal/{zone}/type 2>/dev/null")
                
                if temp_output:
                    try:
                        temp = int(temp_output.strip()) / 1000  # millidegree to degree
                        zone_type = type_output.strip() if type_output else zone
                        
                        if temp >= self.thresholds["cpu_temp_critical"]:
                            self.alerts.append(HardwareAlert(
                                component="temperature",
                                device=zone_type,
                                status=HardwareStatus.CRITICAL,
                                message=f"Temperatura {zone_type}: {temp}°C (CRITICA)",
                                details={"temperature": temp}
                            ))
                        elif temp >= self.thresholds["cpu_temp_warning"]:
                            self.alerts.append(HardwareAlert(
                                component="temperature",
                                device=zone_type,
                                status=HardwareStatus.WARNING,
                                message=f"Temperatura {zone_type}: {temp}°C (elevata)",
                                details={"temperature": temp}
                            ))
                    except ValueError:
                        pass
    
    # =========================================================================
    # KERNEL ERROR CHECKS
    # =========================================================================
    
    def _check_kernel_errors(self) -> None:
        """Controlla errori nel kernel log (dmesg)"""
        # Ultimi 1000 messaggi del kernel
        output = self._run_command("dmesg --level=err,crit,alert,emerg -T 2>/dev/null | tail -100")
        if not output:
            # Fallback senza timestamp
            output = self._run_command("dmesg --level=err,crit,alert,emerg 2>/dev/null | tail -100")
        
        if not output:
            return
        
        # Pattern per errori hardware comuni
        error_patterns = {
            "mce": (r'mce:|Machine check', "MCE (Machine Check Exception)"),
            "io_error": (r'I/O error|Buffer I/O error|blk_update_request', "I/O Error"),
            "ata_error": (r'ata\d+.*error|SATA.*error', "SATA/ATA Error"),
            "nvme_error": (r'nvme.*error|nvme.*failed', "NVMe Error"),
            "memory_error": (r'EDAC|ECC|memory error|page allocation failure', "Memory Error"),
            "pcie_error": (r'PCIe.*error|AER.*error', "PCIe Error"),
            "hardware_error": (r'Hardware Error|hardware error', "Hardware Error"),
            "thermal": (r'thermal|CPU.*throttl|temperature above', "Thermal Event"),
            "filesystem": (r'EXT4-fs error|XFS.*error|BTRFS.*error|filesystem error', "Filesystem Error"),
        }
        
        found_errors = {}
        
        for line in output.split('\n'):
            line_lower = line.lower()
            
            for error_type, (pattern, description) in error_patterns.items():
                if re.search(pattern, line, re.IGNORECASE):
                    if error_type not in found_errors:
                        found_errors[error_type] = {
                            "description": description,
                            "count": 0,
                            "last_message": ""
                        }
                    found_errors[error_type]["count"] += 1
                    found_errors[error_type]["last_message"] = line.strip()[:200]
        
        # Genera alert per ogni tipo di errore trovato
        for error_type, error_info in found_errors.items():
            severity = HardwareStatus.CRITICAL if error_type in ["mce", "memory_error", "hardware_error"] else HardwareStatus.WARNING
            
            self.alerts.append(HardwareAlert(
                component="kernel",
                device=error_type,
                status=severity,
                message=f"Kernel: {error_info['count']} {error_info['description']}",
                details={
                    "error_type": error_type,
                    "count": error_info["count"],
                    "last_message": error_info["last_message"]
                }
            ))
    
    # =========================================================================
    # UTILITY METHODS
    # =========================================================================
    
    def get_alerts_by_severity(self, severity: HardwareStatus) -> List[HardwareAlert]:
        """Filtra alert per severità"""
        return [a for a in self.alerts if a.status == severity]
    
    def get_alerts_by_component(self, component: str) -> List[HardwareAlert]:
        """Filtra alert per componente"""
        return [a for a in self.alerts if a.component == component]
    
    def has_critical_alerts(self) -> bool:
        """Verifica se ci sono alert critici"""
        return any(a.status == HardwareStatus.CRITICAL for a in self.alerts)
    
    def to_dict(self) -> List[Dict[str, Any]]:
        """Converte gli alert in lista di dizionari"""
        return [
            {
                "component": a.component,
                "device": a.device,
                "status": a.status.value,
                "message": a.message,
                "details": a.details,
                "timestamp": a.timestamp.isoformat()
            }
            for a in self.alerts
        ]
    
    def get_summary(self) -> Dict[str, Any]:
        """Ritorna un riepilogo dello stato hardware"""
        return {
            "total_alerts": len(self.alerts),
            "critical": len(self.get_alerts_by_severity(HardwareStatus.CRITICAL)),
            "warning": len(self.get_alerts_by_severity(HardwareStatus.WARNING)),
            "by_component": {
                "disk": len(self.get_alerts_by_component("disk")),
                "memory": len(self.get_alerts_by_component("memory")),
                "raid": len(self.get_alerts_by_component("raid")),
                "temperature": len(self.get_alerts_by_component("temperature")),
                "kernel": len(self.get_alerts_by_component("kernel")),
            },
            "overall_status": (
                HardwareStatus.CRITICAL.value if self.has_critical_alerts()
                else HardwareStatus.WARNING.value if self.alerts
                else HardwareStatus.OK.value
            )
        }
    
    def get_full_status(self) -> Dict[str, Any]:
        """
        Raccoglie lo stato completo dell'hardware (non solo alert).
        Include informazioni su dischi, temperature, memoria, RAID.
        """
        status = {
            "disks": [],
            "temperatures": [],
            "memory": {},
            "raid": [],
            "alerts": self.to_dict(),
            "summary": self.get_summary()
        }
        
        # Raccogli info dischi
        disks = self._get_disk_devices()
        for disk in disks:
            disk_info = self._get_disk_info(disk)
            if disk_info:
                status["disks"].append(disk_info)
        
        # Raccogli temperature
        temps = self._get_all_temperatures()
        if temps:
            status["temperatures"] = temps
        
        # Raccogli info memoria
        mem_info = self._get_memory_info()
        if mem_info:
            status["memory"] = mem_info
        
        # Raccogli info RAID
        raid_info = self._get_raid_info()
        if raid_info:
            status["raid"] = raid_info
        
        return status
    
    def _get_disk_info(self, device: str) -> Optional[Dict[str, Any]]:
        """Ottiene informazioni dettagliate su un disco"""
        info = {"device": device}
        
        # SMART health
        output = self._run_command(f"smartctl -H {device} 2>/dev/null")
        if output:
            if "PASSED" in output:
                info["smart_status"] = "PASSED"
            elif "FAILED" in output:
                info["smart_status"] = "FAILED"
            else:
                info["smart_status"] = "UNKNOWN"
        
        # Modello e seriale
        output = self._run_command(f"smartctl -i {device} 2>/dev/null")
        if output:
            for line in output.split('\n'):
                if "Model" in line or "Device Model" in line:
                    info["model"] = line.split(':')[-1].strip()
                elif "Serial" in line:
                    info["serial"] = line.split(':')[-1].strip()
                elif "Capacity" in line or "User Capacity" in line:
                    info["capacity"] = line.split(':')[-1].strip()
        
        # Temperatura
        output = self._run_command(f"smartctl -A {device} 2>/dev/null")
        if output:
            for line in output.split('\n'):
                if "Temperature" in line and "Celsius" in line:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if p.isdigit() and i > 0:
                            info["temperature"] = int(p)
                            break
                # Settori riallocati
                if "Reallocated_Sector" in line:
                    parts = line.split()
                    if len(parts) >= 10:
                        try:
                            info["reallocated_sectors"] = int(parts[9])
                        except ValueError:
                            pass
        
        return info if len(info) > 1 else None
    
    def _get_all_temperatures(self) -> List[Dict[str, Any]]:
        """Raccoglie tutte le temperature del sistema"""
        temps = []
        
        # Prova con sensors
        output = self._run_command("sensors -j 2>/dev/null")
        if output:
            try:
                import json
                data = json.loads(output)
                for chip, values in data.items():
                    if isinstance(values, dict):
                        for sensor, readings in values.items():
                            if isinstance(readings, dict):
                                for key, value in readings.items():
                                    if "input" in key.lower() and isinstance(value, (int, float)):
                                        temps.append({
                                            "chip": chip,
                                            "sensor": sensor,
                                            "temperature": round(value, 1)
                                        })
            except:
                pass
        
        # Fallback: prova con sensors semplice
        if not temps:
            output = self._run_command("sensors 2>/dev/null")
            if output:
                current_chip = ""
                for line in output.split('\n'):
                    if not line.startswith(' ') and ':' not in line and line.strip():
                        current_chip = line.strip()
                    elif '°C' in line:
                        parts = line.split(':')
                        if len(parts) >= 2:
                            sensor = parts[0].strip()
                            temp_match = re.search(r'([+-]?\d+\.?\d*)\s*°C', parts[1])
                            if temp_match:
                                temps.append({
                                    "chip": current_chip,
                                    "sensor": sensor,
                                    "temperature": float(temp_match.group(1))
                                })
        
        # Fallback: /sys/class/thermal
        if not temps:
            output = self._run_command("cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null")
            if output:
                for i, line in enumerate(output.strip().split('\n')):
                    try:
                        temp = int(line) / 1000
                        temps.append({
                            "chip": "thermal_zone",
                            "sensor": f"zone{i}",
                            "temperature": temp
                        })
                    except ValueError:
                        pass
        
        return temps
    
    def _get_memory_info(self) -> Dict[str, Any]:
        """Raccoglie informazioni sulla memoria"""
        info = {}
        
        # /proc/meminfo
        output = self._run_command("cat /proc/meminfo 2>/dev/null")
        if output:
            for line in output.split('\n'):
                if line.startswith("MemTotal:"):
                    info["total_kb"] = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    info["available_kb"] = int(line.split()[1])
                elif line.startswith("SwapTotal:"):
                    info["swap_total_kb"] = int(line.split()[1])
                elif line.startswith("SwapFree:"):
                    info["swap_free_kb"] = int(line.split()[1])
        
        # ECC info
        output = self._run_command("edac-util -s 2>/dev/null")
        if output and "No errors" not in output:
            info["ecc_status"] = output.strip()
        else:
            info["ecc_status"] = "ok" if output else "not_available"
        
        return info
    
    def _get_raid_info(self) -> List[Dict[str, Any]]:
        """Raccoglie informazioni su RAID mdadm e ZFS"""
        raid_info = []
        
        # mdadm
        output = self._run_command("cat /proc/mdstat 2>/dev/null")
        if output and "md" in output:
            for line in output.split('\n'):
                if line.startswith('md'):
                    parts = line.split()
                    if len(parts) >= 3:
                        raid_info.append({
                            "type": "mdadm",
                            "device": parts[0].rstrip(':'),
                            "status": "active" if "active" in line else "inactive",
                            "level": parts[3] if len(parts) > 3 else "unknown"
                        })
        
        # ZFS
        output = self._run_command("zpool list -H -o name,health,size,alloc,free,cap 2>/dev/null")
        if output:
            for line in output.strip().split('\n'):
                parts = line.split('\t')
                if len(parts) >= 6:
                    raid_info.append({
                        "type": "zfs",
                        "device": parts[0],
                        "status": parts[1].lower(),
                        "size": parts[2],
                        "used": parts[3],
                        "free": parts[4],
                        "capacity": parts[5]
                    })
        
        return raid_info
