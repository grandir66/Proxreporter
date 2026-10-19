#!/usr/bin/env python3
"""Esecutore SSH su una base Linux, separato dal vecchio installer sui nodi.

Il config non viaggia verso DA-Link. Il percorso non usa il merge centrale o il fallback locale
del vecchio CLI.

Accessi (19/10/2026, collaudo VM 900: «deve essere facile»): se nella cartella c'è
`accessi.json` (0600, lasciato dal comando unico di DA-Link) `configure` non chiede niente, lo
consuma e lo cancella. La chiave dell'host si salva al primo collegamento; una chiave cambiata si
rifiuta (`dimentica` la fa riprendere, quando l'host è stato reinstallato). Senza `accessi.json`
resta il percorso interattivo con la chiave pubblica verificata a mano.

Esito: ogni `run` scrive `esito.json` (0600) con un codice per host (`ESITI`, niente testo
libero né segreti) e, se c'è, chiama `dopo-raccolta <esito.json>` (di root, non scrivibile da
altri): è lì che DA-Link riceve lo stato vero della raccolta.
"""
import argparse
import base64
import copy
import datetime as dt
import fcntl
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile

LOG = logging.getLogger('proxreporter.remote')
HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}')
UTENTE = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
# Il contratto con DA-Link (`dalink/catalogo/comando_proxreporter.py`, ESITI): si aggiunge, non
# si rinomina.
ESITI = ('riuscita', 'accesso_rifiutato', 'chiave_host_cambiata', 'host_irraggiungibile',
         'chiave_privata_mancante', 'errore_ssh', 'raccolta_fallita', 'non_configurato')
ATTESA_SSH = 15
# Nella cartella, ma non sono file di un host.
FILE_DI_SERVIZIO = {'template.json', 'accessi.json', 'esito.json'}


def read_private(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('configurazione non privata o non regolare')
    return json.loads(path.read_text())


def write_private(path, data):
    _write_text(path, json.dumps(data, indent=2))


def _write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.remote-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _distruggi(path):
    """Sovrascrive e cancella: il file aveva una password o una chiave privata."""
    path = Path(path)
    try:
        size = path.lstat().st_size
        with path.open('r+b') as stream:
            stream.write(b'\0' * size)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def impronta(key):
    """La forma di `ssh-keygen -l -E sha256`."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return 'SHA256:' + base64.b64encode(digest).decode().rstrip('=')


def voce_host(host, port):
    return host if port == 22 else f'[{host}]:{port}'


def impara_chiave(host, port, known):
    """Fiducia al primo collegamento (Riccardo, 19/10): la chiave che l'host presenta ora."""
    import paramiko

    sock = socket.create_connection((host, port), timeout=ATTESA_SSH)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=ATTESA_SSH)
        key = transport.get_remote_server_key()
    finally:
        transport.close()
    _write_text(known, f'{voce_host(host, port)} {key.get_name()} {key.get_base64()}\n')
    return impronta(key)


def _rinomina_voce(known, host, port):
    """Porta SSH cambiata, stessa chiave: la voce passa da `host` a `[host]:porta` (o viceversa),
    altrimenti paramiko non la trova e la raccolta direbbe «chiave cambiata» (revisione 19/10)."""
    import paramiko

    voce = voce_host(host, port)
    righe = []
    for line in Path(known).read_text().splitlines():
        entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
        if entry is None or entry.key is None:
            continue
        righe.append(f'{voce} {entry.key.get_name()} {entry.key.get_base64()}')
    if righe:
        _write_text(known, '\n'.join(righe) + '\n')


def _impronta_salvata(known):
    import paramiko

    for line in Path(known).read_text().splitlines():
        entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
        if entry is not None and entry.key is not None:
            return impronta(entry.key)
    return None


def _scrivi_host(path, template, host, port, username, password, key_file, known):
    cfg = copy.deepcopy(template['config'])
    cfg['execution_mode'] = 'ssh'
    cfg['configure_notifications'] = False
    cfg['ssh'] = {'enabled': True, 'host': host, 'port': port, 'username': username,
                  'password': password, 'key_file': str(key_file) if key_file else None,
                  'host_key_policy': 'reject', 'known_hosts_file': str(known)}
    cfg['client']['server_identifier'] = host
    cfg.setdefault('system', {})['output_directory'] = str(path.parent / 'reports' / host)
    if template.get('encrypt', False):
        from src.proxreporter.security import SecurityManager
        cfg = SecurityManager(path.parent / '.secret.key').encrypt_config_passwords(cfg)
    write_private(path.parent / (host + '.json'), cfg)


def hosts(template):
    values = template.get('hosts')
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ValueError('servono da 1 a 16 host')
    if any(not isinstance(h, str) or not HOST.fullmatch(h) for h in values):
        raise ValueError('host non valido')
    if len(set(values)) != len(values):
        raise ValueError('host duplicato')
    return values


def configure(template_path):
    path = Path(template_path)
    template = read_private(path)
    if (path.parent / 'accessi.json').exists():
        return configure_accessi(path, template)
    configure_interattivo(path, template)
    return 0


def configure_accessi(path, template):
    """Gli accessi scritti da DA-Link: nessuna domanda, il file si consuma comunque."""
    accessi_path = path.parent / 'accessi.json'
    try:
        accessi = read_private(accessi_path)
        selected = hosts(template)
        dati = accessi.get('host')
        dimentica = accessi.get('dimentica', [])
        if (accessi.get('versione') != 1 or not isinstance(dati, dict)
                or set(dati) != set(selected) or not isinstance(dimentica, list)):
            raise ValueError('accessi.json non corrisponde agli host del template')
        pronti = []
        for host in selected:
            voce = dati[host]
            port, username = voce.get('porta', 22), voce.get('utente', 'root')
            password, chiave = voce.get('password') or '', voce.get('chiave') or ''
            if (not isinstance(port, int) or not 1 <= port <= 65535
                    or not isinstance(username, str) or not UTENTE.fullmatch(username)
                    or not isinstance(password, str) or not isinstance(chiave, str)
                    or bool(password) == bool(chiave)):
                raise ValueError('accesso non valido')
            pronti.append((host, port, username, password, chiave))
        # Validato tutto: da qui si scrive.
        tenuti = set(selected)
        for f in path.parent.iterdir():
            nome = f.name
            if nome in FILE_DI_SERVIZIO or not f.is_file():
                continue
            for suffisso in ('.json', '.known_hosts', '.chiave'):
                if nome.endswith(suffisso) and nome[:-len(suffisso)] not in tenuti:
                    _distruggi(f)
                    print(f'{nome[:-len(suffisso)]}: tolto (non più fra gli host)')
        # Un host spento non ferma l'installazione: la prima raccolta lo dice col suo codice.
        for host, port, username, password, chiave in pronti:
            known = path.parent / (host + '.known_hosts')
            key_file = None
            if chiave:
                key_file = path.parent / (host + '.chiave')
                _write_text(key_file, chiave if chiave.endswith('\n') else chiave + '\n')
            else:
                # Si entra con password: una chiave di prima (magari sostituita) non resta.
                _distruggi(path.parent / (host + '.chiave'))
            _scrivi_host(path, template, host, port, username, password, key_file, known)
            if host in dimentica:
                known.unlink(missing_ok=True)
            if known.exists():
                _rinomina_voce(known, host, port)
                print(f'{host}: chiave dell\'host già nota ({_impronta_salvata(known)})')
                continue
            try:
                fp = impara_chiave(host, port, known)
                print(f'{host}: chiave dell\'host salvata al primo collegamento ({fp})')
            except Exception as exc:
                LOG.error('%s: chiave dell\'host non letta (%s)', host, type(exc).__name__)
                print(f'{host}: host non raggiungibile, chiave non letta '
                      '(si salverà al primo collegamento riuscito)')
        return 0
    finally:
        _distruggi(accessi_path)


def configure_interattivo(path, template):
    import paramiko

    for host in hosts(template):
        target = path.parent / (host + '.json')
        if target.exists():
            read_private(target)
            print(f'{host}: configurazione esistente conservata')
            continue
        print(f'Accesso SSH a {host}; chiave pubblica da verificare con il cliente.')
        username = input('Utente SSH [root]: ').strip() or 'root'
        port = int(input('Porta SSH [22]: ').strip() or '22')
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username) or not 1 <= port <= 65535:
            raise ValueError('utente o porta non validi')
        public_key = input('Chiave pubblica host verificata (tipo e base64): ').strip()
        if len(public_key.split()) != 2:
            raise ValueError('chiave pubblica non valida')
        known = path.parent / (host + '.known_hosts')
        host_entry = host if port == 22 else f'[{host}]:{port}'
        entry = paramiko.hostkeys.HostKeyEntry.from_line(f'{host_entry} {public_key}')
        if entry is None or entry.key is None:
            raise ValueError('chiave pubblica non valida')
        key = input('File chiave privata sulla base (vuoto per password): ').strip()
        password = '' if key else getpass.getpass('Password SSH (non visualizzata): ')
        if key and not Path(key).is_file():
            raise ValueError('chiave privata non trovata')
        if not key and not password:
            raise ValueError('credenziale SSH mancante')
        fd, staged = tempfile.mkstemp(dir=path.parent, prefix='.known-')
        with os.fdopen(fd, 'w') as stream:
            stream.write(f'{host_entry} {public_key}\n')
        os.replace(staged, known)
        _scrivi_host(path, template, host, port, username, password, key or None, known)
        print(f'{host}: configurazione salvata, nessuna connessione eseguita')


class _ChiaveNonCoincide(Exception):
    """L'host presenta una chiave di un tipo non salvato: per noi è una chiave cambiata."""


def prova_ssh(ssh):
    """Collegamento di prova prima del report: (codice, impronta). Solo classi d'errore, mai
    il messaggio (un'eccezione di trasporto può contenere credenziali)."""
    import paramiko

    class Rifiuta(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            raise _ChiaveNonCoincide()

    host, port = ssh['host'], int(ssh.get('port') or 22)
    known = Path(ssh['known_hosts_file'])
    key_file = ssh.get('key_file')
    if key_file and not Path(key_file).is_file():
        return 'chiave_privata_mancante', None
    try:
        if not known.exists():
            impara_chiave(host, port, known)
            print(f'{host}: chiave dell\'host salvata al primo collegamento')
    except Exception as exc:
        LOG.error('%s: chiave dell\'host non letta (%s)', host, type(exc).__name__)
        return 'host_irraggiungibile', None
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(str(known))
        client.set_missing_host_key_policy(Rifiuta())
        client.connect(host, port=port, username=ssh.get('username', 'root'),
                       password=ssh.get('password') or None, key_filename=key_file or None,
                       look_for_keys=False, allow_agent=False, timeout=ATTESA_SSH,
                       banner_timeout=ATTESA_SSH, auth_timeout=ATTESA_SSH)
        return 'riuscita', impronta(client.get_transport().get_remote_server_key())
    except (paramiko.BadHostKeyException, _ChiaveNonCoincide):
        return 'chiave_host_cambiata', None
    except paramiko.AuthenticationException:
        return 'accesso_rifiutato', None
    except (OSError, EOFError):
        return 'host_irraggiungibile', None
    except Exception as exc:
        LOG.error('%s: collegamento SSH fallito (%s)', host, type(exc).__name__)
        return 'errore_ssh', None
    finally:
        client.close()


def _dopo_raccolta(cartella, esito_path):
    gancio = cartella / 'dopo-raccolta'
    try:
        info = gancio.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        LOG.error('dopo-raccolta ignorato: deve essere un file di root non scrivibile da altri')
        return
    try:
        subprocess.run([str(gancio), str(esito_path)], stdin=subprocess.DEVNULL, timeout=120,
                       check=False)
    except Exception as exc:
        LOG.error('dopo-raccolta fallito (%s)', type(exc).__name__)


def run_all(template_path, report=None, prova=None):
    from src.proxreporter.security import SecurityManager

    path = Path(template_path)
    template = read_private(path)
    selected = hosts(template)
    lock_path = path.parent / 'run.lock'
    with lock_path.open('a') as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error('esecuzione remota già in corso')
            return 1
        if report is None:
            from proxmox_core import run_report
            report = run_report
        prova = prova or prova_ssh
        esiti = {}
        for host in selected:
            codice, fp = 'raccolta_fallita', None
            try:
                target = path.parent / (host + '.json')
                if not target.exists():
                    codice = 'non_configurato'
                    raise ValueError('host senza configurazione')
                cfg = read_private(target)
                if template.get('encrypt', False):
                    read_private_key = path.parent / '.secret.key'
                    if not read_private_key.is_file():
                        raise ValueError('chiave di decifratura mancante')
                    cfg = SecurityManager(read_private_key).decrypt_config(cfg)
                ssh = cfg.get('ssh', {})
                if (cfg.get('execution_mode') != 'ssh' or ssh.get('host') != host
                        or ssh.get('host_key_policy') != 'reject'
                        or not ssh.get('known_hosts_file')
                        or cfg.get('configure_notifications') is not False):
                    raise ValueError('configurazione remota incoerente')
                codice, fp = prova(ssh)
                if codice != 'riuscita':
                    LOG.error('%s: %s', host, codice)
                    continue
                codice = 'raccolta_fallita'
                output = Path(cfg['system']['output_directory'])
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
                (output / 'csv').mkdir(exist_ok=True, mode=0o700)
                (output / 'backup').mkdir(exist_ok=True, mode=0o700)
                client = cfg['client']
                report(cfg, client['codcli'], client['nomecliente'], host, output, False)
                codice = 'riuscita'
            except (Exception, SystemExit) as exc:
                # Niente repr/traceback: un'eccezione di trasporto può contenere credenziali.
                LOG.error('%s: raccolta fallita (%s)', host, type(exc).__name__)
            finally:
                esiti[host] = {'esito': codice, 'impronta': fp}
        quando = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        esito_path = path.parent / 'esito.json'
        write_private(esito_path, {'versione': 1, 'quando': quando, 'host': esiti})
        _dopo_raccolta(path.parent, esito_path)
        return int(any(e['esito'] != 'riuscita' for e in esiti.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('azione', choices=('configure', 'run'))
    parser.add_argument('--template', required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        if args.azione == 'configure':
            return configure(args.template)
        return run_all(args.template)
    except Exception as exc:
        LOG.error('configurazione remota rifiutata (%s)', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
