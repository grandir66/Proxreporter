"""In modalità ssh il target di notifica si crea quando manca.

Il controllo stampava "EXISTS" o "NOT_EXISTS" e cercava "EXISTS" nella
risposta: "NOT_EXISTS" lo contiene, quindi il target risultava sempre presente
e non veniva mai creato (trovato il 05/10/2026).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import proxmox_core as pc  # noqa: E402


def test_target_mancante_in_ssh_viene_creato():
    comandi = []

    def esegui(cmd, *a, **k):
        comandi.append(cmd)
        if "NOT_EXISTS" in cmd:
            return "NOT_EXISTS"
        return ""

    cfg = {"smtp": {"recipients": "proxmox@domarc.it", "password": "finta-per-la-prova"}}
    pc.configure_smtp_notification(None, "123", "ssh", esegui, cfg)
    creazioni = [c for c in comandi if "create" in c and "notifications/endpoints" in c]
    assert creazioni, "il target mancante non è stato creato: %r" % comandi
