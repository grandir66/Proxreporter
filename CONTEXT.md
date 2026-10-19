# CONTEXT.md — Proxreporter

Fonte per il codice esistente: README.md. Repo ~/Progetti/Proxreporter,
censimento ~/Progetti/MAPPA-PROGETTI.html; logica del portale in DA-Proxmox,
~/Progetti/DA-Proxmox/CONTEXT.md. Segreti solo ACCESSI.md e vault esterno.

Sviluppo19/10/2026 autorizzato da Riccardo via DA-Link: esecutore remoto su base
Linux, worktree ~/.codex/worktrees/proxreporter-remoto-20261019,
ramo codex/esecutore-remoto-20261019 da origin/maincf78d19. Primario intatto.
Nuovo remote_runner.py: configure acquisisce credenziali solo localmente;
run esegue run_report per più host in serie sotto lock; config e chiavi0600,
chiavi host SSH strict. Nessun fallback locale, modifica notifiche nodi,
sync config centrale o auto-update durante il report. Percorso locale invariato.
Report esistenti riusati; nessuna installazione di codice sui nodi remoti.

Verifica: scripts/controlla.sh,141test verdi, compilazione Python verde.
Due host simulati e prosecuzione dopo guasto, priorità SSH, policy strict,
configurazione cifrata/rerun/permessi e nessun segreto negli errori verificati.
Nessun host reale interrogato. Manca collaudo base Linux con due host del laboratorio.
Survey_audit.py resta locale: va separato esecutore/destinazione e verificato
arruolamento/stato per host prima di offrirlo come survey remota.
Configurazione password/SFTP conserva il contratto precedente; migrazione
account SFTP comune per cliente è la tappa C2, non inclusa.

Cancello: bash scripts/controlla.sh con .venv Python3.13 e requirements.txt+pytest.
Piano coordinatore: worktree DA-Link da-link-rilievi-20261019,
docs/superpowers/plans/2026-10-19-configuratore-diretto.md.
