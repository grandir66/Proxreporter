import os
import logging
from datetime import datetime
from pathlib import Path
import jinja2

logger = logging.getLogger("proxreporter")

class HTMLReporter:
    def __init__(self, template_dir):
        self.template_dir = template_dir
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_dir),
            autoescape=jinja2.select_autoescape(['html', 'xml'])
        )
    
    def generate_report(self, data, output_path):
        """
        Genera un report HTML dai dati raccolti.
        
        Args:
            data (dict): Dizionario contenente tutti i dati (client, cluster, hosts, vms).
            output_path (str): Percorso dove salvare il file HTML.
        """
        try:
            template = self.env.get_template('report.html.j2')
            
            # Aggiungi timestamp formattato se non presente
            if 'date_generated' not in data:
                data['date_generated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            html_content = template.render(**data)
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            logger.info(f"✓ Report HTML generato: {output_path}")
            return True
        except Exception as e:
            logger.error(f"✗ Errore generazione report HTML: {e}")
            return False
