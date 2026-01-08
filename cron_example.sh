#!/bin/bash
# Esempio di configurazione cron per proxmox_report.py
# 
# Per installare nel crontab:
#   crontab -e
#   Aggiungi una delle righe seguenti

# Esegui ogni giorno alle 2:00 AM
# 0 2 * * * cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py >> /var/log/proxmox_report.log 2>&1

# Esegui ogni giorno alle 8:00 AM
# 0 8 * * * cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py >> /var/log/proxmox_report.log 2>&1

# Esegui ogni 6 ore
# 0 */6 * * * cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py >> /var/log/proxmox_report.log 2>&1

# Esegui ogni lunedì alle 9:00 AM
# 0 9 * * 1 cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py >> /var/log/proxmox_report.log 2>&1

# Esegui ogni giorno alle 2:00 AM con configurazione personalizzata
# 0 2 * * * cd /path/to/Proxreporter && /usr/bin/python3 proxmox_report.py --config /path/to/custom_config.json >> /var/log/proxmox_report.log 2>&1

# IMPORTANTE:
# - Sostituisci /path/to/Proxreporter con il percorso reale dello script
# - Verifica il percorso di python3 con: which python3
# - Assicurati che lo script abbia i permessi di esecuzione: chmod +x proxmox_report.py
# - Per testare manualmente: python3 proxmox_report.py

echo "Esempi di configurazione cron per proxmox_report.py"
echo ""
echo "Per installare nel crontab:"
echo "  1. Modifica questo file con il percorso corretto"
echo "  2. Copia la riga desiderata"
echo "  3. Esegui: crontab -e"
echo "  4. Incolla la riga"
echo "  5. Salva e esci"
echo ""
echo "Per visualizzare i log:"
echo "  tail -f /var/log/proxmox_report.log"

