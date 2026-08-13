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
    warn "Docker is not installed on this system."
    read -p "Would you like to install Docker now automatically? (y/N): " -r yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        log "Installing Docker..."
        curl -sSL https://get.docker.com | sh
        log "Adding current user to docker group..."
        sudo usermod -aG docker "$USER"
        log "Docker installed successfully!"
        warn "You may need to log out and log back in, or run 'newgrp docker' for group changes to take effect."
    else
        error "Docker is required to run the services. Exiting."
    fi
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

# Get local IP address for easy dashboard access
IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
if [[ -z "$IP_ADDR" ]]; then
    IP_ADDR="127.0.0.1"
fi

if command -v docker-compose &>/dev/null && ! command -v docker &>/dev/null; then
    docker-compose up --build -d
    log "Startup successful!"
    printf "\n\033[1;36m======================================================================\033[0m\n"
    printf "\033[1;32m🍓 樹莓派邊緣節點 (Edge Sensor Node) 已成功在背景啟動！\033[0m\n"
    printf "\033[1;33m👉 樹莓派內網即時儀表板 (Edge Local Dashboard)：\033[0m\n"
    printf "   \033[1;37mhttp://%s:8080/dashboard\033[0m\n" "$IP_ADDR"
    printf "   (或在 Pi 本機瀏覽器打開: \033[1;37mhttp://127.0.0.1:8080/dashboard\033[0m)\n"
    printf "\033[1;36m======================================================================\033[0m\n\n"
    docker-compose logs -f edge-sensor
else
    if docker info &>/dev/null; then
        docker compose up --build -d
        log "Startup successful!"
        printf "\n\033[1;36m======================================================================\033[0m\n"
        printf "\033[1;32m🍓 樹莓派邊緣節點 (Edge Sensor Node) 已成功在背景啟動！\033[0m\n"
        printf "\033[1;33m👉 樹莓派內網即時儀表板 (Edge Local Dashboard)：\033[0m\n"
        printf "   \033[1;37mhttp://%s:8080/dashboard\033[0m\n" "$IP_ADDR"
        printf "   (或在 Pi 本機瀏覽器打開: \033[1;37mhttp://127.0.0.1:8080/dashboard\033[0m)\n"
        printf "\033[1;36m======================================================================\033[0m\n\n"
        docker compose logs -f edge-sensor
    elif sg docker -c "docker info" &>/dev/null; then
        log "Running docker compose under 'docker' group..."
        sg docker -c "docker compose up --build -d"
        log "Startup successful!"
        printf "\n\033[1;36m======================================================================\033[0m\n"
        printf "\033[1;32m🍓 樹莓派邊緣節點 (Edge Sensor Node) 已成功在背景啟動！\033[0m\n"
        printf "\033[1;33m👉 樹莓派內網即時儀表板 (Edge Local Dashboard)：\033[0m\n"
        printf "   \033[1;37mhttp://%s:8080/dashboard\033[0m\n" "$IP_ADDR"
        printf "   (或在 Pi 本機瀏覽器打開: \033[1;37mhttp://127.0.0.1:8080/dashboard\033[0m)\n"
        printf "\033[1;36m======================================================================\033[0m\n\n"
        sg docker -c "docker compose logs -f edge-sensor"
    else
        warn "Permission to docker socket denied. Running with sudo..."
        sudo docker compose up --build -d
        log "Startup successful!"
        printf "\n\033[1;36m======================================================================\033[0m\n"
        printf "\033[1;32m🍓 樹莓派邊緣節點 (Edge Sensor Node) 已成功在背景啟動！\033[0m\n"
        printf "\033[1;33m👉 樹莓派內網即時儀表板 (Edge Local Dashboard)：\033[0m\n"
        printf "   \033[1;37mhttp://%s:8080/dashboard\033[0m\n" "$IP_ADDR"
        printf "   (或在 Pi 本機瀏覽器打開: \033[1;37mhttp://127.0.0.1:8080/dashboard\033[0m)\n"
        printf "\033[1;36m======================================================================\033[0m\n\n"
        sudo docker compose logs -f edge-sensor
    fi
fi
