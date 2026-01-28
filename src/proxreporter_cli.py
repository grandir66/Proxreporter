#!/usr/bin/env python3
"""
Proxreporter CLI entry point.

This script provides the main entry point for running Proxreporter
from the command line.
"""

import sys
from pathlib import Path

# Add the src directory to Python path
src_dir = Path(__file__).resolve().parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from proxreporter.cli import main

if __name__ == "__main__":
    main()
