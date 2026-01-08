# Proxmox Local Report Generator

Script per generare report delle VM attive e caratteristiche dell'host/cluster Proxmox, funzionante direttamente sull'host Proxmox.

## Caratteristiche

- ✅ **Modalità multipla**: Funziona localmente su host Proxmox, via SSH remoto, o via API
- ✅ **Rilevamento automatico**: Rileva automaticamente la modalità migliore
- ✅ Estrae informazioni su VM attive
- ✅ Estrae informazioni su host e cluster
- ✅ Genera report in formato CSV
- ✅ Crea backup configurazione Proxmox (locale o via SSH)
- ✅ Carica file su server remoto via SFTP (nella directory configurata)
- ✅ Nomi file strutturati con codcli, cliente e identificatore server
- ✅ Schedulabile con cron

## Requisiti

- Python 3.6+
- Paramiko (per SSH/SFTP): `pip install paramiko`
- Accesso root o utente con permessi Proxmox
- Script può essere eseguito:
  - **Localmente** sull'host Proxmox
  - **Via SSH** da un client remoto
  - **Via API** da qualsiasi sistema

## Configurazione

### 1. Configurare `config.json`

Aggiungere le seguenti sezioni al file `config.json`:

```json
{
    "client": {
        "codcli": "CLI001",
        "nomecliente": "NomeCliente",
        "server_identifier": "PX01"
    },
    "sftp": {
        "enabled": true,
        "host": "192.168.1.100",
        "port": 22,
        "username": "backup_user",
        "password": "backup_password",
        "base_path": "/home/proxmox/uploads"
    }
}
```

### 2. Parametri configurazione

- **client.codcli**: Codice cliente (es: "CLI001")
- **client.nomecliente**: Nome cliente (es: "Azienda SRL")
- **client.server_identifier**: Identificatore del server/cluster (es: "PX01")
- **sftp.enabled**: Abilita/disabilita upload SFTP
- **sftp.host**: IP o hostname server remoto
- **sftp.port**: Porta SSH (default: 22)
- **sftp.username**: Username per connessione SSH
- **sftp.password**: Password per connessione SSH
- **sftp.base_path**: Directory remota in cui salvare i file

### 3. Feature toggle (`features`)

Nel file `config.json` è possibile attivare o disattivare singole funzionalità tramite la sezione `features`:

- `collect_cluster` (default: true) – raccoglie informazioni sul cluster (`pvecm`, elenco nodi)
- `collect_host` (default: true) – genera il CSV principale degli host
- `collect_host_details` (default: true) – include metriche avanzate (CPU, RAM, swap, rootfs, subscription)
- `collect_storage` (default: true) – genera il CSV dello storage (`pvesm status`)
- `collect_network` (default: true) – genera il CSV delle interfacce (`pvesh get /nodes/<node>/network`, `/etc/network/interfaces`)
- `collect_vms` (default: true) – raccoglie VM e produce il CSV dedicato
- `collect_containers` (default: false) – riservato per integrazioni LXC future
- `collect_backup` (default: true) – esegue il backup di configurazione (solo locale/SSH)
- `collect_perf` (default: false) – placeholder per eventuali snapshot `pveperf`

## Utilizzo

### Esecuzione base

```bash
python3 proxmox_report.py
```

### Con configurazione personalizzata

```bash
python3 proxmox_report.py --config custom_config.json
```

### Senza upload SFTP

```bash
python3 proxmox_report.py --no-sftp
```

### Con directory output personalizzata

```bash
python3 proxmox_report.py --output-dir /custom/path
```

## Output

Lo script genera:

1. **File CSV VM**: `reports/csv/proxmox_vms_local_YYYYMMDD_HHMMSS.csv`
   - Contiene informazioni su VM attive
   - Include informazioni host nella prima riga

2. **File CSV Host**: `reports/csv/proxmox_hosts_YYYYMMDD_HHMMSS.csv`
   - Informazioni principali host (nome, versione, CPU, RAM)

3. **File CSV Storage**: `reports/csv/proxmox_hosts_storage_YYYYMMDD_HHMMSS.csv`
   - Dettagli storage Proxmox (nome, tipo, spazio totale/libero/usato)

4. **File CSV Network**: `reports/csv/proxmox_hosts_network_YYYYMMDD_HHMMSS.csv`
   - Interfacce di rete (nome, MAC, IP, bridge, VLAN)

5. **File Backup**: `reports/backup/proxmox_config_backup_YYYYMMDD_HHMMSS.tar.gz`
   - Backup configurazione Proxmox
   - Include: /etc/pve, /etc/network/interfaces, /etc/hosts, /etc/resolv.conf, /etc/corosync, /etc/ssh

6. **Upload SFTP** (se abilitato):
   - Tutti i file CSV e il backup vengono copiati su server remoto nella directory `base_path`
   - I nomi dei file includono automaticamente `codcli`, `nomecliente` e, se configurato, `server_identifier`

## Schedulazione con Cron

### Installazione

1. Modificare `cron_example.sh` con il percorso corretto
2. Aprire crontab: `crontab -e`
3. Aggiungere una delle righe di esempio

### Esempi cron

```bash
# Ogni giorno alle 2:00 AM
0 2 * * * cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py >> /var/log/proxmox_report.log 2>&1

# Ogni 6 ore
0 */6 * * * cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py >> /var/log/proxmox_report.log 2>&1

# Ogni lunedì alle 9:00 AM
0 9 * * 1 cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py >> /var/log/proxmox_report.log 2>&1
```

### Visualizzazione log

```bash
tail -f /var/log/proxmox_report.log
```

## Modalità di esecuzione

Lo script supporta tre modalità di esecuzione, rilevate automaticamente:

### 1. Modalità Locale

**Quando**: Script eseguito direttamente sull'host Proxmox

**Rilevamento**: Presenza di `/etc/pve`, `/usr/bin/pvesh`, o `/usr/bin/qm`

**Vantaggi**:

- Accesso diretto ai file di sistema
- Nessuna connessione di rete richiesta
- Backup completo possibile

### 2. Modalità SSH

**Quando**: Script eseguito da client remoto con configurazione SSH

**Rilevamento**: Configurazione `ssh.host` e `ssh.username` presenti in `config.json`

**Vantaggi**:

- Esecuzione remota senza installare script su Proxmox
- Accesso completo ai comandi Proxmox via SSH
- Backup remoto possibile

**Configurazione richiesta**:

```json
"ssh": {
    "host": "192.168.1.100",
    "port": 22,
    "username": "root",
    "password": "password"
}
```

### 3. Modalità API

**Quando**: Nessuna delle modalità precedenti disponibile

**Rilevamento**: Fallback automatico

**Vantaggi**:

- Funziona da qualsiasi sistema
- Non richiede accesso SSH
- Usa API Proxmox standard

## Metodi di estrazione dati

Lo script prova diversi metodi in ordine:

1. **pvesh** (Proxmox VE Shell) - Metodo preferito (locale o SSH)
2. **API** (localhost:8006 o host remoto) - Fallback se pvesh non disponibile
3. **Lettura diretta file** (/etc/pve/qemu-server) - Ultimo fallback (solo locale)

## Struttura file CSV

### CSV VM (`proxmox_vms_local_*.csv`)

Contiene le seguenti colonne:

- `node`: Nome nodo Proxmox
- `vmid`: ID VM
- `name`: Nome VM
- `status`: Stato VM (running, stopped, etc.)
- `cpu_cores`: Numero core CPU
- `cpu_sockets`: Numero socket CPU
- `memory_mb`: Memoria in MB
- `disk_gb`: Disco in GB
- `bios`: Tipo BIOS
- `agent`: QEMU Guest Agent (0/1)
- `uptime`: Uptime in secondi

La prima riga contiene informazioni sull'host.

### CSV Host (`proxmox_hosts_*.csv`)

Contiene informazioni principali host:

- `hostname`: Nome host Proxmox
- `proxmox_version`: Versione Proxmox VE
- `cpu_model`: Modello CPU
- `cpu_cores`: Numero core CPU
- `cpu_sockets`: Numero socket CPU
- `cpu_threads`: Thread per core
- `memory_total_gb`: Memoria totale in GB
- `memory_used_gb`: Memoria usata in GB
- `memory_free_gb`: Memoria libera in GB

### CSV Storage (`proxmox_hosts_storage_*.csv`)

Contiene dettagli storage:

- `hostname`: Nome host
- `storage_name`: Nome storage
- `storage_type`: Tipo storage (dir, lvm, nfs, cifs, etc.)
- `status`: Stato storage (active, unavailable, etc.)
- `total_gb`: Spazio totale in GB
- `used_gb`: Spazio usato in GB
- `available_gb`: Spazio disponibile in GB
- `content`: Tipi di contenuto supportati

### CSV Network (`proxmox_hosts_network_*.csv`)

Contiene interfacce di rete:

- `hostname`: Nome host
- `interface_name`: Nome interfaccia (es: eth0, enp3s0, vmbr0)
- `mac_address`: Indirizzo MAC
- `ip_addresses`: Indirizzi IP (separati da `;` se multipli)
- `bridge`: Bridge associato (se presente)
- `vlan`: VLAN associata (se presente)
- `state`: Stato interfaccia (up, down, unknown)

## Troubleshooting

### Errore: "pvesh non trovato"

Lo script userà automaticamente l'API locale come fallback. Assicurarsi che:

- L'API Proxmox sia accessibile su localhost:8006
- Le credenziali in `config.json` siano corrette

### Errore: "Connessione SFTP fallita"

Verificare:

- Server remoto raggiungibile
- Credenziali SSH corrette
- Porta SSH aperta
- Permessi utente sul server remoto

### Nessuna VM trovata

Verificare:

- Esistono VM in stato "running"
- Permessi di lettura su /etc/pve
- API Proxmox funzionante

## Note

- Lo script rileva automaticamente la modalità migliore (locale/SSH/API)
- Per backup completo, eseguire come root (locale) o con utente root via SSH
- I file vengono sempre generati localmente (sul sistema dove viene eseguito lo script)
- La directory remota indicata in `sftp.base_path` viene creata automaticamente se non esiste
- Modalità SSH richiede configurazione `ssh` in `config.json`
- Modalità API funziona anche da sistemi non-Linux (es. macOS, Windows)

## Supporto

Per problemi o domande, consultare la documentazione principale del progetto.
