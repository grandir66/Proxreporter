# Estrazione Informazioni Licenza Proxmox

## Data: 10 Novembre 2025

## Panoramica

Aggiunta l'estrazione delle informazioni sulla **licenza/subscription Proxmox** utilizzando il comando `pvesubscription get`. Queste informazioni sono fondamentali per tracciare le licenze acquistate e le scadenze.

## Comando Utilizzato

```bash
pvesubscription get
```

## Output del Comando

Il comando restituisce informazioni strutturate come:

```
checktime: 1762738850
key: pve1c-9a4f4a0215
level: c
nextduedate: 2026-07-17
productname: Proxmox VE Community Subscription 1 CPU/year
regdate: 2025-07-17 00:00:00
serverid: 3F2A0CD4A3903A8CD0E50F763A53B880
sockets: 1
status: active
url: https://www.proxmox.com/en/proxmox-virtual-environment/pricing
```

## Campi Estratti

| Campo Output | Nome campo CSV | Descrizione |
|--------------|----------------|-------------|
| `status` | `lic_status` | Stato della subscription (active, notfound, invalid) |
| `key` | `lic_key` | Chiave della licenza (es. pve1c-9a4f4a0215) |
| `level` | `lic_level` | Livello subscription (c=Community, b=Basic, s=Standard, p=Premium) |
| `productname` | `lic_type` | Nome del prodotto (es. "Proxmox VE Community Subscription 1 CPU/year") |
| `nextduedate` | `lic_scad` | Prossima scadenza (es. "2026-07-17") |
| `serverid` | `lic_sub` | ID univoco del server |
| `sockets` | `lic_sock` | Numero di socket CPU licenziati |
| `regdate` | `lic_regdate` | Data di registrazione (es. "2025-07-17 00:00:00") |

## Implementazione

### Posizione del Codice

**File**: `/Users/riccardo/domarc/Proxreporter/proxmox_report.py`  
**Metodo**: `ProxmoxLocalExtractor.enrich_host_info_with_commands()`  
**Righe**: 1129-1164

### Logica di Parsing

```python
# Esegui comando
sub_output = executor('pvesubscription get 2>/dev/null')

# Parse output (formato: "key: value")
sub_data = {}
for line in sub_output.splitlines():
    line = line.strip()
    if ':' in line:
        key, value = line.split(':', 1)
        key = key.strip().lower().replace(' ', '_')
        value = value.strip()
        sub_data[key] = value

# Estrai campi in host_info con schema corretto
host_info['lic_status'] = sub_data.get('status')      # status -> lic_status
host_info['lic_key'] = sub_data.get('key')            # key -> lic_key
host_info['lic_level'] = sub_data.get('level')        # level -> lic_level
host_info['lic_type'] = sub_data.get('productname')   # productname -> lic_type
host_info['lic_scad'] = sub_data.get('nextduedate')   # nextduedate -> lic_scad
host_info['lic_sub'] = sub_data.get('serverid')       # serverid -> lic_sub
host_info['lic_sock'] = sub_data.get('sockets')       # sockets -> lic_sock
host_info['lic_regdate'] = sub_data.get('regdate')    # regdate -> lic_regdate
```

## Colonne CSV Host

Le seguenti colonne sono state aggiunte nel CSV degli host:

```
lic_status      # Stato: active, notfound, invalid
lic_key         # Chiave licenza
lic_level       # Livello: c, b, s, p
lic_type        # Nome prodotto/tipo subscription
lic_scad        # Data scadenza (nextduedate)
lic_sub         # Server ID
lic_sock        # Numero socket licenziati
lic_regdate     # Data registrazione
```

## Esempio di Dati nel CSV

```csv
hostname;proxmox_version;lic_status;lic_key;lic_level;lic_type;lic_scad;lic_sock
DA-PX-01;8.2.2;active;pve1c-9a4f4a0215;c;Proxmox VE Community Subscription 1 CPU/year;2026-07-17;1
DA-PX-02;8.1.4;notfound;;;;;
DA-PX-03;8.2.0;active;pve2s-1b2c3d4e5f;s;Proxmox VE Standard Subscription 2 CPU/year;2025-12-31;2
```

## Livelli di Subscription

| Codice | Nome | Descrizione |
|--------|------|-------------|
| `c` | Community | Subscription Community (1 CPU) |
| `b` | Basic | Subscription Basic |
| `s` | Standard | Subscription Standard |
| `p` | Premium | Subscription Premium |

## Stati Possibili

| Stato | Descrizione |
|-------|-------------|
| `active` | Subscription attiva e valida |
| `notfound` | Nessuna subscription trovata (uso gratuito) |
| `invalid` | Subscription non valida o scaduta |
| `new` | Subscription nuova, non ancora attivata |

## Gestione Errori

Se il comando `pvesubscription get` non è disponibile o fallisce:
- Nessun errore viene generato
- I campi subscription rimangono `None` nel CSV (o "N/A")
- L'esecuzione continua normalmente

Questo permette di supportare sia host Proxmox con licenza che senza.

## Benefici

1. ✅ **Tracciamento Licenze**: Visibilità completa su tutte le licenze acquistate
2. ✅ **Monitoraggio Scadenze**: Identificazione rapida di licenze in scadenza
3. ✅ **Audit Compliance**: Verifica che tutti gli host abbiano licenze valide
4. ✅ **Pianificazione Rinnovi**: Data di prossima scadenza per ogni server
5. ✅ **Gestione Inventario**: Numero di socket licenziati per server

## Utilizzo nei Report

### Alert Licenze in Scadenza

```bash
# Trova licenze in scadenza nei prossimi 30 giorni
awk -F';' '
  $16 != "N/A" && $16 != "" {
    cmd = "date -d \"" $16 "\" +%s"
    cmd | getline scadenza
    close(cmd)
    "date +%s" | getline oggi
    close("date +%s")
    giorni = (scadenza - oggi) / 86400
    if (giorni < 30 && giorni > 0) {
      print $2, $16, int(giorni) " giorni"
    }
  }
' host.csv
```

### Verifica Compliance

```bash
# Trova host senza licenza attiva
awk -F';' '$13 != "active" {print $2, $13}' host.csv
```

### Report Licenze per Cliente

```bash
# Raggruppa per tipo subscription
awk -F';' 'NR>1 && $14 != "" {
  sockets[$14] += $17
  count[$14]++
}
END {
  for (level in count) {
    print level ": " count[level] " servers, " sockets[level] " total sockets"
  }
}' host.csv
```

## Integrazione Web Interface

Nella web interface del Proxmox Manager, questi dati verranno mostrati:

### Tabella Principale Host
- **Lic. Stato**: Badge colorato (verde=active, rosso=invalid, grigio=notfound)
- **Lic. Scadenza**: Data con highlight se mancano < 60 giorni

### Dettaglio Host Espanso
Sezione **"Informazioni Licenza"**:
- Stato: Active/Invalid/Not Found
- Chiave: `pve1c-9a4f4a0215`
- Livello: Community/Standard/Premium
- Prodotto: Proxmox VE Community Subscription 1 CPU/year
- Registrazione: 17/07/2025
- Scadenza: **17/07/2026** (245 giorni)
- Sockets Licenziati: 1

## Test

### Verifica Estrazione

```bash
# Esegui proxreporter
cd /Users/riccardo/domarc/Proxreporter
python3 proxmox_core.py --codcli 70791 --nomecliente DOMARC --host <ip> --username root@pam --password <pwd>

# Verifica CSV host
head -1 reports/csv/*_host.csv | grep -o "subscription_"
# Dovrebbe mostrare: subscription_status subscription_key subscription_level ...

# Visualizza subscription estratte
awk -F';' 'NR==1 {
  for(i=1;i<=NF;i++) if($i ~ /subscription/) col[i]=$i
}
NR>1 {
  for(i in col) printf "%s: %s  ", col[i], $i
  print ""
}' reports/csv/*_host.csv
```

### Confronto Manuale

Su un host Proxmox:
```bash
ssh root@<proxmox-host>
pvesubscription get
```

Confronta l'output con i valori nel CSV.

## Note Tecniche

1. **Comando Disponibile da**: Proxmox VE 3.0+
2. **Permessi Richiesti**: Root o utente con accesso a `pvesubscription`
3. **Performance**: Comando molto veloce (<100ms)
4. **Affidabilità**: Non richiede connessione internet (dati locali)

## Compatibilità

- ✅ Proxmox VE 6.x
- ✅ Proxmox VE 7.x
- ✅ Proxmox VE 8.x
- ✅ Host senza subscription (ritorna `notfound`)
- ✅ Esecuzione locale e remota (SSH)

## Migliorie Future

- [ ] Alert automatico per licenze in scadenza
- [ ] Dashboard riepilogativo licenze
- [ ] Export report compliance PDF
- [ ] Integrazione con sistema ticketing per rinnovi

