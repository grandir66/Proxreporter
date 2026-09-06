# Graylog - Normalizzazione Campi Backup

Pipeline rules per uniformare i nomi dei campi tra i risultati di backup
di sorgenti diverse (Veeam, Proxmox PVE), senza modificare i messaggi syslog originali.

## Problema

I job di backup Veeam (`VEEAM_JOB_RESULT`) e Proxmox (`PVE_BACKUP_RESULT`)
usano nomi diversi per gli stessi concetti:

| Concetto | Veeam | Proxmox | Dopo normalizzazione |
|---|---|---|---|
| Inizio job | `start_time` | `job_start_time` | entrambi disponibili |
| Fine job | `end_time` | `job_end_time` | entrambi disponibili |
| Durata (sec) | `duration_seconds` | `job_duration_seconds` | entrambi disponibili |
| Durata (min) | `duration_minutes` | `job_duration_minutes` | entrambi disponibili |
| Totale oggetti | `objects_total` | `vm_count` | entrambi disponibili |
| Successi | `objects_success` | `vms_success` | entrambi disponibili |
| Warning | `objects_warning` | `vms_warning` | entrambi disponibili |
| Falliti | `objects_failed` | `vms_failed` | entrambi disponibili |

### Campi comuni (già identici, nessuna modifica)

`agent_hostname`, `status`, `client` (code/name/site), `message_type`, `version`, `timestamp`

### Campi esclusivi Veeam

`job_name`, `job_id`, `job_type`, `data_size_bytes`, `data_size_gb`,
`transferred_bytes`, `transferred_gb`, `bottleneck`, `is_retry`, `result_message`

### Campi esclusivi Proxmox

`user`, `task_ids`, `vms[].vmid`, `vms[].type`, `vms[].exit_status`, `vms[].task_id`

### Campo aggiunto dalla normalizzazione

`backup_source` = `"veeam"` o `"proxmox"` — per filtrare/raggruppare per sorgente.

## Soluzione

Due pipeline rules che creano **alias bidirezionali**: ogni sorgente ottiene i nomi
dell'altra come campi aggiuntivi. I campi originali restano invariati.

Dopo la normalizzazione, su **entrambe** le sorgenti si può cercare con:
- `job_start_time:*` oppure `start_time:*`
- `objects_total:>0` oppure `vm_count:>0`
- `backup_source:veeam OR backup_source:proxmox`

## Installazione

### Opzione 1: Content Pack (consigliato)

1. In Graylog → **System → Content Packs**
2. Cliccare **Upload** e selezionare `content_pack_backup_normalization.json`
3. Installare il content pack
4. Verificare in **System → Pipelines** che la pipeline "Backup Field Normalization"
   sia collegata allo stream "All messages" (o allo stream specifico dei backup)

### Opzione 2: Importazione manuale

1. In Graylog → **System → Pipelines → Manage rules**
2. Creare due nuove regole incollando il contenuto di:
   - `pipeline_rules/normalize_veeam_backup.rule`
   - `pipeline_rules/normalize_pve_backup.rule`
3. Creare una pipeline "Backup Field Normalization"
4. Aggiungere entrambe le regole allo **Stage 0** con match **either**
5. Collegare la pipeline allo stream "All messages" o allo stream dei backup

## Verifica

Dopo l'installazione, i messaggi `VEEAM_JOB_RESULT` avranno anche:
`job_start_time`, `job_end_time`, `job_duration_seconds`, `job_duration_minutes`,
`vm_count`, `vms_success`, `vms_warning`, `vms_failed`, `backup_source`

E i messaggi `PVE_BACKUP_RESULT` avranno anche:
`start_time`, `end_time`, `duration_seconds`, `duration_minutes`,
`objects_total`, `objects_success`, `objects_warning`, `objects_failed`, `backup_source`

## Query di esempio per dashboard unificate

```
# Tutti i backup falliti (qualsiasi sorgente)
message_type:(VEEAM_JOB_RESULT OR PVE_BACKUP_RESULT) AND status:failed

# Durata job > 60 minuti
message_type:(VEEAM_JOB_RESULT OR PVE_BACKUP_RESULT) AND duration_minutes:>60

# Backup di un cliente specifico
message_type:(VEEAM_JOB_RESULT OR PVE_BACKUP_RESULT) AND client.code:71343

# Solo sorgente Veeam
backup_source:veeam AND status:*

# Confronto successi per sorgente (aggregazione)
# Raggruppa per backup_source, somma objects_success
```
