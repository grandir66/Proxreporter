"""Destinatari delle notifiche dei backup (target da-alert-<codcli>).

Dal 2026-10-05 (Riccardo) anche proxmox@domarc.it riceve le notifiche, accanto a
mycheckcentral che controlla gli esiti dei backup: si AGGIUNGE, non si sostituisce.
La configurazione gira a ogni esecuzione e unisce gli indirizzi già presenti nel
target con quelli dei job: è lì che l'indirizzo nuovo arriva anche sui cluster
già installati.
"""
import json

import proxmox_core as pc


def _esecutore(comandi, mailto_target, mailto_job):
    def esegui(cmd):
        comandi.append(cmd)
        if "matchers/" in cmd and "pvesh get" in cmd:
            return "NOT_EXISTS"
        if cmd.startswith("pvesh get /cluster/backup"):
            return json.dumps([{"id": "backup-1", "mailto": mailto_job}])
        if "pvesh get /cluster/notifications/endpoints/" in cmd:
            return json.dumps({"mailto": mailto_target})
        return ""
    return esegui


def test_target_esistente_riceve_anche_proxmox_domarc():
    comandi = []
    pc.configure_backup_jobs_notification(
        "da-alert-70791", "70791", "ssh",
        _esecutore(comandi, ["domarcsrl+pxbackup@mycheckcentral.cc"], "helpdesk@domarc.it"))
    aggiornamento = [c for c in comandi if c.startswith("pvesh set /cluster/notifications/endpoints/") and "--mailto" in c]
    assert aggiornamento, comandi
    for indirizzo in ("proxmox@domarc.it", "domarcsrl+pxbackup@mycheckcentral.cc", "helpdesk@domarc.it"):
        assert indirizzo in aggiornamento[-1]


def test_destinatari_predefiniti_tengono_mycheckcentral():
    assert pc.DEFAULT_RECIPIENTS == ["domarcsrl+pxbackup@mycheckcentral.cc", "proxmox@domarc.it"]


def test_percorso_giusto_e_indirizzi_ripetuti():
    comandi = []
    pc.configure_backup_jobs_notification(
        "da-alert-70791", "70791", "ssh",
        _esecutore(comandi, ["domarcsrl+pxbackup@mycheckcentral.cc"], ""))
    letture = [c for c in comandi if c.startswith("pvesh get /cluster/notifications/endpoints/")]
    assert letture and all("/endpoints/smtp/da-alert-70791" in c for c in letture)
    scrittura = [c for c in comandi if c.startswith("pvesh set ")][-1]
    assert "/endpoints/smtp/da-alert-70791" in scrittura
    assert scrittura.count("--mailto") == 2          # un --mailto per indirizzo: pvesh vuole un <array>


def test_target_non_leggibile_non_si_tocca():
    comandi = []

    def esegui(cmd):
        comandi.append(cmd)
        if "matchers/" in cmd and "pvesh get" in cmd:
            return "NOT_EXISTS"
        if cmd.startswith("pvesh get /cluster/backup"):
            return json.dumps([{"id": "b", "mailto": "helpdesk@domarc.it"}])
        return ""                                     # la lettura del target non risponde
    pc.configure_backup_jobs_notification("da-alert-1", "1", "ssh", esegui)
    assert not [c for c in comandi if c.startswith("pvesh set /cluster/notifications/endpoints/")]


def test_target_gia_completo_nessuna_scrittura():
    comandi = []
    pc.configure_backup_jobs_notification(
        "da-alert-1", "1", "ssh",
        _esecutore(comandi, ["domarcsrl+pxbackup@mycheckcentral.cc", "proxmox@domarc.it", "helpdesk@domarc.it"],
                   "helpdesk@domarc.it"))
    assert not [c for c in comandi if c.startswith("pvesh set /cluster/notifications/endpoints/")]
