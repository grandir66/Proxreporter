# AGENTS.md — Proxreporter

Report e audit sui nodi Proxmox dei clienti: CSV/HTML, SFTP, GELF, heartbeat orario,
monitor hardware (SMART, RAID, ECC). Installazione, aggiornamento e uso a mano nel
`README.md`.

## Regole del repo

- **Si installa e si auto-aggiorna dal repo GitHub**: `install.sh` e `migrate.py`
  fanno `git fetch` + `git reset --hard origin/main` sui nodi dei clienti. **Ogni
  push su `main` arriva ai clienti**: vale come un rilascio in produzione e chiede
  conferma. Archiviare o rendere privato il repo romperebbe i clienti.

## Regole comuni (qui non si ricopiano)

Valgono le regole dell'area in `~/Progetti/AGENTS.md` e quelle di comportamento in
`~/.claude/AGENTS.md` (contesto su disco in `CONTEXT.md`, niente prove a caso,
loop di fine sessione). Questo file dice solo ciò che è di questo repo.
