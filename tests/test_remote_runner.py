import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

import remote_runner as rr
from proxmox_report import ProxmoxLocalExtractor


def test_due_host_forzati_ssh_e_errore_non_ferma_secondo(tmp_path):
    template = {'hosts': ['pve1.cliente.it', '192.168.1.12'], 'config': {
        'client': {'codcli': '71734', 'nomecliente': 'Cliente'}, 'smtp': {'enabled': False}}}
    f = tmp_path / 'template.json'
    f.write_text(json.dumps(template)); f.chmod(0o600)
    for host in template['hosts']:
        p = tmp_path / (host + '.json')
        p.write_text(json.dumps({'execution_mode': 'ssh', 'configure_notifications': False,
            'ssh': {'host': host, 'username': 'root', 'password': 'Finta-non-stampare',
                    'host_key_policy': 'reject', 'known_hosts_file': '/root/known_hosts'},
            'client': template['config']['client'],
            'system': {'output_directory': str(tmp_path / host)}})); p.chmod(0o600)
    run = Mock(side_effect=[RuntimeError('Finta-non-stampare'), None])
    assert rr.run_all(f, report=run) == 1
    assert run.call_count == 2
    assert [c.args[0]['ssh']['host'] for c in run.call_args_list] == template['hosts']


def test_ssh_esplicito_non_ripiega_su_locale(monkeypatch):
    monkeypatch.setattr(os.path, 'exists', lambda p: True)
    e = ProxmoxLocalExtractor({'execution_mode': 'ssh', 'ssh': {'host': 'pve.cliente.it'}})
    assert e.detect_execution_mode() == 'ssh'


def test_ssh_strict_carica_chiavi_e_non_autoaccetta(monkeypatch):
    import proxmox_report as pr
    fake = Mock()
    monkeypatch.setattr(pr.paramiko, 'SSHClient', lambda: fake)
    e = ProxmoxLocalExtractor({'execution_mode': 'ssh', 'ssh': {
        'host': 'pve.cliente.it', 'host_key_policy': 'reject',
        'known_hosts_file': '/root/known_hosts', 'username': 'root', 'password': 'Finta'}})
    assert e.connect_ssh()
    fake.load_host_keys.assert_called_once_with('/root/known_hosts')
    assert isinstance(fake.set_missing_host_key_policy.call_args.args[0], pr.paramiko.RejectPolicy)
    assert fake.connect.call_args.kwargs['look_for_keys'] is False


def test_file_config_pubblico_rifiutato(tmp_path):
    f=tmp_path/'template.json'; f.write_text('{}'); f.chmod(0o644)
    with pytest.raises(ValueError):
        rr.read_private(f)


def test_configure_due_host_cifrati_permessi_rerun_e_segreti_locali(tmp_path, monkeypatch):
    import paramiko
    import getpass
    import builtins
    public = paramiko.RSAKey.generate(1024)
    key = public.get_name() + ' ' + public.get_base64()
    template = {'hosts': ['pve1.cliente.it', 'pve2.cliente.it'], 'encrypt': True,
                'config': {'client': {'codcli': '71734', 'nomecliente': 'Cliente'}}}
    f = tmp_path / 'template.json'; rr.write_private(f, template)
    answers = iter(['root', '', key, '', 'root', '2222', key, ''])
    monkeypatch.setattr(builtins, 'input', lambda _: next(answers))
    monkeypatch.setattr(getpass, 'getpass', lambda _: 'Finta-Solo-Base')
    rr.configure(f)
    rr.configure(f)  # non ripete i prompt e conserva chiavi/accessi già configurati
    for host in template['hosts']:
        cfg = rr.read_private(tmp_path / (host + '.json'))
        assert cfg['ssh']['password'].startswith('ENC:')
        assert 'Finta-Solo-Base' not in json.dumps(cfg)
        assert (tmp_path / (host + '.json')).stat().st_mode & 0o777 == 0o600
    run = Mock()
    assert rr.run_all(f, report=run) == 0
    assert [call.args[0]['ssh']['password'] for call in run.call_args_list] == ['Finta-Solo-Base']*2


def test_chiave_sconosciuta_rifiutata_senza_password_nei_log(monkeypatch, caplog):
    import proxmox_report as pr
    client = Mock()
    client.connect.side_effect = RuntimeError('Segreto-Non-Loggare')
    monkeypatch.setattr(pr.paramiko, 'SSHClient', lambda: client)
    e = ProxmoxLocalExtractor({'ssh': {'host': 'pve.cliente.it', 'username': 'root',
                  'password': 'Segreto-Non-Loggare', 'host_key_policy': 'reject',
                  'known_hosts_file': '/root/known_hosts'}})
    assert e.connect_ssh() is False
    assert 'Segreto-Non-Loggare' not in caplog.text
