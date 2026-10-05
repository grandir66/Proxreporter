"""Esito dei backup vzdump inviati a Graylog (PVE_BACKUP_RESULT).

Fino alla 2.21.1 l'esito si leggeva da ``exitstatus``, un campo che l'elenco
``/nodes/<nodo>/tasks`` di Proxmox non ha: l'esito sta in ``status``. Risultato
(2026-10-05, Graylog del live): 4.858 PVE_BACKUP_RESULT in 7 giorni su 31
clienti, TUTTI «warning» con ``exit_status`` vuoto — un backup fallito e uno
riuscito erano indistinguibili. E siccome ogni job era «warning», partivano
tutti: dopo la correzione devono partire tutti lo stesso, riusciti compresi.
"""
import pve_monitor as pm


def _task(status, upid="UPID:pxa:1", vmid="", start=1_000_000, end=1_000_600, **altro):
    t = {"upid": upid, "id": vmid, "user": "root@pam", "type": "vzdump",
         "starttime": start, "status": status}
    if end is not None:
        t["endtime"] = end
    t.update(altro)
    return t


def test_ok_e_un_successo():
    assert pm.esito_task_vzdump(_task("OK")) == ("success", "OK")


def test_warnings_e_un_avviso():
    assert pm.esito_task_vzdump(_task("WARNINGS: 2")) == ("warning", "WARNINGS: 2")


def test_un_testo_di_errore_e_un_fallimento():
    assert pm.esito_task_vzdump(_task("job errors"))[0] == "failed"
    assert pm.esito_task_vzdump(_task("unable to open file"))[0] == "failed"


def test_in_corso_non_conta():
    assert pm.esito_task_vzdump(_task("running")) is None
    assert pm.esito_task_vzdump(_task("", end=None)) is None


def test_exitstatus_se_c_e_vale_ancora():
    """Dall'endpoint di un task singolo l'esito arriva in ``exitstatus``."""
    assert pm.esito_task_vzdump(_task("stopped", exitstatus="OK")) == ("success", "OK")


class _Syslog:
    def __init__(self):
        self.inviati = []

    def send(self, tipo, dati, test_mode):
        self.inviati.append((tipo, dati))


def _monitor(monkeypatch, tasks):
    mon = pm.PVEMonitor({"pve_monitor": {"enabled": True}})
    mon.node = "pxa"
    mon.syslog = _Syslog()
    monkeypatch.setattr(pm, "pvesh_get", lambda *a, **k: tasks)
    monkeypatch.setattr(pm, "get_cluster_resources_cached", lambda: [])
    return mon


def test_ogni_job_parte_col_suo_esito(monkeypatch):
    tasks = [
        _task("OK", upid="UPID:a", start=1_000_000),
        _task("job errors", upid="UPID:b", start=2_000_000, end=2_000_600),
        _task("running", upid="UPID:c", start=3_000_000, end=None),
    ]
    mon = _monitor(monkeypatch, tasks)
    esito = mon._collect_backup_results(test_mode=False)
    assert esito["sent"] is True and esito["tasks"] == 2
    tipi = [t for t, _ in mon.syslog.inviati]
    assert tipi == ["PVE_BACKUP_RESULT", "PVE_BACKUP_RESULT"]
    per_upid = {d["task_ids"][0]: d for _, d in mon.syslog.inviati}
    assert per_upid["UPID:a"]["status"] == "success"
    assert per_upid["UPID:a"]["vms"][0]["exit_status"] == "OK"
    assert per_upid["UPID:b"]["status"] == "failed"


def test_il_successo_parte_anche_col_vecchio_freno_spento(monkeypatch):
    """Le installazioni hanno ``send_backup_result_on_success: false`` nel
    config.json: il job riuscito deve arrivare lo stesso."""
    mon = _monitor(monkeypatch, [_task("OK")])
    mon.send_backup_result_on_success = False
    mon._collect_backup_results(test_mode=False)
    assert [d["status"] for _, d in mon.syslog.inviati] == ["success"]
