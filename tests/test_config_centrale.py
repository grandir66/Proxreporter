"""La configurazione centrale deve arrivare su disco (config.json del nodo).

Fino al 2026-10-05 sync_remote_config confrontava la sezione fusa con se stessa:
merge_remote_defaults fa una copia superficiale e modifica i dizionari delle
sezioni sul posto, quindi «cambiato» risultava sempre falso e il file non si
salvava mai. Effetto misurato su DA-PX-01: il centro diceva recipients
proxmox@domarc.it da febbraio, il nodo mandava ancora a proxreporter@domarc.it.
"""
import json

import remote_config as rc


def test_destinatari_centrali_salvati_su_disco(tmp_path, monkeypatch):
    locale = {"smtp": {"enabled": True, "host": "esva.domarc.it", "recipients": "proxreporter@domarc.it"},
              "client": {"codcli": "1"}}
    f = tmp_path / "config.json"
    f.write_text(json.dumps(locale))
    remoto = {"smtp": {"host": "esva.domarc.it", "recipients": "proxmox@domarc.it"}}
    monkeypatch.setattr(rc, "download_remote_config", lambda cfg, d: remoto)
    rc.sync_remote_config(json.loads(f.read_text()), f)
    assert json.loads(f.read_text())["smtp"]["recipients"] == "proxmox@domarc.it"


def test_nessuna_differenza_nessuna_scrittura(tmp_path, monkeypatch):
    locale = {"smtp": {"enabled": True, "host": "esva.domarc.it", "recipients": "proxmox@domarc.it"}}
    f = tmp_path / "config.json"
    f.write_text(json.dumps(locale))
    prima = f.stat().st_mtime_ns
    monkeypatch.setattr(rc, "download_remote_config", lambda cfg, d: {"smtp": {"recipients": "proxmox@domarc.it"}})
    monkeypatch.setattr(rc, "save_merged_config", lambda *a: (_ for _ in ()).throw(AssertionError("non doveva salvare")))
    rc.sync_remote_config(json.loads(f.read_text()), f)
    assert f.stat().st_mtime_ns == prima


def test_chiave_survey_tolta_al_centro_sparisce_dal_nodo(tmp_path, monkeypatch):
    """Il 13/10 `solo` è stato TOLTO dal master per accendere la survey a tutti,
    ma la fusione copiava una chiave solo se c'era: ogni nodo ha tenuto il vecchio
    elenco degli 8 pilota salvato in config.json, e per cinque giorni 35 agenti
    su 44 non hanno mai chiamato il portale («non attivo per questo cliente»).
    Trovato il 18/10. `solo` assente al centro = nessuna restrizione sul nodo."""
    import survey_audit as sa
    pilota = ["71734", "70791", "73226", "70931", "71354", "73322", "71686", "71679"]
    locale = {"client": {"codcli": "70008"},
              "survey": {"enabled": True, "portale": "https://p", "arruolamento": "S", "solo": pilota}}
    f = tmp_path / "config.json"
    f.write_text(json.dumps(locale))
    centro = {"survey": {"enabled": True, "portale": "https://p", "arruolamento": "S"}}
    monkeypatch.setattr(rc, "download_remote_config", lambda cfg, d: centro)
    rc.sync_remote_config(json.loads(f.read_text()), f)
    survey = json.loads(f.read_text())["survey"]
    assert "solo" not in survey
    assert sa.attivo_per(survey, "70008")


def test_survey_assente_al_centro_resta_quella_del_nodo(tmp_path, monkeypatch):
    """Se il centro non ha proprio la sezione survey (master vecchio, o letto
    male), il nodo non perde la sua, `solo` compreso."""
    locale = {"survey": {"enabled": True, "portale": "https://p", "solo": ["1"]}}
    f = tmp_path / "config.json"
    f.write_text(json.dumps(locale))
    monkeypatch.setattr(rc, "download_remote_config", lambda cfg, d: {"smtp": {"host": "x"}})
    rc.sync_remote_config(json.loads(f.read_text()), f)
    assert json.loads(f.read_text())["survey"]["solo"] == ["1"]
