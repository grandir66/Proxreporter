#!/usr/bin/env python3
"""Esecutore SSH su una base Linux, separato dal vecchio installer sui nodi.

I segreti sono acquisiti sul terminale della base. Il config non viaggia verso DA-Link.
Il percorso non usa il merge centrale o il fallback locale del vecchio CLI.
"""
import argparse
import copy
import fcntl
import getpass
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile

LOG = logging.getLogger('proxreporter.remote')
HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}')


def read_private(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('configurazione non privata o non regolare')
    return json.loads(path.read_text())


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.remote-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


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
    import paramiko
    from src.proxreporter.security import SecurityManager

    path = Path(template_path)
    template = read_private(path)
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
        cfg = copy.deepcopy(template['config'])
        cfg['execution_mode'] = 'ssh'
        cfg['configure_notifications'] = False
        cfg['ssh'] = {'enabled': True, 'host': host, 'port': port, 'username': username,
                      'password': password, 'key_file': key or None,
                      'host_key_policy': 'reject', 'known_hosts_file': str(known)}
        cfg['client']['server_identifier'] = host
        cfg.setdefault('system', {})['output_directory'] = str(path.parent / 'reports' / host)
        if template.get('encrypt', False):
            cfg = SecurityManager(path.parent / '.secret.key').encrypt_config_passwords(cfg)
        write_private(target, cfg)
        print(f'{host}: configurazione salvata, nessuna connessione eseguita')


def run_all(template_path, report=None):
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
        failed = False
        for host in selected:
            try:
                cfg = read_private(path.parent / (host + '.json'))
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
                output = Path(cfg['system']['output_directory'])
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
                (output / 'csv').mkdir(exist_ok=True, mode=0o700)
                (output / 'backup').mkdir(exist_ok=True, mode=0o700)
                client = cfg['client']
                report(cfg, client['codcli'], client['nomecliente'], host, output, False)
            except (Exception, SystemExit) as exc:
                # Niente repr/traceback: un'eccezione di trasporto può contenere credenziali.
                LOG.error('%s: raccolta fallita (%s)', host, type(exc).__name__)
                failed = True
        return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('azione', choices=('configure', 'run'))
    parser.add_argument('--template', required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        if args.azione == 'configure':
            configure(args.template)
            return 0
        return run_all(args.template)
    except Exception as exc:
        LOG.error('configurazione remota rifiutata (%s)', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
