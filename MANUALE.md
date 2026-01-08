# Proxmox Reporter - Manuale Completo

## Scopo del sistema

**Proxmox Reporter** è un sistema di monitoraggio, reportistica e gestione centralizzata per infrastrutture virtualizzate basate su Proxmox VE, progettato per fornire visibilità completa e aggiornata sullo stato delle risorse IT distribuite presso clienti.

### Obiettivi principali

Il sistema nasce per rispondere alle seguenti esigenze operative:

1. **Monitoraggio centralizzato multi-cliente**
   - Raccolta automatica di dati da decine/centinaia di nodi Proxmox distribuiti
   - Aggregazione informazioni in formato standard per analisi centralizzata
   - Identificazione univoca per cliente tramite codice cliente (`codcli`)

2. **Visibilità completa dell'infrastruttura**
   - Inventario hardware fisico (CPU, RAM, storage, temperature, BIOS)
   - Stato virtualizzazione (VM attive, risorse allocate/utilizzate)
   - Configurazione rete (interfacce, bridge, VLAN, IP)
   - Capacità storage (utilizzo, disponibilità, trend)

3. **Backup configurazione distribuito**
   - Salvataggio automatico configurazioni Proxmox (VM, container, storage)
   - Conservazione storica per disaster recovery
   - Possibilità di ripristino rapido in caso di guasti

4. **Automazione senza presidio**
   - Esecuzione schedulata via cron senza intervento manuale
   - Upload automatico report su server centralizzato SFTP
   - Gestione errori e retry automatici

5. **Auto-aggiornamento del sistema di monitoraggio**
   - Distribuzione automatica di nuove versioni su tutti i nodi
   - Nessun intervento on-site per manutenzione software
   - Rollback automatico in caso di problemi

### Scenari d'uso

#### Scenario 1: MSP con clienti distribuiti
Un Managed Service Provider gestisce infrastrutture Proxmox per 50 clienti:
- Ogni nodo esegue il report giornalmente alle 2:00 AM
- I dati vengono caricati su server SFTP centrale
- Un sistema di analisi (es. Power BI, Excel) elabora i CSV per:
  - Dashboard utilizzo risorse per cliente
  - Alert su saturazione storage
  - Trend crescita VM/risorse
  - Report mensili per fatturazione

#### Scenario 2: Azienda multi-sede
Azienda con 10 data center geograficamente distribuiti:
- Ogni sede ha un cluster Proxmox (2-4 nodi)
- Report centralizzato fornisce:
  - Vista unificata su tutte le VM aziendali
  - Capacità residua per pianificare nuovi progetti
  - Verifica compliance (backup, licenze, patch)
- Backup configurazioni per disaster recovery inter-sede

#### Scenario 3: Audit e compliance
Necessità di documentare l'infrastruttura IT per:
- Audit ISO 27001 / GDPR
- Due diligence in caso di acquisizioni
- Pianificazione budget IT annuale
- Verifica SLA (uptime, performance)

### Vantaggi operativi

✅ **Riduzione costi operativi**: nessun accesso manuale ai nodi, tutto automatizzato  
✅ **Visibilità real-time**: dati aggiornati quotidianamente/orariamente  
✅ **Scalabilità**: gestisci 1 o 1000 nodi con lo stesso effort  
✅ **Standardizzazione**: formato CSV uniforme indipendente dalla versione Proxmox  
✅ **Disaster recovery**: backup configurazioni sempre disponibile  
✅ **Manutenibilità**: aggiornamenti distribuiti automaticamente senza downtime  

### Integrazione con altri sistemi

I dati esportati in CSV possono essere facilmente integrati con:

- **Business Intelligence**: Power BI, Tableau, Grafana
- **Ticketing/ITSM**: ServiceNow, Jira, Freshservice (import automatico asset)
- **Monitoring**: Zabbix, Nagios, Prometheus (metriche esportate)
- **Fatturazione**: calcolo costi basato su risorse allocate per cliente
- **Inventory**: CMDB, Asset Management systems

---

## Indice
1. [Panoramica del progetto](#panoramica-del-progetto)
2. [Architettura e componenti](#architettura-e-componenti)
3. [Requisiti di sistema](#requisiti-di-sistema)
4. [Installazione](#installazione)
5. [Configurazione](#configurazione)
6. [Utilizzo](#utilizzo)
7. [Formato dei dati esportati](#formato-dei-dati-esportati)
8. [Sistema di aggiornamento automatico](#sistema-di-aggiornamento-automatico)
9. [Risoluzione problemi](#risoluzione-problemi)
10. [Manutenzione](#manutenzione)

---

## Panoramica del progetto

**Proxmox Reporter** è un sistema di monitoraggio e reportistica per infrastrutture Proxmox VE che:

- Raccoglie informazioni dettagliate su host, VM, storage e rete
- Genera report CSV strutturati
- Crea backup della configurazione Proxmox
- Carica automaticamente i dati su server SFTP remoto
- Si auto-aggiorna scaricando nuove versioni dal server SFTP
- Funziona sia in locale sul nodo Proxmox che da remoto via API/SSH

### Caratteristiche principali

- ✅ **Esecuzione locale o remota**: interroga il nodo su cui viene eseguito o un host Proxmox remoto via API/SSH
- ✅ **Nessun file di configurazione**: tutti i parametri si passano da riga di comando
- ✅ **Setup automatico**: wizard interattivo per installazione e configurazione cron
- ✅ **Auto-aggiornamento**: scarica e applica automaticamente nuove versioni degli script
- ✅ **Upload SFTP con fallback**: prova multiple combinazioni host/porta in caso di problemi di rete
- ✅ **Rotazione file**: mantiene cronologia dei report con limite configurabile
- ✅ **Formato CSV standard**: delimitatore `;` per compatibilità Excel/LibreOffice

---

## Architettura e componenti

### File principali

#### `proxmox_core.py`
Script principale per automazioni e cron. Gestisce:
- Parsing argomenti CLI
- Costruzione configurazione runtime
- Orchestrazione raccolta dati
- Generazione CSV e backup
- Upload SFTP
- Integrazione auto-aggiornamento

**Modalità di esecuzione**: `local` (comandi locali), `ssh` (comandi remoti via SSH), `api` (solo API REST)

#### `proxmox_report.py`
Libreria core riutilizzabile contenente:
- `ProxmoxLocalExtractor`: classe per estrazione dati da Proxmox
- `ProxmoxBackupIntegrated`: gestione backup configurazione
- `SFTPUploader`: upload file via SFTP con retry
- Utility di formattazione e conversione

#### `setup.py`
Installer interattivo che:
1. Verifica/installa dipendenze (`lshw`, `python3-paramiko`)
2. Copia script in `/opt/proxreport` (o directory scelta)
3. Configura job cron con wizard guidato
4. Imposta auto-aggiornamento (opzionale)

#### `update_scripts.py`
Sistema di auto-aggiornamento:
- Scarica nuove versioni da `/home/proxmox/proxreport` su server SFTP
- Confronta hash SHA256
- Crea backup prima di aggiornare
- Ripristina in caso di errori

### Flusso di esecuzione

```
┌─────────────────────────────────────┐
│  proxmox_core.py --auto-update      │
└──────────┬──────────────────────────┘
           │
           ├─> [Auto-update?]
           │   └─> update_scripts.py
           │       ├─> Scarica da SFTP
           │       ├─> Confronta hash
           │       └─> Sostituisci se diverso
           │
           ├─> [Detect execution mode]
           │   └─> local / ssh / api
           │
           ├─> [Estrazione dati]
           │   ├─> Cluster info
           │   ├─> Host details (CPU, RAM, temp, HW)
           │   ├─> Storage (pvesm status)
           │   ├─> Network interfaces
           │   └─> VM running (pvesh/qm)
           │
           ├─> [Generazione output]
           │   ├─> CSV: vms, hosts, storage, network
           │   └─> Backup: tar.gz configurazione
           │
           └─> [Upload SFTP]
               ├─> Tentativo host:porta configurati
               ├─> Fallback porta 22
               ├─> Fallback IP alternativo
               └─> Report finale
```

---

## Requisiti di sistema

### Sistema operativo
- Proxmox VE 7.x o superiore
- Debian 11/12 (Bullseye/Bookworm)
- Python 3.9+

### Dipendenze software

**Obbligatorie:**
```bash
apt install python3 python3-paramiko lshw
```

**Opzionali (per funzionalità avanzate):**
- `lm-sensors` (lettura temperature)
- `dmidecode` (info BIOS/hardware)
- `efibootmgr` (boot mode detection)

### Permessi

- **Esecuzione locale**: richiede `root` per accesso completo a comandi Proxmox (`pvesm`, `pvesh`, `qm`)
- **Esecuzione remota**: utente con privilegi SSH e API Proxmox (tipicamente `root@pam`)

### Rete

- Accesso SFTP al server di destinazione (porta 11122 o 22)
- Per esecuzione remota: porta 22 (SSH) e 8006 (API Proxmox)

---

## Installazione

### Metodo 1: Setup automatico (raccomandato)

```bash
# 1. Scarica o trasferisci i file sorgente
cd /tmp
# (trasferisci proxmox_core.py, proxmox_report.py, setup.py, update_scripts.py)

# 2. Esegui setup come root
sudo python3 setup.py
```

Il wizard chiederà:
- **Directory installazione** (default: `/opt/proxreport`)
- **Directory output report** (default: `/var/log/proxreporter`)
- **Codice cliente** e **nome cliente**
- Modalità **locale** o **remota** (con credenziali)
- Override parametri **SFTP** (opzionale)
- Abilitare **auto-aggiornamento** (default: sì)
- **Frequenza cron** (giornaliera/oraria/custom)

Al termine, gli script sono installati e il cron configurato.

### Metodo 2: Installazione manuale

```bash
# 1. Crea directory
sudo mkdir -p /opt/proxreport

# 2. Copia script
sudo cp proxmox_core.py proxmox_report.py update_scripts.py /opt/proxreport/
sudo chmod +x /opt/proxreport/*.py

# 3. Installa dipendenze
sudo apt update
sudo apt install -y python3-paramiko lshw

# 4. Configura cron manualmente
sudo crontab -e
# Aggiungi:
# 0 2 * * * python3 /opt/proxreport/proxmox_core.py --codcli 12345 --nomecliente CLIENTE --local --auto-update >> /var/log/proxreporter/cron.log 2>&1
```

---

## Configurazione

### Parametri principali

#### Parametri obbligatori
```bash
--codcli CODICE          # Codice identificativo cliente
--nomecliente NOME       # Nome cliente (usato nei nomi file)
```

#### Modalità di esecuzione
```bash
--local                  # Forza modalità locale (default se nessun --host)
--host IP_O_HOSTNAME     # Host Proxmox remoto (abilita modalità remota)
--username USER          # Username API/SSH (es. root@pam)
--password PASS          # Password per API e SSH
--ssh-port PORTA         # Porta SSH (default: 22)
```

#### Output e upload
```bash
--output-dir PATH        # Directory output (default: reports)
--no-upload              # Non eseguire upload SFTP
```

#### Override SFTP
```bash
--sftp-host HOST         # Server SFTP (default: sftp.domarc.it)
--sftp-port PORTA        # Porta SFTP (default: 11122)
--sftp-user USER         # Username SFTP (default: proxmox)
--sftp-password PASS     # Password SFTP
--sftp-base-path PATH    # Path remoto (default: /home/proxmox/uploads)
```

#### Auto-aggiornamento
```bash
--auto-update            # Verifica e applica aggiornamenti prima di eseguire
--skip-update            # Salta verifica aggiornamenti (usato internamente)
```

### Configurazione hardcoded

In `proxmox_core.py` (righe 100-111):

```python
SFTP_ENABLED = True
SFTP_HOST = "sftp.domarc.it"
SFTP_PORT = 11122
SFTP_USERNAME = "proxmox"
SFTP_PASSWORD = "PX!h03f257m"
SFTP_BASE_PATH = "/home/proxmox/uploads"
SFTP_FALLBACK_HOST = "192.168.20.14"

FEATURES_DEFAULT = {
    "collect_cluster": True,
    "collect_host": True,
    "collect_host_details": True,
    "collect_storage": True,
    "collect_network": True,
    "collect_vms": True,
    "collect_containers": False,
    "collect_backup": True,
    "collect_perf": True,
}
```

Per modificare i default, edita questi valori o passa gli override da CLI.

### Personalizzazione campi esportati

#### CSV VM
In `proxmox_core.py` (righe 1250-1290):

```python
VMS_EXPORT_FIELDS = [
    "node",
    "vmid",
    "name",
    "status",
    # ... aggiungi/rimuovi campi
]
```

#### CSV Host
In `proxmox_core.py` (righe 1410-1447):

```python
HOST_EXPORT_FIELDS: List[Tuple[str, str]] = [
    ("server_identifier", "srv_id"),
    ("uptime_human", "uptime"),
    ("manager_version", "prox_ver"),
    # ... (campo_interno, nome_colonna_csv)
]
```

#### CSV Storage
In `proxmox_core.py` (righe 1887-1897):

```python
fieldnames = [
    "server_identifier",
    "hostname",
    "name",
    "type",
    "status",
    "total_gb",
    "used_gb",
    "available_gb",
    "used_percent",
    "content",
]
```

#### CSV Network
In `proxmox_core.py` (righe 1967-1983):

```python
fieldnames = [
    "server_identifier",
    "hostname",
    "category",
    "name",
    "type",
    "state",
    "mac_address",
    "ip4",
    "ip6",
    "ip_addresses",
    "bridge",
    "members",
    "vlan_id",
    "speed_mbps",
    "comment",
]
```

---

## Utilizzo

### Esecuzione manuale locale

```bash
# Esecuzione base (nodo locale)
sudo python3 /opt/proxreport/proxmox_core.py \
  --codcli 70791 \
  --nomecliente DOMARC \
  --local

# Con auto-aggiornamento
sudo python3 /opt/proxreport/proxmox_core.py \
  --codcli 70791 \
  --nomecliente DOMARC \
  --local \
  --auto-update

# Senza upload SFTP
sudo python3 /opt/proxreport/proxmox_core.py \
  --codcli 70791 \
  --nomecliente DOMARC \
  --local \
  --no-upload
```

### Esecuzione remota

```bash
# Interroga host Proxmox remoto
python3 /opt/proxreport/proxmox_core.py \
  --codcli 70791 \
  --nomecliente DOMARC \
  --host 192.168.40.11 \
  --username root@pam \
  --password "MySecretPass" \
  --auto-update
```

### Verifica manuale aggiornamenti

```bash
sudo python3 /opt/proxreport/update_scripts.py
```

Output:
```
=== PROXMOX REPORTER - AUTO-UPDATE ===
Directory installazione: /opt/proxreport
→ Connessione al server SFTP...
  ✓ Connesso a sftp.domarc.it:11122
→ Verifica aggiornamento: proxmox_core.py
  → Versione diversa rilevata
    Locale:  a3f2e8b1c...
    Remoto:  d7e9f1a2b...
→ Applicazione aggiornamenti (1 file)...
  → Backup: proxmox_core.py → proxmox_core.py.bak
  ✓ Aggiornato: proxmox_core.py
=== AGGIORNAMENTO COMPLETATO ===
```

### Visualizzazione job cron

```bash
# Lista job cron utente corrente
crontab -l

# Lista job cron di root
sudo crontab -l
```

### Modifica job cron

```bash
sudo crontab -e
```

Esempio entry:
```cron
# Esegui ogni giorno alle 2:00 AM con auto-update
0 2 * * * python3 /opt/proxreport/proxmox_core.py --codcli 70791 --nomecliente DOMARC --local --auto-update >> /var/log/proxreporter/cron.log 2>&1
```

### Visualizzazione log

```bash
# Log esecuzione cron
tail -f /var/log/proxreporter/cron.log

# Ultimi 50 log
tail -n 50 /var/log/proxreporter/cron.log
```

---

## Formato dei dati esportati

### Struttura directory output

```
/var/log/proxreporter/
├── csv/
│   ├── 70791_DOMARC_DA-PX-RG_prox_vms.csv
│   ├── 70791_DOMARC_DA-PX-RG_prox_vms.csv.1
│   ├── 70791_DOMARC_DA-PX-RG_prox_hosts.csv
│   ├── 70791_DOMARC_DA-PX-RG_prox_storage.csv
│   └── 70791_DOMARC_DA-PX-RG_prox_network.csv
└── backup/
    ├── 70791_DOMARC_DA-PX-RG_prox_backup.tar.gz
    └── 70791_DOMARC_DA-PX-RG_prox_backup.tar.gz.1
```

**Naming**: `{codcli}_{nomecliente}_{server_identifier}_prox_{tipo}.{ext}`

**Rotazione**: mantiene max 5 copie (configurabile), rinomina automaticamente in `.1`, `.2`, ecc.

### CSV VM (`*_prox_vms.csv`)

**Delimitatore**: `;`  
**Encoding**: UTF-8  
**Campi principali**:

| Campo | Descrizione | Esempio |
|-------|-------------|---------|
| `node` | Nome nodo Proxmox | `DA-PX-01` |
| `vmid` | ID VM | `100` |
| `name` | Nome VM | `web-server-prod` |
| `status` | Stato VM | `running` |
| `vm_type` | Tipo | `qemu` |
| `cores` | CPU cores | `4` |
| `sockets` | CPU sockets | `1` |
| `mem_used` | RAM utilizzata | `2.5 GiB` |
| `mem_total` | RAM totale | `8.0 GiB` |
| `disk_size` | Spazio disco totale | `100.0 GiB` |
| `uptime` | Uptime | `15d 3h 42m 18s` |
| `primary_ip` | IP primario | `192.168.1.100` |
| `ipv4` | Lista IPv4 | `192.168.1.100 \| 10.0.0.50` |
| `bios` | Tipo BIOS | `seabios` / `ovmf` |
| `ostype` | OS type | `l26` (Linux 2.6+) |
| `agent_enabled` | QEMU agent | `Yes` / `No` |
| `agent_version` | Versione agent | `7.2.0` |
| `disks` | Lista disks | `scsi0, scsi1` |
| `num_disks` | Numero dischi | `2` |
| `disks_details` | Dettagli dischi | `{id=scsi0 storage=local-lvm...}` |
| `networks` | Lista interfacce | `net0, net1` |
| `num_networks` | Numero interfacce | `2` |
| `networks_details` | Dettagli network | `{id=net0 model=virtio mac=...}` |
| `snapshots_count` | Numero snapshot | `3` |
| `tags` | Tag VM | `production;backup` |

**Formato dettagli (disks/networks)**: ogni oggetto racchiuso in `{}`, separati da newline nella stessa cella.

### CSV Host (`*_prox_hosts.csv`)

**Campi principali**:

| Campo CSV | Campo interno | Descrizione | Esempio |
|-----------|---------------|-------------|---------|
| `srv_id` | `server_identifier` | Identificativo server | `DA-PX-01` |
| `uptime` | `uptime_human` | Uptime | `45d 12h 30m` |
| `prox_ver` | `manager_version` | Versione Proxmox | `9.0.11` |
| `prox_kern` | `kernel_version` | Kernel | `6.14.11-4-pve` |
| `cpu` | `cpu_model` | Modello CPU | `Intel Xeon E5-2680 v4` |
| `cpu_cores` | `cpu_cores` | Core fisici | `16` |
| `cpu_sockets` | `cpu_sockets` | Socket CPU | `2` |
| `load_15m` | `load_average_15m` | Load average 15min | `2.45` |
| `mem_tot` | `memory_total_gb` | RAM totale | `128.0` |
| `mem_used` | `memory_used_gb` | RAM utilizzata | `96.3` |
| `swap_tot` | `swap_total_gb` | Swap totale | `8.0` |
| `temp_max` | `temperature_highest_c` | Temperatura massima | `54.2` |
| `HW_bios` | `bios_vendor` | Vendor BIOS | `American Megatrends` |
| `HW_prod` | `system_manufacturer` | Produttore | `Supermicro` |
| `HW_model` | `system_product` | Modello | `X10DRi` |
| `lic_status` | `license_status` | Stato licenza | `active` / `notfound` |
| `lic_level` | `license_level` | Livello licenza | `c` (community) |

**Nota**: le colonne hardware (`HW_*`) aggregano output `lshw` in liste separate da `\|`.

### CSV Storage (`*_prox_storage.csv`)

Dati estratti da `pvesm status`:

| Campo | Descrizione | Esempio |
|-------|-------------|---------|
| `server_identifier` | ID server | `DA-PX-01` |
| `hostname` | Hostname | `DA-PX-01` |
| `name` | Nome storage | `NFS-VM-QNAP-765` |
| `type` | Tipo storage | `nfs` / `dir` / `zfspool` / `lvmthin` |
| `status` | Stato | `active` / `disabled` |
| `total_gb` | Spazio totale | `8000.0` |
| `used_gb` | Spazio utilizzato | `1780.5` |
| `available_gb` | Spazio disponibile | `6219.5` |
| `used_percent` | Percentuale uso | `22.27%` |
| `content` | Contenuto | (se disponibile) |

**Formato dimensioni**: valori in GiB con 1 decimale.

### CSV Network (`*_prox_network.csv`)

Interfacce di rete del nodo:

| Campo | Descrizione | Esempio |
|-------|-------------|---------|
| `server_identifier` | ID server | `DA-PX-01` |
| `hostname` | Hostname | `DA-PX-01` |
| `category` | Categoria | `physical` / `bridge` / `bond` / `vlan` |
| `name` | Nome interfaccia | `vmbr0` / `enp1s0` |
| `type` | Tipo | `bridge` / `eth` |
| `state` | Stato | `up` / `down` |
| `mac_address` | MAC address | `00:1A:2B:3C:4D:5E` |
| `ip4` | IPv4 principale | `192.168.1.10` |
| `ip6` | IPv6 principale | `fe80::...` |
| `ip_addresses` | Lista IP | `192.168.1.10 \| 10.0.0.1` |
| `bridge` | Bridge associato | `vmbr0` |
| `members` | Membri (bond/bridge) | `enp1s0, enp2s0` |
| `vlan_id` | VLAN ID | `100` |
| `speed_mbps` | Velocità link | `1000.0 Mbps` |
| `comment` | Commento | (da config) |

**Nota**: solo interfacce fisiche `up` o interfacce virtuali (bridge/bond/vlan) vengono esportate.

### Backup (`*_prox_backup.tar.gz`)

Archivio `.tar.gz` contenente:

```
/etc/pve/
├── nodes/
│   └── {nodename}/
│       ├── qemu-server/
│       │   ├── 100.conf
│       │   └── ...
│       ├── lxc/
│       └── ...
├── storage.cfg
├── corosync.conf
├── datacenter.cfg
└── ...
```

**Esclusi**: file temporanei, lock, socket.

---

## Sistema di aggiornamento automatico

### Architettura

```
Server SFTP                        Nodo Proxmox
/home/proxmox/proxreport/          /opt/proxreport/
├── proxmox_core.py        <───┐   ├── proxmox_core.py
└── proxmox_report.py          │   ├── proxmox_report.py
                               │   └── update_scripts.py
                               │         │
                               └─────────┘
                                  (download)
```

### Flusso aggiornamento

1. **Trigger**: `proxmox_core.py --auto-update` eseguito da cron
2. **Check**: `update_scripts.py` scarica script da SFTP
3. **Confronto**: calcola hash SHA256 locale vs remoto
4. **Backup**: copia versione corrente in `.backup/`
5. **Aggiornamento**: sostituisce file se hash differenti
6. **Riavvio**: `proxmox_core.py` si ri-esegue con `--skip-update`
7. **Esecuzione**: procede normalmente con versione aggiornata

### Pubblicazione nuove versioni

```bash
# 1. Testa localmente la nuova versione
python3 proxmox_core.py --codcli TEST --nomecliente TEST --local --no-upload

# 2. Carica su server SFTP
sftp -P 11122 proxmox@sftp.domarc.it
sftp> cd /home/proxmox/proxreport
sftp> put proxmox_core.py
sftp> put proxmox_report.py
sftp> quit

# 3. Verifica hash remoto
ssh -p 11122 proxmox@sftp.domarc.it
$ sha256sum /home/proxmox/proxreport/*.py
```

Tutti i nodi con `--auto-update` scaricheranno automaticamente la nuova versione alla prossima esecuzione cron.

### Rollback manuale

Se un aggiornamento causa problemi:

```bash
cd /opt/proxreport/.backup
ls -lh  # verifica backup disponibili
sudo cp proxmox_core.py.bak ../proxmox_core.py
sudo cp proxmox_report.py.bak ../proxmox_report.py
```

---

## Risoluzione problemi

### 1. Storage non rilevato

**Sintomo**: CSV storage vuoto o mancante

**Causa**: comando `pvesm status` non disponibile/non autorizzato

**Soluzione**:
```bash
# Verifica esecuzione manuale
pvesm status

# Se comando non trovato, verifica PATH
which pvesm
# Output atteso: /usr/sbin/pvesm

# Se non autorizzato, esegui come root
sudo pvesm status
```

**Fix**: assicurati che lo script venga eseguito come `root` o con `sudo`.

### 2. Upload SFTP fallito

**Sintomo**: messaggio "✗ Upload SFTP fallito su tutti i tentativi"

**Diagnosi**:
```bash
# Test connessione manuale
sftp -P 11122 proxmox@sftp.domarc.it
# oppure
sftp -P 22 proxmox@sftp.domarc.it

# Test IP fallback
sftp -P 11122 proxmox@192.168.20.14
```

**Fix**:
- Verifica firewall (porte 11122 e 22)
- Controlla credenziali in `proxmox_core.py` (righe 105-108)
- Usa override CLI: `--sftp-host` / `--sftp-port` / `--sftp-password`

### 3. VM non rilevate

**Sintomo**: CSV vms vuoto

**Causa**: `pvesh` non disponibile o solo VM stopped

**Diagnosi**:
```bash
# Verifica pvesh
pvesh get /nodes

# Lista VM locali
qm list
```

**Nota**: di default vengono esportate **solo VM running**. Per includere tutte le VM, modifica filtro in `get_full_vm_details()` (riga ~1779):

```python
# Prima (solo running)
if status_value != "running":
    continue

# Dopo (tutte)
# (rimuovi o commenta le 2 righe sopra)
```

### 4. Dipendenze mancanti

**Sintomo**: errori import o comandi non trovati

**Fix**:
```bash
# Reinstalla dipendenze
sudo apt update
sudo apt install -y python3-paramiko lshw

# Verifica installazione
python3 -c "import paramiko; print('OK')"
which lshw
```

### 5. Permessi insufficienti

**Sintomo**: `PermissionError` durante creazione file/directory

**Fix**:
```bash
# Verifica proprietario directory
ls -ld /opt/proxreport

# Correggi permessi
sudo chown -R root:root /opt/proxreport
sudo chmod 755 /opt/proxreport
sudo chmod 755 /opt/proxreport/*.py

# Directory output
sudo mkdir -p /var/log/proxreporter
sudo chmod 755 /var/log/proxreporter
```

### 6. Auto-update in loop

**Sintomo**: script si riavvia continuamente

**Causa**: flag `--skip-update` non impostato correttamente

**Fix temporaneo**:
```bash
# Esegui senza auto-update
python3 /opt/proxreport/proxmox_core.py --codcli XX --nomecliente YY --local --skip-update
```

**Fix permanente**: rimuovi `--auto-update` dal crontab.

### 7. Versione Proxmox/Kernel errata

**Sintomo**: campi `prox_ver` / `prox_kern` vuoti o troppo verbosi

**Diagnosi**:
```bash
pveversion
# Output atteso: pve-manager/9.0.11/3bf5476b (running kernel: 6.14.11-4-pve)
```

**Fix**: il parsing è automatico. Se il formato output di `pveversion` cambia, aggiorna `parse_manager_version_string()` in `proxmox_report.py`.

---

## Manutenzione

### Pulizia log e vecchi report

```bash
# Pulizia manuale log cron (mantiene ultimi 1000 righe)
tail -n 1000 /var/log/proxreporter/cron.log > /tmp/cron.log.tmp
mv /tmp/cron.log.tmp /var/log/proxreporter/cron.log

# Pulizia vecchi CSV (oltre 30 giorni)
find /var/log/proxreporter/csv -name "*.csv*" -mtime +30 -delete
find /var/log/proxreporter/backup -name "*.tar.gz*" -mtime +30 -delete
```

**Nota**: la rotazione automatica mantiene max 5 copie, quindi solitamente non serve pulizia manuale.

### Backup configurazione cron

```bash
# Esporta crontab corrente
sudo crontab -l > /root/crontab_backup_$(date +%Y%m%d).txt
```

### Monitoraggio esecuzioni

Script per alert se ultima esecuzione è troppo vecchia:

```bash
#!/bin/bash
# check_proxreport.sh
LOG="/var/log/proxreporter/cron.log"
MAX_AGE_HOURS=25  # avvisa se non eseguito da oltre 25h

if [ ! -f "$LOG" ]; then
    echo "ALERT: Log file non trovato"
    exit 1
fi

LAST_RUN=$(stat -c %Y "$LOG")
NOW=$(date +%s)
AGE_HOURS=$(( ($NOW - $LAST_RUN) / 3600 ))

if [ $AGE_HOURS -gt $MAX_AGE_HOURS ]; then
    echo "ALERT: Ultima esecuzione $AGE_HOURS ore fa"
    exit 1
else
    echo "OK: Report eseguito $AGE_HOURS ore fa"
    exit 0
fi
```

### Aggiornamento manuale script

Se il sistema di auto-aggiornamento non funziona:

```bash
# 1. Scarica nuove versioni
cd /tmp
sftp -P 11122 proxmox@sftp.domarc.it:/home/proxmox/proxreport/proxmox_core.py
sftp -P 11122 proxmox@sftp.domarc.it:/home/proxmox/proxreport/proxmox_report.py

# 2. Backup versione corrente
sudo cp /opt/proxreport/proxmox_core.py /opt/proxreport/.backup/
sudo cp /opt/proxreport/proxmox_report.py /opt/proxreport/.backup/

# 3. Sostituisci
sudo mv /tmp/proxmox_core.py /opt/proxreport/
sudo mv /tmp/proxmox_report.py /opt/proxreport/
sudo chmod 755 /opt/proxreport/*.py

# 4. Test
sudo python3 /opt/proxreport/proxmox_core.py --codcli TEST --nomecliente TEST --local --no-upload
```

### Disinstallazione

```bash
# 1. Rimuovi job cron
sudo crontab -e
# (elimina riga proxmox_core.py)

# 2. Rimuovi script
sudo rm -rf /opt/proxreport

# 3. Rimuovi log/report (opzionale)
sudo rm -rf /var/log/proxreporter

# 4. Rimuovi dipendenze (opzionale, se non usate altrove)
sudo apt remove --purge python3-paramiko lshw
```

---

## Appendice

### A. Struttura completa file

```
Proxreporter/
├── proxmox_core.py           # Script principale cron
├── proxmox_report.py         # Libreria core
├── setup.py                  # Installer/configuratore
├── update_scripts.py         # Auto-updater
├── MANUALE.md                # Questo file
├── README_LOCAL_REPORT.md    # Quick start
├── requirements.txt          # Dipendenze Python
└── cron_example.sh           # Esempi configurazione cron
```

### B. Variabili ambiente supportate

Nessuna: tutti i parametri si passano da CLI o si configurano hardcoded.

### C. Exit codes

- `0`: successo
- `1`: errore generico (parametri mancanti, connessione fallita, ecc.)
- `130`: interruzione utente (Ctrl+C)

### D. Compatibilità versioni

- **Proxmox VE**: testato su 7.x e 8.x, compatibile con 9.x
- **Python**: richiede 3.9+, testato fino a 3.11
- **Debian**: 11 (Bullseye) e 12 (Bookworm)

### E. Performance

Su sistema medio (16 core, 128GB RAM, 8 storage, 50 VM):
- **Tempo esecuzione**: ~45-60 secondi (locale), ~90-120 secondi (remoto)
- **Dimensione CSV**: ~50-200 KB per file
- **Dimensione backup**: ~5-15 MB (dipende da numero VM/LXC)
- **Upload SFTP**: ~5-10 secondi (dipende da banda)

### F. Sicurezza

**Best practices**:
1. Esegui sempre come `root` (richiesto per pvesm/pvesh)
2. Non esporre password in file di testo (usa solo CLI)
3. Proteggi directory `/opt/proxreport` (chmod 700)
4. Abilita firewall con porte SSH/SFTP limitate
5. Usa chiavi SSH invece di password (modifica `SFTPUploader` per supportarle)
6. Ruota periodicamente credenziali SFTP

**Limitazioni**:
- Password SFTP hardcoded nello script (da migliorare con keyring/vault)
- Nessuna crittografia backup (implementare GPG se necessario)
- Log contiene parametri CLI (potenzialmente password)

---

## Supporto

Per segnalazioni bug, richieste feature o assistenza:

- **Email**: supporto@domarc.it
- **Log**: allega sempre `/var/log/proxreporter/cron.log` (ultimi 100 righe)
- **Info sistema**: output di `pveversion` e `uname -a`

---

**Versione manuale**: 1.1  
**Data**: 2025-01-10  
**Autore**: Proxmox Reporter Team
