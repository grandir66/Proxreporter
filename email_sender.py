"""
Proxreporter - Email Sender Module

Modulo per l'invio di report via email SMTP.

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import logging
import os

logger = logging.getLogger("proxreporter")

class EmailSender:
    def __init__(self, config):
        self.smtp_config = config.get('smtp', {})
        self.enabled = self.smtp_config.get('enabled', False)
        
    def send_report(self, html_content, subject, attachments=None):
        """
        Invia report via email.
        
        Args:
            html_content (str): Contenuto HTML del body.
            subject (str): Oggetto della mail.
            attachments (list): Lista di percorsi file (assoluti) da allegare.
        """
        if not self.enabled:
            logger.info("→ Invio email disabilitato (smtp.enabled=false)")
            return False
            
        host = self.smtp_config.get('host')
        port = int(self.smtp_config.get('port', 25))
        user = self.smtp_config.get('user')
        password = self.smtp_config.get('password')
        sender = self.smtp_config.get('sender', user)
        recipients = self.smtp_config.get('recipients', [])
        use_tls = self.smtp_config.get('use_tls', False)
        use_ssl = self.smtp_config.get('use_ssl', False)
        
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(',') if r.strip()]
            
        if not all([host, user, password, recipients]):
            missing = []
            if not host: missing.append("host")
            if not user: missing.append("user")
            if not password: missing.append("password")
            if not recipients: missing.append("recipients")
            logger.error(f"Configurazione SMTP incompleta (mancano: {', '.join(missing)})")
            return False
        
        logger.info(f"  Server: {host}:{port}")
        logger.info(f"  User: {user}")
        logger.info(f"  Sender: {sender}")
        logger.info(f"  Recipients: {', '.join(recipients)}")
        logger.info(f"  SSL: {use_ssl}, TLS: {use_tls}")
            
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = ", ".join(recipients)
        
        # Attach HTML body
        msg.attach(MIMEText(html_content, 'html'))
        
        # Attachments
        if attachments:
            for fpath in attachments:
                if fpath and os.path.exists(fpath):
                    try:
                        with open(fpath, "rb") as f:
                            part = MIMEApplication(f.read(), Name=os.path.basename(fpath))
                        part['Content-Disposition'] = f'attachment; filename="{os.path.basename(fpath)}"'
                        msg.attach(part)
                        logger.info(f"  Allegato: {os.path.basename(fpath)}")
                    except Exception as e:
                        logger.warning(f"Impossibile allegare {fpath}: {e}")
        
        try:
            context = ssl.create_default_context()
            
            # SSL implicito (porta 465 o use_ssl=True)
            if use_ssl or port == 465:
                logger.info("  Connessione con SSL implicito...")
                with smtplib.SMTP_SSL(host, port, context=context) as server:
                    server.login(user, password)
                    server.send_message(msg)
            # STARTTLS (porta 587 o use_tls=True)
            elif use_tls or port == 587:
                logger.info("  Connessione con STARTTLS...")
                with smtplib.SMTP(host, port) as server:
                    server.starttls(context=context)
                    server.login(user, password)
                    server.send_message(msg)
            # Nessuna crittografia (porta 25, SSL/TLS disabilitati)
            else:
                logger.info("  Connessione senza crittografia...")
                with smtplib.SMTP(host, port) as server:
                    server.login(user, password)
                    server.send_message(msg)
                    
            logger.info(f"✓ Email inviata correttamente a {', '.join(recipients)}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"✗ Errore autenticazione SMTP: {e}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"✗ Errore SMTP: {e}")
            return False
        except Exception as e:
            logger.error(f"✗ Errore invio email: {e}")
            return False
