#!/bin/bash
# Reels AutoPilot - Raspberry Pi Setup Script
# For Pi Zero 2 W (512MB RAM)

set -e

echo "=========================================="
echo "  Reels AutoPilot - Pi Setup"
echo "=========================================="

# Update system
echo "[1/6] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install Python 3 and dependencies
echo "[2/6] Installing Python 3 and build tools..."
sudo apt install -y python3 python3-venv python3-pip python3-dev \
    libffi-dev libssl-dev git ffmpeg

# Create virtual environment
echo "[3/6] Creating Python virtual environment..."
cd /home/pi/reels-autopilot
python3 -m venv venv
source venv/bin/activate

# Install Python packages (use --no-cache-dir to save RAM during install)
echo "[4/6] Installing Python packages (this may take a while on Pi Zero)..."
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir -r requirements.txt

# Create required directories
echo "[5/6] Setting up directories..."
mkdir -p database downloads

# Install systemd services
echo "[6/6] Installing systemd services..."
sudo cp services/reels-autopilot.service /etc/systemd/system/
sudo cp services/reels-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable reels-autopilot.service
sudo systemctl enable reels-web.service

echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "Start the services:"
echo "  sudo systemctl start reels-autopilot"
echo "  sudo systemctl start reels-web"
echo ""
echo "Dashboard: http://<pi-ip>:8080"
echo ""
echo "View logs:"
echo "  journalctl -u reels-autopilot -f"
echo "  journalctl -u reels-web -f"
echo ""
