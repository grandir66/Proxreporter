#!/usr/bin/env python3
"""Manda a survey.domarc.it/proxmox la verifica del cluster, una volta a
settimana, dal solo nodo capofila.

NON raccoglie niente da sé: scarica `audit-nodo.py` dal portale e lo esegue.
Lo strumento che raccoglie e quello che analizza devono essere lo stesso, o il
giorno che una soglia cambia il cliente si sente dire due cose diverse.

Solo stdlib: gira sui nodi Proxmox, dove non si installa nulla.
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

INSTALL = "/opt/proxreport"
FILE_CODICE = INSTALL + "/.survey_codice"      # NON in config.json: viene riscritto
FILE_STATO = INSTALL + "/.survey_stato.json"
CACHE = INSTALL + "/cache/audit-nodo.py"
LOG = "/var/log/proxreporter/survey.log"

VERSIONE_AGENTE = "1.0"
ATTESA_REVOCATO = 24 * 3600
_verboso = False


# ---------------------------------------------------------------- decisioni

def attivo_per(survey, codcli):
    """`enabled` più l'elenco `solo`: il master config è uno per tutta la
    flotta, e il pilota si accende mettendo un codcli nell'elenco."""
    if not (survey or {}).get("enabled"):
        return False
    solo = [str(x).strip().upper() for x in (survey.get("solo") or []) if str(x).strip()]
    return (not solo) or (codcli or "").strip().upper() in solo


def pianificazione(codcli):
    """Un istante stabile nella settimana, ricavato dal codcli. Cinquanta
    cluster non devono arrivare tutti alle 6:00 di lunedì: ogni arrivo fa
    rianalizzare il portale. Stabile perché una scansione che manca si nota
    solo se si sa quando doveva arrivare."""
    n = int(hashlib.sha256((codcli or "").strip().upper().encode()).hexdigest()[:8], 16)
    return (n % 60, 2 + (n // 60) % 5, (n // 3600) % 7)      # fra le 2 e le 6


def tocca_a_me(stato_cluster, hostname):
    """Invia il primo in ordine alfabetico fra i nodi ONLINE. Fuori cluster,
    invia sempre. Niente flag su un nodo: un flag sul nodo che viene spento o
    reinstallato zittisce l'invio per sempre e non se ne accorge nessuno."""
    online = sorted(n.get("name") for n in (stato_cluster or [])
                    if n.get("type") == "node" and n.get("online") and n.get("name"))
    return (not online) or online[0] == hostname


def troppo_presto(stato, adesso, giorni=3):
    """Un invio riuscito da meno di tre giorni ferma l'esecuzione: protegge da
    un cron rimasto doppio e dalla settimana in cui due nodi si credono
    entrambi capofila."""
    ultimo = (stato or {}).get("ultimo_invio")
    return bool(ultimo) and (adesso - float(ultimo)) < giorni * 86400


def accettabile(testo):
    """Lo strumento scaricato è Python compilabile. Un proxy che risponde 502
    manda una pagina HTML, e senza questo controllo la si esegue."""
    try:
        compile(testo, "audit-nodo.py", "exec")
    except (SyntaxError, ValueError):
        return False
    return True


# ------------------------------------------------------------------- log

def log(messaggio):
    """Una riga con l'ora. Scrive nel file di log; con -v anche a schermo.
    Se il file non è scrivibile lo dice su stderr: il log non tace mai."""
    riga = "%s survey_audit: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), messaggio)
    try:
        cartella = os.path.dirname(LOG)
        if cartella:
            os.makedirs(cartella, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(riga + "\n")
    except OSError as e:
        sys.stderr.write("%s [log non scrivibile: %s]\n" % (riga, e))
        return
    if _verboso:
        print(riga)


# ------------------------------------------------------------ file e stato

def leggi_config(percorso=None):
    """config.json di Prox Reporter; {} se manca o è illeggibile (lo dice il log)."""
    percorso = percorso or (INSTALL + "/config.json")
    try:
        with open(percorso) as f:
            dati = json.load(f)
        return dati if isinstance(dati, dict) else {}
    except (OSError, ValueError) as e:
        log("config.json non leggibile (%s): %s" % (percorso, e))
        return {}


def stato_cluster():
    """`pvesh get /cluster/status`; [] fuori cluster o se pvesh non risponde
    (in quel caso lo scrive nel log, perché [] vuol dire «tocca a me»)."""
    try:
        r = subprocess.run(["pvesh", "get", "/cluster/status", "--output-format", "json"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        log("pvesh non eseguibile (%s): trattato come nodo singolo" % e)
        return []
    if r.returncode != 0:
        log("pvesh uscito con %s: trattato come nodo singolo (%s)"
            % (r.returncode, (r.stderr or "").strip()[:200]))
        return []
    try:
        dati = json.loads(r.stdout)
    except ValueError:
        log("pvesh ha risposto con JSON non valido: trattato come nodo singolo")
        return []
    return dati if isinstance(dati, list) else []


def _scrivi_atomico(percorso, testo, modo=0o600):
    cartella = os.path.dirname(percorso)
    if cartella:
        os.makedirs(cartella, exist_ok=True)
    tmp = percorso + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modo)
    with os.fdopen(fd, "w") as f:
        f.write(testo)
    os.chmod(tmp, modo)
    os.replace(tmp, percorso)


def codice_salvato():
    try:
        with open(FILE_CODICE) as f:
            return str(json.load(f).get("codice") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def salva_codice(codice, codcli, cluster):
    dati = {"codice": codice, "codcli": codcli, "cluster": cluster,
            "emesso_il": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _scrivi_atomico(FILE_CODICE, json.dumps(dati), 0o600)


def leggi_stato():
    try:
        with open(FILE_STATO) as f:
            dati = json.load(f)
        return dati if isinstance(dati, dict) else {}
    except (OSError, ValueError):
        return {}


def scrivi_stato(**campi):
    dati = leggi_stato()
    dati.update(campi)
    _scrivi_atomico(FILE_STATO, json.dumps(dati), 0o600)


# ------------------------------------------------------------------ rete

def arruola(portale, segreto, codcli, cliente, cluster, nodo):
    """Chiede il codice al portale. Ritorna il codice, o '' se non c'è
    (attesa, revoca, portale non pronto, rete giù): mai un'eccezione."""
    url = portale.rstrip("/") + "/api/agente/arruola"
    corpo = json.dumps({"codcli": codcli, "cliente": cliente, "cluster": cluster,
                        "nodo": nodo, "versione_agente": VERSIONE_AGENTE}).encode()
    req = urllib.request.Request(url, data=corpo, method="POST", headers={
        "Content-Type": "application/json", "X-Arruolamento": segreto or ""})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            stato = r.getcode()
            testo = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # Gli errori sono HTML: si decide dal codice di stato, mai dal corpo.
        if e.code == 409:
            scrivi_stato(revocato_il=time.time())
            log("arruolamento: codice revocato dal portale (409), nessun tentativo per 24 ore")
        elif e.code == 403:
            log("arruolamento rifiutato (403): segreto assente o sbagliato")
            avvisa("warning", "survey: il portale rifiuta il segreto di arruolamento (403)")
        elif e.code == 404:
            log("arruolamento non ancora disponibile sul portale (404): riprovo alla prossima esecuzione")
        else:
            log("arruolamento: risposta inattesa dal portale (%s)" % e.code)
        return ""
    except (urllib.error.URLError, OSError) as e:
        log("arruolamento: portale non raggiungibile (%s): riprovo alla prossima esecuzione" % e)
        return ""
    if stato == 202:
        log("arruolamento registrato: in attesa di approvazione sul portale")
        return ""
    if stato == 200:
        try:
            codice = str(json.loads(testo).get("codice") or "").strip()
        except (ValueError, AttributeError):
            codice = ""
        if codice:
            return codice
        log("arruolamento: 200 senza codice nella risposta")
        return ""
    log("arruolamento: risposta inattesa dal portale (%s)" % stato)
    return ""


def scarica_strumento(portale, codice):
    """Scarica audit-nodo.py e lo accetta solo se compila; altrimenti tiene la
    copia in cache. Ritorna il percorso, '' se non c'è né rete né cache."""
    url = "%s/script/audit-nodo.py?codice=%s" % (portale.rstrip("/"), urllib.parse.quote(codice, safe=""))
    motivo = ""
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            testo = r.read().decode("utf-8")
        if accettabile(testo):
            _scrivi_atomico(CACHE, testo, 0o700)
            return CACHE
        motivo = "il file scaricato non è Python valido (troncato o pagina di errore)"
    except urllib.error.HTTPError as e:
        motivo = "download rifiutato (%s)" % e.code
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as e:
        motivo = "download fallito (%s)" % e
    if os.path.exists(CACHE):
        log("%s: uso la copia in cache" % motivo)
        return CACHE
    log("%s e nessuna copia in cache" % motivo)
    return ""


def esegui(strumento, portale, codice, cliente, codcli):
    """Lancia lo strumento in una cartella temporanea, rimossa sempre."""
    tmp = tempfile.mkdtemp(prefix="survey-")
    try:
        cmd = [sys.executable, strumento, "--json", tmp + "/raccolta.json", "--output", tmp,
               "--invia", portale, "--codice-portale", codice, "--cliente", cliente,
               "--codice", codcli, "--no-color", "--breve"]
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, universal_newlines=True,
                               timeout=2400)
        except subprocess.TimeoutExpired:
            log("strumento interrotto dopo 2400 secondi")
            return 124
        coda = (r.stdout or "").strip().splitlines()[-15:]
        for riga in coda:
            log("  | " + riga)
        return r.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def avvisa(gravita, testo):
    """Passa da alert_manager, la strada che già arriva a noi. Se manca o
    fallisce lo scrive nel log: non solleva e non tace."""
    try:
        aqui = os.path.dirname(os.path.abspath(__file__))
        for p in (aqui, INSTALL):
            if p not in sys.path:
                sys.path.insert(0, p)
        import alert_manager
        sev = getattr(alert_manager.AlertSeverity, str(gravita).upper(),
                      alert_manager.AlertSeverity.ERROR)
        gestore = alert_manager.AlertManager(leggi_config())
        gestore.send_alert(alert_manager.AlertType.CUSTOM, sev,
                           "Survey: verifica del cluster", testo, force_immediate=True)
    except Exception as e:  # noqa: BLE001 - l'allarme non deve mai far cadere l'agente
        log("allarme NON inviato (%s: %s): %s" % (type(e).__name__, e, testo))


# ------------------------------------------------------------------ main

def _esegui_agente():
    cfg = leggi_config()
    client = cfg.get("client") or {}
    survey = cfg.get("survey") or {}
    codcli = str(client.get("codcli") or "").strip()
    cliente = str(client.get("nomecliente") or "").strip()

    if not attivo_per(survey, codcli):
        log("non attivo per questo cliente (enabled falso o codcli fuori dall'elenco `solo`): esco")
        return 0
    portale = str(survey.get("portale") or "").strip()
    if not portale:
        log("sezione survey senza `portale`: esco")
        return 0

    nodo = socket.gethostname().split(".")[0]
    stato = stato_cluster()
    if not tocca_a_me(stato, nodo):
        log("non sono il nodo capofila (%s): esco" % nodo)
        return 0
    cluster = next((n.get("name") or "" for n in stato if n.get("type") == "cluster"), "")

    adesso = time.time()
    st = leggi_stato()
    if troppo_presto(st, adesso):
        log("ultimo invio riuscito da meno di 3 giorni: esco")
        return 0

    codice = codice_salvato()
    if not codice:
        rev = st.get("revocato_il")
        if rev and adesso - float(rev) < ATTESA_REVOCATO:
            log("codice revocato da meno di 24 ore: non riprovo")
            return 0
        codice = arruola(portale, str(survey.get("arruolamento") or ""),
                         codcli, cliente, cluster, nodo)
        if not codice:
            log("nessun codice ottenuto (attesa, revoca o portale non pronto): esco senza guasto")
            return 0
        salva_codice(codice, codcli, cluster)
        log("arruolato: codice salvato")

    strumento = scarica_strumento(portale, codice)
    if not strumento:
        scrivi_stato(ultimo_tentativo=adesso, esito="strumento_assente")
        avvisa("critical", "survey: strumento audit-nodo.py non scaricabile e nessuna copia in cache (%s)" % codcli)
        return 1

    rc = esegui(strumento, portale, codice, cliente, codcli)
    if rc == 0:
        scrivi_stato(ultimo_invio=time.time(), ultimo_tentativo=adesso, esito="ok")
        log("verifica inviata al portale")
        return 0
    scrivi_stato(ultimo_tentativo=adesso, esito="errore_%s" % rc)
    log("strumento uscito con codice %s" % rc)
    avvisa("error", "survey: la verifica del cluster non è stata inviata (uscita %s, %s)" % (rc, codcli))
    return 1


def main(argv=None):
    global _verboso
    ap = argparse.ArgumentParser(description="Invia la verifica del cluster al portale survey")
    ap.add_argument("-v", "--verbose", action="store_true", help="scrive il log anche a schermo")
    args = ap.parse_args(argv)
    _verboso = args.verbose
    try:
        return _esegui_agente()
    except Exception as e:  # noqa: BLE001 - unico except largo: verso cron non passa nulla, ma scrive
        log("errore imprevisto: %s: %s" % (type(e).__name__, e))
        avvisa("critical", "survey: errore imprevisto dell'agente (%s: %s)" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
