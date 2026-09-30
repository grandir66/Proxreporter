"""
Test del ripiego di porta in proxmox_report.SFTPUploader.connect():
se la 11122 non risponde a livello di rete si prova subito la 22
sullo stesso host; mai su errori di autenticazione o di protocollo.
"""

import sys
from pathlib import Path
from unittest import mock

import paramiko
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import proxmox_report  # noqa: E402

PRIMARY = "sftp.example.test"
FAILOVER = "10.0.0.14"


def _config():
    return {
        "sftp": {
            "enabled": True,
            "host": PRIMARY,
            "port": 11122,
            "username": "u",
            "password": "p",
            "fallback_host": FAILOVER,
            "fallback_port": 22,
        }
    }


def _net_error(port):
    return paramiko.ssh_exception.NoValidConnectionsError(
        {("127.0.0.1", port): ConnectionRefusedError(111, "refused")}
    )


def _run(behaviour):
    """behaviour: (host, port) -> None per successo, o eccezione da sollevare."""
    calls = []

    class FakeClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, host, port=22, **kwargs):
            calls.append((host, port))
            outcome = behaviour(host, port)
            if outcome is not None:
                raise outcome

        def close(self):
            pass

    with mock.patch.object(proxmox_report.paramiko, "SSHClient", FakeClient), \
            mock.patch.object(proxmox_report.time, "sleep") as sleep:
        ok = proxmox_report.SFTPUploader(_config()).connect()
    return ok, calls, sleep


def test_rete_11122_giu_poi_22_subito():
    def behaviour(host, port):
        return _net_error(port) if port == 11122 else None

    ok, calls, sleep = _run(behaviour)
    assert ok is True
    assert calls == [(PRIMARY, 11122), (PRIMARY, 22)]
    sleep.assert_not_called()


def test_timeout_socket_conta_come_rete():
    import socket

    def behaviour(host, port):
        return socket.timeout("timed out") if port == 11122 else None

    ok, calls, sleep = _run(behaviour)
    assert ok is True
    assert calls == [(PRIMARY, 11122), (PRIMARY, 22)]
    sleep.assert_not_called()


def test_autenticazione_non_cambia_porta():
    def behaviour(host, port):
        if host == PRIMARY:
            return paramiko.AuthenticationException("Authentication failed.")
        return None

    ok, calls, sleep = _run(behaviour)
    assert (PRIMARY, 22) not in calls
    assert calls[:3] == [(PRIMARY, 11122)] * 3  # retry come oggi
    assert calls[-1] == (FAILOVER, 22)
    assert ok is True


def test_errore_protocollo_non_cambia_porta():
    def behaviour(host, port):
        if host == PRIMARY:
            return paramiko.SSHException("Error reading SSH protocol banner")
        return None

    ok, calls, _ = _run(behaviour)
    assert (PRIMARY, 22) not in calls


def test_rete_giu_ovunque_ricade_su_retry_e_failover():
    def behaviour(host, port):
        return _net_error(port)

    ok, calls, sleep = _run(behaviour)
    assert ok is False
    assert (PRIMARY, 22) in calls
    assert (FAILOVER, 22) in calls
    assert calls.count((PRIMARY, 11122)) == 3  # 3 tentativi come oggi
    assert sleep.call_count == 2  # attese 5s e 10s invariate


def test_porta_gia_22_nessun_ripiego():
    cfg = _config()
    cfg["sftp"]["port"] = 22
    calls = []

    class FakeClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, host, port=22, **kwargs):
            calls.append((host, port))
            raise _net_error(port)

        def close(self):
            pass

    with mock.patch.object(proxmox_report.paramiko, "SSHClient", FakeClient), \
            mock.patch.object(proxmox_report.time, "sleep"):
        assert proxmox_report.SFTPUploader(cfg).connect() is False
    assert calls.count((PRIMARY, 22)) == 3
    assert (PRIMARY, 11122) not in calls
