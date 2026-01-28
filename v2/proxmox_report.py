#!/usr/bin/env python3
"""
Proxmox Local Report Generator
Versione che funziona direttamente sull'host Proxmox
Genera report CSV delle VM attive, caratteristiche host e cluster
Copia file su server remoto via SFTP in cartella codcli_nomecliente
Include backup configurazione nella stessa cartella

Utilizzo:
    python3 proxmox_report.py
    python3 proxmox_report.py --config custom_config.json
    python3 proxmox_report.py --no-sftp
"""

import os
import sys
import json
import csv
import subprocess
import socket
import platform
import re
from datetime import datetime, timedelta
from pathlib import Path
import ipaddress
import paramiko
import argparse
import tarfile
import shutil
import urllib.request
import urllib.parse
import urllib.error
import ssl
import logging
import time
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional, Tuple

# Logger initialization
logger = logging.getLogger("proxreporter")

# ============================================================================
# CONFIGURAZIONE
# ============================================================================

DEFAULT_CONFIG_FILE = "config.json"


def bytes_to_gib(value):
    """Converte byte in Gibibyte, restituisce None se non disponibile"""
    try:
        if value is None:
            return None
        return value / (1024 ** 3)
    except Exception:
        return None


def safe_round(value, digits=2):
    """Arrotonda un valore numerico gestendo None"""
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except Exception:
        return None


def seconds_to_human(seconds):
    """Converte secondi in formato leggibile (Xd Yh Zm Ws)"""
    try:
        if seconds is None:
            return None
        seconds = int(seconds)
        if seconds < 0:
            seconds = 0
        delta = timedelta(seconds=seconds)
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs or not parts:
            parts.append(f"{secs}s")
        return ' '.join(parts)
    except Exception:
        return None


def to_bool(value):
    """Converte un valore generico in boolean (Yes/No)"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', 'up')
    return None


def format_bool_for_csv(value):
    if value is None:
        return 'N/A'
    return 'Yes' if to_bool(value) else 'No'


def join_values(value, separator='; '):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return separator.join(str(v) for v in value if v not in (None, ''))
    return str(value)


def parse_manager_version_string(value: str) -> Tuple[str, Optional[str]]:
    if not value:
        return "", None
    cleaned = value.strip()
    kernel = None
    kernel_match = re.search(r"running kernel:\s*([^)]+)", cleaned, re.IGNORECASE)
    if kernel_match:
        kernel = kernel_match.group(1).strip()
    version = cleaned
    manager_match = re.search(r"pve-manager/([0-9A-Za-z.\-]+)", cleaned)
    if manager_match:
        version = manager_match.group(1)
    else:
        generic_match = re.search(r"\b\d+(?:\.\d+)+(?:-\d+)?\b", cleaned)
        if generic_match:
            version = generic_match.group(0)
    return version, kernel


def compute_cidr(address, netmask):
    if not address or not netmask:
        return None
    try:
        network = ipaddress.ip_network((address, netmask), strict=False)
        return f"{network.network_address}/{network.prefixlen}"
    except Exception:
        return None


def normalize_network_entries(entries):
    normalized: List[Dict[str, Any]] = []
    if not entries:
        return normalized

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        name = entry.get('iface') or entry.get('name')
        if not name:
            continue

        raw_type = (entry.get('type') or '').lower()
        category = 'other'
        if raw_type in ('network device', 'physical', 'eth', 'ethernet'):
            category = 'physical'
        elif 'bridge' in raw_type:
            category = 'bridge'
        elif 'bond' in raw_type:
            category = 'bond'
        elif 'vlan' in raw_type:
            category = 'vlan'
        else:
            # fallback su convenzioni nome interfaccia
            if name.startswith(('vmbr', 'br')):
                category = 'bridge'
            elif name.startswith('bond'):
                category = 'bond'
            elif '.' in name or name.startswith(('vlan', 'tap')):
                category = 'vlan'
            elif name.startswith(('eth', 'en', 'em', 'wl')):
                category = 'physical'

        members_value = entry.get('bridge_ports') or entry.get('ports') or entry.get('slaves')
        if isinstance(members_value, str):
            members_value = members_value.split()
        members = join_values(members_value)
        vlan_id = entry.get('bridge_vids') or entry.get('tag') or entry.get('vlan-id') or entry.get('vlan')
        cidr = entry.get('cidr') or compute_cidr(entry.get('address'), entry.get('netmask'))
        ip_addresses = join_values([addr for addr in [entry.get('address'), entry.get('address6')] if addr])

        normalized.append({
            'name': name,
            'type': entry.get('type', 'unknown'),
            'category': category,
            'active': to_bool(entry.get('active')),
            'state': 'up' if to_bool(entry.get('active')) else 'down',
            'mac_address': entry.get('hwaddr'),
            'ip': entry.get('address'),
            'ip6': entry.get('address6'),
            'ip_addresses': ip_addresses,
            'bridge': entry.get('bridge'),
            'members': members,
            'vlan_id': vlan_id,
            'cidr': cidr,
            'gateway': entry.get('gateway'),
            'gateway6': entry.get('gateway6'),
            'method': entry.get('method'),
            'method6': entry.get('method6'),
            'bond_mode': entry.get('bond_mode'),
            'bond_xmit_hash_policy': entry.get('bond_xmit_hash_policy'),
            'comment': entry.get('comments') or entry.get('comment'),
            'speed_mbps': entry.get('speed')  # valorizzato successivamente se disponibile
        })

    return normalized


def parse_interfaces_config(content):
    """Parsa /etc/network/interfaces e ritorna dizionario per interface"""
    configs = {}
    autos = set()
    current_iface = None
    if not content:
        return configs
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('auto '):
            parts = line.split()
            autos.update(parts[1:])
            continue
        if line.startswith('allow-hotplug '):
            parts = line.split()
            autos.update(parts[1:])
            continue
        if line.startswith('iface '):
            parts = line.split()
            if len(parts) >= 2:
                current_iface = parts[1]
                configs.setdefault(current_iface, {})
                if current_iface in autos:
                    configs[current_iface]['autostart'] = True
            continue
        if current_iface is None:
            continue
        if ' ' in line:
            key, value = line.split(None, 1)
        else:
            key, value = line, ''
        key = key.replace('-', '_')
        existing = configs.setdefault(current_iface, {})
        existing[key] = value.strip()
    for iface in autos:
        configs.setdefault(iface, {})['autostart'] = True
    return configs

# ============================================================================
# CLASSE PROXMOX LOCAL EXTRACTOR
# ============================================================================

class ProxmoxLocalExtractor:
    """Estrattore dati da Proxmox VE usando comandi locali o SSH"""
    
    def __init__(self, config=None, features=None):
        self.config = config
        self.features = features or {}
        self.hostname = None
        self.cluster_info = {}
        self.node_info = {}
        self.vms_data = []
        self.is_proxmox_host = False
        self.ssh_client = None
        self.execution_mode = 'local'  # 'local', 'ssh', 'api'
    
    def detect_execution_mode(self):
        """Rileva se siamo su host Proxmox o remoto"""
        logger.info("→ Rilevamento modalità esecuzione...")
        
        # Verifica se siamo su host Proxmox
        proxmox_indicators = [
            '/etc/pve',  # Directory Proxmox
            '/usr/bin/pvesh',  # Comando pvesh
            '/usr/bin/qm',  # Comando qm
        ]
        
        for indicator in proxmox_indicators:
            if os.path.exists(indicator):
                self.is_proxmox_host = True
                self.execution_mode = 'local'
                logger.info(f"  ✓ Rilevato host Proxmox (indicatore: {indicator})")
                return 'local'
        
        # Se non siamo su Proxmox, verifica se abbiamo configurazione SSH
        ssh_config = self.config.get('ssh', {})
        if ssh_config.get('host') and ssh_config.get('username'):
            self.execution_mode = 'ssh'
            logger.info(f"  ✓ Modalità SSH remota (host: {ssh_config.get('host')})")
            return 'ssh'
        
        # Fallback all'API
        self.execution_mode = 'api'
        logger.info(f"  ✓ Modalità API remota")
        return 'api'
    
    def connect_ssh(self):
        """Connette via SSH al server Proxmox"""
        ssh_config = self.config.get('ssh', {})
        
        if not ssh_config.get('host'):
            return False
        
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            host = ssh_config.get('host')
            port = ssh_config.get('port', 22)
            username = ssh_config.get('username', 'root')
            password = ssh_config.get('password', '')
            
            logger.info(f"  → Connessione SSH a {host}:{port}...")
            self.ssh_client.connect(host, port=port, username=username, password=password, timeout=30)
            logger.info(f"  ✓ Connessione SSH stabilita")
            return True
        except Exception as e:
            logger.info(f"  ✗ Errore connessione SSH: {e}")
            return False
    
    def execute_remote_command(self, command):
        """Esegue comando remoto via SSH"""
        if not self.ssh_client:
            return None
        
        try:
            stdin, stdout, stderr = self.ssh_client.exec_command(command, timeout=30)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status == 0:
                return stdout.read().decode('utf-8').strip()
            else:
                error = stderr.read().decode('utf-8').strip()
                cmd_preview = command[:60] + '...' if len(command) > 60 else command
                if error:
                    logger.info(f"  ⚠ Errore comando '{cmd_preview}': {error}")
                else:
                    logger.info(f"  ⚠ Comando '{cmd_preview}' fallito (exit code: {exit_status})")
                return None
        except Exception as e:
            cmd_preview = command[:60] + '...' if len(command) > 60 else command
            logger.info(f"  ⚠ Errore esecuzione comando '{cmd_preview}': {e}")
            return None
    
    def execute_local_command(self, command):
        """Esegue comando locale"""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                cmd_preview = command[:60] + '...' if len(command) > 60 else command
                if result.stderr:
                    logger.info(f"  ⚠ Errore comando '{cmd_preview}': {result.stderr.strip()}")
                else:
                    logger.info(f"  ⚠ Comando '{cmd_preview}' fallito (exit code: {result.returncode})")
                return None
        except subprocess.TimeoutExpired:
            cmd_preview = command[:60] + '...' if len(command) > 60 else command
            logger.info(f"  ⚠ Timeout comando '{cmd_preview}' (30s)")
            return None
        except Exception as e:
            cmd_preview = command[:60] + '...' if len(command) > 60 else command
            logger.info(f"  ⚠ Errore esecuzione comando '{cmd_preview}': {e}")
            return None
    
    def execute_command(self, command):
        """Esegue comando in base alla modalità"""
        if self.execution_mode == 'ssh':
            return self.execute_remote_command(command)
        else:
            return self.execute_local_command(command)

    def _guess_interface_category(self, name: str, entry: Optional[Dict[str, Any]] = None) -> str:
        if entry and entry.get('category'):
            return entry['category']
        iface_type = (entry.get('type') if entry else None) or ''
        iface_type = iface_type.lower() if isinstance(iface_type, str) else ''
        lower_name = name.lower()
        if iface_type in ('network device', 'physical', 'eth', 'ethernet'):
            return 'physical'
        if 'bridge' in iface_type or lower_name.startswith(('vmbr', 'br')):
            return 'bridge'
        if 'bond' in iface_type or lower_name.startswith('bond'):
            return 'bond'
        if 'vlan' in iface_type or '.' in name or lower_name.startswith('vlan'):
            return 'vlan'
        if lower_name.startswith(('eth', 'en', 'em', 'wl')):
            return 'physical'
        return 'other'

    def _get_interface_speed(self, iface: str, executor) -> Optional[str]:
        """Prova a determinare la velocità dell'interfaccia in Mbps"""
        try:
            speed_output = executor(f'cat /sys/class/net/{iface}/speed 2>/dev/null')
            if speed_output:
                speed_output = speed_output.strip()
                if speed_output.isdigit():
                    return speed_output
        except Exception:
            pass
        try:
            ethtool_output = executor(f'ethtool {iface} 2>/dev/null')
            if ethtool_output:
                match = re.search(r'Speed:\s*([\d\.]+)\s*([A-Za-z]+)', ethtool_output)
                if match:
                    value = match.group(1)
                    unit = match.group(2).lower()
                    try:
                        numeric = float(value)
                        if unit.startswith('g'):
                            return str(int(numeric * 1000))
                        if unit.startswith('m'):
                            return str(int(numeric))
                        if unit.startswith('k'):
                            return str(int(numeric / 1000))
                    except Exception:
                        return match.group(0).replace('Speed:', '').strip()
        except Exception:
            pass
        return None

    def fetch_network_entries_via_pvesh(self, node_name):
        """Recupera network info via pvesh (locale o SSH)"""
        if not node_name:
            return []
        try:
            command = f"pvesh get /nodes/{node_name}/network --output-format json"
            output = None
            if self.execution_mode == 'ssh':
                output = self.execute_command(command)
            else:
                result = subprocess.run(
                    ['pvesh', 'get', f'/nodes/{node_name}/network', '--output-format', 'json'],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if result.returncode == 0:
                    output = result.stdout
            if output:
                data = json.loads(output)
                if isinstance(data, dict) and 'data' in data:
                    data = data['data']
                return normalize_network_entries(data)
        except FileNotFoundError:
            logger.info("      ⚠ pvesh non trovato, impossibile ottenere configurazione network")
        except Exception as e:
            logger.info(f"      ⚠ Errore lettura network nodo {node_name}: {e}")
        return []
    
    def get_hostname(self):
        """Ottiene hostname del sistema"""
        try:
            self.hostname = socket.gethostname()
            return self.hostname
        except:
            return "unknown"
    
    def get_node_info(self):
        """Ottiene informazioni sul nodo Proxmox"""
        logger.info("→ Estrazione informazioni nodo...")
        
        node_info = {
            'hostname': None,
            'os': platform.system(),
            'os_release': platform.release(),
            'cpu_count': os.cpu_count() if self.execution_mode == 'local' else None,
            'memory_total': None,
            'disk_info': []
        }
        
        # Hostname
        if self.execution_mode == 'ssh':
            hostname = self.execute_command('hostname')
            node_info['hostname'] = hostname if hostname else 'unknown'
        else:
            node_info['hostname'] = self.get_hostname()
        
        # Memoria totale
        if self.execution_mode == 'ssh':
            meminfo = self.execute_command('grep MemTotal /proc/meminfo')
            if meminfo:
                try:
                    mem_kb = int(meminfo.split()[1])
                    node_info['memory_total'] = mem_kb * 1024
                except:
                    pass
        else:
            try:
                with open('/proc/meminfo', 'r') as f:
                    for line in f:
                        if line.startswith('MemTotal:'):
                            node_info['memory_total'] = int(line.split()[1]) * 1024
                            break
            except:
                pass
        
        # Informazioni disco
        if self.execution_mode == 'ssh':
            df_output = self.execute_command('df -h /')
            if df_output:
                lines = df_output.split('\n')
                if len(lines) > 1:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        node_info['disk_info'].append({
                            'filesystem': parts[0],
                            'size': parts[1],
                            'used': parts[2],
                            'available': parts[3],
                            'mount': parts[5] if len(parts) > 5 else '/'
                        })
        else:
            try:
                result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')
                    if len(lines) > 1:
                        parts = lines[1].split()
                        if len(parts) >= 4:
                            node_info['disk_info'].append({
                                'filesystem': parts[0],
                                'size': parts[1],
                                'used': parts[2],
                                'available': parts[3],
                                'mount': parts[5] if len(parts) > 5 else '/'
                            })
            except:
                pass
        
        # Informazioni CPU
        if self.execution_mode == 'ssh':
            cpuinfo = self.execute_command('grep "model name" /proc/cpuinfo | head -1')
            if cpuinfo:
                model_match = re.search(r'model name\s*:\s*(.+)', cpuinfo)
                if model_match:
                    node_info['cpu_model'] = model_match.group(1).strip()
            cpu_count = self.execute_command('nproc')
            if cpu_count:
                try:
                    node_info['cpu_count'] = int(cpu_count)
                except:
                    pass
        else:
            try:
                with open('/proc/cpuinfo', 'r') as f:
                    cpu_info = f.read()
                    model_match = re.search(r'model name\s*:\s*(.+)', cpu_info)
                    if model_match:
                        node_info['cpu_model'] = model_match.group(1).strip()
            except:
                pass
        
        self.node_info = node_info
        logger.info(f"  ✓ Hostname: {node_info['hostname']}")
        if node_info['cpu_count']:
            logger.info(f"  ✓ CPU cores: {node_info['cpu_count']}")
        if node_info['memory_total']:
            mem_gb = node_info['memory_total'] / (1024**3)
            logger.info(f"  ✓ Memoria totale: {mem_gb:.2f} GB")
        
        return node_info
    
    def get_cluster_info(self):
        """Ottiene informazioni sul cluster Proxmox"""
        logger.info("→ Estrazione informazioni cluster...")
        
        cluster_info = {
            'is_cluster': False,
            'cluster_name': None,
            'nodes': []
        }
        
        # Verifica se è un cluster
        corosync_conf = '/etc/pve/corosync.conf'
        
        if self.execution_mode == 'ssh':
            # Leggi via SSH
            content = self.execute_command(f'cat {corosync_conf} 2>/dev/null')
            if content:
                try:
                    # Cerca nome cluster
                    name_match = re.search(r'name:\s*(\S+)', content)
                    if name_match:
                        cluster_info['cluster_name'] = name_match.group(1)
                    
                    # Cerca nodi
                    node_matches = re.findall(r'ring0_addr:\s*(\S+)', content)
                    if node_matches:
                        cluster_info['is_cluster'] = True
                        cluster_info['nodes'] = list(set(node_matches))
                        logger.info(f"  ✓ Cluster: {cluster_info['cluster_name']}")
                        logger.info(f"  ✓ Nodi: {', '.join(cluster_info['nodes'])}")
                    else:
                        logger.info("  ℹ Nodo standalone (non cluster)")
                except Exception as e:
                    logger.info(f"  ⚠ Errore parsing corosync.conf: {e}")
            else:
                logger.info("  ℹ Nodo standalone (non cluster)")
        else:
            # Leggi localmente
            if os.path.exists(corosync_conf):
                try:
                    with open(corosync_conf, 'r') as f:
                        content = f.read()
                        
                        # Cerca nome cluster
                        name_match = re.search(r'name:\s*(\S+)', content)
                        if name_match:
                            cluster_info['cluster_name'] = name_match.group(1)
                        
                        # Cerca nodi
                        node_matches = re.findall(r'ring0_addr:\s*(\S+)', content)
                        if node_matches:
                            cluster_info['is_cluster'] = True
                            cluster_info['nodes'] = list(set(node_matches))
                            logger.info(f"  ✓ Cluster: {cluster_info['cluster_name']}")
                            logger.info(f"  ✓ Nodi: {', '.join(cluster_info['nodes'])}")
                        else:
                            logger.info("  ℹ Nodo standalone (non cluster)")
                except Exception as e:
                    logger.info(f"  ⚠ Errore lettura corosync.conf: {e}")
            else:
                logger.info("  ℹ Nodo standalone (non cluster)")
        
        self.cluster_info = cluster_info
        return cluster_info
    
    def fetch_nodes_via_pvesh(self):
        """Recupera elenco nodi cluster via pvesh (locale o SSH)"""
        output = None
        try:
            command = "pvesh get /nodes --output-format json"
            if self.execution_mode == 'ssh':
                output = self.execute_command(command)
            else:
                result = subprocess.run(
                    ['pvesh', 'get', '/nodes', '--output-format', 'json'],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if result.returncode == 0:
                    output = result.stdout
            if output:
                data = json.loads(output)
                if isinstance(data, dict) and 'data' in data:
                    data = data['data']
                if isinstance(data, list):
                    return data
        except FileNotFoundError:
            logger.info("  ⚠ pvesh non trovato, impossibile ottenere elenco nodi")
        except Exception as e:
            logger.info(f"  ⚠ Errore lettura nodi via pvesh: {e}")
        return []

    def get_all_hosts_info(self):
        """Ottiene informazioni dettagliate per tutti i nodi rilevati"""
        logger.info("→ Estrazione informazioni dettagliate degli host del cluster...")

        all_hosts_info: List[Dict[str, Any]] = []
        discovered_nodes: List[str] = []

        local_hostname = self.node_info.get('hostname') or self.get_hostname()
        if local_hostname:
            discovered_nodes.append(local_hostname)

        # Se siamo in grado di interrogare il cluster, aggiungi anche gli altri nodi
        if feature_enabled(self.features, 'collect_cluster', True) and self.execution_mode in ('local', 'ssh'):
            nodes_data = self.fetch_nodes_via_pvesh()
            for node in nodes_data:
                node_name = node.get('node')
                if node_name and node_name not in discovered_nodes:
                    discovered_nodes.append(node_name)
            if len(discovered_nodes) > 1:
                logger.info(f"  ℹ Nodi cluster rilevati: {', '.join(discovered_nodes)}")
        else:
            if self.execution_mode not in ('local', 'ssh'):
                logger.info("  ℹ Modalità API: raccolta limitata alle informazioni generiche")

        # Aggiorna cluster_info per il report riassuntivo
        self.cluster_info.setdefault('nodes', discovered_nodes if discovered_nodes else [local_hostname])

        # Estrai informazioni per ogni nodo individuato (local first)
        for node_name in discovered_nodes:
            info = self.get_detailed_host_info_for_node(node_name)
            if info:
                all_hosts_info.append(info)
                cpu_summary = info.get('cpu_total_cores') or info.get('cpu_cores') or 'N/A'
                mem_total = info.get('memory_total_gb')
                mem_summary = f"{mem_total:.2f} GiB" if isinstance(mem_total, (int, float)) else 'N/A'
                logger.info(f"  ✓ Host {info.get('hostname')}: CPU {cpu_summary}, RAM {mem_summary}, Storage: {len(info.get('storage', []))} storage")
            else:
                logger.info(f"  ⚠ Impossibile ottenere informazioni host {node_name}")

        return all_hosts_info
    
    def get_detailed_host_info_via_api(self, node_name, api_get):
        """Ottiene informazioni dettagliate host via API Proxmox"""
        host_info = {
            'hostname': node_name,
            'status': None,
            'uptime_seconds': None,
            'uptime_human': None,
            'proxmox_version': None,
            'manager_version': None,
            'kernel_version': None,
            'cpu_model': None,
            'cpu_cores': None,
            'cpu_sockets': None,
            'cpu_threads': None,
            'cpu_total_cores': None,
            'cpu_usage_percent': None,
            'io_delay_percent': None,
            'load_average_1m': None,
            'load_average_5m': None,
            'load_average_15m': None,
            'memory_total_gb': None,
            'memory_used_gb': None,
            'memory_free_gb': None,
            'memory_usage_percent': None,
            'ksm_sharing_gb': None,
            'swap_total_gb': None,
            'swap_used_gb': None,
            'swap_free_gb': None,
            'swap_usage_percent': None,
            'rootfs_total_gb': None,
            'rootfs_used_gb': None,
            'rootfs_free_gb': None,
            'rootfs_usage_percent': None,
            'storage': [],
            'network_interfaces': [],
            'license_status': None,
            'license_message': None,
            'license_level': None,
            'subscription_type': None,
            'subscription_key': None,
            'subscription_server_id': None,
            'subscription_sockets': None,
            'subscription_last_check': None,
            'subscription_next_due': None,
            'lic_status': None,
            'lic_key': None,
            'lic_level': None,
            'lic_type': None,
            'lic_scad': None,
            'lic_sub': None,
            'lic_sock': None,
            'lic_regdate': None,
            'repository_status': None,
            'boot_mode': None
        }
        
        try:
            # Info nodo completo
            node_info = api_get(f'nodes/{node_name}/status')
            if node_info:
                host_info['status'] = node_info.get('status')
                host_info['uptime_seconds'] = node_info.get('uptime')
                host_info['uptime_human'] = seconds_to_human(host_info['uptime_seconds'])
                
                maxcpu = node_info.get('maxcpu')
                if maxcpu is not None:
                    host_info['cpu_total_cores'] = maxcpu
                
                cpu_usage = node_info.get('cpu')
                if cpu_usage is not None:
                    host_info['cpu_usage_percent'] = safe_round(cpu_usage * 100, 2)
                
                io_delay = node_info.get('io_delay')
                if io_delay is not None:
                    host_info['io_delay_percent'] = safe_round(io_delay, 2)
                
                loadavg = node_info.get('loadavg')
                if isinstance(loadavg, (list, tuple)) and len(loadavg) >= 3:
                    host_info['load_average_1m'] = safe_round(float(loadavg[0]), 2)
                    host_info['load_average_5m'] = safe_round(float(loadavg[1]), 2)
                    host_info['load_average_15m'] = safe_round(float(loadavg[2]), 2)
                elif isinstance(loadavg, str):
                    parts = re.split(r'[,\s]+', loadavg.strip())
                    if len(parts) >= 3:
                        try:
                            host_info['load_average_1m'] = safe_round(float(parts[0]), 2)
                            host_info['load_average_5m'] = safe_round(float(parts[1]), 2)
                            host_info['load_average_15m'] = safe_round(float(parts[2]), 2)
                        except Exception:
                            pass
                
                # CPU info
                if 'cpuinfo' in node_info:
                    cpuinfo = node_info['cpuinfo']
                    host_info['cpu_cores'] = cpuinfo.get('cores', None)
                    host_info['cpu_sockets'] = cpuinfo.get('sockets', None)
                    host_info['cpu_model'] = cpuinfo.get('model', None)
                    host_info['cpu_threads'] = cpuinfo.get('cpus', None)  # Thread totali
                    if host_info['cpu_threads'] and not host_info['cpu_total_cores']:
                        host_info['cpu_total_cores'] = host_info['cpu_threads']
                
                # Memory
                if 'memory' in node_info:
                    mem = node_info['memory']
                    mem_total = mem.get('total', 0)
                    mem_used = mem.get('used', 0)
                    mem_free = mem.get('free', 0)
                    mem_total_gb = bytes_to_gib(mem_total)
                    mem_used_gb = bytes_to_gib(mem_used)
                    mem_free_gb = bytes_to_gib(mem_free)
                    if mem_total_gb is not None:
                        host_info['memory_total_gb'] = mem_total_gb
                    if mem_used_gb is not None:
                        host_info['memory_used_gb'] = mem_used_gb
                    elif mem_total_gb is not None and mem_free_gb is not None:
                        host_info['memory_used_gb'] = max(mem_total_gb - mem_free_gb, 0)
                    if mem_free_gb is not None:
                        host_info['memory_free_gb'] = mem_free_gb
                    if mem_total and mem_used is not None:
                        host_info['memory_usage_percent'] = safe_round((mem_used / mem_total) * 100, 2)
                    elif mem_total and mem_free is not None:
                        host_info['memory_usage_percent'] = safe_round(((mem_total - mem_free) / mem_total) * 100, 2)
                
                # KSM
                ksm = node_info.get('ksm', {})
                if isinstance(ksm, dict):
                    shared = bytes_to_gib(ksm.get('shared'))
                    if shared is not None:
                        host_info['ksm_sharing_gb'] = shared
                
                # Swap
                swap = node_info.get('swap', {})
                if isinstance(swap, dict):
                    swap_total = bytes_to_gib(swap.get('total'))
                    swap_used = bytes_to_gib(swap.get('used'))
                    swap_free = bytes_to_gib(swap.get('free'))
                    if swap_total is not None:
                        host_info['swap_total_gb'] = swap_total
                    if swap_used is not None:
                        host_info['swap_used_gb'] = swap_used
                    if swap_free is not None:
                        host_info['swap_free_gb'] = swap_free
                    if swap.get('total'):
                        used = swap.get('used')
                        if used is not None:
                            host_info['swap_usage_percent'] = safe_round((used / swap['total']) * 100, 2)
                
                # Root filesystem
                rootfs = node_info.get('rootfs', {})
                if isinstance(rootfs, dict):
                    root_total = bytes_to_gib(rootfs.get('total'))
                    root_used = bytes_to_gib(rootfs.get('used'))
                    root_free = bytes_to_gib(rootfs.get('free'))
                    if root_total is not None:
                        host_info['rootfs_total_gb'] = root_total
                    if root_used is not None:
                        host_info['rootfs_used_gb'] = root_used
                    if root_free is not None:
                        host_info['rootfs_free_gb'] = root_free
                    if rootfs.get('total') and rootfs.get('used') is not None:
                        host_info['rootfs_usage_percent'] = safe_round((rootfs['used'] / rootfs['total']) * 100, 2)
            
            # Versione
            try:
                version_info = api_get(f'nodes/{node_name}/version')
                if version_info:
                    manager_version = version_info.get('version')
                    if manager_version:
                        parsed_version, kernel_from_manager = parse_manager_version_string(manager_version)
                        host_info['manager_version'] = parsed_version
                        if not host_info['proxmox_version']:
                            host_info['proxmox_version'] = parsed_version
                        if kernel_from_manager and not host_info.get('kernel_version'):
                            host_info['kernel_version'] = kernel_from_manager
                    kernel_version = version_info.get('kernel') or version_info.get('running_kernel') or version_info.get('release')
                    if kernel_version:
                        host_info['kernel_version'] = kernel_version.strip()
            except:
                pass
            
            # Stato repository
            try:
                repos_info = api_get(f'nodes/{node_name}/apt/repositories')
                repo_entries = []
                repositories = []
                if isinstance(repos_info, dict):
                    repositories = repos_info.get('repositories') or repos_info.get('data') or []
                elif isinstance(repos_info, list):
                    repositories = repos_info
                for repo in repositories:
                    if not isinstance(repo, dict):
                        continue
                    name = repo.get('name') or repo.get('handle') or repo.get('description') or 'repository'
                    enabled = repo.get('enabled')
                    status = repo.get('status')
                    entry = name
                    if enabled is not None:
                        entry += f" [{'enabled' if enabled else 'disabled'}]"
                    if status:
                        entry += f" - {status}"
                    repo_entries.append(entry)
                if repo_entries:
                    host_info['repository_status'] = '; '.join(repo_entries)
            except Exception:
                pass
            
            # Informazioni licenza/subscription
            try:
                subscription_info = api_get(f'nodes/{node_name}/subscription')
                if isinstance(subscription_info, dict):
                    logger.info(f"  → Subscription info ricevuta via API per {node_name}")
                    # Usa schema corretto lic_*
                    host_info['license_status'] = subscription_info.get('status')
                    host_info['license_message'] = subscription_info.get('message')
                    host_info['license_level'] = subscription_info.get('level')
                    
                    # status -> lic_status
                    host_info['lic_status'] = subscription_info.get('status')
                    # key -> lic_key
                    host_info['lic_key'] = subscription_info.get('key')
                    # level -> lic_level
                    host_info['lic_level'] = subscription_info.get('level')
                    # productname -> lic_type
                    host_info['lic_type'] = subscription_info.get('productname') or subscription_info.get('type')
                    # serverid -> lic_sub
                    host_info['lic_sub'] = subscription_info.get('serverid')
                    # sockets -> lic_sock
                    host_info['lic_sock'] = subscription_info.get('sockets')
                    
                    # nextduedate -> lic_scad
                    next_due = subscription_info.get('nextduedate') or subscription_info.get('nextdue')
                    if next_due:
                        host_info['lic_scad'] = str(next_due)
                    
                    # regdate -> lic_regdate
                    reg_date = subscription_info.get('regdate')
                    if reg_date:
                        host_info['lic_regdate'] = str(reg_date)
                    
                    logger.info(f"  → lic_status: {host_info.get('lic_status')}, lic_key: {host_info.get('lic_key')}")
            except Exception as e:
                logger.info(f"  ⚠ Errore lettura subscription via API: {e}")
            
            # Storage - IMPORTANTE: estrai per ogni nodo
            try:
                storage_list = api_get(f'nodes/{node_name}/storage')
                if storage_list:
                    for storage in storage_list:
                        storage_name = storage.get('storage', 'N/A')
                        storage_info = {
                            'name': storage_name,
                            'type': storage.get('type', 'N/A'),
                            'status': storage.get('status', 'N/A'),
                            'total_gb': None,
                            'used_gb': None,
                            'available_gb': None,
                            'content': storage.get('content', 'N/A')
                        }
                        
                        # Dettagli storage per questo nodo specifico
                        try:
                            storage_details = api_get(f'nodes/{node_name}/storage/{storage_name}/status')
                            if storage_details:
                                if storage_details.get('total'):
                                    storage_info['total_gb'] = storage_details.get('total') / (1024**3)
                                if storage_details.get('used'):
                                    storage_info['used_gb'] = storage_details.get('used') / (1024**3)
                                if storage_details.get('avail'):
                                    storage_info['available_gb'] = storage_details.get('avail') / (1024**3)
                        except Exception as e:
                            logger.info(f"      ⚠ Errore dettagli storage {storage_name}: {e}")
                        
                        host_info['storage'].append(storage_info)
            except Exception as e:
                logger.info(f"      ⚠ Errore lettura storage nodo {node_name}: {e}")
            # Network interfaces via API
            try:
                network_data = api_get(f'nodes/{node_name}/network')
                normalized_network = normalize_network_entries(network_data)
                if normalized_network:
                    host_info['network_interfaces'] = normalized_network
            except Exception as e:
                logger.info(f"      ⚠ Errore rete nodo {node_name}: {e}")
            
        except Exception as e:
            logger.info(f"      ⚠ Errore estrazione info nodo {node_name}: {e}")
            return None
        
        if not host_info.get('network_interfaces'):
            network_entries = self.fetch_network_entries_via_pvesh(node_name)
            if network_entries:
                host_info['network_interfaces'] = network_entries
        
        return host_info

    def enrich_host_info_with_commands(self, host_info, executor):
        """Integra informazioni host utilizzando comandi locali/SSH"""
        collect_host_details = feature_enabled(self.features, 'collect_host_details', True)
        collect_network = feature_enabled(self.features, 'collect_network', True)
        if not callable(executor) or (not collect_host_details and not collect_network):
            return
        
        if collect_host_details:
            if host_info.get('uptime_seconds') is None:
                try:
                    uptime_output = executor('cat /proc/uptime 2>/dev/null')
                    if uptime_output:
                        uptime_seconds = float(uptime_output.strip().split()[0])
                        host_info['uptime_seconds'] = int(uptime_seconds)
                        host_info['uptime_human'] = seconds_to_human(host_info['uptime_seconds'])
                except Exception:
                    pass
            elif not host_info.get('uptime_human'):
                host_info['uptime_human'] = seconds_to_human(host_info.get('uptime_seconds'))

            if host_info.get('load_average_1m') is None:
                try:
                    loadavg_output = executor('cat /proc/loadavg 2>/dev/null')
                    if loadavg_output:
                        parts = loadavg_output.strip().split()
                        if len(parts) >= 3:
                            host_info['load_average_1m'] = safe_round(float(parts[0]), 2)
                            host_info['load_average_5m'] = safe_round(float(parts[1]), 2)
                            host_info['load_average_15m'] = safe_round(float(parts[2]), 2)
                except Exception:
                    pass

            try:
                meminfo_output = executor('cat /proc/meminfo 2>/dev/null')
            except Exception:
                meminfo_output = None
            if meminfo_output:
                try:
                    mem_total_match = re.search(r'MemTotal:\s+(\d+)', meminfo_output)
                    mem_free_match = re.search(r'MemFree:\s+(\d+)', meminfo_output)
                    mem_available_match = re.search(r'MemAvailable:\s+(\d+)', meminfo_output)
                    swap_total_match = re.search(r'SwapTotal:\s+(\d+)', meminfo_output)
                    swap_free_match = re.search(r'SwapFree:\s+(\d+)', meminfo_output)
                    if host_info.get('memory_total_gb') is None and mem_total_match:
                        host_info['memory_total_gb'] = int(mem_total_match.group(1)) / 1024 / 1024
                    if host_info.get('memory_free_gb') is None:
                        if mem_available_match:
                            host_info['memory_free_gb'] = int(mem_available_match.group(1)) / 1024 / 1024
                        elif mem_free_match:
                            host_info['memory_free_gb'] = int(mem_free_match.group(1)) / 1024 / 1024
                    if host_info.get('memory_used_gb') is None and host_info.get('memory_total_gb') is not None and host_info.get('memory_free_gb') is not None:
                        host_info['memory_used_gb'] = max(host_info['memory_total_gb'] - host_info['memory_free_gb'], 0)
                    if host_info.get('memory_usage_percent') is None and host_info.get('memory_total_gb') and host_info.get('memory_used_gb') is not None:
                        try:
                            host_info['memory_usage_percent'] = safe_round((host_info['memory_used_gb'] / host_info['memory_total_gb']) * 100, 2)
                        except Exception:
                            pass
                    if swap_total_match and host_info.get('swap_total_gb') is None:
                        host_info['swap_total_gb'] = int(swap_total_match.group(1)) / 1024 / 1024
                    if swap_free_match and host_info.get('swap_free_gb') is None:
                        host_info['swap_free_gb'] = int(swap_free_match.group(1)) / 1024 / 1024
                    if host_info.get('swap_total_gb') is not None and host_info.get('swap_free_gb') is not None:
                        host_info['swap_used_gb'] = host_info['swap_total_gb'] - host_info['swap_free_gb']
                        try:
                            host_info['swap_usage_percent'] = safe_round((host_info['swap_used_gb'] / host_info['swap_total_gb']) * 100, 2) if host_info['swap_total_gb'] else None
                        except Exception:
                            pass
                except Exception:
                    pass

            if host_info.get('ksm_sharing_gb') is None:
                try:
                    ksm_output = executor('cat /sys/kernel/mm/ksm/pages_sharing 2>/dev/null')
                    if ksm_output:
                        pages = int(ksm_output.strip())
                        host_info['ksm_sharing_gb'] = (pages * 4096) / (1024 ** 3)
                except Exception:
                    pass

            if host_info.get('rootfs_total_gb') is None:
                try:
                    rootfs_output = executor('df -B1 / 2>/dev/null | tail -1')
                    if rootfs_output:
                        parts = rootfs_output.split()
                        if len(parts) >= 5:
                            total = float(parts[1])
                            used = float(parts[2])
                            free = float(parts[3])
                            host_info['rootfs_total_gb'] = total / (1024 ** 3)
                            host_info['rootfs_used_gb'] = used / (1024 ** 3)
                            host_info['rootfs_free_gb'] = free / (1024 ** 3)
                            if total:
                                host_info['rootfs_usage_percent'] = safe_round((used / total) * 100, 2)
                except Exception:
                    pass

            if host_info.get('cpu_total_cores') is None:
                try:
                    nproc_output = executor('nproc 2>/dev/null')
                    if nproc_output:
                        host_info['cpu_total_cores'] = int(nproc_output.strip())
                except Exception:
                    pass

            if host_info.get('kernel_version') is None:
                try:
                    kernel_output = executor('uname -r 2>/dev/null')
                    if kernel_output:
                        host_info['kernel_version'] = kernel_output.strip()
                except Exception:
                    pass

            if host_info.get('manager_version') is None or host_info.get('proxmox_version') is None:
                try:
                    manager_output = executor('pveversion 2>/dev/null | head -1')
                    if manager_output:
                        manager_output = manager_output.strip()
                        parsed_version, kernel_from_manager = parse_manager_version_string(manager_output)
                        host_info.setdefault('manager_version', parsed_version)
                        if not host_info.get('proxmox_version'):
                            host_info['proxmox_version'] = parsed_version
                        if kernel_from_manager and not host_info.get('kernel_version'):
                            host_info['kernel_version'] = kernel_from_manager
                except Exception:
                    pass

            if host_info.get('boot_mode') is None:
                try:
                    boot_mode_output = executor('[ -d /sys/firmware/efi ] && echo EFI || echo BIOS')
                    if boot_mode_output:
                        boot_mode = boot_mode_output.strip()
                        secure_output = executor('mokutil --sb-state 2>/dev/null')
                        if secure_output and 'enabled' in secure_output.lower():
                            boot_mode += ' (Secure Boot)'
                        elif secure_output and 'disabled' in secure_output.lower():
                            boot_mode += ' (Secure Boot disabled)'
                        host_info['boot_mode'] = boot_mode
                except Exception:
                    pass

            # Estrai informazioni sulla licenza/subscription
            try:
                sub_output = executor('pvesubscription get 2>/dev/null')
                if sub_output:
                    logger.info(f"  → Output pvesubscription get ricevuto ({len(sub_output)} caratteri)")
                    # Parse output di pvesubscription get
                    sub_data = {}
                    for line in sub_output.splitlines():
                        line = line.strip()
                        if ':' in line:
                            key, value = line.split(':', 1)
                            key = key.strip().lower().replace(' ', '_')
                            value = value.strip()
                            sub_data[key] = value
                    
                    logger.info(f"  → Parsed {len(sub_data)} campi subscription")
                    
                    # Estrai campi principali con schema corretto
                    # status -> lic_status
                    if sub_data.get('status'):
                        host_info['lic_status'] = sub_data['status']
                        logger.info(f"  → lic_status: {sub_data['status']}")
                    # key -> lic_key
                    if sub_data.get('key'):
                        host_info['lic_key'] = sub_data['key']
                    # level -> lic_level
                    if sub_data.get('level'):
                        host_info['lic_level'] = sub_data['level']
                    # productname -> lic_type
                    if sub_data.get('productname'):
                        host_info['lic_type'] = sub_data['productname']
                    # nextduedate -> lic_scad
                    if sub_data.get('nextduedate'):
                        host_info['lic_scad'] = sub_data['nextduedate']
                    # serverid -> lic_sub
                    if sub_data.get('serverid'):
                        host_info['lic_sub'] = sub_data['serverid']
                    # sockets -> lic_sock
                    if sub_data.get('sockets'):
                        host_info['lic_sock'] = sub_data['sockets']
                    # regdate (manteniamo anche questo per completezza)
                    if sub_data.get('regdate'):
                        host_info['lic_regdate'] = sub_data['regdate']
                else:
                    logger.info("  ⚠ pvesubscription get non ha restituito output")
            except Exception as e:
                logger.info(f"  ⚠ Errore durante estrazione subscription: {e}")

        if not collect_network:
            return

        try:
            # NUOVO APPROCCIO: Leggi PRIMA tutte le interfacce da /etc/network/interfaces
            # Questo file contiene TUTTE le interfacce configurate con informazioni complete
            try:
                interfaces_content = executor('cat /etc/network/interfaces 2>/dev/null')
            except Exception:
                interfaces_content = None
            
            interfaces_config = parse_interfaces_config(interfaces_content) if interfaces_content else {}
            
            # Crea un dizionario di interfacce partendo dal file /etc/network/interfaces
            # Questo garantisce che TUTTE le interfacce configurate siano incluse
            entries_by_name: Dict[str, Dict[str, Any]] = {}
            
            for iface_name, config in interfaces_config.items():
                entry = {
                    'name': iface_name,
                    'state': 'unknown',
                    'active': None,
                }
                
                # Estrai informazioni dal file /etc/network/interfaces
                if config.get('hwaddress') or config.get('hwaddr'):
                    entry['mac_address'] = (config.get('hwaddress') or config.get('hwaddr')).strip()
                
                # Estrai IP address
                if config.get('address'):
                    addr = config.get('address').strip()
                    # Distingui IPv4 da IPv6
                    if ':' in addr:
                        entry['ip6'] = addr
                    else:
                        entry['ip'] = addr
                
                # Estrai members (bridge_ports, bond_slaves, etc.)
                cfg_members = config.get('bridge_ports') or config.get('ports') or config.get('slaves') or config.get('bond_slaves')
                if cfg_members:
                    if isinstance(cfg_members, str):
                        cfg_members = cfg_members.split()
                    entry['members'] = join_values(cfg_members)
                
                # Estrai VLAN ID (da tag, bridge_vids, vlan_id, vlan)
                vlan = config.get('bridge_vids') or config.get('tag') or config.get('vlan_id') or config.get('vlan')
                if vlan:
                    entry['vlan_id'] = str(vlan).strip()
                
                # Estrai bond-mode (algoritmo di bilanciamento)
                if config.get('bond_mode'):
                    entry['bond_mode'] = config.get('bond_mode').strip()
                
                # Estrai gateway (IPv4 e IPv6)
                if config.get('gateway'):
                    entry['gateway'] = config.get('gateway').strip()
                if config.get('gateway6'):
                    entry['gateway6'] = config.get('gateway6').strip()
                
                # Estrai netmask
                if config.get('netmask'):
                    entry['netmask'] = config.get('netmask').strip()
                
                # Estrai bridge associato
                if config.get('bridge'):
                    entry['bridge'] = config.get('bridge').strip()
                
                # Estrai VLAN parent device
                if config.get('vlan_raw_device') or config.get('vlan_dev'):
                    parent = config.get('vlan_raw_device') or config.get('vlan_dev')
                    if isinstance(parent, str):
                        entry['vlan_parent'] = parent.strip()
                
                entries_by_name[iface_name] = entry
            
            # Ora arricchisci con informazioni da network_interfaces (se disponibili dall'API)
            network_entries = host_info.get('network_interfaces') or []
            for api_entry in network_entries:
                name = api_entry.get('name')
                if not name:
                    continue
                
                if name in entries_by_name:
                    # Merge: API info ha priorità su stato, MAC da sistema, ecc.
                    entry = entries_by_name[name]
                    if api_entry.get('state'):
                        entry['state'] = api_entry.get('state')
                    if api_entry.get('active') is not None:
                        entry['active'] = api_entry.get('active')
                    if api_entry.get('mac_address') and api_entry.get('mac_address') != 'N/A':
                        entry['mac_address'] = api_entry.get('mac_address')
                    if api_entry.get('type'):
                        entry['type'] = api_entry.get('type')
                    if api_entry.get('ip') and not entry.get('ip'):
                        entry['ip'] = api_entry.get('ip')
                    if api_entry.get('ip6') and not entry.get('ip6'):
                        entry['ip6'] = api_entry.get('ip6')
                else:
                    # Interfaccia rilevata dall'API ma non in /etc/network/interfaces
                    entries_by_name[name] = api_entry
            
            # Arricchisci con informazioni dal sistema (ip link, operstate, speed)
            for iface_name, entry in entries_by_name.items():
                # Categoria interfaccia
                entry['category'] = self._guess_interface_category(iface_name, entry)
                
                # Stato operativo
                if entry.get('state') == 'unknown':
                    try:
                        state_output = executor(f'cat /sys/class/net/{iface_name}/operstate 2>/dev/null')
                        if state_output:
                            state_value = state_output.strip()
                            entry['state'] = state_value
                            entry['active'] = True if state_value.lower() == 'up' else False
                    except Exception:
                        pass
                
                # MAC address (se non già presente)
                if not entry.get('mac_address') or entry.get('mac_address') == 'N/A':
                    try:
                        mac_output = executor(f'cat /sys/class/net/{iface_name}/address 2>/dev/null')
                        if mac_output and mac_output.strip():
                            entry['mac_address'] = mac_output.strip()
                    except Exception:
                        pass
                
                # IP addresses dal sistema (se non già presenti)
                if not entry.get('ip') or not entry.get('ip6'):
                    try:
                        ip_addr_output = executor(f'ip addr show {iface_name} 2>/dev/null')
                        if ip_addr_output:
                            if not entry.get('ip'):
                                ipv4_matches = re.findall(r'inet\s+([\d.]+)', ip_addr_output)
                                if ipv4_matches:
                                    entry['ip'] = ipv4_matches[0]
                            if not entry.get('ip6'):
                                ipv6_matches = re.findall(r'inet6\s+([0-9a-fA-F:]+)', ip_addr_output)
                                if ipv6_matches:
                                    entry['ip6'] = ipv6_matches[0]
                    except Exception:
                        pass
                
                # Speed per interfacce fisiche
                if entry['category'] == 'physical':
                    try:
                        speed = self._get_interface_speed(iface_name, executor)
                        if speed:
                            entry['speed_mbps'] = speed
                    except Exception:
                        pass
                
                # Bridge: se è un bridge, il nome del bridge è l'interfaccia stessa
                if entry['category'] == 'bridge' and not entry.get('bridge'):
                    entry['bridge'] = iface_name
                
                # Combina IP addresses
                if entry.get('ip_addresses') in (None, '', 'N/A'):
                    entry['ip_addresses'] = join_values([entry.get('ip'), entry.get('ip6')])
            
            # Aggiorna host_info con tutte le interfacce
            host_info['network_interfaces'] = list(entries_by_name.values())
        except Exception as e:
            logger.info(f"  ⚠ Errore lettura interfacce di rete: {e}")
    
    def get_detailed_host_info(self):
        """Ottiene informazioni dettagliate sull'host corrente (backward compatibility)"""
        return self.get_detailed_host_info_for_node(None)
    
    def get_detailed_host_info_for_node(self, node_name=None):
        """Ottiene informazioni dettagliate su un host Proxmox specifico"""
        if node_name:
            logger.info(f"    → Estrazione info per nodo: {node_name}")
        else:
            logger.info("    → Estrazione info host corrente...")
        
        host_info = {
            'hostname': node_name or self.node_info.get('hostname', 'unknown'),
            'status': None,
            'uptime_seconds': None,
            'uptime_human': None,
            'proxmox_version': None,
            'manager_version': None,
            'kernel_version': None,
            'cpu_model': None,
            'cpu_cores': None,
            'cpu_sockets': None,
            'cpu_threads': None,
            'cpu_total_cores': None,
            'cpu_usage_percent': None,
            'io_delay_percent': None,
            'load_average_1m': None,
            'load_average_5m': None,
            'load_average_15m': None,
            'memory_total_gb': None,
            'memory_used_gb': None,
            'memory_free_gb': None,
            'memory_usage_percent': None,
            'ksm_sharing_gb': None,
            'swap_total_gb': None,
            'swap_used_gb': None,
            'swap_free_gb': None,
            'swap_usage_percent': None,
            'rootfs_total_gb': None,
            'rootfs_used_gb': None,
            'rootfs_free_gb': None,
            'rootfs_usage_percent': None,
            'storage': [],
            'network_interfaces': [],
            'license_status': None,
            'license_message': None,
            'license_level': None,
            'subscription_type': None,
            'subscription_key': None,
            'subscription_server_id': None,
            'subscription_sockets': None,
            'subscription_last_check': None,
            'subscription_next_due': None,
            'lic_status': None,
            'lic_key': None,
            'lic_level': None,
            'lic_type': None,
            'lic_scad': None,
            'lic_sub': None,
            'lic_sock': None,
            'lic_regdate': None,
            'repository_status': None,
            'boot_mode': None
        }
        
        # Prova sempre prima con API se node_name è specificato (per ottenere dati del nodo specifico)
        # Anche se execution_mode non è 'api', proviamo l'API come fallback per nodi remoti
        use_api = self.execution_mode == 'api'
        if not use_api:
            proxmox_api_conf = self.config.get('proxmox', {})
            if proxmox_api_conf.get('host') or proxmox_api_conf.get('username') or proxmox_api_conf.get('password'):
                use_api = True
        if node_name and node_name != self.node_info.get('hostname'):
            use_api = True
        
        if use_api:
            try:
                proxmox_config = self.config.get('proxmox', {})
                hosts_to_try = []
                
                hosts_to_try.append(('localhost', 8006))
                proxmox_host = proxmox_config.get('host', '')
                if proxmox_host and 'localhost' not in proxmox_host.lower():
                    if ':' in proxmox_host:
                        host, port = proxmox_host.split(':', 1)
                        try:
                            hosts_to_try.append((host, int(port)))
                        except:
                            hosts_to_try.append((host, 8006))
                    else:
                        hosts_to_try.append((proxmox_host, 8006))
                
                for host, port in hosts_to_try:
                    try:
                        base_url = f"https://{host}:{port}/api2/json"
                        ssl_context = ssl._create_unverified_context()
                        auth_url = f"{base_url}/access/ticket"
                        username = proxmox_config.get('username', 'root@pam')
                        password = proxmox_config.get('password', '')
                        
                        data = urllib.parse.urlencode({
                            'username': username,
                            'password': password
                        }).encode('utf-8')
                        
                        cookie_jar = CookieJar()
                        opener = urllib.request.build_opener(
                            urllib.request.HTTPCookieProcessor(cookie_jar),
                            urllib.request.HTTPSHandler(context=ssl_context)
                        )
                        
                        request = urllib.request.Request(auth_url, data=data, method='POST')
                        response = opener.open(request, timeout=10)
                        result = json.loads(response.read().decode('utf-8'))['data']
                        ticket = result['ticket']
                        csrf_token = result['CSRFPreventionToken']
                        
                        def api_get(endpoint):
                            url = f"{base_url}/{endpoint}"
                            req = urllib.request.Request(url)
                            req.add_header('Cookie', f'PVEAuthCookie={ticket}')
                            req.add_header('CSRFPreventionToken', csrf_token)
                            resp = opener.open(req, timeout=10)
                            return json.loads(resp.read().decode('utf-8'))['data']
                        
                        # Info nodo completo
                        node_status = api_get(f'nodes/{node_name}/status')
                        if node_status:
                            host_info['hostname'] = node_name
                            host_info['status'] = node_status.get('status')

                            uptime = node_status.get('uptime')
                            if uptime is not None:
                                try:
                                    host_info['uptime_seconds'] = int(uptime)
                                except Exception:
                                    host_info['uptime_seconds'] = uptime
                                host_info['uptime_human'] = seconds_to_human(host_info['uptime_seconds'])

                            cpu_usage = node_status.get('cpu')
                            if cpu_usage is not None:
                                host_info['cpu_usage_percent'] = safe_round(float(cpu_usage) * 100, 2)

                            io_delay = node_status.get('io_delay')
                            if io_delay is not None:
                                host_info['io_delay_percent'] = safe_round(io_delay, 2)

                            loadavg = node_status.get('loadavg')
                            if isinstance(loadavg, (list, tuple)) and len(loadavg) >= 3:
                                host_info['load_average_1m'] = safe_round(float(loadavg[0]), 2)
                                host_info['load_average_5m'] = safe_round(float(loadavg[1]), 2)
                                host_info['load_average_15m'] = safe_round(float(loadavg[2]), 2)
                            elif isinstance(loadavg, str):
                                parts = re.split(r'[,\s]+', loadavg.strip())
                                if len(parts) >= 3:
                                    try:
                                        host_info['load_average_1m'] = safe_round(float(parts[0]), 2)
                                        host_info['load_average_5m'] = safe_round(float(parts[1]), 2)
                                        host_info['load_average_15m'] = safe_round(float(parts[2]), 2)
                                    except Exception:
                                        pass

                            cpuinfo = node_status.get('cpuinfo', {})
                            host_info['cpu_model'] = cpuinfo.get('model') or host_info.get('cpu_model')
                            host_info['cpu_cores'] = cpuinfo.get('cores') or host_info.get('cpu_cores')
                            host_info['cpu_sockets'] = cpuinfo.get('sockets') or host_info.get('cpu_sockets')
                            host_info['cpu_threads'] = cpuinfo.get('cpus') or host_info.get('cpu_threads')
                            if cpuinfo.get('cpus'):
                                host_info['cpu_total_cores'] = cpuinfo.get('cpus')
                            elif cpuinfo.get('cores'):
                                host_info['cpu_total_cores'] = cpuinfo.get('cores')

                            memory_info = node_status.get('memory', {})
                            mem_total = memory_info.get('total')
                            mem_used = memory_info.get('used')
                            mem_free = memory_info.get('free')
                            if mem_total is not None:
                                host_info['memory_total_gb'] = bytes_to_gib(mem_total)
                            if mem_used is not None:
                                host_info['memory_used_gb'] = bytes_to_gib(mem_used)
                            if mem_free is not None:
                                host_info['memory_free_gb'] = bytes_to_gib(mem_free)
                            if mem_total and mem_used is not None:
                                host_info['memory_usage_percent'] = safe_round((mem_used / mem_total) * 100, 2)

                            ksm_info = node_status.get('ksm', {})
                            if isinstance(ksm_info, dict):
                                shared = bytes_to_gib(ksm_info.get('shared'))
                                if shared is not None:
                                    host_info['ksm_sharing_gb'] = shared

                            swap_info = node_status.get('swap', {})
                            if isinstance(swap_info, dict):
                                swap_total = swap_info.get('total')
                                swap_used = swap_info.get('used')
                                swap_free = swap_info.get('free')
                                if swap_total is not None:
                                    host_info['swap_total_gb'] = bytes_to_gib(swap_total)
                                if swap_used is not None:
                                    host_info['swap_used_gb'] = bytes_to_gib(swap_used)
                                if swap_free is not None:
                                    host_info['swap_free_gb'] = bytes_to_gib(swap_free)
                                if swap_total and swap_used is not None:
                                    host_info['swap_usage_percent'] = safe_round((swap_used / swap_total) * 100, 2)

                            rootfs_info = node_status.get('rootfs', {})
                            if isinstance(rootfs_info, dict):
                                root_total = rootfs_info.get('total')
                                root_used = rootfs_info.get('used')
                                root_free = rootfs_info.get('free')
                                if root_total is not None:
                                    host_info['rootfs_total_gb'] = bytes_to_gib(root_total)
                                if root_used is not None:
                                    host_info['rootfs_used_gb'] = bytes_to_gib(root_used)
                                if root_free is not None:
                                    host_info['rootfs_free_gb'] = bytes_to_gib(root_free)
                                if root_total and root_used is not None:
                                    host_info['rootfs_usage_percent'] = safe_round((root_used / root_total) * 100, 2)

                            # Versione / kernel
                            try:
                                version_info = api_get(f'nodes/{node_name}/version')
                                if version_info:
                                    manager_version = version_info.get('version')
                                    if manager_version:
                                        parsed_version, kernel_from_manager = parse_manager_version_string(manager_version)
                                        host_info['manager_version'] = parsed_version
                                        host_info.setdefault('proxmox_version', parsed_version)
                                        if kernel_from_manager and not host_info.get('kernel_version'):
                                            host_info['kernel_version'] = kernel_from_manager
                                    kernel_version = version_info.get('kernel') or version_info.get('running_kernel') or version_info.get('release')
                                    if kernel_version:
                                        host_info['kernel_version'] = kernel_version.strip()
                            except Exception:
                                pass

                            # Informazioni subscription/licenza
                            try:
                                subscription_info = api_get(f'nodes/{node_name}/subscription')
                                if isinstance(subscription_info, dict):
                                    host_info['license_status'] = subscription_info.get('status')
                                    host_info['license_message'] = subscription_info.get('message')
                                    host_info['license_level'] = subscription_info.get('level')
                                    host_info['subscription_type'] = subscription_info.get('productname') or subscription_info.get('type')
                                    host_info['subscription_key'] = subscription_info.get('key')
                                    host_info['subscription_server_id'] = subscription_info.get('serverid')
                                    host_info['subscription_sockets'] = subscription_info.get('sockets')

                                    checktime = subscription_info.get('checktime') or subscription_info.get('lastcheck')
                                    if checktime:
                                        try:
                                            if isinstance(checktime, (int, float)):
                                                host_info['subscription_last_check'] = datetime.fromtimestamp(checktime).isoformat()
                                            else:
                                                host_info['subscription_last_check'] = str(checktime)
                                        except Exception:
                                            host_info['subscription_last_check'] = str(checktime)

                                    next_due = subscription_info.get('nextduedate') or subscription_info.get('nextdue')
                                    if next_due:
                                        host_info['subscription_next_due'] = str(next_due)
                            except Exception:
                                pass

                            # Stato repository APT
                            try:
                                repos_info = api_get(f'nodes/{node_name}/apt/repositories')
                                repo_entries = []
                                repositories = []
                                if isinstance(repos_info, dict):
                                    repositories = repos_info.get('repositories') or repos_info.get('data') or []
                                elif isinstance(repos_info, list):
                                    repositories = repos_info
                                for repo in repositories:
                                    if not isinstance(repo, dict):
                                        continue
                                    name = repo.get('name') or repo.get('handle') or repo.get('description') or 'repository'
                                    enabled = repo.get('enabled')
                                    status = repo.get('status')
                                    entry = name
                                    if enabled is not None:
                                        entry += f" [{'enabled' if enabled else 'disabled'}]"
                                    if status:
                                        entry += f" - {status}"
                                    repo_entries.append(entry)
                                if repo_entries:
                                    host_info['repository_status'] = '; '.join(repo_entries)
                            except Exception:
                                pass

                            # Network (via API)
                            try:
                                network_payload = api_get(f'nodes/{node_name}/network')
                                network_data = network_payload.get('data') if isinstance(network_payload, dict) and 'data' in network_payload else network_payload
                                normalized_network = normalize_network_entries(network_data)
                                if normalized_network:
                                    host_info['network_interfaces'] = normalized_network
                            except Exception as network_exc:
                                logger.info(f"      ⚠ Errore rete nodo {node_name}: {network_exc}")
                            
                            # Storage
                            try:
                                storage_list = api_get(f'nodes/{node_name}/storage')
                                if storage_list:
                                    for storage in storage_list:
                                        storage_name = storage.get('storage', 'N/A')
                                        storage_info = {
                                            'name': storage_name,
                                            'type': storage.get('type', 'N/A'),
                                            'status': storage.get('status', 'N/A'),
                                            'total_gb': None,
                                            'used_gb': None,
                                            'available_gb': None,
                                            'content': storage.get('content', 'N/A')
                                        }
                                        
                                        # Dettagli storage
                                        try:
                                            storage_details = api_get(f'nodes/{node_name}/storage/{storage_name}/status')
                                            if storage_details:
                                                if storage_details.get('total'):
                                                    storage_info['total_gb'] = storage_details.get('total') / (1024**3)
                                                if storage_details.get('used'):
                                                    storage_info['used_gb'] = storage_details.get('used') / (1024**3)
                                                if storage_details.get('avail'):
                                                    storage_info['available_gb'] = storage_details.get('avail') / (1024**3)
                                        except:
                                            pass
                                        
                                        host_info['storage'].append(storage_info)
                            except:
                                pass
                            
                            # Network interfaces: l'API Proxmox non ha un endpoint diretto
                            # Le interfacce vengono estratte via comandi locali/SSH nel blocco successivo
                        
                        break
                    except:
                        continue
            except Exception as e:
                logger.info(f"      ⚠ Errore API per nodo {node_name}: {e}")
        
        # Enrichment con comandi solo per host corrente e se possiamo eseguire comandi
        can_use_commands = (not node_name or node_name == self.node_info.get('hostname')) and self.execution_mode != 'api'
        if can_use_commands:
            self.enrich_host_info_with_commands(host_info, self.execute_command)
        
        # Se non abbiamo ottenuto dati via API e siamo sull'host corrente, usa comandi locali/SSH
        # (solo se node_name è None o se siamo sull'host corrente)
        if (not node_name or node_name == self.node_info.get('hostname')) and not host_info.get('cpu_model'):
            # Versione Proxmox
            try:
                if self.execution_mode == 'ssh':
                    version_output = self.execute_command('pveversion 2>/dev/null || cat /etc/pve/version 2>/dev/null')
                    if version_output:
                        version_output = version_output.strip()
                        parsed_version, kernel_from_manager = parse_manager_version_string(version_output)
                        effective_version = parsed_version or version_output
                        host_info['proxmox_version'] = effective_version
                        if not host_info.get('manager_version'):
                            host_info['manager_version'] = effective_version
                        if kernel_from_manager and not host_info.get('kernel_version'):
                            host_info['kernel_version'] = kernel_from_manager
                else:
                    try:
                        # Prova pveversion
                        result = subprocess.run(['pveversion'], capture_output=True, text=True, timeout=10)
                        if result.returncode == 0:
                            version_output = result.stdout.strip()
                            parsed_version, kernel_from_manager = parse_manager_version_string(version_output)
                            effective_version = parsed_version or version_output
                            host_info['proxmox_version'] = effective_version
                            if not host_info.get('manager_version'):
                                host_info['manager_version'] = effective_version
                            if kernel_from_manager and not host_info.get('kernel_version'):
                                host_info['kernel_version'] = kernel_from_manager
                        else:
                            # Fallback a file
                            if os.path.exists('/etc/pve/version'):
                                with open('/etc/pve/version', 'r') as f:
                                    version_output = f.read().strip()
                                    parsed_version, kernel_from_manager = parse_manager_version_string(version_output)
                                    effective_version = parsed_version or version_output
                                    host_info['proxmox_version'] = effective_version
                                    if not host_info.get('manager_version'):
                                        host_info['manager_version'] = effective_version
                                    if kernel_from_manager and not host_info.get('kernel_version'):
                                        host_info['kernel_version'] = kernel_from_manager
                    except FileNotFoundError:
                        # Fallback a file
                        if os.path.exists('/etc/pve/version'):
                            with open('/etc/pve/version', 'r') as f:
                                version_output = f.read().strip()
                                parsed_version, kernel_from_manager = parse_manager_version_string(version_output)
                                effective_version = parsed_version or version_output
                                host_info['proxmox_version'] = effective_version
                                if not host_info.get('manager_version'):
                                    host_info['manager_version'] = effective_version
                                if kernel_from_manager and not host_info.get('kernel_version'):
                                    host_info['kernel_version'] = kernel_from_manager
                    except Exception:
                        pass
            except Exception as e:
                logger.info(f"  ⚠ Errore lettura versione Proxmox: {e}")
        
        # CPU dettagliata
        try:
            if self.execution_mode == 'ssh':
                cpuinfo = self.execute_command('cat /proc/cpuinfo 2>/dev/null')
                if cpuinfo:
                    # Modello CPU
                    model_match = re.search(r'model name\s*:\s*(.+)', cpuinfo)
                    if model_match:
                        host_info['cpu_model'] = model_match.group(1).strip()
                    
                    # Core fisici
                    physical_cores = self.execute_command('grep -c "^processor" /proc/cpuinfo 2>/dev/null')
                    if physical_cores:
                        try:
                            host_info['cpu_cores'] = int(physical_cores)
                        except:
                            pass
                    
                    # Socket
                    socket_output = self.execute_command('lscpu 2>/dev/null | grep "Socket(s)" | awk \'{print $2}\'')
                    if socket_output:
                        try:
                            host_info['cpu_sockets'] = int(socket_output)
                        except:
                            pass
                    
                    # Thread per core
                    threads = self.execute_command('lscpu 2>/dev/null | grep "Thread(s) per core" | awk \'{print $4}\'')
                    if threads:
                        try:
                            host_info['cpu_threads'] = int(threads)
                        except:
                            pass
            else:
                try:
                    with open('/proc/cpuinfo', 'r') as f:
                        cpu_info = f.read()
                        model_match = re.search(r'model name\s*:\s*(.+)', cpu_info)
                        if model_match:
                            host_info['cpu_model'] = model_match.group(1).strip()
                    
                    # Core fisici
                    try:
                        result = subprocess.run(['nproc'], capture_output=True, text=True, timeout=10)
                        if result.returncode == 0:
                            host_info['cpu_cores'] = int(result.stdout.strip())
                    except:
                        pass
                    
                    # Socket e thread
                    try:
                        result = subprocess.run(['lscpu'], capture_output=True, text=True, timeout=10)
                        if result.returncode == 0:
                            lscpu_output = result.stdout
                            socket_match = re.search(r'Socket\(s\):\s*(\d+)', lscpu_output)
                            if socket_match:
                                host_info['cpu_sockets'] = int(socket_match.group(1))
                            thread_match = re.search(r'Thread\(s\) per core:\s*(\d+)', lscpu_output)
                            if thread_match:
                                host_info['cpu_threads'] = int(thread_match.group(1))
                    except:
                        pass
                except Exception as e:
                    logger.info(f"  ⚠ Errore lettura CPU info: {e}")
        except Exception as e:
            logger.info(f"  ⚠ Errore generale CPU: {e}")
        
        # Memoria dettagliata
        try:
            if self.execution_mode == 'ssh':
                meminfo = self.execute_command('cat /proc/meminfo 2>/dev/null')
                if meminfo:
                    mem_total = re.search(r'MemTotal:\s+(\d+)', meminfo)
                    mem_free = re.search(r'MemFree:\s+(\d+)', meminfo)
                    mem_available = re.search(r'MemAvailable:\s+(\d+)', meminfo)
                    
                    if mem_total:
                        host_info['memory_total_gb'] = int(mem_total.group(1)) / 1024 / 1024
                    if mem_available:
                        host_info['memory_free_gb'] = int(mem_available.group(1)) / 1024 / 1024
                        if mem_total:
                            host_info['memory_used_gb'] = host_info['memory_total_gb'] - host_info['memory_free_gb']
                    elif mem_free:
                        host_info['memory_free_gb'] = int(mem_free.group(1)) / 1024 / 1024
                        if mem_total:
                            host_info['memory_used_gb'] = host_info['memory_total_gb'] - host_info['memory_free_gb']
            else:
                try:
                    with open('/proc/meminfo', 'r') as f:
                        meminfo = f.read()
                        mem_total = re.search(r'MemTotal:\s+(\d+)', meminfo)
                        mem_free = re.search(r'MemFree:\s+(\d+)', meminfo)
                        mem_available = re.search(r'MemAvailable:\s+(\d+)', meminfo)
                        
                        if mem_total:
                            host_info['memory_total_gb'] = int(mem_total.group(1)) / 1024 / 1024
                        if mem_available:
                            host_info['memory_free_gb'] = int(mem_available.group(1)) / 1024 / 1024
                            if mem_total:
                                host_info['memory_used_gb'] = host_info['memory_total_gb'] - host_info['memory_free_gb']
                        elif mem_free:
                            host_info['memory_free_gb'] = int(mem_free.group(1)) / 1024 / 1024
                            if mem_total:
                                host_info['memory_used_gb'] = host_info['memory_total_gb'] - host_info['memory_free_gb']
                except Exception as e:
                    logger.info(f"  ⚠ Errore lettura memoria: {e}")
        except Exception as e:
            logger.info(f"  ⚠ Errore generale memoria: {e}")
        
        # Storage (solo se non già ottenuto via API e siamo sull'host corrente)
        if not host_info.get('storage') and (not node_name or node_name == self.node_info.get('hostname')):
            try:
                if self.execution_mode == 'ssh':
                    storage_output = self.execute_command('pvesm status --output-format json 2>/dev/null')
                    if storage_output:
                        try:
                            storage_data = json.loads(storage_output)
                            for storage in storage_data:
                                storage_info = {
                                    'name': storage.get('name', 'N/A'),
                                    'type': storage.get('type', 'N/A'),
                                    'status': storage.get('status', 'N/A'),
                                    'total_gb': None,
                                    'used_gb': None,
                                    'available_gb': None,
                                    'content': storage.get('content', 'N/A')
                                }
                                
                                # Converti size da bytes a GB
                                if storage.get('total'):
                                    storage_info['total_gb'] = storage.get('total') / (1024**3)
                                if storage.get('used'):
                                    storage_info['used_gb'] = storage.get('used') / (1024**3)
                                if storage.get('avail'):
                                    storage_info['available_gb'] = storage.get('avail') / (1024**3)
                                
                                host_info['storage'].append(storage_info)
                        except Exception as e:
                            logger.info(f"      ⚠ Errore parsing storage: {e}")
                else:
                    try:
                        result = subprocess.run(
                            ['pvesm', 'status', '--output-format', 'json'],
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                        if result.returncode == 0:
                            storage_data = json.loads(result.stdout)
                            for storage in storage_data:
                                storage_info = {
                                    'name': storage.get('name', 'N/A'),
                                    'type': storage.get('type', 'N/A'),
                                    'status': storage.get('status', 'N/A'),
                                    'total_gb': None,
                                    'used_gb': None,
                                    'available_gb': None,
                                    'content': storage.get('content', 'N/A')
                                }
                                
                                if storage.get('total'):
                                    storage_info['total_gb'] = storage.get('total') / (1024**3)
                                if storage.get('used'):
                                    storage_info['used_gb'] = storage.get('used') / (1024**3)
                                if storage.get('avail'):
                                    storage_info['available_gb'] = storage.get('avail') / (1024**3)
                                
                                host_info['storage'].append(storage_info)
                    except FileNotFoundError:
                        logger.info(f"      ⚠ pvesm non trovato, storage non disponibile per {node_name or 'host corrente'}")
                    except Exception as e:
                        logger.info(f"      ⚠ Errore lettura storage: {e}")
            except Exception as e:
                logger.info(f"      ⚠ Errore generale storage: {e}")
        
        # Interfacce di rete (solo quelle UP)
        try:
            if self.execution_mode == 'ssh':
                # Ottieni interfacce via ip command
                ip_output = self.execute_command('ip -o link show 2>/dev/null')
                if ip_output:
                    interfaces = []
                    for line in ip_output.split('\n'):
                        parts = line.split(':')
                        if len(parts) >= 2:
                            iface_name = parts[1].strip().split()[0]
                            if iface_name and iface_name != 'lo':
                                interfaces.append(iface_name)
                    
                    for iface in interfaces:
                        try:
                            iface_info = {
                                'name': iface,
                                'mac_address': None,
                                'ip_addresses': [],
                                'bridge': None,
                                'vlan': None,
                                'state': None
                            }
                            
                            # State (controlla PRIMA se è UP)
                            state_output = self.execute_command(f'cat /sys/class/net/{iface}/operstate 2>/dev/null')
                            if state_output:
                                iface_info['state'] = state_output.strip()
                            
                            # Solo interfacce UP
                            if iface_info['state'] and iface_info['state'].lower() == 'up':
                                # MAC address
                                mac_output = self.execute_command(f'cat /sys/class/net/{iface}/address 2>/dev/null')
                                if mac_output:
                                    iface_info['mac_address'] = mac_output.strip()
                                
                                # IP addresses
                                ip_addr_output = self.execute_command(f'ip addr show {iface} 2>/dev/null')
                                if ip_addr_output:
                                    ip_matches = re.findall(r'inet\s+([\d.]+)', ip_addr_output)
                                    iface_info['ip_addresses'] = ip_matches
                                
                                # Bridge e VLAN da /etc/network/interfaces
                                interfaces_file = self.execute_command(f'grep -A 10 "iface {iface}" /etc/network/interfaces 2>/dev/null')
                                if interfaces_file:
                                    bridge_match = re.search(r'bridge_ports\s+(\S+)', interfaces_file)
                                    if bridge_match:
                                        iface_info['bridge'] = bridge_match.group(1)
                                    
                                    vlan_match = re.search(r'vlan-raw-device\s+(\S+)', interfaces_file)
                                    if vlan_match:
                                        iface_info['vlan'] = vlan_match.group(1)
                                
                                host_info['network_interfaces'].append(iface_info)
                        except Exception as e:
                            logger.info(f"  ⚠ Errore lettura interfaccia {iface}: {e}")
                            continue
            else:
                try:
                    # Lista interfacce
                    result = subprocess.run(['ip', '-o', 'link', 'show'], capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        interfaces = []
                        for line in result.stdout.split('\n'):
                            parts = line.split(':')
                            if len(parts) >= 2:
                                iface_name = parts[1].strip().split()[0]
                                if iface_name and iface_name != 'lo':
                                    interfaces.append(iface_name)
                        
                        for iface in interfaces:
                            try:
                                iface_info = {
                                    'name': iface,
                                    'mac_address': None,
                                    'ip_addresses': [],
                                    'bridge': None,
                                    'vlan': None,
                                    'state': None
                                }
                                
                                # State (controlla PRIMA se è UP)
                                try:
                                    with open(f'/sys/class/net/{iface}/operstate', 'r') as f:
                                        iface_info['state'] = f.read().strip()
                                except:
                                    pass
                                
                                # Solo interfacce UP
                                if iface_info['state'] and iface_info['state'].lower() == 'up':
                                    # MAC address
                                    try:
                                        with open(f'/sys/class/net/{iface}/address', 'r') as f:
                                            iface_info['mac_address'] = f.read().strip()
                                    except:
                                        pass
                                    
                                    # IP addresses
                                    try:
                                        result = subprocess.run(['ip', 'addr', 'show', iface], capture_output=True, text=True, timeout=10)
                                        if result.returncode == 0:
                                            ip_matches = re.findall(r'inet\s+([\d.]+)', result.stdout)
                                            iface_info['ip_addresses'] = ip_matches
                                    except:
                                        pass
                                    
                                    # Bridge e VLAN da /etc/network/interfaces
                                    try:
                                        with open('/etc/network/interfaces', 'r') as f:
                                            content = f.read()
                                            pattern = rf'iface\s+{iface}.*?(?=\n\S|\Z)'
                                            match = re.search(pattern, content, re.DOTALL)
                                            if match:
                                                iface_config = match.group(0)
                                                bridge_match = re.search(r'bridge_ports\s+(\S+)', iface_config)
                                                if bridge_match:
                                                    iface_info['bridge'] = bridge_match.group(1)
                                                
                                                vlan_match = re.search(r'vlan-raw-device\s+(\S+)', iface_config)
                                                if vlan_match:
                                                    iface_info['vlan'] = vlan_match.group(1)
                                    except:
                                        pass
                                    
                                    host_info['network_interfaces'].append(iface_info)
                            except Exception as e:
                                logger.info(f"  ⚠ Errore lettura interfaccia {iface}: {e}")
                                continue
                except Exception as e:
                    logger.info(f"  ⚠ Errore lettura interfacce: {e}")
        except Exception as e:
            logger.info(f"  ⚠ Errore generale interfacce: {e}")
        
        logger.info(f"  ✓ Hostname: {host_info['hostname']}")
        if host_info.get('status'):
            logger.info(f"  ✓ Stato nodo: {host_info['status']}")
        if host_info.get('uptime_human'):
            uptime_line = f"  ✓ Uptime: {host_info['uptime_human']}"
            if host_info.get('uptime_seconds') is not None:
                uptime_line += f" ({int(host_info['uptime_seconds'])}s)"
            logger.info(uptime_line)
        
        if host_info.get('cpu_model'):
            logger.info(f"  ✓ CPU: {host_info['cpu_model']}")
        
        if host_info.get('cpu_total_cores'):
            cpu_line = f"  ✓ CPU totali: {host_info['cpu_total_cores']}"
            if host_info.get('cpu_usage_percent') is not None:
                cpu_line += f" - utilizzo {host_info['cpu_usage_percent']:.2f}%"
            logger.info(cpu_line)
        elif host_info.get('cpu_usage_percent') is not None:
            logger.info(f"  ✓ CPU usage: {host_info['cpu_usage_percent']:.2f}%")
        
        if host_info.get('io_delay_percent') is not None:
            logger.info(f"  ✓ IO delay: {host_info['io_delay_percent']:.2f}%")
        
        load_values = [
            host_info.get('load_average_1m'),
            host_info.get('load_average_5m'),
            host_info.get('load_average_15m')
        ]
        if all(value is not None for value in load_values):
            logger.info(f"  ✓ Load average: {load_values[0]:.2f}, {load_values[1]:.2f}, {load_values[2]:.2f}")
        
        if host_info.get('memory_total_gb') is not None:
            if host_info.get('memory_used_gb') is not None and host_info.get('memory_usage_percent') is not None:
                logger.info(f"  ✓ RAM usage: {host_info['memory_usage_percent']:.2f}% ({host_info['memory_used_gb']:.2f} GiB of {host_info['memory_total_gb']:.2f} GiB)")
            else:
                logger.info(f"  ✓ RAM totale: {host_info['memory_total_gb']:.2f} GiB")
        
        if host_info.get('ksm_sharing_gb') is not None:
            logger.info(f"  ✓ KSM sharing: {host_info['ksm_sharing_gb']:.2f} GiB")
        
        if host_info.get('swap_total_gb') is not None:
            if host_info.get('swap_used_gb') is not None and host_info.get('swap_usage_percent') is not None:
                logger.info(f"  ✓ Swap usage: {host_info['swap_usage_percent']:.2f}% ({host_info['swap_used_gb']:.2f} GiB of {host_info['swap_total_gb']:.2f} GiB)")
            else:
                logger.info(f"  ✓ Swap totale: {host_info['swap_total_gb']:.2f} GiB")
        
        if host_info.get('rootfs_total_gb') is not None:
            if host_info.get('rootfs_used_gb') is not None and host_info.get('rootfs_usage_percent') is not None:
                logger.info(f"  ✓ RootFS: {host_info['rootfs_usage_percent']:.2f}% ({host_info['rootfs_used_gb']:.2f} GiB of {host_info['rootfs_total_gb']:.2f} GiB)")
            else:
                logger.info(f"  ✓ RootFS totale: {host_info['rootfs_total_gb']:.2f} GiB")
        
        if host_info.get('kernel_version'):
            logger.info(f"  ✓ Kernel: {host_info['kernel_version']}")
        if host_info.get('manager_version'):
            logger.info(f"  ✓ Manager Version: {host_info['manager_version']}")
        elif host_info.get('proxmox_version'):
            logger.info(f"  ✓ Versione Proxmox: {host_info['proxmox_version']}")
        
        if host_info.get('boot_mode'):
            logger.info(f"  ✓ Boot mode: {host_info['boot_mode']}")
        
        if host_info.get('license_status') or host_info.get('license_level'):
            license_parts = []
            if host_info.get('license_level'):
                license_parts.append(f"livello {host_info['license_level']}")
            if host_info.get('license_status'):
                license_parts.append(host_info['license_status'])
            if license_parts:
                logger.info(f"  ✓ Licenza: {', '.join(license_parts)}")
            if host_info.get('license_message'):
                logger.info(f"    ℹ {host_info['license_message']}")
        
        if host_info.get('subscription_type'):
            sub_line = f"  ✓ Subscription: {host_info['subscription_type']}"
            sockets = host_info.get('subscription_sockets')
            if sockets not in (None, 'N/A'):
                sub_line += f" ({sockets} socket{'s' if str(sockets) not in ('1', '1.0') else ''})"
            logger.info(sub_line)
        if host_info.get('subscription_key'):
            logger.info(f"    ➜ Key: {host_info['subscription_key']}")
        if host_info.get('subscription_server_id'):
            logger.info(f"    ➜ Server ID: {host_info['subscription_server_id']}")
        if host_info.get('subscription_last_check'):
            logger.info(f"    ➜ Ultimo controllo: {host_info['subscription_last_check']}")
        if host_info.get('subscription_next_due'):
            logger.info(f"    ➜ Prossima scadenza: {host_info['subscription_next_due']}")
        
        if host_info.get('repository_status'):
            logger.info(f"  ✓ Repository: {host_info['repository_status']}")
        
        logger.info(f"  ✓ Storage trovati: {len(host_info['storage'])}")
        logger.info(f"  ✓ Interfacce di rete: {len(host_info['network_interfaces'])}")
        
        return host_info
    
    def save_host_info_to_csv(self, all_hosts_info, filename, codcli, nomecliente, max_copies=5, server_identifier=None):
        """Salva informazioni host/storage/network in CSV (rispettando le feature abilitate)"""
        try:
            directory = os.path.dirname(filename)
            if isinstance(all_hosts_info, dict):
                all_hosts_info = [all_hosts_info]

            collect_host = feature_enabled(self.features, 'collect_host', True)
            collect_storage = feature_enabled(self.features, 'collect_storage', True)
            collect_network = feature_enabled(self.features, 'collect_network', True)

            host_filepath = None
            storage_filepath = None
            network_filepath = None
            host_success = True

            host_fieldnames = [
                'server_identifier',
                'hostname',
                'status',
                'uptime_seconds',
                'uptime_human',
                'cpu_model',
                'cpu_total_cores',
                'cpu_cores',
                'cpu_sockets',
                'cpu_threads',
                'cpu_usage_percent',
                'io_delay_percent',
                'load_average_1m',
                'load_average_5m',
                'load_average_15m',
                'memory_total_gb',
                'memory_used_gb',
                'memory_free_gb',
                'memory_usage_percent',
                'ksm_sharing_gb',
                'swap_total_gb',
                'swap_used_gb',
                'swap_free_gb',
                'swap_usage_percent',
                'rootfs_total_gb',
                'rootfs_used_gb',
                'rootfs_free_gb',
                'rootfs_usage_percent',
                'proxmox_version',
                'manager_version',
                'kernel_version',
                'boot_mode',
                'license_status',
                'license_level',
                'license_message',
                'lic_status',
                'lic_key',
                'lic_level',
                'lic_type',
                'lic_scad',
                'lic_sub',
                'lic_sock',
                'lic_regdate',
                'repository_status'
            ]

            storage_fieldnames = [
                'server_identifier',
                'hostname',
                'storage_name',
                'storage_type',
                'status',
                'total_gb',
                'used_gb',
                'available_gb',
                'content'
            ]

            network_fieldnames = [
                'server_identifier',
                'hostname',
                'category',
                'name',
                'type',
                'state',
                'mac_address',
                'ip_addresses',
                'gateway',
                'gateway6',
                'netmask',
                'bridge',
                'members',
                'vlan_id',
                'bond_mode',
                'speed_mbps',
                'comment'
            ]

            def fmt_value(value, digits=2):
                if value is None:
                    return 'N/A'
                if isinstance(value, (int, float)):
                    return f"{value:.{digits}f}"
                return str(value)

            if collect_host:
                host_rows = []
                for host_info in all_hosts_info:
                    # IMPORTANTE: Estrai informazioni licenza se non già presenti
                    if not host_info.get('lic_status') and self.execution_mode in ('local', 'ssh'):
                        try:
                            logger.info(f"  → Tentativo estrazione licenza per {host_info.get('hostname')}")
                            sub_output = self.execute_command('pvesubscription get 2>/dev/null')
                            if sub_output:
                                logger.info(f"  → Output pvesubscription get ricevuto ({len(sub_output)} caratteri)")
                                sub_data = {}
                                for line in sub_output.splitlines():
                                    line = line.strip()
                                    if ':' in line:
                                        key, value = line.split(':', 1)
                                        key = key.strip().lower().replace(' ', '_')
                                        value = value.strip()
                                        sub_data[key] = value
                                
                                logger.info(f"  → Parsed {len(sub_data)} campi subscription")
                                if sub_data.get('status'):
                                    host_info['lic_status'] = sub_data['status']
                                    logger.info(f"  → lic_status: {sub_data['status']}")
                                if sub_data.get('key'):
                                    host_info['lic_key'] = sub_data['key']
                                if sub_data.get('level'):
                                    host_info['lic_level'] = sub_data['level']
                                if sub_data.get('productname'):
                                    host_info['lic_type'] = sub_data['productname']
                                if sub_data.get('nextduedate'):
                                    host_info['lic_scad'] = sub_data['nextduedate']
                                if sub_data.get('serverid'):
                                    host_info['lic_sub'] = sub_data['serverid']
                                if sub_data.get('sockets'):
                                    host_info['lic_sock'] = sub_data['sockets']
                                if sub_data.get('regdate'):
                                    host_info['lic_regdate'] = sub_data['regdate']
                            else:
                                logger.info("  ⚠ pvesubscription get non ha restituito output")
                        except Exception as e:
                            logger.info(f"  ⚠ Errore estrazione licenza: {e}")
                    
                    row = {}
                    for field in host_fieldnames:
                        value = host_info.get(field)
                        if field == 'server_identifier':
                            row[field] = server_identifier if server_identifier else 'N/A'
                            continue
                        if field in ('hostname', 'status', 'uptime_human', 'cpu_model', 'proxmox_version',
                                     'manager_version', 'kernel_version', 'boot_mode',
                                     'license_status', 'license_level', 'license_message', 'repository_status',
                                     'lic_status', 'lic_key', 'lic_level', 'lic_type', 'lic_scad', 'lic_sub', 'lic_regdate'):
                            if isinstance(value, str):
                                cleaned = value.replace('\n', ' ').strip()
                                row[field] = cleaned if cleaned else 'N/A'
                            else:
                                row[field] = value if value not in (None, '') else 'N/A'
                        elif field == 'uptime_seconds':
                            row[field] = int(value) if value is not None else 'N/A'
                        elif field in ('cpu_total_cores', 'cpu_cores', 'cpu_sockets', 'cpu_threads', 'subscription_sockets', 'lic_sock'):
                            try:
                                row[field] = int(value) if value is not None else 'N/A'
                            except Exception:
                                row[field] = value if value is not None else 'N/A'
                        else:
                            row[field] = fmt_value(value)
                    host_rows.append(row)
                if host_rows:
                    host_base_filename = os.path.basename(filename)
                    host_filepath = os.path.join(directory, host_base_filename)
                    rotate_files(directory, host_base_filename, max_copies)
                    with open(host_filepath, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=host_fieldnames)
                        writer.writeheader()
                        writer.writerows(host_rows)
                else:
                    host_success = False
            else:
                host_success = True

            if collect_storage:
                storage_rows = []
                storage_base_filename = generate_filename(codcli, nomecliente, 'storage', 'csv', server_identifier=server_identifier)
                for host_info in all_hosts_info:
                    if host_info.get('storage'):
                        for storage in host_info['storage']:
                            storage_rows.append({
                                'server_identifier': server_identifier if server_identifier else 'N/A',
                                'hostname': host_info.get('hostname', 'N/A'),
                                'storage_name': storage.get('name', 'N/A'),
                                'storage_type': storage.get('type', 'N/A'),
                                'status': storage.get('status', 'N/A'),
                                'total_gb': f"{storage.get('total_gb', 0):.2f}" if storage.get('total_gb') else 'N/A',
                                'used_gb': f"{storage.get('used_gb', 0):.2f}" if storage.get('used_gb') else 'N/A',
                                'available_gb': f"{storage.get('available_gb', 0):.2f}" if storage.get('available_gb') else 'N/A',
                                'content': storage.get('content', 'N/A')
                            })
                if storage_rows:
                    storage_filepath = os.path.join(directory, storage_base_filename)
                    rotate_files(directory, storage_base_filename, max_copies)
                    with open(storage_filepath, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=storage_fieldnames)
                        writer.writeheader()
                        writer.writerows(storage_rows)

            if collect_network:
                network_rows = []
                network_base_filename = generate_filename(codcli, nomecliente, 'network', 'csv', server_identifier=server_identifier)
                for host_info in all_hosts_info:
                    hostname = host_info.get('hostname', 'N/A')
                    if host_info.get('network_interfaces'):
                        for iface in host_info['network_interfaces']:
                            ip_value = iface.get('ip_addresses')
                            if not ip_value:
                                ip_value = join_values([iface.get('ip'), iface.get('ip6')])
                            category = iface.get('category') or self._guess_interface_category(iface.get('name', ''), iface)
                            state_value = iface.get('state') or ('up' if to_bool(iface.get('active')) else 'down') if iface.get('active') is not None else iface.get('state', 'N/A')
                            if category == 'physical' and str(state_value).lower() == 'down':
                                continue
                            members_value = iface.get('members')
                            if not members_value:
                                members_value = iface.get('bridge_ports') or iface.get('ports_slaves')
                            bond_mode = iface.get('bond_mode')
                            speed_value = iface.get('speed_mbps')
                            if isinstance(speed_value, (int, float)):
                                speed_value = str(int(speed_value))
                            row = {
                                'server_identifier': server_identifier if server_identifier else 'N/A',
                                'hostname': hostname,
                                'category': category or 'other',
                                'name': iface.get('name', 'N/A'),
                                'type': iface.get('type', 'N/A'),
                                'state': state_value if state_value not in (None, '') else 'N/A',
                                'mac_address': iface.get('mac_address', 'N/A'),
                                'ip_addresses': ip_value if ip_value not in (None, '') else 'N/A',
                                'gateway': iface.get('gateway') if iface.get('gateway') not in (None, '', 'N/A') else 'N/A',
                                'gateway6': iface.get('gateway6') if iface.get('gateway6') not in (None, '', 'N/A') else 'N/A',
                                'netmask': iface.get('netmask') if iface.get('netmask') not in (None, '', 'N/A') else 'N/A',
                                'bridge': iface.get('bridge', 'N/A'),
                                'members': join_values(members_value) if members_value else 'N/A',
                                'vlan_id': iface.get('vlan_id') or iface.get('vlan') or iface.get('tag') or 'N/A',
                                'bond_mode': bond_mode if bond_mode not in (None, '') else 'N/A',
                                'speed_mbps': speed_value if speed_value not in (None, '') else 'N/A',
                                'comment': iface.get('comment') or iface.get('comments') or 'N/A'
                            }
                            network_rows.append(row)
                if network_rows:
                    network_filepath = os.path.join(directory, network_base_filename)
                    rotate_files(directory, network_base_filename, max_copies)
                    with open(network_filepath, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=network_fieldnames)
                        writer.writeheader()
                        writer.writerows(network_rows)

            return host_success, storage_filepath, network_filepath
        except Exception as e:
            logger.info(f"✗ Errore salvataggio info host: {e}")
            import traceback
            traceback.print_exc()
            return False, None, None
    
    def get_vms_from_local_api(self):
        """Ottiene VM usando API Proxmox con informazioni complete (come proxmox_auto_report.py)"""
        logger.info("  → Tentativo via API con estrazione completa...")
        
        vms = []
        
        try:
            proxmox_config = self.config.get('proxmox', {})
            
            # Prova prima API locale, poi remota se configurata
            hosts_to_try = []
            
            # 1. Prova localhost
            hosts_to_try.append(('localhost', 8006))
            
            # 2. Se configurato host remoto, prova anche quello
            proxmox_host = proxmox_config.get('host', '')
            if proxmox_host and 'localhost' not in proxmox_host.lower():
                if ':' in proxmox_host:
                    host, port = proxmox_host.split(':', 1)
                    try:
                        hosts_to_try.append((host, int(port)))
                    except:
                        hosts_to_try.append((host, 8006))
                else:
                    hosts_to_try.append((proxmox_host, 8006))
            
            for host, port in hosts_to_try:
                try:
                    base_url = f"https://{host}:{port}/api2/json"
                    ssl_context = ssl._create_unverified_context()
                    
                    # Autenticazione
                    auth_url = f"{base_url}/access/ticket"
                    username = proxmox_config.get('username', 'root@pam')
                    password = proxmox_config.get('password', '')
                    
                    data = urllib.parse.urlencode({
                        'username': username,
                        'password': password
                    }).encode('utf-8')
                    
                    cookie_jar = CookieJar()
                    opener = urllib.request.build_opener(
                        urllib.request.HTTPCookieProcessor(cookie_jar),
                        urllib.request.HTTPSHandler(context=ssl_context)
                    )
                    
                    request = urllib.request.Request(auth_url, data=data, method='POST')
                    response = opener.open(request, timeout=10)
                    result = json.loads(response.read().decode('utf-8'))['data']
                    ticket = result['ticket']
                    csrf_token = result['CSRFPreventionToken']
                    
                    logger.info(f"    ✓ Connesso a {host}:{port}")
                    
                    # Funzione helper per richieste API
                    def api_get(endpoint):
                        url = f"{base_url}/{endpoint}"
                        req = urllib.request.Request(url)
                        req.add_header('Cookie', f'PVEAuthCookie={ticket}')
                        req.add_header('CSRFPreventionToken', csrf_token)
                        resp = opener.open(req, timeout=10)
                        return json.loads(resp.read().decode('utf-8'))['data']
                    
                    # Ottieni nodi
                    nodes_data = api_get('nodes')
                    
                    for node in nodes_data:
                        node_name = node['node']
                        logger.info(f"      → Scanning nodo: {node_name}")
                        
                        # Ottieni VM
                        node_vms = api_get(f'nodes/{node_name}/qemu')
                        
                        for vm in node_vms:
                            vmid = vm.get('vmid', 0)
                            status = vm.get('status', 'unknown')
                            
                            # Solo VM attive
                            if status == 'running':
                                # Dati base VM
                                vm_data = {
                                    'node': node_name,
                                    'vmid': vmid,
                                    'name': vm.get('name', f'VM-{vmid}'),
                                    'status': status,
                                    'cpu': vm.get('cpu', 0),
                                    'maxcpu': vm.get('maxcpu', 0),
                                    'mem': vm.get('mem', 0),
                                    'maxmem': vm.get('maxmem', 0),
                                    'disk': vm.get('disk', 0),
                                    'maxdisk': vm.get('maxdisk', 0),
                                    'uptime': vm.get('uptime', 0),
                                    'netin': vm.get('netin', 0),
                                    'netout': vm.get('netout', 0),
                                    'diskread': vm.get('diskread', 0),
                                    'diskwrite': vm.get('diskwrite', 0),
                                }
                                
                                logger.info(f"        → VM {vmid} ({vm_data['name']}): {status}")
                                
                                # Configurazione VM completa
                                try:
                                    config = api_get(f'nodes/{node_name}/qemu/{vmid}/config')
                                    
                                    if config:
                                        # BIOS e Machine type
                                        vm_data['bios'] = config.get('bios', 'seabios')
                                        vm_data['machine'] = config.get('machine', 'pc')
                                        vm_data['agent'] = '1' if config.get('agent') else '0'
                                        
                                        # CPU dalla configurazione
                                        cores = int(config.get('cores', 1))
                                        sockets = int(config.get('sockets', 1))
                                        vm_data['maxcpu'] = cores * sockets
                                        vm_data['cores'] = cores
                                        vm_data['sockets'] = sockets
                                        
                                        # Dischi
                                        disks = []
                                        disk_details = []
                                        for key in config:
                                            if key.startswith(('scsi', 'sata', 'ide', 'virtio')):
                                                disks.append(key)
                                                disk_info = config[key]
                                                if isinstance(disk_info, str):
                                                    disk_detail = {'id': key}
                                                    parts = disk_info.split(',')
                                                    first_part = parts[0]
                                                    if ':' in first_part:
                                                        storage_vol = first_part.split(':', 1)
                                                        disk_detail['storage'] = storage_vol[0]
                                                        if len(storage_vol) > 1:
                                                            disk_detail['volume'] = storage_vol[1]
                                                    else:
                                                        disk_detail['storage'] = 'N/A'
                                                    
                                                    for part in parts[1:]:
                                                        if '=' in part:
                                                            param_name, param_value = part.split('=', 1)
                                                            if param_name == 'size':
                                                                disk_detail['size'] = param_value
                                                            elif param_name == 'media':
                                                                disk_detail['media'] = param_value
                                                            elif param_name == 'cache':
                                                                disk_detail['cache'] = param_value
                                                    
                                                    disk_details.append(disk_detail)
                                        
                                        vm_data['num_disks'] = len(disks)
                                        vm_data['disks'] = ', '.join(disks) if disks else 'N/A'
                                        vm_data['disks_details'] = json.dumps(disk_details) if disk_details else ''
                                        
                                        # Reti
                                        networks = []
                                        network_details = []
                                        for key in config:
                                            if key.startswith('net'):
                                                networks.append(key)
                                                net_info = config[key]
                                                if isinstance(net_info, str):
                                                    net_detail = {'id': key}
                                                    parts = net_info.split(',')
                                                    first_part = parts[0] if parts else ''
                                                    if '=' in first_part:
                                                        model, mac = first_part.split('=', 1)
                                                        net_detail['model'] = model
                                                        net_detail['mac'] = mac
                                                    
                                                    for part in parts[1:]:
                                                        if '=' in part:
                                                            k, v = part.split('=', 1)
                                                            if k == 'bridge':
                                                                net_detail['bridge'] = v
                                                            elif k == 'tag':
                                                                net_detail['vlan'] = v
                                                            elif k == 'firewall':
                                                                net_detail['firewall'] = v
                                                            elif k == 'rate':
                                                                net_detail['rate'] = v
                                                    
                                                    network_details.append(net_detail)
                                        
                                        vm_data['num_networks'] = len(networks)
                                        vm_data['networks'] = ', '.join(networks) if networks else 'N/A'
                                        vm_data['networks_details'] = json.dumps(network_details) if network_details else ''
                                except Exception as e:
                                    logger.info(f"          ⚠ Errore configurazione VM {vmid}: {e}")
                                    vm_data['bios'] = 'N/A'
                                    vm_data['machine'] = 'N/A'
                                    vm_data['agent'] = '0'
                                    vm_data['num_disks'] = 0
                                    vm_data['disks'] = 'N/A'
                                    vm_data['disks_details'] = ''
                                    vm_data['num_networks'] = 0
                                    vm_data['networks'] = 'N/A'
                                    vm_data['networks_details'] = ''
                                
                                # IP Addresses (solo per VM running)
                                ips = []
                                if status == 'running':
                                    try:
                                        agent_info = api_get(f'nodes/{node_name}/qemu/{vmid}/agent/network-get-interfaces')
                                        if agent_info:
                                            result = agent_info.get('result', agent_info)
                                            if isinstance(result, list):
                                                for iface in result:
                                                    if not isinstance(iface, dict):
                                                        continue
                                                    iface_name = iface.get('name', '').lower()
                                                    if iface_name in ['lo', 'loopback']:
                                                        continue
                                                    if 'ip-addresses' not in iface:
                                                        continue
                                                    ip_addresses = iface.get('ip-addresses', [])
                                                    if not isinstance(ip_addresses, list):
                                                        continue
                                                    for ip_info in ip_addresses:
                                                        if not isinstance(ip_info, dict):
                                                            continue
                                                        ip = ip_info.get('ip-address', '').strip()
                                                        if not ip:
                                                            continue
                                                        if ip.startswith(('127.', '::1', 'fe80:', '169.254.')):
                                                            continue
                                                        ips.append(ip)
                                    except:
                                        pass
                                
                                # Rimuovi duplicati
                                seen = set()
                                unique_ips = []
                                for ip in ips:
                                    if ip not in seen:
                                        seen.add(ip)
                                        unique_ips.append(ip)
                                
                                vm_data['ip_addresses'] = '; '.join(unique_ips) if unique_ips else 'N/A'
                                
                                vms.append(vm_data)
                                logger.info(f"        ✓ VM {vmid} ({vm_data['name']}): {status}")
                    
                    # Se abbiamo trovato VM, ritorna
                    if vms:
                        return vms
                        
                except urllib.error.URLError as e:
                    if 'Connection refused' in str(e) or 'Errno 61' in str(e):
                        logger.info(f"    ⚠ {host}:{port} non raggiungibile (Connection refused)")
                        continue
                    else:
                        logger.info(f"    ⚠ Errore connessione a {host}:{port}: {e}")
                        continue
                except Exception as e:
                    logger.info(f"    ⚠ Errore API {host}:{port}: {e}")
                    continue
            
            return vms
            
        except Exception as e:
            logger.info(f"    ⚠ Errore generale API: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def get_vms_from_local(self):
        """Ottiene lista VM usando comandi locali o SSH"""
        logger.info("→ Estrazione VM da sistema...")
        
        vms = []
        
        # Usa pvesh (Proxmox VE Shell) se disponibile
        if self.execution_mode == 'ssh':
            # Esegui pvesh via SSH
            pvesh_output = self.execute_command('pvesh get /nodes --output-format json')
            if pvesh_output:
                try:
                    nodes_data = json.loads(pvesh_output)
                except:
                    nodes_data = []
            else:
                nodes_data = []
        else:
            # Esegui pvesh localmente
            try:
                result = subprocess.run(
                    ['pvesh', 'get', '/nodes', '--output-format', 'json'],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
            
                if result.returncode == 0:
                    nodes_data = json.loads(result.stdout)
                else:
                    nodes_data = []
            except FileNotFoundError:
                nodes_data = []
            except Exception as e:
                logger.info(f"  ⚠ Errore pvesh: {e}")
                nodes_data = []
        
        if nodes_data:
            for node in nodes_data:
                node_name = node.get('node', '')
                logger.info(f"  → Scanning nodo: {node_name}")
                
                # Ottieni VM del nodo
                if self.execution_mode == 'ssh':
                    vm_output = self.execute_command(f'pvesh get /nodes/{node_name}/qemu --output-format json')
                    if vm_output:
                        try:
                            node_vms = json.loads(vm_output)
                        except:
                            node_vms = []
                    else:
                        node_vms = []
                else:
                    try:
                        vm_result = subprocess.run(
                            ['pvesh', 'get', f'/nodes/{node_name}/qemu', '--output-format', 'json'],
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                    
                        if vm_result.returncode == 0:
                            node_vms = json.loads(vm_result.stdout)
                        else:
                            node_vms = []
                    except Exception as e:
                        logger.info(f"    ⚠ Errore lettura VM nodo {node_name}: {e}")
                        node_vms = []
                
                for vm in node_vms:
                    vmid = vm.get('vmid', 0)
                    status = vm.get('status', 'unknown')
                    
                    # Solo VM attive
                    if status == 'running':
                        vm_data = {
                            'node': node_name,
                            'vmid': vmid,
                            'name': vm.get('name', f'VM-{vmid}'),
                            'status': status,
                            'cpu': vm.get('cpu', 0),
                            'maxcpu': vm.get('maxcpu', 0),
                            'mem': vm.get('mem', 0),
                            'maxmem': vm.get('maxmem', 0),
                            'disk': vm.get('disk', 0),
                            'maxdisk': vm.get('maxdisk', 0),
                            'uptime': vm.get('uptime', 0),
                        }
                        
                        # Ottieni configurazione dettagliata completa (come proxmox_auto_report.py)
                        if self.execution_mode == 'ssh':
                            config_output = self.execute_command(f'pvesh get /nodes/{node_name}/qemu/{vmid}/config --output-format json')
                            if config_output:
                                try:
                                    config = json.loads(config_output)
                                except:
                                    config = None
                            else:
                                config = None
                        else:
                            try:
                                config_result = subprocess.run(
                                    ['pvesh', 'get', f'/nodes/{node_name}/qemu/{vmid}/config', '--output-format', 'json'],
                                    capture_output=True,
                                    text=True,
                                    timeout=30
                                )
                                
                                if config_result.returncode == 0:
                                    config = json.loads(config_result.stdout)
                                else:
                                    config = None
                            except:
                                config = None
                        
                        if config:
                            # BIOS e Machine type
                            vm_data['bios'] = config.get('bios', 'seabios')
                            vm_data['machine'] = config.get('machine', 'pc')
                            vm_data['agent'] = '1' if config.get('agent') else '0'
                            
                            # CPU dalla configurazione
                            cores = int(config.get('cores', 1))
                            sockets = int(config.get('sockets', 1))
                            vm_data['maxcpu'] = cores * sockets
                            vm_data['cores'] = cores
                            vm_data['sockets'] = sockets
                            
                            # Dischi (come proxmox_auto_report.py)
                            disks = []
                            disk_details = []
                            for key in config:
                                if key.startswith(('scsi', 'sata', 'ide', 'virtio')):
                                    disks.append(key)
                                    disk_info = config[key]
                                    if isinstance(disk_info, str):
                                        disk_detail = {'id': key}
                                        parts = disk_info.split(',')
                                        first_part = parts[0]
                                        if ':' in first_part:
                                            storage_vol = first_part.split(':', 1)
                                            disk_detail['storage'] = storage_vol[0]
                                            if len(storage_vol) > 1:
                                                disk_detail['volume'] = storage_vol[1]
                                        else:
                                            disk_detail['storage'] = 'N/A'
                                        
                                        for part in parts[1:]:
                                            if '=' in part:
                                                param_name, param_value = part.split('=', 1)
                                                if param_name == 'size':
                                                    disk_detail['size'] = param_value
                                                elif param_name == 'media':
                                                    disk_detail['media'] = param_value
                                                elif param_name == 'cache':
                                                    disk_detail['cache'] = param_value
                                        
                                        if 'size' not in disk_detail and '=' in parts[0]:
                                            for part in parts:
                                                if '=' in part:
                                                    param_name, param_value = part.split('=', 1)
                                                    if param_name == 'size':
                                                        disk_detail['size'] = param_value
                                        
                                        disk_details.append(disk_detail)
                            
                            vm_data['num_disks'] = len(disks)
                            vm_data['disks'] = ', '.join(disks) if disks else 'N/A'
                            vm_data['disks_details'] = json.dumps(disk_details) if disk_details else ''
                            
                            # Reti (come proxmox_auto_report.py)
                            networks = []
                            network_details = []
                            for key in config:
                                if key.startswith('net'):
                                    networks.append(key)
                                    net_info = config[key]
                                    if isinstance(net_info, str):
                                        net_detail = {'id': key}
                                        parts = net_info.split(',')
                                        first_part = parts[0] if parts else ''
                                        if '=' in first_part:
                                            model, mac = first_part.split('=', 1)
                                            net_detail['model'] = model
                                            net_detail['mac'] = mac
                                        
                                        for part in parts[1:]:
                                            if '=' in part:
                                                k, v = part.split('=', 1)
                                                if k == 'bridge':
                                                    net_detail['bridge'] = v
                                                elif k == 'tag':
                                                    net_detail['vlan'] = v
                                                elif k == 'firewall':
                                                    net_detail['firewall'] = v
                                                elif k == 'rate':
                                                    net_detail['rate'] = v
                                        
                                        network_details.append(net_detail)
                            
                            vm_data['num_networks'] = len(networks)
                            vm_data['networks'] = ', '.join(networks) if networks else 'N/A'
                            vm_data['networks_details'] = json.dumps(network_details) if network_details else ''
                        else:
                            vm_data['bios'] = 'N/A'
                            vm_data['machine'] = 'N/A'
                            vm_data['agent'] = '0'
                            vm_data['num_disks'] = 0
                            vm_data['disks'] = 'N/A'
                            vm_data['disks_details'] = ''
                            vm_data['num_networks'] = 0
                            vm_data['networks'] = 'N/A'
                            vm_data['networks_details'] = ''
                        
                        # IP Addresses via QEMU Guest Agent (solo per VM running)
                        ips = []
                        if status == 'running' and vm_data.get('agent') == '1':
                            # METODO 1: QEMU Guest Agent - network-get-interfaces (PRINCIPALE)
                            try:
                                if self.execution_mode == 'ssh':
                                    agent_output = self.execute_command(f'pvesh get /nodes/{node_name}/qemu/{vmid}/agent/network-get-interfaces --output-format json 2>/dev/null')
                                    if agent_output:
                                        try:
                                            agent_info = json.loads(agent_output)
                                        except:
                                            agent_info = None
                                    else:
                                        agent_info = None
                                else:
                                    try:
                                        agent_result = subprocess.run(
                                            ['pvesh', 'get', f'/nodes/{node_name}/qemu/{vmid}/agent/network-get-interfaces', '--output-format', 'json'],
                                            capture_output=True,
                                            text=True,
                                            timeout=10
                                        )
                                        if agent_result.returncode == 0:
                                            agent_info = json.loads(agent_result.stdout)
                                        else:
                                            agent_info = None
                                    except:
                                        agent_info = None
                                
                                if agent_info:
                                    result = agent_info.get('result', agent_info) if isinstance(agent_info, dict) else agent_info
                                    
                                    if isinstance(result, list):
                                        for iface in result:
                                            if not isinstance(iface, dict):
                                                continue
                                            
                                            iface_name = iface.get('name', '').lower()
                                            if iface_name in ['lo', 'loopback']:
                                                continue
                                            
                                            if 'ip-addresses' not in iface:
                                                continue
                                            
                                            ip_addresses = iface.get('ip-addresses', [])
                                            if not isinstance(ip_addresses, list):
                                                continue
                                            
                                            for ip_info in ip_addresses:
                                                if not isinstance(ip_info, dict):
                                                    continue
                                                
                                                ip = ip_info.get('ip-address', '').strip()
                                                if not ip:
                                                    continue
                                                if ip.startswith(('127.', '::1', 'fe80:', '169.254.')):
                                                    continue
                                                
                                                ips.append(ip)
                            except Exception as e:
                                pass
                        
                        # Rimuovi duplicati
                        seen = set()
                        unique_ips = []
                        for ip in ips:
                            if ip not in seen:
                                seen.add(ip)
                                unique_ips.append(ip)
                        
                        vm_data['ip_addresses'] = '; '.join(unique_ips) if unique_ips else 'N/A'
                        
                        # Aggiungi anche netin, netout, diskread, diskwrite se disponibili
                        vm_data['netin'] = vm.get('netin', 0)
                        vm_data['netout'] = vm.get('netout', 0)
                        vm_data['diskread'] = vm.get('diskread', 0)
                        vm_data['diskwrite'] = vm.get('diskwrite', 0)
                        
                        vms.append(vm_data)
                        logger.info(f"    ✓ VM {vmid} ({vm_data['name']}): {status}")
            
            self.vms_data = vms
            if vms:
                logger.info(f"✓ Trovate {len(vms)} VM attive (via pvesh)")
                return vms
        
        # Se pvesh non ha funzionato, prova metodo alternativo
        if not vms:
            if self.execution_mode == 'ssh':
                logger.info("  ⚠ pvesh non disponibile via SSH, tentativo metodo alternativo...")
            else:
                logger.info("  ⚠ pvesh non trovato, tentativo metodo alternativo...")
        
        # Metodo alternativo 1: API locale
        if not vms:
            vms = self.get_vms_from_local_api()
            if vms:
                self.vms_data = vms
                logger.info(f"✓ Trovate {len(vms)} VM attive (via API locale)")
                return vms
        
        # Metodo alternativo 2: leggi direttamente da /etc/pve/qemu-server
        try:
            qemu_dir = '/etc/pve/qemu-server'
            if os.path.exists(qemu_dir):
                logger.info("  → Lettura da /etc/pve/qemu-server...")
                
                for filename in os.listdir(qemu_dir):
                    if filename.endswith('.conf'):
                        vmid = int(filename.replace('.conf', ''))
                        
                        # Leggi configurazione VM
                        config_file = os.path.join(qemu_dir, filename)
                        vm_data = {'vmid': vmid, 'node': self.hostname or 'local'}
                        
                        try:
                            with open(config_file, 'r') as f:
                                for line in f:
                                    if '=' in line:
                                        key, value = line.strip().split('=', 1)
                                        if key == 'name':
                                            vm_data['name'] = value
                                        elif key == 'cores':
                                            vm_data['cores'] = int(value)
                                        elif key == 'sockets':
                                            vm_data['sockets'] = int(value)
                                        elif key == 'memory':
                                            vm_data['maxmem'] = int(value) * 1024 * 1024  # MB to bytes
                                        elif key == 'bios':
                                            vm_data['bios'] = value
                                        elif key == 'agent':
                                            vm_data['agent'] = '1'
                            
                            # Verifica se VM è running (tentativo)
                            vm_data['status'] = 'unknown'
                            try:
                                result = subprocess.run(
                                    ['qm', 'status', str(vmid)],
                                    capture_output=True,
                                    text=True,
                                    timeout=10
                                )
                                if 'running' in result.stdout.lower():
                                    vm_data['status'] = 'running'
                                    vms.append(vm_data)
                                    logger.info(f"    ✓ VM {vmid} ({vm_data.get('name', 'N/A')}): running")
                            except:
                                pass
                                
                        except Exception as e:
                            logger.info(f"    ⚠ Errore lettura {filename}: {e}")
                
                self.vms_data = vms
                logger.info(f"✓ Trovate {len(vms)} VM")
                return vms
                
        except Exception as e:
            logger.info(f"  ✗ Errore metodo alternativo: {e}")
        
        return []
    
    def create_host_cluster_report(self):
        """Crea report con informazioni host e cluster"""
        report = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'hostname': self.node_info.get('hostname', 'unknown'),
            'node_info': self.node_info,
            'cluster_info': self.cluster_info,
            'vms_count': len(self.vms_data),
            'vms_active': len([v for v in self.vms_data if v.get('status') == 'running'])
        }
        return report
    
    def save_to_csv(self, filename):
        """Salva dati VM in CSV con tutte le informazioni (come proxmox_auto_report.py)"""
        try:
            # Definisci tutti i campi possibili
            all_fields = [
                'node', 'vmid', 'name', 'status',
                'cpu', 'maxcpu', 'cores', 'sockets',
                'mem', 'maxmem', 'memory_mb',
                'disk', 'maxdisk', 'disk_gb',
                'uptime', 'netin', 'netout', 'diskread', 'diskwrite',
                'bios', 'machine', 'agent',
                'num_disks', 'disks', 'disks_details',
                'num_networks', 'networks', 'networks_details',
                'ip_addresses'
            ]
            
            # Prepara dati per CSV
            csv_data = []
            
            # Aggiungi informazioni host e cluster (sempre presenti)
            host_row = {
                'node': 'HOST_INFO',
                'vmid': 'N/A',
                'name': f"Host: {self.node_info.get('hostname', 'unknown')}",
                'status': 'N/A',
                'cpu': 'N/A',
                'maxcpu': 'N/A',
                'cores': self.node_info.get('cpu_count', 0),
                'sockets': 'N/A',
                'mem': 'N/A',
                'maxmem': 'N/A',
                'memory_mb': self.node_info.get('memory_total', 0) / (1024 * 1024) if self.node_info.get('memory_total') else 0,
                'disk': 'N/A',
                'maxdisk': 'N/A',
                'disk_gb': 'N/A',
                'uptime': 'N/A',
                'netin': 'N/A',
                'netout': 'N/A',
                'diskread': 'N/A',
                'diskwrite': 'N/A',
                'bios': 'N/A',
                'machine': 'N/A',
                'agent': 'N/A',
                'num_disks': 'N/A',
                'disks': 'N/A',
                'disks_details': 'N/A',
                'num_networks': 'N/A',
                'networks': 'N/A',
                'networks_details': 'N/A',
                'ip_addresses': 'N/A'
            }
            csv_data.append(host_row)
            
            # Aggiungi informazioni cluster se presente
            if self.cluster_info.get('is_cluster'):
                cluster_row = {
                    'node': 'CLUSTER_INFO',
                    'vmid': 'N/A',
                    'name': f"Cluster: {self.cluster_info.get('cluster_name', 'unknown')}",
                    'status': 'N/A',
                    'cpu': 'N/A',
                    'maxcpu': 'N/A',
                    'cores': len(self.cluster_info.get('nodes', [])),
                    'sockets': 'N/A',
                    'mem': 'N/A',
                    'maxmem': 'N/A',
                    'memory_mb': 0,
                    'disk': 'N/A',
                    'maxdisk': 'N/A',
                    'disk_gb': 'N/A',
                    'uptime': 'N/A',
                    'netin': 'N/A',
                    'netout': 'N/A',
                    'diskread': 'N/A',
                    'diskwrite': 'N/A',
                    'bios': 'N/A',
                    'machine': 'N/A',
                    'agent': 'N/A',
                    'num_disks': 'N/A',
                    'disks': 'N/A',
                    'disks_details': 'N/A',
                    'num_networks': 'N/A',
                    'networks': 'N/A',
                    'networks_details': 'N/A',
                    'ip_addresses': 'N/A'
                }
                csv_data.append(cluster_row)
            
            # Aggiungi VM con tutte le informazioni
            for vm in self.vms_data:
                csv_row = {
                    'node': vm.get('node', 'N/A'),
                    'vmid': vm.get('vmid', 'N/A'),
                    'name': vm.get('name', 'N/A'),
                    'status': vm.get('status', 'N/A'),
                    'cpu': vm.get('cpu', 0),
                    'maxcpu': vm.get('maxcpu', vm.get('cores', 0) * vm.get('sockets', 1) if vm.get('cores') else 0),
                    'cores': vm.get('cores', vm.get('maxcpu', 0)),
                    'sockets': vm.get('sockets', 1),
                    'mem': vm.get('mem', 0),
                    'maxmem': vm.get('maxmem', 0),
                    'memory_mb': vm.get('maxmem', 0) / (1024 * 1024) if vm.get('maxmem') else 0,
                    'disk': vm.get('disk', 0),
                    'maxdisk': vm.get('maxdisk', 0),
                    'disk_gb': vm.get('maxdisk', 0) / (1024**3) if vm.get('maxdisk') else 0,
                    'uptime': vm.get('uptime', 0),
                    'netin': vm.get('netin', 0),
                    'netout': vm.get('netout', 0),
                    'diskread': vm.get('diskread', 0),
                    'diskwrite': vm.get('diskwrite', 0),
                    'bios': vm.get('bios', 'N/A'),
                    'machine': vm.get('machine', 'N/A'),
                    'agent': vm.get('agent', '0'),
                    'num_disks': vm.get('num_disks', 0),
                    'disks': vm.get('disks', 'N/A'),
                    'disks_details': vm.get('disks_details', ''),
                    'num_networks': vm.get('num_networks', 0),
                    'networks': vm.get('networks', 'N/A'),
                    'networks_details': vm.get('networks_details', ''),
                    'ip_addresses': vm.get('ip_addresses', 'N/A')
                }
                csv_data.append(csv_row)
            
            # Salva CSV (anche se non ci sono VM, salva almeno info host)
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                if csv_data:
                    writer = csv.DictWriter(f, fieldnames=all_fields)
                    writer.writeheader()
                    writer.writerows(csv_data)
                    logger.info(f"  ✓ CSV salvato con {len(csv_data)} righe (host info + {len(self.vms_data)} VM)")
            
            return True
        except Exception as e:
            logger.info(f"✗ Errore salvataggio CSV: {e}")
            import traceback
            traceback.print_exc()
            return False

# ============================================================================
# CLASSE SFTP UPLOADER
# ============================================================================

class SFTPUploader:
    """Gestore upload file via SFTP"""
    
    def __init__(self, config):
        self.config = config
        self.sftp_config = config.get('sftp', config.get('scp', {}))  # Supporta sia 'sftp' che 'scp' per backward compatibility
        self.client_config = config.get('client', {})
        self.ssh_client = None
    
    def connect(self):
        """Connette al server remoto via SSH/SFTP con Retry Logic e Failover"""
        if not self.sftp_config.get('enabled'):
            return False
        
        # Primary Configuration
        host = self.sftp_config.get('host')
        port = self.sftp_config.get('port', 22)
        username = self.sftp_config.get('username')
        password = self.sftp_config.get('password')
        
        # Fallback Configuration
        fallback_host = self.sftp_config.get('fallback_host', '192.168.20.14')
        fallback_port = self.sftp_config.get('fallback_port', 22)
        fallback_username = self.sftp_config.get('fallback_username', username)
        fallback_password = self.sftp_config.get('fallback_password', password)
        
        if not all([host, username, password]):
            logger.error("Configurazione SFTP incompleta (mancano credenziali)")
            return False
        
        # Helper per tentare connessione
        def try_connect(target_host, target_port, user=username, pwd=password):
            try:
                self.ssh_client = paramiko.SSHClient()
                self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                logger.info(f"  → Connessione SFTP a {target_host}:{target_port}...")
                self.ssh_client.connect(target_host, port=target_port, username=user, password=pwd, timeout=30)
                logger.info(f"  ✓ Connessione SFTP stabilita con {target_host}")
                return True
            except Exception as e:
                logger.warning(f"  ⚠ Errore connessione SFTP verso {target_host}: {e}")
                return False

        # Retry Logic Primary
        attempts = 3
        delay = 5
        
        for i in range(attempts):
            logger.info(f"Tentativo {i+1}/{attempts} verso Primary ({host})...")
            if try_connect(host, port, user=username, pwd=password):
                return True
            
            if i < attempts - 1:
                logger.info(f"Attendo {delay}s prima di riprovare...")
                time.sleep(delay)
                delay *= 2  # Backoff esponenziale
        
        logger.error(f"Tutti i tentativi verso {host} sono falliti.")
        
        # Failover
        if fallback_host and fallback_host != host:
            logger.info(f"⚠ TENTATIVO FAILOVER verso {fallback_host}...")
            # Un solo tentativo secco per il failover per non bloccare troppo a lungo
            if try_connect(fallback_host, fallback_port, user=fallback_username, pwd=fallback_password):
                 logger.info("✓ Failover riuscito.")
                 return True
            else:
                 logger.error(f"Anche il failover verso {fallback_host} è fallito.")
        
        return False
    
    def create_remote_directory(self, remote_path):
        """Crea directory remota se non esiste"""
        try:
            sftp = self.ssh_client.open_sftp()
            path = (remote_path or '').replace('\\', '/')
            if not path or path == '.':
                sftp.close()
                return True
            
            is_absolute = path.startswith('/')
            parts = [p for p in path.split('/') if p]
            
            try:
                if is_absolute:
                    sftp.chdir('/')
                    current_path = '/'
                else:
                    current_path = sftp.getcwd() or '.'
            except IOError:
                current_path = '/'
            
            for part in parts:
                if current_path in ('', '/'):
                    current_path = ('/' if is_absolute else '') + part
                else:
                    current_path = current_path.rstrip('/') + '/' + part
                try:
                    sftp.chdir(current_path)
                except IOError:
                    try:
                        sftp.mkdir(current_path)
                        sftp.chdir(current_path)
                        logger.debug(f"Creata directory: {current_path}")
                    except Exception as e:
                        logger.error(f"Errore creazione directory remota {current_path}: {e}")
                        sftp.close()
                        return False
            
            sftp.close()
            return True
        except Exception as e:
            logger.error(f"Errore creazione directory remota: {e}")
            return False
    
    def upload_file(self, local_path, remote_path):
        """Carica file su server remoto con retry"""
        attempts = 3
        delay = 5
        
        for i in range(attempts):
            try:
                sftp = self.ssh_client.open_sftp()
                
                # Crea directory padre se necessario
                remote_dir = os.path.dirname(remote_path)
                if remote_dir:
                    self.create_remote_directory(remote_dir)
                
                # Carica file
                sftp.put(local_path, remote_path)
                sftp.close()
                
                file_size = os.path.getsize(local_path) / (1024 * 1024)  # MB
                logger.info(f"Caricato: {os.path.basename(local_path)} ({file_size:.2f} MB)")
                return True
            except Exception as e:
                logger.warning(f"Errore upload {os.path.basename(local_path)}: {e}")
                
                if i < attempts - 1:
                    logger.info("Tento riconnessione e riprovo...")
                    try:
                        self.ssh_client.close()
                    except: 
                        pass
                    
                    if self.connect():
                        # Connessione riuscita, il loop continua e riprova upload
                        pass 
                    else:
                        logger.error("Riconnessione fallita")
                    
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error("Tutti i tentativi di upload sono falliti")
        return False
    
    def upload_files(self, files, base_path):
        """Carica multipli file"""
        codcli = self.client_config.get('codcli', '')
        nomecliente = self.client_config.get('nomecliente', '')
        
        if not codcli or not nomecliente:
            logger.error("codcli o nomecliente non configurati")
            return False
            
        # Modifica: Non creare sottocartella per permettere ingestione diretta
        # base_remote_path = f"{self.sftp_config.get('base_path', '/home/proxmox/uploads')}/{codcli}_{nomecliente}"
        base_remote_path = self.sftp_config.get('base_path', '/home/proxmox/uploads')
        
        logger.info(f"→ Upload SFTP su {self.sftp_config.get('host')}:{base_remote_path}")
        
        if not self.connect():
            return False
        
        success = True
        uploaded_count = 0
        for file_path_str in files:
            file_path = Path(file_path_str)
            if not file_path.exists():
                logger.warning(f"File non trovato: {file_path}")
                continue
                
            remote_path = f"{base_remote_path}/{file_path.name}"
            if self.upload_file(str(file_path), remote_path):
                uploaded_count += 1
            else:
                success = False
        
        logger.info(f"✓ Upload completato: {uploaded_count}/{len(files)} file")
        
        try:
            self.ssh_client.close()
        except:
            pass
            
        return success
    
    def close(self):
        """Chiude connessione SSH"""
        if self.ssh_client:
            self.ssh_client.close()
            logger.info("✓ Connessione SSH chiusa")

# ============================================================================
# CLASSE BACKUP INTEGRATO
# ============================================================================

class ProxmoxBackupIntegrated:
    """Backup configurazione Proxmox integrato"""
    
    def __init__(self, config):
        self.config = config
        self.timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.backup_file = None
        self.execution_mode = 'local'
        self.ssh_client = None
        self.codcli = None
        self.nomecliente = None
        self.server_identifier = None
    
    def create_backup(self, backup_dir, codcli, nomecliente, max_copies=5, server_identifier=None):
        """Crea backup configurazione Proxmox con rotazione"""
        logger.info("→ Creazione backup configurazione...")
        
        self.codcli = codcli
        self.nomecliente = nomecliente
        self.server_identifier = server_identifier
        
        # Genera nome file backup
        backup_base_filename = generate_filename(codcli, nomecliente, 'backup', 'tar.gz', server_identifier=server_identifier)
        self.backup_file = os.path.join(backup_dir, backup_base_filename)
        
        # Ruota file backup esistenti
        rotate_files(backup_dir, backup_base_filename, max_copies)
        
        # File e directory da includere
        backup_paths = [
            '/etc/pve',
            '/etc/network/interfaces',
            '/etc/hosts',
            '/etc/resolv.conf',
            '/etc/corosync',
            '/etc/ssh'
        ]
        
        if self.execution_mode == 'ssh':
            # Backup via SSH
            return self.create_backup_ssh(backup_paths)
        else:
            # Backup locale
            return self.create_backup_local(backup_paths)
    
    def create_backup_local(self, backup_paths):
        """Crea backup locale"""
        # Verifica percorsi esistenti
        existing_paths = [p for p in backup_paths if os.path.exists(p)]
        
        if not existing_paths:
            logger.info("✗ Nessun percorso valido per backup")
            return False
        
        try:
            with tarfile.open(self.backup_file, 'w:gz') as tar:
                for path in existing_paths:
                    try:
                        tar.add(path, arcname=os.path.basename(path), recursive=True)
                        logger.info(f"  ✓ Aggiunto: {path}")
                    except Exception as e:
                        logger.info(f"  ⚠ Errore aggiunta {path}: {e}")
            
            if os.path.exists(self.backup_file):
                file_size = os.path.getsize(self.backup_file) / (1024 * 1024)
                logger.info(f"✓ Backup creato: {self.backup_file} ({file_size:.2f} MB)")
                return True
            else:
                logger.info("✗ Backup non creato")
                return False
                
        except Exception as e:
            logger.info(f"✗ Errore creazione backup: {e}")
            return False
    
    def create_backup_ssh(self, backup_paths):
        """Crea backup via SSH"""
        if not self.ssh_client:
            logger.info("✗ Connessione SSH non disponibile")
            return False
        
        try:
            # Crea backup remoto
            remote_backup = f"/tmp/proxmox_config_backup_{self.timestamp}.tar.gz"
            backup_cmd = f"tar czf {remote_backup} {' '.join(backup_paths)} 2>&1"
            
            logger.info(f"  → Esecuzione backup remoto...")
            stdin, stdout, stderr = self.ssh_client.exec_command(backup_cmd, timeout=300)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status == 0:
                # Trasferisci file locale
                logger.info(f"  → Trasferimento file in locale...")
                sftp = self.ssh_client.open_sftp()
                sftp.get(remote_backup, self.backup_file)
                sftp.close()
                
                # Rimuovi file remoto
                self.ssh_client.exec_command(f"rm -f {remote_backup}")
                
                if os.path.exists(self.backup_file):
                    file_size = os.path.getsize(self.backup_file) / (1024 * 1024)
                    logger.info(f"✓ Backup creato: {self.backup_file} ({file_size:.2f} MB)")
                    return True
                else:
                    logger.info("✗ Trasferimento file fallito")
                    return False
            else:
                error = stderr.read().decode().strip()
                logger.info(f"✗ Errore backup remoto: {error}")
                return False
                
        except Exception as e:
            logger.info(f"✗ Errore backup SSH: {e}")
            return False
    
    def get_backup_file(self):
        """Ritorna path file backup"""
        return self.backup_file

# ============================================================================
# GESTIONE FILE CON ROTAZIONE
# ============================================================================

def generate_filename(codcli, nomecliente, file_type, extension='csv', server_identifier=None):
    """Genera nome file nel formato codcli_nomecliente[_server]_prox_[tipo]."""
    codcli_clean = str(codcli).strip().replace(' ', '_')
    nomecliente_clean = str(nomecliente).strip().replace(' ', '_')
    server_identifier_clean = None
    if server_identifier:
        server_identifier_clean = str(server_identifier).strip().replace(' ', '_')
    
    parts = [codcli_clean, nomecliente_clean]
    if server_identifier_clean:
        parts.append(server_identifier_clean)
    
    filename = f"{'_'.join(parts)}_prox_{file_type}.{extension}"
    return filename

def rotate_files(directory, base_filename, max_copies=5):
    """Ruota file mantenendo al massimo max_copies copie"""
    if not os.path.exists(directory):
        return
    
    try:
        # Trova tutti i file che corrispondono al pattern
        # Pattern: base_filename, base_filename.1, base_filename.2, etc.
        existing_files = {}
        
        # File principale (senza numero)
        main_file = os.path.join(directory, base_filename)
        if os.path.exists(main_file):
            existing_files[0] = main_file
        
        # File numerati (1, 2, 3, ...)
        for i in range(1, max_copies):
            numbered_file = os.path.join(directory, f"{base_filename}.{i}")
            if os.path.exists(numbered_file):
                existing_files[i] = numbered_file
        
        # Se non ci sono file esistenti, non fare nulla
        if not existing_files:
            return
        
        # Se abbiamo più file del massimo, elimina i più vecchi
        if len(existing_files) >= max_copies:
            # Rimuovi i file più vecchi (quelli con numero più alto)
            sorted_nums = sorted(existing_files.keys(), reverse=True)
            files_to_remove = sorted_nums[:len(existing_files) - max_copies + 1]
            for num in files_to_remove:
                try:
                    os.remove(existing_files[num])
                    logger.info(f"  🗑️ Rimosso file vecchio: {os.path.basename(existing_files[num])}")
                    del existing_files[num]
                except Exception as e:
                    logger.info(f"  ⚠ Errore rimozione {os.path.basename(existing_files[num])}: {e}")
        
        # Rinomina file esistenti (sposta avanti di 1)
        # IMPORTANTE: farlo in ordine inverso per evitare sovrascritture
        # Esempio: file.2 -> file.3, file.1 -> file.2, file -> file.1
        sorted_nums = sorted(existing_files.keys(), reverse=True)
        for num in sorted_nums:
            if num < max_copies - 1:  # Non creare file oltre max_copies-1
                old_filepath = existing_files[num]
                new_num = num + 1
                new_filepath = os.path.join(directory, f"{base_filename}.{new_num}")
                try:
                    # Se il file di destinazione esiste già, rimuovilo prima
                    if os.path.exists(new_filepath):
                        os.remove(new_filepath)
                    os.rename(old_filepath, new_filepath)
                except Exception as e:
                    logger.info(f"  ⚠ Errore rinomina {os.path.basename(old_filepath)}: {e}")
        
    except Exception as e:
        logger.info(f"  ⚠ Errore rotazione file: {e}")

def feature_enabled(features, key, default=True):
    if not features:
        return default
    return features.get(key, default)


def save_file_with_rotation(filepath, content_func, codcli, nomecliente, file_type, extension='csv', max_copies=5, server_identifier=None):
    """Salva file con rotazione automatica"""
    directory = os.path.dirname(filepath)
    base_filename = generate_filename(codcli, nomecliente, file_type, extension, server_identifier=server_identifier)
    final_filepath = os.path.join(directory, base_filename)
    
    # Ruota file esistenti
    rotate_files(directory, base_filename, max_copies)
    
    # Salva nuovo file
    try:
        if callable(content_func):
            # Se è una funzione, chiamala per salvare
            success = content_func(final_filepath)
            if success and os.path.exists(final_filepath):
                return final_filepath
        else:
            # Se è contenuto diretto, salvalo
            with open(final_filepath, 'w', encoding='utf-8') as f:
                f.write(content_func)
            return final_filepath
    except Exception as e:
        logger.info(f"  ⚠ Errore salvataggio {base_filename}: {e}")
        return None

# ============================================================================
# CARICAMENTO CONFIGURAZIONE
# ============================================================================

def load_config(config_file):
    """Carica file configurazione"""
    if not os.path.exists(config_file):
        logger.info(f"✗ File configurazione non trovato: {config_file}")
        return None
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"✓ Configurazione caricata: {config_file}")
        return config
    except Exception as e:
        logger.info(f"✗ Errore lettura configurazione: {e}")
        return None

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Funzione principale"""
    logger.info("=" * 70)
    logger.info("PROXMOX LOCAL REPORT GENERATOR")
    logger.info("=" * 70)
    logger.info()
    
    # Parse argomenti
    parser = argparse.ArgumentParser(
        description='Genera report Proxmox locale con upload SCP',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--config', default=DEFAULT_CONFIG_FILE,
                        help='File configurazione (default: config.json)')
    parser.add_argument('--no-sftp', action='store_true',
                        help='Non caricare file su server remoto via SFTP')
    parser.add_argument('--no-scp', action='store_true', dest='no_sftp',
                        help='Alias per --no-sftp (backward compatibility)')
    parser.add_argument('--output-dir', dest='output_dir',
                        help='Directory output locale (override config)')
    
    args = parser.parse_args()
    
    # Carica config
    config = load_config(args.config)
    if not config:
        sys.exit(1)
    
    # Verifica configurazione client
    client_config = config.get('client', {})
    codcli = client_config.get('codcli', '')
    nomecliente = client_config.get('nomecliente', '')
    server_identifier = client_config.get('server_identifier', '')

    # Feature toggles
    features_config = config.get('features', {}) or {}
    collect_cluster = feature_enabled(features_config, 'collect_cluster', True)
    collect_host = feature_enabled(features_config, 'collect_host', True)
    collect_host_details = feature_enabled(features_config, 'collect_host_details', True)
    collect_storage = feature_enabled(features_config, 'collect_storage', True)
    collect_network = feature_enabled(features_config, 'collect_network', True)
    collect_vms = feature_enabled(features_config, 'collect_vms', True)
    collect_backup = feature_enabled(features_config, 'collect_backup', True)
    collect_containers = feature_enabled(features_config, 'collect_containers', False)
    collect_perf = feature_enabled(features_config, 'collect_perf', False)

    if collect_containers:
        logger.info("⚠ Raccolta container non ancora implementata")
    if collect_perf:
        logger.info("⚠ Raccolta performance (pveperf) non ancora implementata")
    
    if not codcli or not nomecliente:
        logger.info("⚠ ATTENZIONE: codcli o nomecliente non configurati")
        logger.info("  I file verranno generati localmente ma non caricati su server remoto")
        no_sftp = getattr(args, 'no_sftp', False) or getattr(args, 'no_scp', False)
        if not no_sftp:
            logger.info("  Configura codcli e nomecliente in config.json per abilitare upload SFTP")
    
    # Directory output
    system_config = config.get('system', {})
    output_dir = args.output_dir or system_config.get('output_directory', 'reports')
    csv_dir = os.path.join(output_dir, 'csv')
    backup_dir = os.path.join(output_dir, 'backup')
    max_file_copies = system_config.get('max_file_copies', 5)
    
    os.makedirs(csv_dir, exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)
    
    logger.info(f"→ Directory output: {output_dir}")
    logger.info()
    
    # Estrazione dati
    logger.info("=" * 70)
    logger.info("ESTRAZIONE DATI PROXMOX")
    logger.info("=" * 70)
    logger.info()
    
    extractor = ProxmoxLocalExtractor(config, features_config)
    
    # Rileva modalità esecuzione
    execution_mode = extractor.detect_execution_mode()
    logger.info()
    
    # Se modalità SSH, connetti
    if execution_mode == 'ssh':
        if not extractor.connect_ssh():
            logger.info("✗ Impossibile connettersi via SSH, fallback all'API...")
            extractor.execution_mode = 'api'
        logger.info()
    
    # Informazioni nodo
    extractor.get_node_info()
    logger.info()
    
    if collect_cluster:
        extractor.get_cluster_info()
    else:
        logger.info("→ Raccolta informazioni cluster disabilitata")
    logger.info()
    
    # Informazioni dettagliate host (da tutti i nodi del cluster via API)
    # IMPORTANTE: Usa sempre l'API anche in modalità locale per ottenere dati corretti di ogni nodo
    logger.info("→ Estrazione informazioni dettagliate da tutti gli host del cluster (via API)...")
    all_hosts_info = extractor.get_all_hosts_info()
    logger.info()
    
    # Estrazione host/local storage/network se abilitato
    all_hosts_info = []
    if collect_host or collect_storage or collect_network or collect_host_details:
        logger.info("→ Estrazione informazioni host locali...")
        all_hosts_info = extractor.get_all_hosts_info()
        logger.info()
        if all_hosts_info and extractor.execution_mode != 'api' and (collect_network or collect_host_details):
            current_hostname = extractor.node_info.get('hostname')
            for host_info in all_hosts_info:
                if host_info.get('hostname') == current_hostname:
                    extractor.enrich_host_info_with_commands(host_info, extractor.execute_command)
                    break
    else:
        logger.info("→ Raccolta informazioni host disabilitata")
        logger.info()
    
    # Host principale per report
    if all_hosts_info:
        detailed_host_info = all_hosts_info[0]
    else:
        detailed_host_info = extractor.get_detailed_host_info() if (collect_host or collect_host_details) else {}
        if detailed_host_info:
            all_hosts_info = [detailed_host_info]
        else:
            all_hosts_info = []
    
    # VM
    vms = []
    csv_file = None
    if collect_vms:
        logger.info("→ Estrazione VM da sistema...")
        vms = extractor.get_vms_from_local()
        logger.info()
        if not vms:
            logger.info("⚠ Nessuna VM attiva trovata")
            logger.info("  ℹ Verifica:")
            logger.info("    - Sei su un host Proxmox?")
            logger.info("    - L'API Proxmox è raggiungibile?")
            logger.info("    - Ci sono VM in stato 'running'?")
            logger.info("  ℹ Il CSV verrà comunque generato con informazioni host")
        csv_base_filename = generate_filename(codcli, nomecliente, 'vms', 'csv', server_identifier=server_identifier)
        csv_file = os.path.join(csv_dir, csv_base_filename)
        logger.info("→ Rotazione file CSV VM...")
        rotate_files(csv_dir, csv_base_filename, max_file_copies)
        logger.info("→ Salvataggio CSV VM...")
        if extractor.save_to_csv(csv_file):
            if os.path.exists(csv_file):
                file_size = os.path.getsize(csv_file) / 1024
                logger.info(f"✓ CSV VM salvato: {csv_file} ({file_size:.1f} KB)")
            else:
                logger.info("✗ File CSV non creato")
                sys.exit(1)
        else:
            logger.info("✗ Errore salvataggio CSV")
            sys.exit(1)
        logger.info()
    else:
        extractor.vms_data = []
        logger.info("→ Raccolta VM disabilitata")
        logger.info()
    
    # Report host/cluster
    host_report = extractor.create_host_cluster_report()
    host_report['server_identifier'] = server_identifier
    
    # Verifica codcli e nomecliente
    if not codcli or not nomecliente:
        logger.info("✗ codcli o nomecliente non configurati in config.json")
        logger.info("  Configurazione richiesta:")
        logger.info("  - client.codcli")
        logger.info("  - client.nomecliente")
        sys.exit(1)
    
    # Configurazione rotazione file
    max_file_copies = config.get('system', {}).get('max_file_copies', 5)
    
    # Salva CSV VM (anche senza VM, salva info host)
    csv_base_filename = generate_filename(codcli, nomecliente, 'vms', 'csv', server_identifier=server_identifier)
    csv_file = os.path.join(csv_dir, csv_base_filename)
    
    # Ruota file CSV VM esistenti
    logger.info("→ Rotazione file CSV VM...")
    rotate_files(csv_dir, csv_base_filename, max_file_copies)
    
    logger.info("→ Salvataggio CSV VM...")
    if extractor.save_to_csv(csv_file):
        if os.path.exists(csv_file):
            file_size = os.path.getsize(csv_file) / 1024
            logger.info(f"✓ CSV VM salvato: {csv_file} ({file_size:.1f} KB)")
        else:
            logger.info("✗ File CSV non creato")
            sys.exit(1)
    else:
        logger.info("✗ Errore salvataggio CSV")
        sys.exit(1)
    
    logger.info()
    
    # Salva CSV host dettagliato
    host_csv_file = os.path.join(csv_dir, generate_filename(codcli, nomecliente, 'hosts', 'csv', server_identifier=server_identifier))
    storage_file = None
    network_file = None
    
    logger.info("→ Salvataggio CSV host dettagliato...")
    success, storage_file, network_file = extractor.save_host_info_to_csv(
        all_hosts_info, host_csv_file, codcli, nomecliente, max_file_copies, server_identifier=server_identifier
    )
    if success:
        if os.path.exists(host_csv_file):
            file_size = os.path.getsize(host_csv_file) / 1024
            logger.info(f"✓ CSV host salvato: {host_csv_file} ({file_size:.1f} KB)")
        if storage_file and os.path.exists(storage_file):
            file_size = os.path.getsize(storage_file) / 1024
            logger.info(f"✓ CSV storage salvato: {storage_file} ({file_size:.1f} KB)")
        if network_file and os.path.exists(network_file):
            file_size = os.path.getsize(network_file) / 1024
            logger.info(f"✓ CSV network salvato: {network_file} ({file_size:.1f} KB)")
    else:
        logger.info("⚠ Errore salvataggio CSV host (continuo comunque)")
    
    logger.info()
    
    # Backup configurazione (solo se locale o SSH)
    if extractor.execution_mode in ['local', 'ssh']:
        logger.info("=" * 70)
        logger.info("BACKUP CONFIGURAZIONE")
        logger.info("=" * 70)
        logger.info()
        
        backup_manager = ProxmoxBackupIntegrated(config)
        backup_manager.execution_mode = extractor.execution_mode
        backup_manager.ssh_client = extractor.ssh_client if extractor.execution_mode == 'ssh' else None
        
        if backup_manager.create_backup(backup_dir, codcli, nomecliente, max_file_copies, server_identifier=server_identifier):
            backup_file = backup_manager.get_backup_file()
        else:
            backup_file = None
            logger.info("⚠ Backup configurazione non creato")
        
        logger.info()
    else:
        backup_file = None
        logger.info("→ Backup configurazione saltato (modalità API)")
        logger.info()
    
    # Chiudi connessione SSH se aperta
    if extractor.ssh_client:
        extractor.ssh_client.close()
        logger.info("✓ Connessione SSH chiusa")
        logger.info()
    
    # Upload SFTP
    no_sftp = getattr(args, 'no_sftp', False) or getattr(args, 'no_scp', False)
    sftp_config = config.get('sftp', config.get('scp', {}))  # Supporta sia 'sftp' che 'scp'
    
    if not no_sftp and sftp_config.get('enabled'):
        logger.info("=" * 70)
        logger.info("UPLOAD SFTP")
        logger.info("=" * 70)
        logger.info()
        
        if codcli and nomecliente:
            sftp_uploader = SFTPUploader(config)
            
            if sftp_uploader.connect():
                files_to_upload = []
                if csv_file and os.path.exists(csv_file):
                    files_to_upload.append(csv_file)
                if collect_host and host_csv_file and os.path.exists(host_csv_file):
                    files_to_upload.append(host_csv_file)
                if storage_file and os.path.exists(storage_file):
                    files_to_upload.append(storage_file)
                if network_file and os.path.exists(network_file):
                    files_to_upload.append(network_file)
                if backup_file:
                    files_to_upload.append(backup_file)
                
                base_path = sftp_config.get('base_path', '/backups/proxmox')
                sftp_uploader.upload_files(files_to_upload, base_path)
                sftp_uploader.close()
            else:
                logger.info("✗ Connessione SFTP fallita")
        else:
            logger.info("⚠ codcli o nomecliente non configurati, skip upload SFTP")
    else:
        logger.info("→ Upload SFTP disabilitato")
    
    # Riepilogo
    logger.info()
    logger.info("=" * 70)
    logger.info("✓ PROCESSO COMPLETATO")
    logger.info("=" * 70)
    logger.info()
    logger.info("File generati:")
    if csv_file and os.path.exists(csv_file):
        logger.info(f"  📄 CSV VM:  {csv_file}")
    if collect_host and host_csv_file and os.path.exists(host_csv_file):
        logger.info(f"  📄 CSV Host:  {host_csv_file}")
    if collect_storage and storage_file and os.path.exists(storage_file):
        logger.info(f"  📄 CSV Storage:  {storage_file}")
    if collect_network and network_file and os.path.exists(network_file):
        logger.info(f"  📄 CSV Network:  {network_file}")
    if backup_file:
        logger.info(f"  📦 Backup: {backup_file}")
    logger.info()
    logger.info(f"Hostname: {host_report['hostname']}")
    if server_identifier:
        logger.info(f"Server identifier: {server_identifier}")
    if collect_vms:
        logger.info(f"VM attive: {host_report['vms_active']}/{host_report['vms_count']}")
    if collect_cluster and host_report['cluster_info']['is_cluster']:
        logger.info(f"Cluster: {host_report['cluster_info']['cluster_name']}")
    logger.info()

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\n⚠ Operazione interrotta dall'utente")
        sys.exit(0)
    except Exception as e:
        logger.info(f"\n✗ Errore critico: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

