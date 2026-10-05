#!/usr/bin/env python3
"""Manda a survey.domarc.it/proxmox la verifica del cluster, una volta a
settimana, dal solo nodo capofila.

NON raccoglie niente da sé: scarica `audit-nodo.py` dal portale e lo esegue.
Lo strumento che raccoglie e quello che analizza devono essere lo stesso, o il
giorno che una soglia cambia il cliente si sente dire due cose diverse.

Solo stdlib: gira sui nodi Proxmox, dove non si installa nulla.
"""
import argparse
import collections
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
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
MAX_DOWNLOAD = 5 * 1024 * 1024
MAX_CODA = 200          # righe d'uscita dello strumento tenute in memoria
_verboso = False
_segreti = []           # valori esatti da non scrivere mai (segreto di arruolamento, codice)
_RE_CODICE = re.compile(r"PXM-[A-Za-z0-9-]+")
_RE_VERSIONE = re.compile(r"^VERSIONE_SCRIPT\s*=\s*['\"]([^'\"]*)['\"]", re.M)


class CodiceNonValido(Exception):
    """Il portale risponde 401 al download: il codice non vale più."""


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


def invio_confermato(rc, coda):
    """audit-nodo esce 0 anche se l'invio fallisce (401/413/rete) o la raccolta
    è vuota: vale come riuscito solo se ha stampato la conferma del portale."""
    return rc == 0 and any(r.lstrip().startswith("Archiviata:") for r in (coda or []))


def pagina_report(coda):
    """L'indirizzo della pagina del report, se lo strumento l'ha stampato."""
    for r in coda or []:
        if "consultabile su " in r:
            return r.split("consultabile su ", 1)[1].strip()
    return ""


def accettabile(testo):
    """Lo strumento scaricato è Python compilabile. Un proxy che risponde 502
    manda una pagina HTML, e senza questo controllo la si esegue."""
    try:
        compile(testo, "audit-nodo.py", "exec")
    except (SyntaxError, ValueError):
        return False
    return True


def versione_strumento(testo):
    """La versione dichiarata da audit-nodo.py (`VERSIONE_SCRIPT = "x"`), ''
    se manca: compilare non basta a dire che quello sia lo strumento."""
    m = _RE_VERSIONE.search(testo or "")
    return m.group(1) if m else ""


# ------------------------------------------------------------------- log

def ripulisci(testo):
    """Toglie da un testo il codice del portale e il segreto di arruolamento:
    stanno negli URL e nelle eccezioni, e il log finisce anche in una mail."""
    testo = _RE_CODICE.sub("PXM-***", str(testo))
    for v in _segreti:
        if v:
            testo = testo.replace(v, "***")
    return testo


def log(messaggio):
    """Una riga con l'ora. Scrive nel file di log; con -v anche a schermo.
    Se il file non è scrivibile lo dice su stderr: il log non tace mai."""
    riga = "%s survey_audit: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), ripulisci(messaggio))
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
    """`pvesh get /cluster/status`. Tre esiti: la lista (un nodo singolo ne ha
    una con la sola sua voce), oppure None se pvesh non risponde: non so."""
    try:
        r = subprocess.run(["pvesh", "get", "/cluster/status", "--output-format", "json"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        log("pvesh non eseguibile (%s)" % e)
        return None
    if r.returncode != 0:
        log("pvesh uscito con %s (%s)"
            % (r.returncode, (r.stderr or "").strip()[:200]))
        return None
    try:
        dati = json.loads(r.stdout)
    except ValueError:
        log("pvesh ha risposto con JSON non valido")
        return None
    return dati if isinstance(dati, list) else None


def _scrivi_atomico(percorso, testo, modo=0o600):
    cartella = os.path.dirname(percorso)
    if cartella:
        os.makedirs(cartella, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cartella or ".", prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(testo)
        os.chmod(tmp, modo)
        os.replace(tmp, percorso)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    try:
        req = urllib.request.Request(url, data=corpo, method="POST", headers={
            "Content-Type": "application/json", "X-Arruolamento": segreto or ""})
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
    except (ValueError, http.client.InvalidURL):
        log("arruolamento: indirizzo del portale non valido nella configurazione")
        return ""
    except http.client.HTTPException as e:
        log("arruolamento: guasto di rete (%s): riprovo alla prossima esecuzione" % type(e).__name__)
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
            dati = r.read(MAX_DOWNLOAD + 1)
        if len(dati) > MAX_DOWNLOAD:
            motivo = "il file scaricato supera %d MB" % (MAX_DOWNLOAD // (1024 * 1024))
        else:
            testo = dati.decode("utf-8")
            versione = versione_strumento(testo)
            if accettabile(testo) and versione:
                _scrivi_atomico(CACHE, testo, 0o700)
                log("strumento scaricato, versione %s" % versione)
                return CACHE
            motivo = "il file scaricato non è lo strumento (non compila o senza VERSIONE_SCRIPT)"
    except (ValueError, http.client.InvalidURL) as e:
        if isinstance(e, UnicodeDecodeError):
            motivo = "il file scaricato non è testo valido"
        else:
            motivo = "indirizzo del portale non valido nella configurazione"
    except http.client.HTTPException as e:
        motivo = "download fallito (guasto di rete: %s)" % type(e).__name__
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise CodiceNonValido()
        motivo = "download rifiutato (%s)" % e.code
    except (urllib.error.URLError, OSError) as e:
        motivo = "download fallito (%s)" % e
    if os.path.exists(CACHE):
        log("%s: uso la copia in cache" % motivo)
        return CACHE
    log("%s e nessuna copia in cache" % motivo)
    return ""


def esegui(strumento, portale, codice, cliente, codcli, timeout=2400):
    """Lancia lo strumento in una cartella temporanea, rimossa sempre.
    Ritorna (codice di uscita, ultime righe dell'uscita). L'uscita va su un file
    e ne resta in memoria solo la coda; lo strumento gira in un gruppo di
    processi suo, ucciso per intero al timeout (i figli del collector non
    devono restare appesi)."""
    tmp = tempfile.mkdtemp(prefix="survey-")
    try:
        cmd = [sys.executable, strumento, "--json", tmp + "/raccolta.json", "--output", tmp,
               "--invia", portale, "--codice-portale", codice, "--cliente", cliente,
               "--codice", codcli, "--no-color", "--breve"]
        percorso_uscita = os.path.join(tmp, "uscita.txt")
        with open(percorso_uscita, "w") as uscita:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=uscita,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log("strumento interrotto dopo %s secondi" % timeout)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
                rc = 124
        coda = collections.deque(maxlen=MAX_CODA)
        with open(percorso_uscita, errors="replace") as f:
            for riga in f:
                coda.append(riga.rstrip("\n"))
        coda = list(coda)
        for riga in coda[-15:]:
            log("  | " + riga)
        return rc, coda
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _config_decifrata():
    """Il config con i valori `ENC:` in chiaro, come li vuole AlertManager (la
    password SMTP cifrata farebbe fallire la mail). Riusa la funzione
    dell'heartbeat; se non riesce si prosegue col config com'è e lo si scrive:
    resta il syslog."""
    cfg = leggi_config()
    try:
        import heartbeat
        from pathlib import Path

        def _apri(v):
            if isinstance(v, dict):
                return {k: _apri(x) for k, x in v.items()}
            if isinstance(v, str) and v.startswith("ENC:"):
                chiaro = heartbeat.decrypt_password(v, Path(INSTALL))
                if not chiaro:
                    raise ValueError("valore cifrato non decifrabile")
                return chiaro
            return v
        return _apri(cfg)
    except Exception as e:  # noqa: BLE001 - senza decifratura si va avanti col syslog
        log("config non decifrato: impossibile decifrare (%s: %s), l'allarme può arrivare solo dal syslog"
            % (type(e).__name__, e))
        return cfg


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
        gestore = alert_manager.AlertManager(_config_decifrata())
        testo = ripulisci(testo)
        esito = gestore.send_alert(alert_manager.AlertType.CUSTOM, sev,
                                   "Survey: verifica del cluster", testo, force_immediate=True)
        if not esito or not any(esito.values()):
            log("allarme NON consegnato: %s %s" % (gravita, testo))
    except Exception as e:  # noqa: BLE001 - l'allarme non deve mai far cadere l'agente
        log("allarme NON inviato (%s: %s): %s" % (type(e).__name__, e, ripulisci(testo)))


# ------------------------------------------------------------------ main

def _esegui_agente():
    cfg = leggi_config()
    client = cfg.get("client") or {}
    survey = cfg.get("survey") or {}
    _segreti[:] = [str(survey.get("arruolamento") or "").strip()]
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
    if stato is None:
        log("stato del cluster sconosciuto, non invio")
        avvisa("warning", "survey: stato del cluster sconosciuto (pvesh non risponde), nessun invio da %s" % nodo)
        return 0
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
    if codice:
        _segreti.append(codice)
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
        _segreti.append(codice)
        salva_codice(codice, codcli, cluster)
        log("arruolato: codice salvato")

    try:
        strumento = scarica_strumento(portale, codice)
    except CodiceNonValido:
        # Revocato dopo il salvataggio: non si rifà la raccolta di tutto il
        # cluster per un invio che prenderebbe 401. Il file si tiene da parte;
        # al giro dopo l'arruolamento risponde 409 e l'agente tace 24 ore.
        revocato = "%s.revocato-%s" % (FILE_CODICE, time.strftime("%Y%m%d"))
        try:
            os.replace(FILE_CODICE, revocato)
        except OSError as e:
            log("codice non valido (401) ma non riesco a metterlo da parte: %s" % e)
            return 0
        log("il portale rifiuta il codice salvato (401): messo da parte in %s, esco" % revocato)
        return 0
    if not strumento:
        scrivi_stato(ultimo_tentativo=adesso, esito="strumento_assente")
        avvisa("critical", "survey: strumento audit-nodo.py non scaricabile e nessuna copia in cache (%s)" % codcli)
        return 1

    rc, coda = esegui(strumento, portale, codice, cliente, codcli)
    if invio_confermato(rc, coda):
        scrivi_stato(ultimo_invio=time.time(), ultimo_tentativo=adesso, esito="ok")
        pagina = pagina_report(coda)
        log("verifica inviata al portale" + (" (report: %s)" % pagina if pagina else ""))
        return 0
    if rc == 0:
        esito, quadro = "invio_non_confermato", "invio non confermato dal portale"
    else:
        esito, quadro = "errore_%s" % rc, "uscita %s" % rc
    scrivi_stato(ultimo_tentativo=adesso, esito=esito)
    log("strumento: %s" % quadro)
    fine = "\n".join(ripulisci(r) for r in coda[-20:])
    avvisa("error", "survey: la verifica del cluster non è stata inviata (%s, %s)\n%s"
           % (quadro, codcli, fine))
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
        msg = ripulisci("%s: %s" % (type(e).__name__, e))
        log("errore imprevisto: " + msg)
        avvisa("critical", "survey: errore imprevisto dell'agente (%s)" % msg)
        return 1


if __name__ == "__main__":
    sys.exit(main())
