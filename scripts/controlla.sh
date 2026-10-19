#!/bin/bash
# Cancello locale: non contatta host Proxmox o servizi reali.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m py_compile remote_runner.py proxmox_core.py proxmox_report.py
.venv/bin/python -m pytest -q tests
echo 'Tutto verde.'
