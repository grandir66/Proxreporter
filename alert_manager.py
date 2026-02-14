"""
Proxreporter - Alert Manager Module

Gestisce l'invio di alert e notifiche via:
- Email SMTP
- Syslog (UDP/TCP)
- Proxmox notification system

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import logging
import socket
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from enum import Enum

logger = logging.getLogger("proxreporter")


class AlertSeverity(Enum):
    """Livelli di severità per gli alert (compatibili con Syslog)"""
    EMERGENCY = 0    # Sistema inutilizzabile
    ALERT = 1        # Azione immediata richiesta
    CRITICAL = 2     # Condizioni critiche
    ERROR = 3        # Condizioni di errore
    WARNING = 4      # Condizioni di warning
    NOTICE = 5       # Condizioni normali ma significative
    INFO = 6         # Informativo
    DEBUG = 7        # Debug
    

class AlertType(Enum):
    """Tipi di alert supportati"""
    BACKUP_SUCCESS = "backup_success"
    BACKUP_FAILURE = "backup_failure"
    UPLOAD_SUCCESS = "upload_success"
    UPLOAD_FAILURE = "upload_failure"
    REPORT_GENERATED = "report_generated"
    VM_STATUS_CHANGE = "vm_status_change"
    HOST_WARNING = "host_warning"
    STORAGE_WARNING = "storage_warning"
    HARDWARE_WARNING = "hardware_warning"
    HARDWARE_CRITICAL = "hardware_critical"
    DISK_ERROR = "disk_error"
    RAID_DEGRADED = "raid_degraded"
    MEMORY_ERROR = "memory_error"
    TEMPERATURE_WARNING = "temperature_warning"
    KERNEL_ERROR = "kernel_error"
    CUSTOM = "custom"


class SyslogSender:
    """Invia messaggi a un server Syslog remoto via UDP o TCP"""
    
    # Facility codes (RFC 5424)
    FACILITY_LOCAL0 = 16
    FACILITY_LOCAL1 = 17
    FACILITY_LOCAL2 = 18
    FACILITY_LOCAL3 = 19
    FACILITY_LOCAL4 = 20
    FACILITY_LOCAL5 = 21
    FACILITY_LOCAL6 = 22
    FACILITY_LOCAL7 = 23
    
    def __init__(self, config: Dict[str, Any]):
        """
        Inizializza il sender Syslog.
        
        Config keys:
            host: hostname/IP del server syslog
            port: porta (default 514)
            protocol: 'udp' o 'tcp' (default 'udp')
            facility: facility code (default LOCAL0 = 16)
            app_name: nome applicazione (default 'proxreporter')
        """
        self.config = config.get('syslog', {})
        self.enabled = self.config.get('enabled', False)
        self.host = self.config.get('host', '')
        self.port = int(self.config.get('port', 514))
        self.protocol = self.config.get('protocol', 'udp').lower()
        self.facility = int(self.config.get('facility', self.FACILITY_LOCAL0))
        self.app_name = self.config.get('app_name', 'proxreporter')
        self.hostname = socket.gethostname()
        self._socket = None
    
    def _get_socket(self) -> Optional[socket.socket]:
        """Crea o restituisce il socket per la connessione"""
        if self._socket is not None:
            return self._socket
        
        try:
            if self.protocol == 'tcp':
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.settimeout(5)
                self._socket.connect((self.host, self.port))
            else:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._socket.settimeout(5)
            return self._socket
        except Exception as e:
            logger.error(f"✗ Errore connessione Syslog {self.host}:{self.port}: {e}")
            return None
    
    def _build_syslog_message(self, severity: AlertSeverity, message: str, 
                               structured_data: Optional[Dict] = None) -> bytes:
        """
        Costruisce un messaggio Syslog in formato RFC 5424 o GELF per Graylog.
        
        PRI = facility * 8 + severity
        """
        # Check if GELF format is requested
        if self.config.get('format', 'rfc5424').lower() == 'gelf':
            return self._build_gelf_message(severity, message, structured_data)
        
        pri = self.facility * 8 + severity.value
        timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        
        # Structured data (opzionale) - formato ottimizzato per Graylog
        sd = '-'
        if structured_data:
            sd_parts = []
            for key, value in structured_data.items():
                # Escape caratteri speciali
                value_escaped = str(value).replace('\\', '\\\\').replace('"', '\\"').replace(']', '\\]')
                sd_parts.append(f'{key}="{value_escaped}"')
            sd = f'[proxreporter@0 {" ".join(sd_parts)}]'
        
        # Formato RFC 5424: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG
        syslog_msg = f'<{pri}>1 {timestamp} {self.hostname} {self.app_name} - - {sd} {message}'
        
        return syslog_msg.encode('utf-8')
    
    def _build_gelf_message(self, severity: AlertSeverity, message: str,
                            structured_data: Optional[Dict] = None) -> bytes:
        """
        Costruisce un messaggio in formato GELF (Graylog Extended Log Format).
        """
        import time
        
        gelf_msg = {
            "version": "1.1",
            "host": self.hostname,
            "short_message": message[:250] if len(message) > 250 else message,
            "full_message": message,
            "timestamp": time.time(),
            "level": severity.value,
            "_app": self.app_name,
            "_facility": "proxreporter",
        }
        
        # Aggiungi campi strutturati con prefisso _ (GELF requirement)
        if structured_data:
            for key, value in structured_data.items():
                gelf_key = f"_{key}" if not key.startswith('_') else key
                gelf_msg[gelf_key] = str(value)
        
        return (json.dumps(gelf_msg) + '\0').encode('utf-8')
    
    def send(self, severity: AlertSeverity, message: str, 
             alert_type: Optional[AlertType] = None,
             extra_data: Optional[Dict] = None) -> bool:
        """
        Invia un messaggio al server Syslog.
        
        Args:
            severity: livello di severità
            message: messaggio testuale
            alert_type: tipo di alert (opzionale)
            extra_data: dati strutturati aggiuntivi (opzionale)
        
        Returns:
            True se l'invio è riuscito, False altrimenti
        """
        if not self.enabled:
            return False
        
        if not self.host:
            logger.warning("Syslog host non configurato")
            return False
        
        structured_data = extra_data.copy() if extra_data else {}
        if alert_type:
            structured_data['alert_type'] = alert_type.value
        
        try:
            sock = self._get_socket()
            if not sock:
                return False
            
            syslog_message = self._build_syslog_message(severity, message, structured_data)
            
            if self.protocol == 'tcp':
                # TCP richiede newline come terminatore
                sock.sendall(syslog_message + b'\n')
            else:
                sock.sendto(syslog_message, (self.host, self.port))
            
            logger.debug(f"Syslog inviato a {self.host}:{self.port}: {message[:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"✗ Errore invio Syslog: {e}")
            self._socket = None  # Reset socket per retry
            return False
    
    def close(self):
        """Chiude il socket"""
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
            self._socket = None


class AlertManager:
    """
    Gestore centralizzato degli alert.
    Supporta invio via SMTP, Syslog e Proxmox notification system.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Inizializza l'AlertManager con la configurazione.
        
        Config sections:
            smtp: configurazione email
            syslog: configurazione syslog
            alerts: configurazione alert (quali abilitare, soglie, etc.)
        """
        self.config = config
        self.alerts_config = config.get('alerts', {})
        
        # Inizializza i sender
        self.syslog_sender = SyslogSender(config)
        
        # Email sender viene importato quando necessario per evitare dipendenze circolari
        self._email_sender = None
        
        # Buffer per raggruppare alert (opzionale)
        self._alert_buffer: List[Dict] = []
        self._buffer_enabled = self.alerts_config.get('buffer_enabled', False)
        self._buffer_max_size = self.alerts_config.get('buffer_max_size', 10)
    
    @property
    def email_sender(self):
        """Lazy loading dell'email sender"""
        if self._email_sender is None:
            try:
                from email_sender import EmailSender
                self._email_sender = EmailSender(self.config)
            except ImportError:
                logger.warning("EmailSender non disponibile")
                self._email_sender = False
        return self._email_sender if self._email_sender else None
    
    def _should_send_alert(self, alert_type: AlertType, severity: AlertSeverity) -> Dict[str, bool]:
        """
        Determina quali canali devono ricevere l'alert basandosi sulla configurazione.
        
        Returns:
            Dict con chiavi 'email' e 'syslog' e valori booleani
        """
        result = {'email': False, 'syslog': False}
        
        # Configurazione globale
        min_severity_email = self.alerts_config.get('email_min_severity', 'warning')
        min_severity_syslog = self.alerts_config.get('syslog_min_severity', 'info')
        
        severity_map = {
            'emergency': 0, 'alert': 1, 'critical': 2, 'error': 3,
            'warning': 4, 'notice': 5, 'info': 6, 'debug': 7
        }
        
        # Verifica se la severità è sufficiente per ogni canale
        email_threshold = severity_map.get(min_severity_email.lower(), 4)
        syslog_threshold = severity_map.get(min_severity_syslog.lower(), 6)
        
        result['email'] = severity.value <= email_threshold
        result['syslog'] = severity.value <= syslog_threshold
        
        # Configurazione per tipo di alert specifico
        alert_specific = self.alerts_config.get(alert_type.value, {})
        if isinstance(alert_specific, dict):
            if 'email' in alert_specific:
                result['email'] = alert_specific['email']
            if 'syslog' in alert_specific:
                result['syslog'] = alert_specific['syslog']
        
        return result
    
    def send_alert(self, 
                   alert_type: AlertType,
                   severity: AlertSeverity,
                   title: str,
                   message: str,
                   details: Optional[Dict] = None,
                   force_immediate: bool = False) -> Dict[str, bool]:
        """
        Invia un alert a tutti i canali configurati.
        
        Args:
            alert_type: tipo di alert
            severity: livello di severità
            title: titolo breve dell'alert
            message: messaggio completo
            details: dettagli strutturati (opzionale)
            force_immediate: ignora il buffer e invia subito
        
        Returns:
            Dict con risultati per ogni canale {'email': bool, 'syslog': bool}
        """
        results = {'email': False, 'syslog': False}
        
        channels = self._should_send_alert(alert_type, severity)
        
        # Prepara dati strutturati
        structured_data = {
            'alert_type': alert_type.value,
            'severity': severity.name,
            'timestamp': datetime.now().isoformat(),
            'hostname': socket.gethostname(),
        }
        if details:
            structured_data.update(details)
        
        # Syslog
        if channels['syslog'] and self.syslog_sender.enabled:
            syslog_message = f"{title}: {message}"
            results['syslog'] = self.syslog_sender.send(
                severity, syslog_message, alert_type, structured_data
            )
            if results['syslog']:
                logger.info(f"  → Alert inviato via Syslog: {title}")
        
        # Email
        if channels['email'] and self.email_sender:
            # Costruisci HTML per email
            html_content = self._build_alert_email_html(
                alert_type, severity, title, message, details
            )
            subject = f"[Proxreporter {severity.name}] {title}"
            
            results['email'] = self.email_sender.send_report(html_content, subject)
            if results['email']:
                logger.info(f"  → Alert inviato via Email: {title}")
        
        return results
    
    def _build_alert_email_html(self, alert_type: AlertType, severity: AlertSeverity,
                                 title: str, message: str, 
                                 details: Optional[Dict] = None) -> str:
        """Costruisce il contenuto HTML per l'email di alert"""
        
        severity_colors = {
            AlertSeverity.EMERGENCY: '#8B0000',
            AlertSeverity.ALERT: '#FF0000',
            AlertSeverity.CRITICAL: '#DC143C',
            AlertSeverity.ERROR: '#FF4500',
            AlertSeverity.WARNING: '#FFA500',
            AlertSeverity.NOTICE: '#1E90FF',
            AlertSeverity.INFO: '#32CD32',
            AlertSeverity.DEBUG: '#808080',
        }
        
        color = severity_colors.get(severity, '#333333')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        hostname = socket.gethostname()
        
        details_html = ""
        if details:
            details_rows = "".join([
                f"<tr><td style='padding: 8px; border-bottom: 1px solid #ddd;'><strong>{k}</strong></td>"
                f"<td style='padding: 8px; border-bottom: 1px solid #ddd;'>{v}</td></tr>"
                for k, v in details.items()
            ])
            details_html = f"""
            <h3 style="color: #333; margin-top: 20px;">Dettagli</h3>
            <table style="width: 100%; border-collapse: collapse;">
                {details_rows}
            </table>
            """
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
                .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
                .header {{ background: {color}; color: white; padding: 20px; }}
                .header h1 {{ margin: 0; font-size: 1.5em; }}
                .header .badge {{ display: inline-block; background: rgba(255,255,255,0.2); padding: 4px 12px; border-radius: 20px; font-size: 0.8em; margin-top: 10px; }}
                .content {{ padding: 20px; }}
                .message {{ background: #f9f9f9; padding: 15px; border-radius: 5px; margin-bottom: 20px; border-left: 4px solid {color}; }}
                .footer {{ background: #f0f0f0; padding: 15px; font-size: 0.85em; color: #666; text-align: center; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>{title}</h1>
                    <span class="badge">{severity.name}</span>
                    <span class="badge">{alert_type.value}</span>
                </div>
                <div class="content">
                    <div class="message">
                        <p style="margin: 0;">{message}</p>
                    </div>
                    <p><strong>Host:</strong> {hostname}</p>
                    <p><strong>Timestamp:</strong> {timestamp}</p>
                    {details_html}
                </div>
                <div class="footer">
                    <p>Proxreporter Alert System</p>
                    <p>&copy; Domarc SRL</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return html
    
    # Metodi di convenienza per tipi comuni di alert
    
    def alert_backup_success(self, backup_file: str, size_mb: float, 
                              codcli: str, nomecliente: str) -> Dict[str, bool]:
        """Alert per backup completato con successo"""
        return self.send_alert(
            AlertType.BACKUP_SUCCESS,
            AlertSeverity.INFO,
            f"Backup completato - {nomecliente}",
            f"Il backup della configurazione Proxmox è stato completato con successo.",
            {
                'codcli': codcli,
                'nomecliente': nomecliente,
                'backup_file': backup_file,
                'size_mb': f"{size_mb:.2f}",
            }
        )
    
    def alert_backup_failure(self, error: str, codcli: str, nomecliente: str) -> Dict[str, bool]:
        """Alert per errore durante il backup"""
        return self.send_alert(
            AlertType.BACKUP_FAILURE,
            AlertSeverity.ERROR,
            f"Errore backup - {nomecliente}",
            f"Si è verificato un errore durante il backup della configurazione.",
            {
                'codcli': codcli,
                'nomecliente': nomecliente,
                'error': error,
            }
        )
    
    def alert_upload_success(self, files_count: int, destination: str,
                              codcli: str, nomecliente: str) -> Dict[str, bool]:
        """Alert per upload SFTP completato"""
        return self.send_alert(
            AlertType.UPLOAD_SUCCESS,
            AlertSeverity.INFO,
            f"Upload completato - {nomecliente}",
            f"Caricati {files_count} file su {destination}.",
            {
                'codcli': codcli,
                'nomecliente': nomecliente,
                'files_count': files_count,
                'destination': destination,
            }
        )
    
    def alert_upload_failure(self, error: str, destination: str,
                              codcli: str, nomecliente: str) -> Dict[str, bool]:
        """Alert per errore upload SFTP"""
        return self.send_alert(
            AlertType.UPLOAD_FAILURE,
            AlertSeverity.ERROR,
            f"Errore upload - {nomecliente}",
            f"Impossibile caricare i file su {destination}.",
            {
                'codcli': codcli,
                'nomecliente': nomecliente,
                'destination': destination,
                'error': error,
            }
        )
    
    def alert_storage_warning(self, storage_name: str, usage_percent: float,
                               threshold: float, hostname: str) -> Dict[str, bool]:
        """Alert per storage con utilizzo elevato"""
        return self.send_alert(
            AlertType.STORAGE_WARNING,
            AlertSeverity.WARNING,
            f"Storage {storage_name} quasi pieno",
            f"Lo storage {storage_name} su {hostname} ha raggiunto {usage_percent:.1f}% di utilizzo (soglia: {threshold}%).",
            {
                'storage': storage_name,
                'hostname': hostname,
                'usage_percent': f"{usage_percent:.1f}",
                'threshold_percent': f"{threshold:.0f}",
            }
        )
    
    def alert_report_generated(self, report_path: str, 
                                codcli: str, nomecliente: str,
                                vm_count: int, host_count: int) -> Dict[str, bool]:
        """Alert informativo per report generato"""
        return self.send_alert(
            AlertType.REPORT_GENERATED,
            AlertSeverity.INFO,
            f"Report generato - {nomecliente}",
            f"Report Proxmox generato con successo: {vm_count} VM, {host_count} host.",
            {
                'codcli': codcli,
                'nomecliente': nomecliente,
                'report_path': report_path,
                'vm_count': vm_count,
                'host_count': host_count,
            }
        )
    
    # =========================================================================
    # HARDWARE ALERTS
    # =========================================================================
    
    def alert_hardware_issue(self, component: str, device: str, status: str,
                              message: str, details: Dict[str, Any] = None,
                              hostname: str = "") -> Dict[str, bool]:
        """
        Alert generico per problemi hardware.
        
        Args:
            component: disk, memory, raid, temperature, kernel
            device: nome dispositivo (es. /dev/sda, cpu0, md0)
            status: critical o warning
            message: descrizione del problema
            details: dettagli aggiuntivi
            hostname: nome host
        """
        if not hostname:
            hostname = socket.gethostname()
        
        # Determina tipo alert e severità
        if status == "critical":
            alert_type = AlertType.HARDWARE_CRITICAL
            severity = AlertSeverity.CRITICAL
        else:
            alert_type = AlertType.HARDWARE_WARNING
            severity = AlertSeverity.WARNING
        
        # Mappa componente a tipo specifico se disponibile
        component_alert_map = {
            "disk": AlertType.DISK_ERROR,
            "raid": AlertType.RAID_DEGRADED,
            "memory": AlertType.MEMORY_ERROR,
            "temperature": AlertType.TEMPERATURE_WARNING,
            "kernel": AlertType.KERNEL_ERROR,
        }
        if component in component_alert_map:
            alert_type = component_alert_map[component]
        
        alert_details = {
            'component': component,
            'device': device,
            'hostname': hostname,
        }
        if details:
            alert_details.update(details)
        
        return self.send_alert(
            alert_type,
            severity,
            f"Hardware {component.upper()}: {device}",
            message,
            alert_details
        )
    
    def send_hardware_alerts(self, hardware_alerts: list, hostname: str = "") -> Dict[str, int]:
        """
        Invia tutti gli alert hardware rilevati.
        
        Args:
            hardware_alerts: Lista di HardwareAlert dal HardwareMonitor
            hostname: nome host
        
        Returns:
            Dict con conteggio alert inviati per canale
        """
        results = {'syslog': 0, 'email': 0, 'total': 0}
        
        for alert in hardware_alerts:
            status = "critical" if alert.status.value == "critical" else "warning"
            
            result = self.alert_hardware_issue(
                component=alert.component,
                device=alert.device,
                status=status,
                message=alert.message,
                details=alert.details,
                hostname=hostname
            )
            
            if result.get('syslog'):
                results['syslog'] += 1
            if result.get('email'):
                results['email'] += 1
            results['total'] += 1
        
        return results
    
    def close(self):
        """Chiude tutte le connessioni"""
        self.syslog_sender.close()
