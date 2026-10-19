"""Accessi scritti da DA-Link, fiducia alla prima chiave ed esito per host (Riccardo, 19/10/2026).

Il collaudo del 19/10 sulla VM 900: tre blocchi da incollare in ordine, impronta da leggere a mano,
un cron che «completa» da solo. Ora DA-Link lascia `accessi.json` (0600) e `configure` lo consuma
senza domande; la prima chiave vista si salva, una chiave cambiata si rifiuta; ogni raccolta
scrive `esito.json` in codici e chiama `dopo-raccolta`. Le prove parlano con un vero server SSH
(paramiko) su 127.0.0.1: chiave cambiata, password rifiutata e host spento sono quelli veri.
"""
import json
import os
import socket
import stat
import threading
from unittest.mock import Mock

import paramiko
import pytest

import remote_runner as rr

PASSWORD = 'Giusta-Non-Stampare'


class _Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        ok = username == 'root' and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'


class ServerSSH:
    """Un server SSH vero su 127.0.0.1; `chiave` si cambia per simulare un host reinstallato."""

    def __init__(self):
        self.chiave = paramiko.RSAKey.generate(1024)
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.porta = self.sock.getsockname()[1]
        self.fermo = False
        threading.Thread(target=self._accetta, daemon=True).start()

    def _accetta(self):
        while not self.fermo:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.chiave)
            try:
                t.start_server(server=_Server())
            except Exception:
                t.close()

    def chiudi(self):
        self.fermo = True
        self.sock.close()


@pytest.fixture
def server():
    s = ServerSSH()
    yield s
    s.chiudi()


def _base(tmp_path, porta, *, password=PASSWORD, encrypt=False, hosts=('127.0.0.1',)):
    template = {'hosts': list(hosts), 'encrypt': encrypt,
                'config': {'client': {'codcli': '70791', 'nomecliente': 'Domarc'}}}
    t = tmp_path / 'template.json'
    rr.write_private(t, template)
    os.chmod(t, 0o600)
    accessi = {'versione': 1, 'host': {h: {'porta': porta, 'utente': 'root', 'password': password}
                                      for h in hosts}}
    a = tmp_path / 'accessi.json'
    rr.write_private(a, accessi)
    os.chmod(a, 0o600)
    return t, a


def _esito(tmp_path):
    return json.loads((tmp_path / 'esito.json').read_text())['host']


def test_accessi_di_dalink_senza_domande_chiave_imparata_e_file_consumato(tmp_path, server, capsys):
    t, a = _base(tmp_path, server.porta, encrypt=True)
    assert rr.configure(t) == 0
    assert not a.exists(), 'accessi.json con la password non deve restare sulla base'
    known = (tmp_path / '127.0.0.1.known_hosts').read_text().split()
    assert known == [f'[127.0.0.1]:{server.porta}', 'ssh-rsa', server.chiave.get_base64()]
    cfg = rr.read_private(tmp_path / '127.0.0.1.json')
    assert cfg['ssh']['port'] == server.porta and cfg['ssh']['host_key_policy'] == 'reject'
    assert cfg['ssh']['password'].startswith('ENC:')
    out = capsys.readouterr().out
    assert 'salvata al primo collegamento' in out and 'SHA256:' in out
    assert PASSWORD not in out and PASSWORD not in json.dumps(cfg)


def test_raccolta_riuscita_scrive_esito_con_impronta_e_chiama_dopo_raccolta(tmp_path, server):
    t, _ = _base(tmp_path, server.porta)
    rr.configure(t)
    gancio = tmp_path / 'dopo-raccolta'
    copia = tmp_path / 'ricevuto.json'
    gancio.write_text(f'#!/bin/sh\ncp "$1" {copia}\n')
    gancio.chmod(0o700)
    report = Mock()
    assert rr.run_all(t, report=report) == 0
    assert report.call_count == 1
    e = _esito(tmp_path)['127.0.0.1']
    assert e['esito'] == 'riuscita' and e['impronta'].startswith('SHA256:')
    assert json.loads(copia.read_text())['host']['127.0.0.1']['esito'] == 'riuscita'
    assert (tmp_path / 'esito.json').stat().st_mode & 0o777 == 0o600


def test_chiave_cambiata_rifiutata_e_report_non_parte(tmp_path, server):
    t, _ = _base(tmp_path, server.porta)
    rr.configure(t)
    server.chiave = paramiko.RSAKey.generate(1024)   # host reinstallato (o qualcuno in mezzo)
    report = Mock()
    assert rr.run_all(t, report=report) == 1
    assert report.call_count == 0
    assert _esito(tmp_path)['127.0.0.1']['esito'] == 'chiave_host_cambiata'


def test_password_rifiutata_ha_il_suo_codice_e_non_finisce_nei_log(tmp_path, server, caplog):
    t, _ = _base(tmp_path, server.porta, password='Sbagliata-Non-Stampare')
    rr.configure(t)
    assert rr.run_all(t, report=Mock()) == 1
    assert _esito(tmp_path)['127.0.0.1']['esito'] == 'accesso_rifiutato'
    assert 'Sbagliata-Non-Stampare' not in caplog.text


def test_host_spento_irraggiungibile_e_secondo_host_raccolto(tmp_path, server):
    spento = socket.socket()
    spento.bind(('127.0.0.1', 0))
    porta_spenta = spento.getsockname()[1]
    spento.close()
    t, a = _base(tmp_path, server.porta, hosts=('127.0.0.1', 'localhost'))
    acc = rr.read_private(a)
    acc['host']['localhost']['porta'] = porta_spenta
    rr.write_private(a, acc)
    assert rr.configure(t) == 0          # host spento: non ferma l'installazione
    report = Mock()
    assert rr.run_all(t, report=report) == 1
    e = _esito(tmp_path)
    assert e['127.0.0.1']['esito'] == 'riuscita'
    assert e['localhost']['esito'] == 'host_irraggiungibile'
    assert report.call_count == 1


def test_dimentica_riprende_la_chiave_nuova_dopo_la_reinstallazione(tmp_path, server):
    t, a = _base(tmp_path, server.porta)
    rr.configure(t)
    server.chiave = paramiko.RSAKey.generate(1024)
    _, a = _base(tmp_path, server.porta)
    acc = rr.read_private(a)
    acc['dimentica'] = ['127.0.0.1']
    rr.write_private(a, acc)
    assert rr.configure(t) == 0
    assert rr.run_all(t, report=Mock()) == 0
    assert _esito(tmp_path)['127.0.0.1']['esito'] == 'riuscita'


def test_senza_dimentica_la_chiave_salvata_resta_quella(tmp_path, server):
    t, _ = _base(tmp_path, server.porta)
    rr.configure(t)
    prima = (tmp_path / '127.0.0.1.known_hosts').read_text()
    server.chiave = paramiko.RSAKey.generate(1024)
    _base(tmp_path, server.porta)                  # accessi riscritti, nessun «dimentica»
    rr.configure(t)
    assert (tmp_path / '127.0.0.1.known_hosts').read_text() == prima


def test_host_tolti_dalla_preparazione_spariscono_con_i_loro_accessi(tmp_path, server):
    t, _ = _base(tmp_path, server.porta)
    rr.configure(t)
    for nome in ('vecchio.host.it.json', 'vecchio.host.it.known_hosts', 'vecchio.host.it.chiave'):
        rr.write_private(tmp_path / nome, {})
    _base(tmp_path, server.porta)
    rr.configure(t)
    rimasti = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert not [n for n in rimasti if n.startswith('vecchio.')]
    assert 'template.json' in rimasti and '127.0.0.1.json' in rimasti


def test_accessi_incompleti_rifiutati_senza_scrivere(tmp_path, server):
    t, a = _base(tmp_path, server.porta, hosts=('127.0.0.1', 'localhost'))
    acc = rr.read_private(a)
    del acc['host']['localhost']
    rr.write_private(a, acc)
    with pytest.raises(ValueError):
        rr.configure(t)
    assert not (tmp_path / '127.0.0.1.json').exists()
    assert not a.exists(), 'anche rifiutato, il file con la password non resta'


def test_accesso_con_chiave_privata(tmp_path, server):
    t, a = _base(tmp_path, server.porta)
    acc = rr.read_private(a)
    acc['host']['127.0.0.1'] = {'porta': server.porta, 'utente': 'root',
                                'chiave': '-----BEGIN OPENSSH PRIVATE KEY-----\nfinta\n'
                                          '-----END OPENSSH PRIVATE KEY-----\n'}
    rr.write_private(a, acc)
    rr.configure(t)
    chiave = tmp_path / '127.0.0.1.chiave'
    assert stat.S_IMODE(chiave.stat().st_mode) == 0o600
    cfg = rr.read_private(tmp_path / '127.0.0.1.json')
    assert cfg['ssh']['key_file'] == str(chiave) and cfg['ssh']['password'] == ''


def test_dopo_raccolta_non_privato_non_si_esegue(tmp_path, server):
    t, _ = _base(tmp_path, server.porta)
    rr.configure(t)
    segno = tmp_path / 'eseguito'
    gancio = tmp_path / 'dopo-raccolta'
    gancio.write_text(f'#!/bin/sh\ntouch {segno}\n')
    gancio.chmod(0o777)
    rr.run_all(t, report=Mock())
    assert not segno.exists()


def test_host_senza_configurazione_ha_il_suo_codice(tmp_path):
    template = {'hosts': ['pve.cliente.it'], 'config': {'client': {'codcli': '1', 'nomecliente': 'X'}}}
    t = tmp_path / 'template.json'
    rr.write_private(t, template)
    os.chmod(t, 0o600)
    assert rr.run_all(t, report=Mock()) == 1
    assert _esito(tmp_path)['pve.cliente.it']['esito'] == 'non_configurato'


def test_codici_esito_sono_quelli_del_contratto():
    assert set(rr.ESITI) == {'riuscita', 'accesso_rifiutato', 'chiave_host_cambiata',
                             'host_irraggiungibile', 'chiave_privata_mancante', 'errore_ssh',
                             'raccolta_fallita', 'non_configurato'}
