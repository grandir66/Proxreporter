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
            return False
            
        host = self.smtp_config.get('host')
        port = int(self.smtp_config.get('port', 587))
        user = self.smtp_config.get('user')
        password = self.smtp_config.get('password')
        sender = self.smtp_config.get('sender', user)
        recipients = self.smtp_config.get('recipients', [])
        
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(',')]
            
        if not all([host, user, password, recipients]):
            logger.error("Configurazione SMTP incompleta (mancano parametri obbligatori)")
            return False
            
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
                        logger.debug(f"Allegato aggiunto: {os.path.basename(fpath)}")
                    except Exception as e:
                        logger.warning(f"Impossibile allegare {fpath}: {e}")
        
        try:
            context = ssl.create_default_context()
            # Gestione differenza tra SSL (465) e STARTTLS (587/25)
            if port == 465:
                # SSL Implicito
                with smtplib.SMTP_SSL(host, port, context=context) as server:
                    server.login(user, password)
                    server.send_message(msg)
            else:
                # STARTTLS (Esplicito)
                with smtplib.SMTP(host, port) as server:
                    # server.set_debuglevel(1) # Uncomment for debug
                    server.starttls(context=context)
                    server.login(user, password)
                    server.send_message(msg)
                    
            logger.info(f"✓ Email inviata correttamente a {', '.join(recipients)}")
            return True
        except Exception as e:
            logger.error(f"✗ Errore invio email: {e}")
            return False
