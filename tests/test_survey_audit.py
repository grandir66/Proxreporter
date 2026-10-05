"""L'agente che manda la verifica a survey: le decisioni, senza toccare la rete."""
import io
import json
import os
import stat
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import survey_audit as sa  # noqa: E402

# La forma VERA di `pvesh get /cluster/status --output-format json`.
CLUSTER = [
    {"type": "cluster", "name": "pve-acme", "nodes": 3, "quorate": 1},
    {"type": "node", "name": "pve2", "online": 1, "local": 1, "ip": "10.0.0.2"},
    {"type": "node", "name": "pve1", "online": 1, "local": 0, "ip": "10.0.0.1"},
    {"type": "node", "name": "pve3", "online": 0, "local": 0, "ip": "10.0.0.3"},
]


def test_manda_il_primo_in_ordine_alfabetico_fra_gli_online():
    assert sa.tocca_a_me(CLUSTER, "pve1") is True
    assert sa.tocca_a_me(CLUSTER, "pve2") is False


def test_se_il_capofila_e_giu_tocca_al_secondo():
    giu = [n for n in CLUSTER if n.get("name") != "pve1"]
    assert sa.tocca_a_me(giu, "pve2") is True


def test_fuori_cluster_tocca_sempre_a_lui():
    assert sa.tocca_a_me([], "pve-solo") is True
    assert sa.tocca_a_me(None, "pve-solo") is True


def test_un_nodo_spento_non_diventa_capofila():
    """Un nodo offline non deve risultare capofila nemmeno se il suo nome
    venisse prima in ordine alfabetico."""
    solo_spento_primo = [
        {"type": "node", "name": "aaa-spento", "online": 0, "local": 0},
        {"type": "node", "name": "zzz-acceso", "online": 1, "local": 1},
    ]
    assert sa.tocca_a_me(solo_spento_primo, "zzz-acceso") is True


def test_la_pianificazione_e_stabile_e_sparsa():
    assert sa.pianificazione("ACME") == sa.pianificazione("ACME")      # stabile
    istanti = {sa.pianificazione("CL%03d" % i) for i in range(50)}
    assert len(istanti) > 30                                            # sparsa
    for minuto, ora, giorno in istanti:
        assert 0 <= minuto < 60 and 0 <= ora < 24 and 0 <= giorno < 7


def test_non_si_invia_due_volte_nella_stessa_settimana():
    adesso = 1789000000.0
    assert sa.troppo_presto({"ultimo_invio": adesso - 3600}, adesso) is True
    assert sa.troppo_presto({"ultimo_invio": adesso - 4 * 86400}, adesso) is False
    assert sa.troppo_presto({}, adesso) is False


def test_uno_strumento_troncato_non_si_accetta():
    """Un proxy che risponde 502 manda una pagina HTML: se la si esegue,
    l'agente fallisce in modo incomprensibile."""
    assert sa.accettabile("import sys\nprint(1)\n") is True
    assert sa.accettabile("<html><body>502 Bad Gateway</body></html>") is False
    assert sa.accettabile("def raccogli(:\n") is False


def test_il_pilota_si_accende_per_un_codcli_alla_volta():
    """Il master config e' uno per tutti: `solo` accende il pilota senza
    toccare un host, e svuotarlo apre a tutta la flotta."""
    assert sa.attivo_per({"enabled": True, "solo": ["ACME"]}, "ACME") is True
    assert sa.attivo_per({"enabled": True, "solo": ["ACME"]}, "BETA") is False
    assert sa.attivo_per({"enabled": True, "solo": []}, "BETA") is True
    assert sa.attivo_per({"enabled": False, "solo": []}, "ACME") is False
    assert sa.attivo_per({}, "ACME") is False


# ---- la parte che parla col mondo: rete finta, percorsi in tmp_path ----

class _Risposta(io.BytesIO):
    def __init__(self, corpo, status=200):
        super().__init__(corpo)
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


STRUMENTO_OK = 'VERSIONE_SCRIPT = "2.1"\nprint(1)\n'


def _http_error(codice):
    return urllib.error.HTTPError("http://x", codice, "err", {}, io.BytesIO(b"<html>errore</html>"))


@pytest.fixture
def percorsi(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "INSTALL", str(tmp_path))
    monkeypatch.setattr(sa, "FILE_CODICE", str(tmp_path / ".survey_codice"))
    monkeypatch.setattr(sa, "FILE_STATO", str(tmp_path / ".survey_stato.json"))
    monkeypatch.setattr(sa, "CACHE", str(tmp_path / "cache" / "audit-nodo.py"))
    monkeypatch.setattr(sa, "LOG", str(tmp_path / "log" / "survey.log"))
    avvisi = []
    monkeypatch.setattr(sa, "avvisa", lambda g, t: avvisi.append((g, t)))
    return tmp_path, avvisi


def _arruola():
    return sa.arruola("https://p/proxmox", "segreto", "ACME", "Acme Srl", "pve-acme", "pve1")


def test_arruola_200_ritorna_il_codice(percorsi, monkeypatch):
    visto = {}

    def finto(req, timeout=None):
        visto["url"] = req.full_url
        visto["segreto"] = req.get_header("X-arruolamento")
        visto["corpo"] = json.loads(req.data.decode())
        return _Risposta(b'{"codice":"PXM-AAAA-BBBB-CCCC","stato":"attivo"}')

    monkeypatch.setattr(sa.urllib.request, "urlopen", finto)
    assert _arruola() == "PXM-AAAA-BBBB-CCCC"
    assert visto["url"] == "https://p/proxmox/api/agente/arruola"
    assert visto["segreto"] == "segreto"
    assert visto["corpo"]["codcli"] == "ACME" and visto["corpo"]["nodo"] == "pve1"


def test_arruola_202_e_attesa_non_guasto(percorsi, monkeypatch):
    _, avvisi = percorsi
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda req, timeout=None: _Risposta(b'{"stato":"in_attesa"}', 202))
    assert _arruola() == ""
    assert avvisi == []


def test_arruola_409_scrive_revocato_il(percorsi, monkeypatch):
    def finto(req, timeout=None):
        raise _http_error(409)
    monkeypatch.setattr(sa.urllib.request, "urlopen", finto)
    assert _arruola() == ""
    assert sa.leggi_stato().get("revocato_il")


def test_arruola_403_avvisa_e_non_crasha(percorsi, monkeypatch):
    _, avvisi = percorsi

    def finto(req, timeout=None):
        raise _http_error(403)
    monkeypatch.setattr(sa.urllib.request, "urlopen", finto)
    assert _arruola() == ""
    assert avvisi and avvisi[0][0] == "warning"


def test_arruola_404_e_non_ancora_disponibile(percorsi, monkeypatch):
    _, avvisi = percorsi

    def finto(req, timeout=None):
        raise _http_error(404)
    monkeypatch.setattr(sa.urllib.request, "urlopen", finto)
    assert _arruola() == ""
    assert avvisi == []


def test_arruola_errore_di_rete_senza_allarme(percorsi, monkeypatch):
    _, avvisi = percorsi

    def finto(req, timeout=None):
        raise urllib.error.URLError("irraggiungibile")
    monkeypatch.setattr(sa.urllib.request, "urlopen", finto)
    assert _arruola() == ""
    assert avvisi == []


def test_il_codice_si_salva_atomico_e_0600(percorsi):
    tmp, _ = percorsi
    sa.salva_codice("PXM-AAAA-BBBB-CCCC", "ACME", "pve-acme")
    p = tmp / ".survey_codice"
    dati = json.loads(p.read_text())
    assert dati["codice"] == "PXM-AAAA-BBBB-CCCC" and dati["codcli"] == "ACME"
    assert dati["cluster"] == "pve-acme" and dati["emesso_il"]
    assert stat.S_IMODE(os.stat(str(p)).st_mode) == 0o600
    assert sa.codice_salvato() == "PXM-AAAA-BBBB-CCCC"
    assert not [f for f in os.listdir(str(tmp)) if f.endswith(".tmp")]


def test_stato_si_unisce_non_si_sovrascrive(percorsi):
    sa.scrivi_stato(ultimo_invio=1.0)
    sa.scrivi_stato(esito="ok")
    assert sa.leggi_stato() == {"ultimo_invio": 1.0, "esito": "ok"}


def test_download_valido_aggiorna_la_cache(percorsi, monkeypatch):
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda req, timeout=None: _Risposta(STRUMENTO_OK.encode()))
    p = sa.scarica_strumento("https://p/proxmox", "PXM-X")
    assert p == sa.CACHE and "VERSIONE_SCRIPT" in open(p).read()


def test_download_troncato_usa_la_cache(percorsi, monkeypatch):
    os.makedirs(os.path.dirname(sa.CACHE))
    with open(sa.CACHE, "w") as f:
        f.write(STRUMENTO_OK + "# vecchio\n")
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda req, timeout=None: _Risposta(b"<html>502 Bad Gateway</html>"))
    p = sa.scarica_strumento("https://p/proxmox", "PXM-X")
    assert p == sa.CACHE and "# vecchio" in open(p).read()


def test_download_troncato_senza_cache_ritorna_vuoto(percorsi, monkeypatch):
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda req, timeout=None: _Risposta(b"def x(:\n"))
    assert sa.scarica_strumento("https://p/proxmox", "PXM-X") == ""
    assert not os.path.exists(sa.CACHE)


def test_download_401_senza_cache_e_codice_non_valido(percorsi, monkeypatch):
    def finto(req, timeout=None):
        raise _http_error(401)
    monkeypatch.setattr(sa.urllib.request, "urlopen", finto)
    with pytest.raises(sa.CodiceNonValido):
        sa.scarica_strumento("https://p/proxmox", "PXM-X")


# ---- giro di correzioni 1 ----

def test_senza_dichiarazione_di_versione_non_si_accetta(percorsi, monkeypatch):
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda req, timeout=None: _Risposta(b"print('compila ma non e lo strumento')\n"))
    assert sa.scarica_strumento("https://p/proxmox", "PXM-X") == ""
    assert sa.versione_strumento(STRUMENTO_OK) == "2.1"
    assert sa.versione_strumento("x = 1\n# VERSIONE_SCRIPT = 3\n") == ""


def test_download_oltre_5mb_non_si_accetta(percorsi, monkeypatch):
    grosso = (STRUMENTO_OK + "#" * (6 * 1024 * 1024)).encode()
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda req, timeout=None: _Risposta(grosso))
    assert sa.scarica_strumento("https://p/proxmox", "PXM-X") == ""


def _config_portale(tmp, portale):
    (tmp / "config.json").write_text(json.dumps({
        "client": {"codcli": "ACME", "nomecliente": "Acme Srl"},
        "survey": {"enabled": True, "portale": portale, "arruolamento": "SEGRETO-XYZ"}}))


def test_portale_malformato_non_fa_uscire_il_codice(percorsi, monkeypatch):
    tmp, avvisi = percorsi
    _config_portale(tmp, "survey.domarc.it/proxmox")          # niente schema
    sa.salva_codice("PXM-AAAA-BBBB-CCCC", "ACME", "")
    monkeypatch.setattr(sa, "stato_cluster", lambda: [])
    assert sa.main([]) == 1
    log = open(sa.LOG).read()
    assert "PXM-" not in log
    assert all("PXM-" not in t for _, t in avvisi)


def test_arruola_con_portale_malformato_non_espone_nulla(percorsi):
    _, avvisi = percorsi
    assert sa.arruola("survey.domarc.it/proxmox", "SEGRETO-XYZ", "ACME", "A", "", "n") == ""
    assert "SEGRETO-XYZ" not in open(sa.LOG).read()


def test_eccezione_imprevista_e_ripulita_da_codice_e_segreto(percorsi, monkeypatch):
    tmp, avvisi = percorsi
    _config_portale(tmp, "https://p/proxmox")
    sa.salva_codice("PXM-AAAA-BBBB-CCCC", "ACME", "")
    monkeypatch.setattr(sa, "stato_cluster", lambda: [])

    def rompi(portale, codice):
        raise RuntimeError("boom %s e SEGRETO-XYZ" % codice)
    monkeypatch.setattr(sa, "scarica_strumento", rompi)
    assert sa.main([]) == 1
    tutto = open(sa.LOG).read() + " ".join(t for _, t in avvisi)
    assert "PXM-AAAA" not in tutto and "SEGRETO-XYZ" not in tutto
    assert "boom" in tutto


def test_stato_cluster_ignoto_non_invia(percorsi, monkeypatch):
    tmp, avvisi = percorsi
    _config_portale(tmp, "https://p/proxmox")
    sa.salva_codice("PXM-AAAA-BBBB-CCCC", "ACME", "")

    def pvesh_rotto(*a, **k):
        raise sa.subprocess.TimeoutExpired("pvesh", 60)
    monkeypatch.setattr(sa.subprocess, "run", pvesh_rotto)
    assert sa.stato_cluster() is None
    chiamato = []
    monkeypatch.setattr(sa.urllib.request, "urlopen", lambda *a, **k: chiamato.append(1))
    monkeypatch.setattr(sa, "esegui", lambda *a: chiamato.append(2) or 0)
    assert sa.main([]) == 0
    assert chiamato == []
    assert avvisi and avvisi[0][0] == "warning"
    assert "sconosciuto" in open(sa.LOG).read()


def _finto_alert_manager(monkeypatch, risultato):
    import types
    m = types.ModuleType("alert_manager")

    class Sev:
        WARNING = "w"
        ERROR = "e"
        CRITICAL = "c"

    class Tipo:
        CUSTOM = "custom"

    class Gestore:
        def __init__(self, cfg):
            pass

        def send_alert(self, *a, **k):
            return risultato
    m.AlertSeverity, m.AlertType, m.AlertManager = Sev, Tipo, Gestore
    monkeypatch.setitem(sys.modules, "alert_manager", m)


def test_allarme_non_consegnato_si_scrive_nel_log(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "INSTALL", str(tmp_path))
    monkeypatch.setattr(sa, "LOG", str(tmp_path / "s.log"))
    _finto_alert_manager(monkeypatch, {"email": False, "syslog": False})
    sa.avvisa("critical", "guasto X")
    assert "allarme NON consegnato: critical guasto X" in open(sa.LOG).read()


def test_allarme_consegnato_non_scrive_avviso_di_mancata_consegna(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "INSTALL", str(tmp_path))
    monkeypatch.setattr(sa, "LOG", str(tmp_path / "s.log"))
    _finto_alert_manager(monkeypatch, {"email": False, "syslog": True})
    sa.avvisa("critical", "guasto X")
    assert not os.path.exists(sa.LOG) or "NON consegnato" not in open(sa.LOG).read()


# ---- Task 8: sezione centralizzata e cron ----

def test_il_merge_prende_dal_master_e_non_tocca_il_codice():
    from remote_config import merge_remote_defaults
    locale = {"client": {"codcli": "ACME"}, "survey": {"enabled": False, "portale": "http://vecchio"}}
    remoto = {"survey": {"enabled": True, "portale": "https://survey.example/proxmox",
                         "arruolamento": "segreto", "solo": ["ACME"]}}
    unito = merge_remote_defaults(locale, remoto)
    assert unito["survey"]["enabled"] is True
    assert unito["survey"]["portale"] == "https://survey.example/proxmox"
    assert unito["survey"]["solo"] == ["ACME"]
    assert unito["client"]["codcli"] == "ACME"
    assert "codice" not in unito["survey"]


def test_senza_sezione_remota_il_locale_resta_com_e():
    from remote_config import merge_remote_defaults
    locale = {"survey": {"enabled": True, "portale": "https://survey.example/proxmox"}}
    assert merge_remote_defaults(locale, {"syslog": {"host": "x"}})["survey"] == locale["survey"]


def test_il_merge_non_logga_il_segreto_di_arruolamento(caplog):
    import logging
    from remote_config import merge_remote_defaults
    with caplog.at_level(logging.DEBUG):
        merge_remote_defaults({}, {"survey": {"enabled": True, "portale": "https://p",
                                              "arruolamento": "SEGRETO-XYZ"}})
    assert "SEGRETO-XYZ" not in caplog.text


def test_la_riga_di_cron_non_si_duplica():
    from update_scripts import riga_cron_survey, applica_cron
    esistente = "0 6 * * * root /usr/bin/python3 /opt/proxreport/proxmox_core.py\n"
    riga = riga_cron_survey("ACME")
    uno = applica_cron(esistente, riga)
    due = applica_cron(uno, riga)
    assert uno == due
    assert uno.count("survey_audit.py") == 1
    assert "proxmox_core.py" in uno


def test_survey_audit_viaggia_con_gli_script_aggiornati():
    from update_scripts import SCRIPTS_TO_UPDATE
    assert "survey_audit.py" in SCRIPTS_TO_UPDATE


def test_setup_survey_cron_scrive_una_volta_e_sostituisce(tmp_path, capsys):
    from update_scripts import setup_survey_cron
    (tmp_path / "survey_audit.py").write_text("")
    cron = tmp_path / "proxreporter-survey"
    cfg = {"client": {"codcli": "ACME"}}
    assert setup_survey_cron(tmp_path, cfg, cron_file=cron) is True
    primo = cron.read_text()
    assert primo.count("survey_audit.py") == 1
    mtime = cron.stat().st_mtime_ns
    assert setup_survey_cron(tmp_path, cfg, cron_file=cron) is True
    assert cron.read_text() == primo
    assert cron.stat().st_mtime_ns == mtime            # non riscritto
    assert setup_survey_cron(tmp_path, {"client": {"codcli": "BETA"}}, cron_file=cron) is True
    nuovo = cron.read_text()
    assert nuovo != primo and nuovo.count("survey_audit.py") == 1


def test_setup_survey_cron_senza_codcli_non_scrive(tmp_path):
    from update_scripts import setup_survey_cron
    cron = tmp_path / "proxreporter-survey"
    assert setup_survey_cron(tmp_path, {"client": {"codcli": ""}}, cron_file=cron) is False
    assert setup_survey_cron(tmp_path, {}, cron_file=cron) is False
    assert not cron.exists()


# ---- giro di correzioni finale (revisione dell'intero ramo) ----

def _sync(tmp_path, monkeypatch, locale, remoto):
    """Passa da sync_remote_config vera, con il solo scaricamento finto, e
    RILEGGE il file: e' su disco che il difetto si vedeva."""
    import remote_config as rc
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(locale))
    monkeypatch.setattr(rc, "download_remote_config", lambda c, d: remoto)
    ritorno = rc.sync_remote_config(json.loads(cfg_file.read_text()), cfg_file)
    return json.loads(cfg_file.read_text()), ritorno


def test_sync_scrive_su_disco_la_sezione_survey_nuova(tmp_path, monkeypatch):
    """C2: un nodo senza sezione survey la riceveva solo in memoria."""
    su_disco, _ = _sync(tmp_path, monkeypatch, {"client": {"codcli": "ACME"}},
                        {"survey": {"enabled": True, "portale": "https://p/proxmox",
                                    "arruolamento": "s", "solo": ["ACME"]}})
    assert su_disco["survey"]["enabled"] is True
    assert su_disco["survey"]["portale"] == "https://p/proxmox"


def test_sync_scrive_su_disco_lo_spegnimento(tmp_path, monkeypatch):
    """C2: l'interruttore del master non spegneva: enabled restava true."""
    su_disco, _ = _sync(tmp_path, monkeypatch,
                        {"survey": {"enabled": True, "portale": "https://p/proxmox"}},
                        {"survey": {"enabled": False}})
    assert su_disco["survey"]["enabled"] is False
    assert su_disco["survey"]["portale"] == "https://p/proxmox"


def test_il_merge_non_modifica_la_config_locale_ricevuta():
    from remote_config import merge_remote_defaults
    locale = {"survey": {"enabled": True}}
    merge_remote_defaults(locale, {"survey": {"enabled": False}})
    assert locale["survey"]["enabled"] is True


def _strumento_finto(tmp_path, corpo):
    p = tmp_path / "finto.py"
    p.write_text(corpo)
    return str(p)


def test_esegui_rc0_senza_archiviata_non_e_confermato(percorsi):
    """C3: audit-nodo esce 0 anche quando l'invio fallisce."""
    tmp, _ = percorsi
    s = _strumento_finto(tmp, "print('Invio non riuscito: 401')\n")
    rc, coda = sa.esegui(s, "https://p/proxmox", "PXM-AAAA-BBBB", "Acme", "ACME")
    assert rc == 0
    assert sa.invio_confermato(rc, coda) is False


def test_esegui_rc0_con_archiviata_e_confermato(percorsi):
    tmp, _ = percorsi
    s = _strumento_finto(tmp, "print('Archiviata: 0 bloccanti, 2 da valutare.')\n"
                              "print('Il report è consultabile su https://p/r/1')\n")
    rc, coda = sa.esegui(s, "https://p/proxmox", "PXM-AAAA-BBBB", "Acme", "ACME")
    assert sa.invio_confermato(rc, coda) is True
    assert sa.invio_confermato(1, coda) is False


def test_esegui_tiene_solo_la_coda(percorsi):
    tmp, _ = percorsi
    s = _strumento_finto(tmp, "for i in range(5000):\n    print('riga', i)\n")
    rc, coda = sa.esegui(s, "https://p/proxmox", "PXM-AAAA-BBBB", "Acme", "ACME")
    assert len(coda) <= 200 and coda[-1] == "riga 4999"


def test_timeout_uccide_anche_i_sottoprocessi(percorsi):
    """Il collector lancia figli che tengono aperta la pipe: senza killpg
    l'agente resta appeso."""
    import time as _t
    tmp, _ = percorsi
    pidfile = tmp / "figlio.pid"
    s = _strumento_finto(tmp, (
        "import subprocess, sys, time\n"
        "f = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(%r, 'w').write(str(f.pid))\n"
        "time.sleep(60)\n" % str(pidfile)))
    t0 = _t.time()
    rc, coda = sa.esegui(s, "https://p/proxmox", "PXM-AAAA-BBBB", "Acme", "ACME", timeout=2)
    assert rc == 124 and _t.time() - t0 < 20
    pid = int(pidfile.read_text())
    _t.sleep(0.5)
    with pytest.raises(OSError):
        os.kill(pid, 0)


def _giro(percorsi, monkeypatch, esito_esegui):
    tmp, avvisi = percorsi
    _config_portale(tmp, "https://p/proxmox")
    sa.salva_codice("PXM-AAAA-BBBB-CCCC", "ACME", "")
    monkeypatch.setattr(sa, "stato_cluster", lambda: [])
    monkeypatch.setattr(sa, "scarica_strumento", lambda p, c: "/x")
    monkeypatch.setattr(sa, "esegui", lambda *a, **k: esito_esegui)
    return sa.main([]), avvisi


def test_rc0_senza_conferma_non_aggiorna_ultimo_invio_e_allarma(percorsi, monkeypatch):
    rc, avvisi = _giro(percorsi, monkeypatch, (0, ["Invio non riuscito: 401 PXM-AAAA-BBBB-CCCC"]))
    assert rc == 1
    st = sa.leggi_stato()
    assert "ultimo_invio" not in st and st["esito"] == "invio_non_confermato"
    assert avvisi and avvisi[0][0] == "error"
    assert "Invio non riuscito" in avvisi[0][1]
    assert "PXM-AAAA-BBBB-CCCC" not in avvisi[0][1]


def test_rc0_con_conferma_aggiorna_ultimo_invio(percorsi, monkeypatch):
    rc, avvisi = _giro(percorsi, monkeypatch, (0, ["Archiviata: 0 bloccanti, 1 da valutare."]))
    assert rc == 0 and "ultimo_invio" in sa.leggi_stato() and not avvisi


def test_download_401_col_codice_salvato_lo_mette_da_parte(percorsi, monkeypatch):
    """I2: il codice revocato veniva riusato per sempre e la raccolta rifatta."""
    tmp, avvisi = percorsi
    _config_portale(tmp, "https://p/proxmox")
    sa.salva_codice("PXM-AAAA-BBBB-CCCC", "ACME", "")
    monkeypatch.setattr(sa, "stato_cluster", lambda: [])
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(401)))
    chiamato = []
    monkeypatch.setattr(sa, "esegui", lambda *a, **k: chiamato.append(1) or (0, []))
    assert sa.main([]) == 0
    assert chiamato == []
    assert not os.path.exists(sa.FILE_CODICE)
    assert [f for f in os.listdir(str(tmp)) if f.startswith(".survey_codice.revocato-")]
    assert not avvisi


def test_download_401_non_usa_la_cache(percorsi, monkeypatch):
    tmp, _ = percorsi
    os.makedirs(os.path.dirname(sa.CACHE))
    open(sa.CACHE, "w").write(STRUMENTO_OK)
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(401)))
    with pytest.raises(sa.CodiceNonValido):
        sa.scarica_strumento("https://p/proxmox", "PXM-X")


def test_guasto_di_rete_non_e_indirizzo_non_valido(percorsi, monkeypatch):
    import http.client
    tmp, _ = percorsi
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(http.client.RemoteDisconnected("x")))
    assert sa.scarica_strumento("https://p/proxmox", "PXM-X") == ""
    log_ = open(sa.LOG).read()
    assert "download fallito" in log_ and "indirizzo" not in log_
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(http.client.InvalidURL("x")))
    sa.scarica_strumento("https://p/proxmox", "PXM-X")
    assert "indirizzo del portale non valido" in open(sa.LOG).read()


def test_avvisa_decifra_la_password_smtp(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(sa, "INSTALL", str(tmp_path))
    monkeypatch.setattr(sa, "LOG", str(tmp_path / "s.log"))
    (tmp_path / "config.json").write_text(json.dumps({"smtp": {"password": "ENC:abc", "host": "h"}}))
    visto = {}
    m = types.ModuleType("alert_manager")
    m.AlertSeverity = type("S", (), {"ERROR": "e"})
    m.AlertType = type("T", (), {"CUSTOM": "c"})

    class G:
        def __init__(self, cfg):
            visto["cfg"] = cfg

        def send_alert(self, *a, **k):
            return {"email": True}
    m.AlertManager = G
    monkeypatch.setitem(sys.modules, "alert_manager", m)
    hb = types.ModuleType("heartbeat")
    hb.decrypt_password = lambda enc, d: "in-chiaro"
    monkeypatch.setitem(sys.modules, "heartbeat", hb)
    sa.avvisa("error", "x")
    assert visto["cfg"]["smtp"]["password"] == "in-chiaro"


def test_avvisa_senza_decifratura_prosegue_e_lo_scrive(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(sa, "INSTALL", str(tmp_path))
    monkeypatch.setattr(sa, "LOG", str(tmp_path / "s.log"))
    (tmp_path / "config.json").write_text(json.dumps({"smtp": {"password": "ENC:abc"}}))
    _finto_alert_manager(monkeypatch, {"syslog": True})
    hb = types.ModuleType("heartbeat")

    def rotta(enc, d):
        raise RuntimeError("niente chiave")
    hb.decrypt_password = rotta
    monkeypatch.setitem(sys.modules, "heartbeat", hb)
    sa.avvisa("error", "x")
    assert "decifrare" in open(sa.LOG).read()


def test_cron_import_fallito_non_fa_cadere_il_setup(tmp_path, monkeypatch, capsys):
    import update_scripts as us
    (tmp_path / "survey_audit.py").write_text("")

    def rotta(codcli, install_dir=None):
        raise ImportError("no survey_audit")
    monkeypatch.setattr(us, "riga_cron_survey", rotta)
    cron = tmp_path / "cron"
    assert us.setup_survey_cron(tmp_path, {"client": {"codcli": "ACME"}}, cron_file=cron) is False
    assert not cron.exists()
    assert "no survey_audit" in capsys.readouterr().out


def test_cron_senza_script_non_scrive_il_file(tmp_path, capsys):
    import update_scripts as us
    cron = tmp_path / "cron"
    assert us.setup_survey_cron(tmp_path, {"client": {"codcli": "ACME"}}, cron_file=cron) is False
    assert not cron.exists()
    assert "survey_audit.py" in capsys.readouterr().out


def test_cron_usa_install_dir(tmp_path):
    import update_scripts as us
    assert str(tmp_path / "survey_audit.py") in us.riga_cron_survey("ACME", tmp_path)
