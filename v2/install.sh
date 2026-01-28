#!/bin/bash

# Configuration
REPO_URL="https://github.com/grandir66/Proxreporter.git"
BRANCH="main"
INSTALL_DIR="/opt/proxreport"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Proxreporter Installer ===${NC}"

# Check for root
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}Please run as root${NC}"
  exit 1
fi

# Check requirements
echo "→ Checking dependencies..."
MISSING=""

# Check git
if ! command -v git &> /dev/null; then
    MISSING="$MISSING git"
fi

# Check python3
if ! command -v python3 &> /dev/null; then
    MISSING="$MISSING python3"
fi

# Check python3-venv (module check)
if command -v python3 &> /dev/null; then
    if ! python3 -c "import venv" &> /dev/null; then
        MISSING="$MISSING python3-venv"
    fi
fi


if [ ! -z "$MISSING" ]; then
    echo -e "${RED}Missing dependencies:${NC} $MISSING"
    echo "Attempting to install..."
    apt-get update && apt-get install -y git python3 python3-pip python3-venv || { echo -e "${RED}Installation failed.${NC}"; exit 1; }
fi

# Clone/Update Repo
# Clone/Update Repo
if [ -d "$INSTALL_DIR" ]; then
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "→ Updating existing installation in $INSTALL_DIR..."
        cd "$INSTALL_DIR"
        # Force reset to ensure we have the very latest code and clean state
        git fetch origin
        git reset --hard origin/$BRANCH
    else
        echo "⚠ Directory $INSTALL_DIR exists but is not a git repository."
        BACKUP_DIR="${INSTALL_DIR}_bak_$(date +%s)"
        echo "→ Backing up to $BACKUP_DIR..."
        mv "$INSTALL_DIR" "$BACKUP_DIR"
        
        echo "→ Cloning repository to $INSTALL_DIR..."
        git clone -b $BRANCH "$REPO_URL" "$INSTALL_DIR"
    fi
else
    echo "→ Cloning repository to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    git clone -b $BRANCH "$REPO_URL" "$INSTALL_DIR"
fi

# Run Setup
echo "→ Running Python Setup..."
cd "$INSTALL_DIR/v2" || { echo -e "${RED}Directory v2 not found!${NC}"; exit 1; }

# Execute setup.py using the system python3
# Execute setup.py using the system python3
python3 setup.py

echo -e "${GREEN}=== Installation Complete ===${NC}"
