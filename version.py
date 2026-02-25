"""
Proxreporter - Version Information

Questo modulo contiene le informazioni di versione del software.
La versione viene incrementata ad ogni release significativa.

Sviluppatore: Riccardo Grandi
Proprietario: Domarc SRL
Copyright (c) 2024-2026 Domarc SRL - Tutti i diritti riservati.
"""

# Versione semantica: MAJOR.MINOR.PATCH
# MAJOR: modifiche incompatibili
# MINOR: nuove funzionalità retrocompatibili
# PATCH: bug fix retrocompatibili
__version__ = "2.19.1"

# Build date (aggiornato automaticamente)
__build_date__ = "2026-02-28"

# Informazioni complete
VERSION_INFO = {
    "version": __version__,
    "build_date": __build_date__,
    "name": "Proxreporter",
    "author": "Riccardo Grandi",
    "company": "Domarc SRL",
    "url": "https://github.com/grandir66/Proxreporter"
}


def get_version() -> str:
    """Ritorna la versione corrente"""
    return __version__


def get_version_string() -> str:
    """Ritorna stringa versione completa per display"""
    return f"Proxreporter v{__version__} ({__build_date__})"


def get_version_dict() -> dict:
    """Ritorna dizionario con tutte le info versione"""
    return VERSION_INFO.copy()
