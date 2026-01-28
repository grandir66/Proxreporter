#!/bin/bash

# ============================================================================
# Proxreporter V3 Installer
# Modular architecture with improved security and performance
# ============================================================================

# Configuration
REPO_URL="https://github.com/grandir66/Proxreporter.git"
BRANCH="main"
INSTALL_DIR="/opt/proxreport"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}          ${GREEN}Proxreporter V3 Installer${NC}                             ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}          Modular Architecture Edition                          ${BLUE}║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check for root
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}✗ Please run as root${NC}"
  exit 1
fi

# Check requirements
echo -e "${YELLOW}→ Checking dependencies...${NC}"
MISSING=""

# Check git
if ! command -v git &> /dev/null; then
    MISSING="$MISSING git"
fi

# Check python3
if ! command -v python3 &> /dev/null; then
    MISSING="$MISSING python3"
fi

# Check pip
if ! command -v pip3 &> /dev/null; then
    MISSING="$MISSING python3-pip"
fi

if [ ! -z "$MISSING" ]; then
    echo -e "${YELLOW}  Installing missing dependencies:${NC} $MISSING"
    apt-get update -qq && apt-get install -y $MISSING git python3 python3-pip python3-venv || { 
        echo -e "${RED}✗ Dependency installation failed.${NC}"
        exit 1
    }
fi

echo -e "${GREEN}✓ Dependencies OK${NC}"

# Clone/Update Repo
echo ""
if [ -e "$INSTALL_DIR" ]; then
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo -e "${YELLOW}→ Updating existing installation...${NC}"
        cd "$INSTALL_DIR"
        
        # Preserve configuration files
        [ -f "$INSTALL_DIR/config.json" ] && cp "$INSTALL_DIR/config.json" "/tmp/prox_config_tmp.json"
        [ -f "$INSTALL_DIR/.secret.key" ] && cp "$INSTALL_DIR/.secret.key" "/tmp/prox_secret_tmp.key"
        
        # Update
        git fetch origin
        git reset --hard origin/$BRANCH
        
        # Restore configuration files
        [ -f "/tmp/prox_config_tmp.json" ] && mv "/tmp/prox_config_tmp.json" "$INSTALL_DIR/config.json"
        [ -f "/tmp/prox_secret_tmp.key" ] && mv "/tmp/prox_secret_tmp.key" "$INSTALL_DIR/.secret.key"
        
        echo -e "${GREEN}✓ Repository updated${NC}"
    else
        echo -e "${YELLOW}⚠ Found existing path at $INSTALL_DIR (not a git repo).${NC}"
        
        # Preserve configuration
        [ -f "$INSTALL_DIR/config.json" ] && cp "$INSTALL_DIR/config.json" "/tmp/prox_config_tmp.json"
        [ -f "$INSTALL_DIR/.secret.key" ] && cp "$INSTALL_DIR/.secret.key" "/tmp/prox_secret_tmp.key"

        BACKUP_DIR="${INSTALL_DIR}_bak_$(date +%s)"
        echo -e "${YELLOW}  → Backing up to $BACKUP_DIR...${NC}"
        mv "$INSTALL_DIR" "$BACKUP_DIR"
        
        echo -e "${YELLOW}→ Cloning repository...${NC}"
        git clone -b $BRANCH "$REPO_URL" "$INSTALL_DIR"

        # Restore configuration
        [ -f "/tmp/prox_config_tmp.json" ] && mv "/tmp/prox_config_tmp.json" "$INSTALL_DIR/config.json"
        [ -f "/tmp/prox_secret_tmp.key" ] && mv "/tmp/prox_secret_tmp.key" "$INSTALL_DIR/.secret.key"
        
        echo -e "${GREEN}✓ Repository cloned${NC}"
    fi
else
    echo -e "${YELLOW}→ Cloning repository...${NC}"
    git clone -b $BRANCH "$REPO_URL" "$INSTALL_DIR"
    echo -e "${GREEN}✓ Repository cloned${NC}"
fi

# Install Python dependencies
echo ""
echo -e "${YELLOW}→ Installing Python dependencies...${NC}"
pip3 install -q paramiko cryptography 2>/dev/null || pip3 install paramiko cryptography

echo -e "${GREEN}✓ Dependencies installed${NC}"

# Set executable permissions
echo ""
echo -e "${YELLOW}→ Setting permissions...${NC}"
chmod +x "$INSTALL_DIR/"*.py 2>/dev/null || true
chmod +x "$INSTALL_DIR/src/"*.py 2>/dev/null || true
chmod +x "$INSTALL_DIR/src/proxreporter/"*.py 2>/dev/null || true

echo -e "${GREEN}✓ Permissions set${NC}"

# Create symlink for easy access
echo ""
echo -e "${YELLOW}→ Creating command symlink...${NC}"
ln -sf "$INSTALL_DIR/src/proxreporter_cli.py" /usr/local/bin/proxreporter 2>/dev/null || true

# Run setup if config doesn't exist
echo ""
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    echo -e "${YELLOW}→ Running initial setup...${NC}"
    cd "$INSTALL_DIR"
    python3 setup.py
else
    echo -e "${GREEN}✓ Configuration file exists, skipping setup${NC}"
    echo -e "  To reconfigure, run: ${BLUE}python3 $INSTALL_DIR/setup.py${NC}"
fi

# Summary
echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}          ${GREEN}Installation Complete!${NC}                                 ${BLUE}║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Installation directory: ${BLUE}$INSTALL_DIR${NC}"
echo ""
echo -e "${YELLOW}Usage (V3 Modular):${NC}"
echo -e "  python3 $INSTALL_DIR/src/proxreporter_cli.py --config $INSTALL_DIR/config.json --local"
echo ""
echo -e "${YELLOW}Usage (V2 Legacy):${NC}"
echo -e "  python3 $INSTALL_DIR/proxmox_core.py --config $INSTALL_DIR/config.json --local"
echo ""
echo -e "${YELLOW}Or use the symlink:${NC}"
echo -e "  proxreporter --config $INSTALL_DIR/config.json --local"
echo ""
