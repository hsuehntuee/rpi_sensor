#!/usr/bin/env bash
# ==============================================================================
# Raspberry Pi 5 Lepton 3.X Edge Node One-Click Startup Script
# ==============================================================================
set -euo pipefail

# Helper logging functions
log() { printf '\n\033[1;32m[INFO] %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[WARN] %s\033[0m\n' "$*"; }
error() { printf '\n\033[1;31m[ERROR] %s\033[0m\n' "$*" >&2; exit 1; }

# 1. Check if running on a Linux system
if [[ "$(uname -s)" != "Linux" ]]; then
    warn "This script is designed to run on a Linux-based Raspberry Pi OS."
    warn "Please copy these files to your Raspberry Pi and execute the script there."
    exit 0
fi

# 2. Check if Docker is installed
if ! command -v docker &>/dev/null; then
    error "Docker is not installed. Please install Docker first: curl -sSL https://get.docker.com | sh"
fi

# 3. Enable SPI and I2C interfaces (Requires root/sudo privileges)
log "Configuring hardware interfaces (SPI and I2C)..."
if command -v raspi-config &>/dev/null; then
    sudo raspi-config nonint do_spi 0 || warn "Failed to automatically enable SPI via raspi-config. Please verify manually."
    sudo raspi-config nonint do_i2c 0 || warn "Failed to automatically enable I2C via raspi-config. Please verify manually."
    log "SPI and I2C interfaces enabled."
else
    warn "raspi-config not found. Make sure dtparam=spi=on and dtparam=i2c_arm=on are enabled in /boot/firmware/config.txt."
fi

# 4. Check for .env file
if [[ ! -f .env ]]; then
    log "Creating .env configuration file from .env.example..."
    cp .env.example .env
    # Set default Lepton 3.X resolutions
    sed -i 's/^LEPTON_WIDTH=.*/LEPTON_WIDTH=160/' .env
    sed -i 's/^LEPTON_HEIGHT=.*/LEPTON_HEIGHT=120/' .env
    warn "Please edit the .env file to set your DEVICE_ID, SERVER_URL, and API_KEY before starting!"
fi

# 5. Build and run containers
log "Building and starting containerized services..."
if docker compose version &>/dev/null; then
    docker compose up --build -d
elif docker-compose version &>/dev/null; then
    docker-compose up --build -d
else
    error "Neither 'docker compose' nor 'docker-compose' command found. Please check your Docker installation."
fi

log "Startup successful! Displaying logs from the edge-sensor service (Ctrl+C to exit)..."
docker compose logs -f edge-sensor
