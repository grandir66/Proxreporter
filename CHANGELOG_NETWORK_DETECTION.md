# Changelog: Miglioramento Rilevamento Interfacce di Rete

## Data: 10 Novembre 2025

## Problema Identificato

Il modulo `proxreporter` non rilevava correttamente le **VLAN ID** e altre informazioni critiche delle interfacce di rete degli host Proxmox. In particolare:

- Le VLAN configurate con `bridge-vids` non venivano estratte
- Non tutte le interfacce configurate in `/etc/network/interfaces` venivano incluse
- Mancavano informazioni come `bond-mode` (algoritmo di bilanciamento)
- Venivano riportate solo le interfacce "UP", ignorando quelle configurate ma non attive

## Root Cause

La logica precedente in `proxmox_report.py` (metodo `enrich_host_info_with_commands`) partiva dalle interfacce già rilevate tramite API o comandi `ip`, e poi **arricchiva** queste informazioni con dati dal file `/etc/network/interfaces`.

Questo approccio aveva due problemi:
1. **Incomplete Detection**: Se un'interfaccia non era rilevata dall'API o da `ip link`, non veniva inclusa anche se configurata in `/etc/network/interfaces`
2. **Missing VLAN Data**: I VLAN range configurati con `bridge-vids: 2-4094` venivano cercati, ma solo per interfacce già presenti

## Soluzione Implementata

### Nuovo Approccio: Parse-First Strategy

Il metodo `enrich_host_info_with_commands` è stato completamente riscritto con una strategia "parse-first":

1. **Leggi PRIMA tutte le interfacce da `/etc/network/interfaces`**
   - Questo file contiene la configurazione completa e autoritativa
   - Include tutte le interfacce configurate, indipendentemente dallo stato

2. **Estrai tutte le informazioni disponibili dal file**:
   - Nome interfaccia
   - Indirizzo IP (IPv4 e IPv6)
   - MAC address (via `hwaddress`)
   - VLAN ID (da `bridge-vids`, `tag`, `vlan-id`, `vlan`)
   - Bond mode (algoritmo di bilanciamento)
   - Members (bridge-ports, bond-slaves, etc.)
   - Bridge associato
   - VLAN parent device

3. **Arricchisci con dati da API Proxmox** (se disponibili):
   - Stato operativo
   - MAC address da sistema
   - Tipo interfaccia

4. **Completa con informazioni di sistema**:
   - Stato operativo (`/sys/class/net/{iface}/operstate`)
   - MAC address (`/sys/class/net/{iface}/address`)
   - IP addresses (via `ip addr show`)
   - Speed per interfacce fisiche (`/sys/class/net/{iface}/speed`)

### Modifiche al Codice

**File**: `/Users/riccardo/domarc/Proxreporter/proxmox_report.py`  
**Metodo**: `ProxmoxLocalExtractor.enrich_host_info_with_commands()`  
**Righe**: 1129-1280

#### Estratto delle Modifiche Principali

```python
# NUOVO: Parsing completo da /etc/network/interfaces
interfaces_config = parse_interfaces_config(interfaces_content)
entries_by_name: Dict[str, Dict[str, Any]] = {}

for iface_name, config in interfaces_config.items():
    entry = {
        'name': iface_name,
        'state': 'unknown',
        'active': None,
    }
    
    # Estrai VLAN ID con priorità corretta
    vlan = config.get('bridge_vids') or config.get('tag') or config.get('vlan_id') or config.get('vlan')
    if vlan:
        entry['vlan_id'] = str(vlan).strip()
    
    # Estrai bond-mode (algoritmo di bilanciamento)
    if config.get('bond_mode'):
        entry['bond_mode'] = config.get('bond_mode').strip()
    
    # ... altre estrazioni ...
    
    entries_by_name[iface_name] = entry
```

### Campi Estratti da `/etc/network/interfaces`

| Campo | Chiavi ricercate | Descrizione |
|-------|-----------------|-------------|
| `vlan_id` | `bridge-vids`, `tag`, `vlan-id`, `vlan` | VLAN ID o range (es. "2-4094") |
| `bond_mode` | `bond-mode` | Algoritmo di bilanciamento (es. "balance-rr", "802.3ad") |
| `gateway` | `gateway` | Gateway IPv4 |
| `gateway6` | `gateway6` | Gateway IPv6 |
| `netmask` | `netmask` | Netmask IPv4 |
| `members` | `bridge-ports`, `bond-slaves`, `ports`, `slaves` | Interfacce membre di bridge/bond |
| `ip` | `address` | Indirizzo IPv4 |
| `ip6` | `address` (con ':') | Indirizzo IPv6 |
| `mac_address` | `hwaddress`, `hwaddr` | MAC address |
| `bridge` | `bridge` | Nome del bridge associato |
| `vlan_parent` | `vlan-raw-device`, `vlan-dev` | Interfaccia parent per VLAN |

### Esempio di Parsing

Dato questo blocco in `/etc/network/interfaces`:

```
auto vmbr_EXT
iface vmbr_EXT inet static
    address 192.168.1.1
    netmask 255.255.255.0
    gateway 192.168.1.254
    bridge-ports enp5s0f1np1
    bridge-stp off
    bridge-fd 0
    bridge-vlan-aware yes
    bridge-vids 2-4094

auto bond0
iface bond0 inet manual
    bond-slaves eno1np0 eno2np1 eno3np2 eno4np3
    bond-mode 802.3ad
    bond-miimon 100
    bond-xmit-hash-policy layer3+4
```

Il parser ora estrae:

```python
{
    'vmbr_EXT': {
        'name': 'vmbr_EXT',
        'ip': '192.168.1.1',
        'netmask': '255.255.255.0',
        'gateway': '192.168.1.254',
        'members': 'enp5s0f1np1',
        'vlan_id': '2-4094',
        'category': 'bridge',
        ...
    },
    'bond0': {
        'name': 'bond0',
        'members': 'eno1np0; eno2np1; eno3np2; eno4np3',
        'bond_mode': '802.3ad',
        'category': 'bond',
        ...
    }
}
```

## Benefici

1. ✅ **Rilevamento Completo**: Tutte le interfacce configurate vengono riportate
2. ✅ **VLAN Corrette**: I VLAN ID (anche range) vengono estratti correttamente
3. ✅ **Bond Mode**: L'algoritmo di bilanciamento viene incluso nel CSV
4. ✅ **Gateway Estratti**: Gateway IPv4 e IPv6 per bridge e interfacce fisiche
5. ✅ **Netmask**: Netmask IPv4 estratta correttamente
6. ✅ **Consistenza**: `/etc/network/interfaces` è la fonte autoritativa
7. ✅ **Backward Compatible**: Le interfacce non in `/etc/network/interfaces` (se rilevate dall'API) vengono comunque incluse

## File CSV Generati

I CSV di network ora includeranno colonne popolate per:

- `vlan_id` (es. "20", "2-4094")
- `bond_mode` (es. "802.3ad", "balance-rr", "active-backup")
- `gateway` (es. "192.168.1.254")
- `gateway6` (es. "fe80::1")
- `netmask` (es. "255.255.255.0")
- Tutte le interfacce configurate, non solo quelle "up"

## Testing

Per testare le modifiche:

1. Eseguire `proxreporter` su un host Proxmox:
   ```bash
   cd /Users/riccardo/domarc/Proxreporter
   python3 proxmox_core.py --codcli 99999 --nomecliente TEST
   ```

2. Verificare il CSV generato in `reports/csv/`:
   ```bash
   head reports/csv/*_network.csv
   ```

3. Controllare che:
   - La colonna `vlan_id` sia popolata per bridge con VLAN
   - La colonna `bond_mode` sia presente per interfacce bond
   - Tutte le interfacce da `/etc/network/interfaces` siano incluse

## Prossimi Passi

- [ ] Eseguire l'importer per importare i nuovi dati nel database
- [ ] Verificare che la web interface mostri correttamente i VLAN ID
- [ ] Aggiornare la documentazione utente

## Note

La funzione `parse_interfaces_config()` (righe 215-253) già normalizza le chiavi sostituendo `-` con `_`, quindi:
- `bridge-vids` diventa `bridge_vids`
- `bond-mode` diventa `bond_mode`
- `vlan-id` diventa `vlan_id`

Questo è già gestito correttamente nel codice.

