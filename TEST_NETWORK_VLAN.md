# Test: Rilevamento VLAN e Interfacce Complete

## Obiettivo
Verificare che le modifiche al `proxreporter` rilevino correttamente:
- VLAN ID da `/etc/network/interfaces`
- Algoritmo di bilanciamento (bond-mode)
- Tutte le interfacce configurate

## Prerequisiti

1. Accesso SSH a un host Proxmox
2. Script `proxreporter` modificato
3. File `/etc/network/interfaces` con configurazioni VLAN/Bond

## Procedura di Test

### 1. Verifica File di Configurazione

Connettiti all'host Proxmox e visualizza `/etc/network/interfaces`:

```bash
ssh root@<proxmox-host>
cat /etc/network/interfaces
```

**Cerca configurazioni tipo:**
```
iface vmbr_EXT inet manual
    bridge-ports enp5s0f1np1
    bridge-vlan-aware yes
    bridge-vids 2-4094

iface bond0 inet manual
    bond-slaves eno1np0 eno2np1
    bond-mode 802.3ad
```

Annota:
- Nome interfacce con VLAN: `_________________`
- VLAN ID attesi: `_________________`
- Interfacce bond: `_________________`
- Bond mode attesi: `_________________`

### 2. Esegui Proxreporter (Modalità Locale)

**Se esegui SULL'host Proxmox:**

```bash
cd /path/to/Proxreporter
python3 proxmox_report.py
```

**Se esegui da REMOTO:**

```bash
cd /Users/riccardo/domarc/Proxreporter
python3 proxmox_core.py --codcli 70791 --nomecliente DOMARC --host <proxmox-ip> --username root@pam --password <password>
```

### 3. Verifica CSV Generato

```bash
cd /Users/riccardo/domarc/Proxreporter/reports/csv

# Lista ultimi file network
ls -lht *_network.csv | head -3

# Visualizza CSV (con colonne allineate)
column -t -s';' <ultimo_file_network.csv> | less -S
```

### 4. Controlli da Effettuare

#### 4.1 VLAN ID Popolati

```bash
# Estrai colonna vlan_id
awk -F';' '{print $11}' <file_network.csv> | head -10
```

**Verifica:**
- [ ] La colonna `vlan_id` contiene valori (es. "2-4094", "20")
- [ ] I VLAN ID corrispondono a quelli in `/etc/network/interfaces`
- [ ] Le interfacce senza VLAN hanno campo vuoto (non "N/A")

#### 4.2 Bond Mode Visibili

```bash
# Estrai interfacce bond con relativo mode
awk -F';' '$3=="bond" {print $4, $17}' <file_network.csv>
```

**Verifica:**
- [ ] La colonna `bond_mode` è popolata per interfacce bond
- [ ] I valori corrispondono a quelli configurati (es. "802.3ad", "balance-rr")

#### 4.3 Gateway Estratti

```bash
# Estrai interfacce con gateway
awk -F';' '$9!="" && $9!="N/A" {print $4, $9}' <file_network.csv>
```

**Verifica:**
- [ ] La colonna `gateway` è popolata per bridge/interfacce con gateway configurato
- [ ] I valori corrispondono a quelli in `/etc/network/interfaces`

#### 4.4 Tutte le Interfacce Presenti

```bash
# Conta interfacce nel CSV
tail -n +2 <file_network.csv> | wc -l

# Conta interfacce in /etc/network/interfaces
ssh root@<proxmox-host> "grep '^iface ' /etc/network/interfaces | wc -l"
```

**Verifica:**
- [ ] Il numero di interfacce nel CSV è >= a quelle configurate
- [ ] Interfacce bond, bridge, vlan sono tutte presenti
- [ ] Anche interfacce "down" sono incluse se configurate

#### 4.5 IP Addresses Estratti

```bash
# Estrai interfacce con IP
awk -F';' '$7!="" {print $4, $7}' <file_network.csv>
```

**Verifica:**
- [ ] Gli IP configurati in `/etc/network/interfaces` sono presenti
- [ ] Gli IP rilevati da `ip addr` sono inclusi

### 5. Test di Importazione

Una volta verificato che il CSV è corretto, importalo nel database:

```bash
cd /Users/riccardo/domarc/stormshield-manager/web_interface

# Copia il CSV nella cartella SFTP staging
cp /Users/riccardo/domarc/Proxreporter/reports/csv/<file_network.csv> \
   modules/importproxmox/tmp/importproxmox_staging/

# Esegui l'importer
python3 modules/importproxmox/importer.py
```

### 6. Verifica Web Interface

1. Apri il browser: `http://localhost:5000`
2. Naviga a: **Proxmox Manager**
3. Seleziona un host
4. Espandi sezione **Network**

**Verifica:**
- [ ] La colonna `VLAN ID` mostra i valori corretti
- [ ] Interfacce bond mostrano categoria "bond"
- [ ] Tutte le interfacce configurate sono visibili

## Esempio di Output Atteso

### CSV Network (con VLAN, Bond Mode, Gateway)

```
server_identifier;hostname;category;name;type;state;mac_address;ip_addresses;gateway;gateway6;netmask;bridge;members;vlan_id;bond_mode;speed_mbps;comment
DA-PX-01;DA-PX-01;bridge;vmbr_EXT;bridge;up;00:11:22:33:44:55;192.168.1.1;192.168.1.254;;255.255.255.0;;enp5s0f1np1;2-4094;;
DA-PX-01;DA-PX-01;bond;bond0;bond;up;aa:bb:cc:dd:ee:ff;;;;;;eno1np0; eno2np1; eno3np2; eno4np3;;802.3ad;;
DA-PX-01;DA-PX-01;bridge;vmbr_INT;bridge;up;11:22:33:44:55:66;;;;;;bond0;20;;;
DA-PX-01;DA-PX-01;physical;enp3s0;eth;up;ff:ee:dd:cc:bb:aa;;;;;;;;2500.0 Mbps;
```

### Database (Tabella proxmox_networks)

```sql
SELECT name, category, vlan_id, state 
FROM proxmox_networks 
WHERE codcli = '70791' AND server_name = 'DA-PX-01'
ORDER BY category, name;
```

**Risultato atteso:**

| name | category | vlan_id | state |
|------|----------|---------|-------|
| bond0 | bond | | up |
| vmbr_EXT | bridge | 2-4094 | up |
| vmbr_INT | bridge | 20 | up |
| enp3s0 | physical | | up |

## Troubleshooting

### VLAN ID ancora vuoti

**Possibili cause:**
1. Il file `/etc/network/interfaces` non ha la direttiva `bridge-vids`
2. La direttiva ha un nome diverso (es. `vlan-id` invece di `bridge-vids`)

**Soluzione:**
Verifica il contenuto esatto del file:
```bash
ssh root@<proxmox-host> "grep -A 10 'iface vmbr' /etc/network/interfaces"
```

### Interfacce mancanti nel CSV

**Possibile causa:** Errore durante l'esecuzione del parser

**Soluzione:**
Esegui proxreporter in modalità debug:
```bash
python3 proxmox_report.py 2>&1 | tee debug.log
grep "Errore" debug.log
```

### Tabelle database non esistono

**Causa:** L'importer non è mai stato eseguito correttamente

**Soluzione:**
```bash
cd /Users/riccardo/domarc/stormshield-manager/web_interface
python3 modules/importproxmox/importer.py --create-tables
```

## Report Test

Compila dopo aver eseguito i test:

```
Data test: _______________
Host testato: _______________
Codice cliente: _______________

Risultati:
- [ ] VLAN ID estratti correttamente
- [ ] Bond mode visibile
- [ ] Tutte interfacce presenti
- [ ] Importazione database riuscita
- [ ] Web interface mostra dati corretti

Note aggiuntive:
_________________________________
_________________________________
_________________________________
```

## Contatti

Per segnalare problemi o risultati:
- Documentare in `CHANGELOG_NETWORK_DETECTION.md`
- Aggiungere esempi in `MANUALE.md`

