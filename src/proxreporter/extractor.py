"""
Proxmox data extraction module.

Provides data extraction from Proxmox via API and local commands.
"""

import json
import logging
import ssl
import subprocess
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from http.cookiejar import CookieJar

from .exceptions import ExtractionError, APIConnectionError, AuthenticationError
from .ssh import SSHConnection, create_executor
from .utils import (
    bytes_to_gib,
    seconds_to_human,
    safe_round,
    safe_int,
    safe_float,
    safe_divide,
    calculate_percentage,
    get_hostname,
)

logger = logging.getLogger("proxreporter.extractor")


class ProxmoxAPIClient:
    """
    Proxmox API client with connection management.
    
    Features:
    - Cookie-based session authentication
    - Configurable SSL verification
    - Request timeout handling
    - Error handling with specific exceptions
    """
    
    DEFAULT_TIMEOUT = 30
    
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """
        Initialize API client.
        
        Args:
            host: Proxmox host (host:port format).
            username: API username (e.g., root@pam).
            password: API password.
            verify_ssl: Whether to verify SSL certificates.
            timeout: Request timeout in seconds.
        """
        self.host = host if ':' in host else f"{host}:8006"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        
        self._ticket: Optional[str] = None
        self._csrf_token: Optional[str] = None
        self._ssl_context: Optional[ssl.SSLContext] = None
        self._cookie_jar = CookieJar()
    
    def _get_ssl_context(self) -> ssl.SSLContext:
        """Get or create SSL context."""
        if self._ssl_context is None:
            if self.verify_ssl:
                self._ssl_context = ssl.create_default_context()
            else:
                self._ssl_context = ssl.create_default_context()
                self._ssl_context.check_hostname = False
                self._ssl_context.verify_mode = ssl.CERT_NONE
                logger.warning(
                    "SSL verification disabled. This is insecure for production."
                )
        return self._ssl_context
    
    def authenticate(self) -> bool:
        """
        Authenticate with Proxmox API.
        
        Returns:
            True if authentication successful.
            
        Raises:
            AuthenticationError: If authentication fails.
        """
        url = f"https://{self.host}/api2/json/access/ticket"
        
        data = urllib.parse.urlencode({
            'username': self.username,
            'password': self.password,
        }).encode('utf-8')
        
        try:
            request = urllib.request.Request(url, data=data, method='POST')
            response = urllib.request.urlopen(
                request,
                context=self._get_ssl_context(),
                timeout=self.timeout
            )
            
            result = json.loads(response.read().decode('utf-8'))
            data = result.get('data', {})
            
            self._ticket = data.get('ticket')
            self._csrf_token = data.get('CSRFPreventionToken')
            
            if not self._ticket:
                raise AuthenticationError(
                    "No ticket received from API",
                    host=self.host,
                    username=self.username
                )
            
            logger.info(f"Authenticated with Proxmox API at {self.host}")
            return True
            
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AuthenticationError(
                    "Invalid credentials",
                    host=self.host,
                    username=self.username
                )
            raise APIConnectionError(
                f"API authentication failed (HTTP {e.code})",
                host=self.host,
                details=str(e)
            )
        except urllib.error.URLError as e:
            raise APIConnectionError(
                "Failed to connect to Proxmox API",
                host=self.host,
                details=str(e)
            )
        except Exception as e:
            raise APIConnectionError(
                "Unexpected error during authentication",
                host=self.host,
                details=str(e)
            )
    
    def get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """
        Make GET request to API endpoint.
        
        Args:
            endpoint: API endpoint (e.g., '/nodes', '/cluster/status').
            
        Returns:
            Response data or None on failure.
        """
        if not self._ticket:
            self.authenticate()
        
        # Normalize endpoint
        if not endpoint.startswith('/'):
            endpoint = '/' + endpoint
        
        url = f"https://{self.host}/api2/json{endpoint}"
        
        try:
            request = urllib.request.Request(url)
            request.add_header('Cookie', f"PVEAuthCookie={self._ticket}")
            
            response = urllib.request.urlopen(
                request,
                context=self._get_ssl_context(),
                timeout=self.timeout
            )
            
            result = json.loads(response.read().decode('utf-8'))
            return result.get('data')
            
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Token expired, re-authenticate
                self._ticket = None
                return self.get(endpoint)
            logger.warning(f"API request failed for {endpoint}: HTTP {e.code}")
            return None
        except Exception as e:
            logger.warning(f"API request failed for {endpoint}: {e}")
            return None


class ProxmoxExtractor:
    """
    Proxmox data extractor with API and local command support.
    
    Features:
    - Parallel data extraction
    - API and local command fallback
    - Structured data output
    - Progress tracking
    """
    
    def __init__(
        self,
        api_client: Optional[ProxmoxAPIClient] = None,
        ssh_connection: Optional[SSHConnection] = None,
        local_mode: bool = True,
        max_workers: int = 4,
    ):
        """
        Initialize extractor.
        
        Args:
            api_client: Proxmox API client.
            ssh_connection: SSH connection for remote commands.
            local_mode: Whether running on the Proxmox host.
            max_workers: Maximum parallel workers for data extraction.
        """
        self.api = api_client
        self.ssh = ssh_connection
        self.local_mode = local_mode
        self.max_workers = max_workers
        
        # Create command executor
        self._executor = create_executor(ssh_connection, local_mode)
    
    def run_command(self, command: str) -> str:
        """Execute a command and return output."""
        return self._executor(command)
    
    def run_pvesh(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """
        Run pvesh command to get API data.
        
        Args:
            endpoint: API endpoint.
            
        Returns:
            Parsed JSON data or None.
        """
        try:
            output = self.run_command(f"pvesh get {endpoint} --output-format json 2>/dev/null")
            if output:
                return json.loads(output)
        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.debug(f"pvesh {endpoint} failed: {e}")
        return None
    
    def get_cluster_status(self) -> Optional[Dict[str, Any]]:
        """Get cluster status information."""
        if self.api:
            return self.api.get('/cluster/status')
        return self.run_pvesh('/cluster/status')
    
    def get_nodes(self) -> List[Dict[str, Any]]:
        """Get list of cluster nodes."""
        data = None
        
        if self.api:
            data = self.api.get('/nodes')
        
        if not data:
            data = self.run_pvesh('/nodes')
        
        return data if isinstance(data, list) else []
    
    def get_node_status(self, node: str) -> Optional[Dict[str, Any]]:
        """Get detailed status for a specific node."""
        if self.api:
            return self.api.get(f'/nodes/{node}/status')
        return self.run_pvesh(f'/nodes/{node}/status')
    
    def get_vms(self, node: str) -> List[Dict[str, Any]]:
        """Get VMs for a specific node."""
        data = None
        
        if self.api:
            data = self.api.get(f'/nodes/{node}/qemu')
        
        if not data:
            data = self.run_pvesh(f'/nodes/{node}/qemu')
        
        return data if isinstance(data, list) else []
    
    def get_vm_config(self, node: str, vmid: int) -> Optional[Dict[str, Any]]:
        """Get configuration for a specific VM."""
        if self.api:
            return self.api.get(f'/nodes/{node}/qemu/{vmid}/config')
        return self.run_pvesh(f'/nodes/{node}/qemu/{vmid}/config')
    
    def get_containers(self, node: str) -> List[Dict[str, Any]]:
        """Get LXC containers for a specific node."""
        data = None
        
        if self.api:
            data = self.api.get(f'/nodes/{node}/lxc')
        
        if not data:
            data = self.run_pvesh(f'/nodes/{node}/lxc')
        
        return data if isinstance(data, list) else []
    
    def get_storage(self, node: str) -> List[Dict[str, Any]]:
        """Get storage information for a node."""
        data = None
        
        if self.api:
            data = self.api.get(f'/nodes/{node}/storage')
        
        if not data:
            data = self.run_pvesh(f'/nodes/{node}/storage')
        
        return data if isinstance(data, list) else []
    
    def get_network(self, node: str) -> List[Dict[str, Any]]:
        """Get network interfaces for a node."""
        data = None
        
        if self.api:
            data = self.api.get(f'/nodes/{node}/network')
        
        if not data:
            data = self.run_pvesh(f'/nodes/{node}/network')
        
        return data if isinstance(data, list) else []
    
    def extract_host_info(self) -> Dict[str, Any]:
        """
        Extract detailed host information.
        
        Returns:
            Dictionary with host details.
        """
        info = {
            'hostname': get_hostname(),
            'fqdn': '',
            'ip_address': '',
            'cpu_model': '',
            'cpu_cores': 0,
            'cpu_sockets': 0,
            'cpu_threads': 0,
            'memory_total_gb': 0,
            'memory_used_gb': 0,
            'memory_usage_percent': 0,
            'pve_version': '',
            'kernel_version': '',
            'uptime_seconds': 0,
            'uptime_human': '',
            'lic_status': 'unknown',
            'lic_level': '',
            'lic_key': '',
        }
        
        # Get PVE version
        try:
            output = self.run_command('pveversion 2>/dev/null')
            if output:
                info['pve_version'] = output.strip().split('\n')[0]
        except Exception:
            pass
        
        # Get kernel version
        try:
            output = self.run_command('uname -r 2>/dev/null')
            if output:
                info['kernel_version'] = output.strip()
        except Exception:
            pass
        
        # Get CPU info
        try:
            output = self.run_command('lscpu 2>/dev/null')
            if output:
                for line in output.split('\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        key = key.strip().lower()
                        value = value.strip()
                        
                        if 'model name' in key:
                            info['cpu_model'] = value
                        elif key == 'cpu(s)':
                            info['cpu_threads'] = safe_int(value)
                        elif 'core(s) per socket' in key:
                            info['cpu_cores'] = safe_int(value)
                        elif 'socket(s)' in key:
                            info['cpu_sockets'] = safe_int(value)
        except Exception:
            pass
        
        # Get memory info
        try:
            output = self.run_command('free -b 2>/dev/null')
            if output:
                for line in output.split('\n'):
                    if line.startswith('Mem:'):
                        parts = line.split()
                        if len(parts) >= 3:
                            total = safe_int(parts[1])
                            used = safe_int(parts[2])
                            info['memory_total_gb'] = bytes_to_gib(total)
                            info['memory_used_gb'] = bytes_to_gib(used)
                            if total > 0:
                                info['memory_usage_percent'] = calculate_percentage(used, total)
        except Exception:
            pass
        
        # Get uptime
        try:
            output = self.run_command('cat /proc/uptime 2>/dev/null')
            if output:
                uptime_seconds = safe_float(output.split()[0])
                info['uptime_seconds'] = int(uptime_seconds)
                info['uptime_human'] = seconds_to_human(uptime_seconds)
        except Exception:
            pass
        
        # Get subscription status
        try:
            output = self.run_command('pvesubscription get 2>/dev/null')
            if output:
                for line in output.split('\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        key = key.strip().lower()
                        value = value.strip()
                        
                        if key == 'status':
                            info['lic_status'] = value
                        elif key == 'level':
                            info['lic_level'] = value
                        elif key == 'key':
                            info['lic_key'] = value
        except Exception:
            pass
        
        # Get hostname/FQDN
        try:
            output = self.run_command('hostname -f 2>/dev/null')
            if output:
                info['fqdn'] = output.strip()
        except Exception:
            pass
        
        # Get IP address
        try:
            output = self.run_command("hostname -I 2>/dev/null | awk '{print $1}'")
            if output:
                info['ip_address'] = output.strip()
        except Exception:
            pass
        
        return info
    
    def extract_all_vms(self) -> List[Dict[str, Any]]:
        """
        Extract all VMs from all nodes with parallel processing.
        
        Returns:
            List of VM dictionaries.
        """
        all_vms = []
        nodes = self.get_nodes()
        
        if not nodes:
            logger.warning("No nodes found")
            return all_vms
        
        def process_node(node_info: Dict[str, Any]) -> List[Dict[str, Any]]:
            node_name = node_info.get('node', '')
            if not node_name:
                return []
            
            vms = []
            
            # Get VMs
            for vm in self.get_vms(node_name):
                vmid = vm.get('vmid')
                if vmid:
                    # Get detailed config
                    config = self.get_vm_config(node_name, vmid) or {}
                    
                    vms.append({
                        'vmid': vmid,
                        'name': vm.get('name', f'VM-{vmid}'),
                        'type': 'qemu',
                        'status': vm.get('status', 'unknown'),
                        'node': node_name,
                        'cpus': config.get('cores', vm.get('cpus', 0)),
                        'memory_gb': bytes_to_gib(
                            config.get('memory', vm.get('maxmem', 0)) * 1024 * 1024
                        ),
                        'disk_gb': bytes_to_gib(vm.get('maxdisk', 0)),
                        'os_type': config.get('ostype', ''),
                        'uptime': seconds_to_human(vm.get('uptime', 0)),
                        'description': config.get('description', ''),
                    })
            
            # Get containers
            for ct in self.get_containers(node_name):
                vmid = ct.get('vmid')
                if vmid:
                    vms.append({
                        'vmid': vmid,
                        'name': ct.get('name', f'CT-{vmid}'),
                        'type': 'lxc',
                        'status': ct.get('status', 'unknown'),
                        'node': node_name,
                        'cpus': ct.get('cpus', 0),
                        'memory_gb': bytes_to_gib(ct.get('maxmem', 0)),
                        'disk_gb': bytes_to_gib(ct.get('maxdisk', 0)),
                        'os_type': 'linux',
                        'uptime': seconds_to_human(ct.get('uptime', 0)),
                    })
            
            return vms
        
        # Process nodes in parallel
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(process_node, node): node 
                for node in nodes
            }
            
            for future in as_completed(futures):
                try:
                    vms = future.result()
                    all_vms.extend(vms)
                except Exception as e:
                    node = futures[future]
                    logger.error(f"Error processing node {node.get('node')}: {e}")
        
        logger.info(f"Extracted {len(all_vms)} VMs from {len(nodes)} nodes")
        return all_vms
    
    def extract_all_storage(self) -> List[Dict[str, Any]]:
        """
        Extract storage information from all nodes.
        
        Returns:
            List of storage dictionaries.
        """
        all_storage = []
        nodes = self.get_nodes()
        
        for node_info in nodes:
            node_name = node_info.get('node', '')
            if not node_name:
                continue
            
            for storage in self.get_storage(node_name):
                total = storage.get('total', 0)
                used = storage.get('used', 0)
                avail = storage.get('avail', 0)
                
                all_storage.append({
                    'hostname': node_name,
                    'storage_name': storage.get('storage', ''),
                    'storage_type': storage.get('type', ''),
                    'total_gb': bytes_to_gib(total),
                    'used_gb': bytes_to_gib(used),
                    'available_gb': bytes_to_gib(avail),
                    'usage_percent': calculate_percentage(used, total) if total > 0 else 0,
                    'content': storage.get('content', ''),
                    'shared': storage.get('shared', 0) == 1,
                    'active': storage.get('active', 0) == 1,
                })
        
        return all_storage
    
    def extract_all_network(self) -> List[Dict[str, Any]]:
        """
        Extract network interface information from all nodes.
        
        Returns:
            List of interface dictionaries.
        """
        all_interfaces = []
        nodes = self.get_nodes()
        
        for node_info in nodes:
            node_name = node_info.get('node', '')
            if not node_name:
                continue
            
            for iface in self.get_network(node_name):
                all_interfaces.append({
                    'hostname': node_name,
                    'interface_name': iface.get('iface', ''),
                    'interface_type': iface.get('type', ''),
                    'mac_address': iface.get('hwaddr', ''),
                    'ip_addresses': iface.get('address', ''),
                    'gateway': iface.get('gateway', ''),
                    'bridge_ports': iface.get('bridge_ports', ''),
                    'vlan_id': iface.get('vlan-id', ''),
                    'mtu': iface.get('mtu', ''),
                    'state': 'active' if iface.get('active') else 'inactive',
                })
        
        return all_interfaces
