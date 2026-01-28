#!/bin/bash

# Configuration
REPO_URL="https://github.com/grandir66/Proxreporter.git"
BRANCH="main"
INSTALL_DIR="/opt/proxreport"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
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
if [ -e "$INSTALL_DIR" ]; then
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "→ Updating existing installation in $INSTALL_DIR..."
        cd "$INSTALL_DIR"
        
        # Preserve configuration files
        [ -f "$INSTALL_DIR/config.json" ] && cp "$INSTALL_DIR/config.json" "/tmp/prox_config_tmp.json"
        [ -f "$INSTALL_DIR/.secret.key" ] && cp "$INSTALL_DIR/.secret.key" "/tmp/prox_secret_tmp.key"
        # Also check old v2 location for migration
        [ -f "$INSTALL_DIR/v2/config.json" ] && cp "$INSTALL_DIR/v2/config.json" "/tmp/prox_config_tmp.json"
        [ -f "$INSTALL_DIR/v2/.secret.key" ] && cp "$INSTALL_DIR/v2/.secret.key" "/tmp/prox_secret_tmp.key"
        
        # Force reset to ensure we have the very latest code and clean state
        git fetch origin
        git reset --hard origin/$BRANCH
        
        # Restore configuration files
        [ -f "/tmp/prox_config_tmp.json" ] && mv "/tmp/prox_config_tmp.json" "$INSTALL_DIR/config.json"
        [ -f "/tmp/prox_secret_tmp.key" ] && mv "/tmp/prox_secret_tmp.key" "$INSTALL_DIR/.secret.key"
    else
        echo -e "${YELLOW}⚠ Found existing path at $INSTALL_DIR (not a git repo).${NC}"
        
        # Preserve configuration if exists (check both old and new locations)
        [ -f "$INSTALL_DIR/config.json" ] && cp "$INSTALL_DIR/config.json" "/tmp/prox_config_tmp.json"
        [ -f "$INSTALL_DIR/.secret.key" ] && cp "$INSTALL_DIR/.secret.key" "/tmp/prox_secret_tmp.key"
        [ -f "$INSTALL_DIR/v2/config.json" ] && cp "$INSTALL_DIR/v2/config.json" "/tmp/prox_config_tmp.json"
        [ -f "$INSTALL_DIR/v2/.secret.key" ] && cp "$INSTALL_DIR/v2/.secret.key" "/tmp/prox_secret_tmp.key"

        BACKUP_DIR="${INSTALL_DIR}_bak_$(date +%s)"
        echo "→ Backing up to $BACKUP_DIR..."
        mv "$INSTALL_DIR" "$BACKUP_DIR"
        
        echo "→ Cloning repository to $INSTALL_DIR..."
        git clone -b $BRANCH "$REPO_URL" "$INSTALL_DIR"

        # Restore configuration
        [ -f "/tmp/prox_config_tmp.json" ] && mv "/tmp/prox_config_tmp.json" "$INSTALL_DIR/config.json"
        [ -f "/tmp/prox_secret_tmp.key" ] && mv "/tmp/prox_secret_tmp.key" "$INSTALL_DIR/.secret.key"
    fi
else
    echo "→ Cloning repository to $INSTALL_DIR..."
    git clone -b $BRANCH "$REPO_URL" "$INSTALL_DIR"
fi

# Set executable permissions on Python scripts
echo "→ Setting executable permissions..."
chmod +x "$INSTALL_DIR/"*.py 2>/dev/null || true

# Run Setup
echo "→ Running Python Setup..."
cd "$INSTALL_DIR" || { echo -e "${RED}Installation directory not found!${NC}"; exit 1; }

# Execute setup.py using the system python3
python3 setup.py

echo -e "${GREEN}=== Installation Complete ===${NC}"
echo ""
echo "Usage:"
echo "  python3 $INSTALL_DIR/proxmox_core.py --config $INSTALL_DIR/config.json --local"
echo ""
