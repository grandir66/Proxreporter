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
DEPS="git python3 python3-venv"
MISSING=""
for pkg in $DEPS; do
    if ! command -v $pkg &> /dev/null && ! dpkg -l $pkg &> /dev/null; then
         # Basic check, might need refinement for different distros
         MISSING="$MISSING $pkg"
    fi
done

if [ ! -z "$MISSING" ]; then
    echo -e "${RED}Missing dependencies:${NC} $MISSING"
    echo "Attempting to install..."
    apt-get update && apt-get install -y git python3 python3-pip python3-venv || { echo -e "${RED}Installation failed.${NC}"; exit 1; }
fi

# Clone/Update Repo
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "→ Updating existing installation in $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull origin $BRANCH
else
    echo "→ Cloning repository to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    git clone -b $BRANCH "$REPO_URL" "$INSTALL_DIR"
fi

# Run Setup
echo "→ Running Python Setup..."
cd "$INSTALL_DIR/v2" || { echo -e "${RED}Directory v2 not found!${NC}"; exit 1; }

# Execute setup.py using the system python3
python3 setup.py

echo -e "${GREEN}=== Installation Complete ===${NC}"
