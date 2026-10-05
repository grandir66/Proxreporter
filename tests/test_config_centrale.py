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
