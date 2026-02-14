#!/usr/bin/env python3
"""
Proxreporter - Test Alert System

Script per testare l'invio di alert via Syslog e SMTP.
Invia alert di test per verificare la configurazione.

Uso:
    python3 test_alerts.py --config /opt/proxreport/config.json
    python3 test_alerts.py --config /opt/proxreport/config.json --syslog-only
    python3 test_alerts.py --config /opt/proxreport/config.json --smtp-only

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import argparse
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

# Aggiungi directory corrente al path
sys.path.insert(0, str(Path(__file__).parent))

def load_config(config_path: str) -> dict:
    """Carica e decripta la configurazione"""
    config_file = Path(config_path)
    
    if not config_file.exists():
        print(f"✗ File config non trovato: {config_path}")
        sys.exit(1)
    
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    # Prova a decriptare le password
    key_file = config_file.parent / ".secret.key"
    if key_file.exists():
        try:
            from cryptography.fernet import Fernet
            with open(key_file, 'rb') as f:
                key = f.read()
            cipher = Fernet(key)
            
            def decrypt_recursive(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        obj[k] = decrypt_recursive(v)
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        obj[i] = decrypt_recursive(v)
                elif isinstance(obj, str) and obj.startswith("ENC:"):
                    try:
                        return cipher.decrypt(obj[4:].encode()).decode()
                    except:
                        pass
                return obj
            
            decrypt_recursive(config)
        except ImportError:
            print("⚠ cryptography non disponibile, password potrebbero essere cifrate")
    
    # Prova a caricare configurazione remota
    try:
        from remote_config import download_remote_config, merge_remote_defaults
        remote_config = download_remote_config(config, config_file.parent)
        if remote_config:
            config = merge_remote_defaults(config, remote_config)
            print("✓ Configurazione remota caricata e applicata")
            
            # Mostra stato SMTP dopo merge
            smtp_enabled = config.get('smtp', {}).get('enabled', False)
            smtp_host = config.get('smtp', {}).get('host', '')
            smtp_recipients = config.get('smtp', {}).get('recipients', '')
            print(f"  SMTP: enabled={smtp_enabled}, host={smtp_host}, recipients={smtp_recipients}")
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠ Configurazione remota non disponibile: {e}")
    
    return config


def test_syslog(config: dict) -> bool:
    """Testa l'invio di un messaggio Syslog"""
    syslog_config = config.get('syslog', {})
    
    if not syslog_config.get('enabled'):
        print("⚠ Syslog non abilitato nella configurazione")
        return False
    
    host = syslog_config.get('host', '')
    port = int(syslog_config.get('port', 514))
    protocol = syslog_config.get('protocol', 'udp').lower()
    
    if not host:
        print("✗ Syslog host non configurato")
        return False
    
    print(f"\n{'='*60}")
    print("TEST SYSLOG")
    print(f"{'='*60}")
    print(f"  Server:    {host}:{port}")
    print(f"  Protocollo: {protocol.upper()}")
    
    try:
        from alert_manager import AlertManager, AlertSeverity, AlertType
        
        alert_manager = AlertManager(config)
        
        # Invia alert di test
        hostname = socket.gethostname()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        result = alert_manager.send_alert(
            AlertType.CUSTOM,
            AlertSeverity.INFO,
            f"Test Syslog - {hostname}",
            f"Questo è un messaggio di test inviato da Proxreporter alle {timestamp}",
            {
                'test': 'true',
                'hostname': hostname,
                'timestamp': timestamp
            }
        )
        
        if result.get('syslog'):
            print(f"  ✓ Messaggio Syslog inviato con successo!")
            print(f"    Verifica su Graylog cercando: 'Test Syslog - {hostname}'")
            return True
        else:
            print("  ✗ Invio Syslog fallito")
            return False
            
    except Exception as e:
        print(f"  ✗ Errore: {e}")
        return False


def test_syslog_raw(config: dict) -> bool:
    """Test diretto connessione Syslog senza AlertManager"""
    syslog_config = config.get('syslog', {})
    
    host = syslog_config.get('host', '')
    port = int(syslog_config.get('port', 514))
    protocol = syslog_config.get('protocol', 'udp').lower()
    
    if not host:
        return False
    
    print(f"\n→ Test connessione raw a {host}:{port} ({protocol})...")
    
    try:
        if protocol == 'tcp':
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(5)
        
        # Messaggio Syslog RFC 5424
        hostname = socket.gethostname()
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        pri = 16 * 8 + 6  # LOCAL0.INFO
        message = f"<{pri}>1 {timestamp} {hostname} proxreporter-test - - - Test connessione Syslog raw"
        
        if protocol == 'tcp':
            sock.sendall((message + '\n').encode('utf-8'))
        else:
            sock.sendto(message.encode('utf-8'), (host, port))
        
        sock.close()
        print(f"  ✓ Connessione {protocol.upper()} riuscita e messaggio inviato")
        return True
        
    except socket.timeout:
        print(f"  ✗ Timeout connessione a {host}:{port}")
        return False
    except ConnectionRefusedError:
        print(f"  ✗ Connessione rifiutata da {host}:{port}")
        return False
    except Exception as e:
        print(f"  ✗ Errore connessione: {e}")
        return False


def resolve_sender_template(sender_template: str, config: dict) -> str:
    """Risolve il template del sender con codcli e nomecliente"""
    if not sender_template:
        return sender_template
    
    client_config = config.get('client', {})
    codcli = client_config.get('codcli', 'unknown')
    nomecliente = client_config.get('nomecliente', 'unknown')
    
    # Sanitizza nomecliente per uso in email
    nomecliente_safe = ''.join(c if c.isalnum() or c in '-_' else '' for c in nomecliente)
    
    resolved = sender_template.replace('{codcli}', str(codcli))
    resolved = resolved.replace('{nomecliente}', nomecliente_safe)
    
    return resolved


def test_smtp(config: dict) -> bool:
    """Testa l'invio di una email SMTP"""
    smtp_config = config.get('smtp', {})
    
    if not smtp_config.get('enabled'):
        print("⚠ SMTP non abilitato nella configurazione")
        print("  Per abilitarlo, imposta smtp.enabled = true in config.json")
        return False
    
    host = smtp_config.get('host', '')
    port = int(smtp_config.get('port', 25))
    user = smtp_config.get('user', '')
    password = smtp_config.get('password', '')
    sender_template = smtp_config.get('sender', '')
    sender = resolve_sender_template(sender_template, config)
    recipients = smtp_config.get('recipients', '')
    
    if not host:
        print("✗ SMTP host non configurato")
        return False
    
    if not recipients:
        print("✗ Nessun destinatario email configurato (smtp.recipients)")
        return False
    
    print(f"\n{'='*60}")
    print("TEST SMTP")
    print(f"{'='*60}")
    print(f"  Server:      {host}:{port}")
    print(f"  User:        {user}")
    print(f"  Sender:      {sender}")
    print(f"  Recipients:  {recipients}")
    print(f"  TLS:         {smtp_config.get('use_tls', False)}")
    print(f"  SSL:         {smtp_config.get('use_ssl', False)}")
    
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        hostname = socket.gethostname()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Crea messaggio
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Proxreporter TEST] Alert di test da {hostname}"
        msg['From'] = sender
        msg['To'] = recipients
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <div style="background: #4CAF50; color: white; padding: 20px; border-radius: 5px;">
                <h2>✓ Test Email Proxreporter</h2>
            </div>
            <div style="padding: 20px; background: #f5f5f5; border-radius: 5px; margin-top: 10px;">
                <p><strong>Host:</strong> {hostname}</p>
                <p><strong>Timestamp:</strong> {timestamp}</p>
                <p><strong>Messaggio:</strong> Questo è un messaggio di test per verificare la configurazione SMTP.</p>
            </div>
            <p style="color: #666; font-size: 12px; margin-top: 20px;">
                Proxreporter - © Domarc SRL
            </p>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(html_body, 'html'))
        
        print("\n→ Connessione al server SMTP...")
        
        # Connessione
        use_ssl = smtp_config.get('use_ssl', False)
        use_tls = smtp_config.get('use_tls', False)
        
        if use_ssl or port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            if use_tls or port == 587:
                server.starttls()
        
        print("  ✓ Connessione stabilita")
        
        # Login se necessario
        if user and password:
            print(f"→ Login come {user}...")
            server.login(user, password)
            print("  ✓ Login riuscito")
        
        # Invio
        print("→ Invio email...")
        server.sendmail(sender, recipients.split(','), msg.as_string())
        server.quit()
        
        print(f"  ✓ Email inviata con successo a {recipients}!")
        return True
        
    except smtplib.SMTPAuthenticationError as e:
        print(f"  ✗ Errore autenticazione SMTP: {e}")
        return False
    except smtplib.SMTPConnectError as e:
        print(f"  ✗ Errore connessione SMTP: {e}")
        return False
    except Exception as e:
        print(f"  ✗ Errore SMTP: {e}")
        return False


def test_smtp_connection(config: dict) -> bool:
    """Test solo connessione SMTP senza invio"""
    smtp_config = config.get('smtp', {})
    
    host = smtp_config.get('host', '')
    port = int(smtp_config.get('port', 25))
    
    if not host:
        return False
    
    print(f"\n→ Test connessione SMTP a {host}:{port}...")
    
    try:
        import smtplib
        
        use_ssl = smtp_config.get('use_ssl', False)
        
        if use_ssl or port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=5)
        else:
            server = smtplib.SMTP(host, port, timeout=5)
        
        server.quit()
        print(f"  ✓ Connessione SMTP riuscita")
        return True
        
    except Exception as e:
        print(f"  ✗ Errore connessione SMTP: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test alert Proxreporter (Syslog/SMTP)")
    parser.add_argument('--config', '-c', required=True, help='Percorso config.json')
    parser.add_argument('--syslog-only', action='store_true', help='Testa solo Syslog')
    parser.add_argument('--smtp-only', action='store_true', help='Testa solo SMTP')
    parser.add_argument('--connection-only', action='store_true', help='Testa solo connessione (no invio)')
    
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print("PROXREPORTER - TEST SISTEMA ALERT")
    print(f"{'='*60}")
    print(f"Config: {args.config}")
    print(f"Host:   {socket.gethostname()}")
    print(f"Data:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Carica configurazione
    config = load_config(args.config)
    
    results = {}
    
    # Test Syslog
    if not args.smtp_only:
        if args.connection_only:
            results['syslog'] = test_syslog_raw(config)
        else:
            results['syslog_raw'] = test_syslog_raw(config)
            results['syslog'] = test_syslog(config)
    
    # Test SMTP
    if not args.syslog_only:
        if args.connection_only:
            results['smtp'] = test_smtp_connection(config)
        else:
            results['smtp_connection'] = test_smtp_connection(config)
            results['smtp'] = test_smtp(config)
    
    # Riepilogo
    print(f"\n{'='*60}")
    print("RIEPILOGO")
    print(f"{'='*60}")
    
    all_ok = True
    for test_name, result in results.items():
        status = "✓ OK" if result else "✗ FALLITO"
        print(f"  {test_name}: {status}")
        if not result:
            all_ok = False
    
    if all_ok:
        print(f"\n✓ Tutti i test completati con successo!")
    else:
        print(f"\n⚠ Alcuni test sono falliti. Verifica la configurazione.")
    
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
