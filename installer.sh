#!/bin/bash
#=============================================================
#   ROOTFAHIM FVM PANEL INSTALLER
#   Author  : ROOTFAHIM
#   Github  : https://github.com/RootFahim
#   Watermark: ROOTFAHIM - Everywhere 💀
#=============================================================

# ------- ROOTFAHIM COLORS -------
RED='\033[1;31m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
MAGENTA='\033[1;35m'
CYAN='\033[1;36m'
WHITE='\033[1;37m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

# ------- ROOTFAHIM GLOBAL VARS -------
REPO_URL="https://github.com/RootFahim/fvm-panel"
INSTALL_DIR="/root/fvm-panel"
SERVICE_NAME="fvm"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
WATERMARK="ROOTFAHIM"

# ------- TYPE WRITER EFFECT -------
typewriter() {
    local text="$1"
    local delay="${2:-0.02}"
    for ((i=0; i<${#text}; i++)); do
        printf "%s" "${text:$i:1}"
        sleep "$delay"
    done
    echo
}

# ------- SPINNER ANIMATION -------
spinner() {
    local pid=$1
    local msg="$2"
    local spin=('⣾' '⣽' '⣻' '⢿' '⡿' '⣟' '⣯' '⣷')
    local i=0
    tput civis 2>/dev/null
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r${CYAN}  %s ${WHITE}%s ${DIM}[ROOTFAHIM]${NC}" "${spin[$i]}" "$msg"
        i=$(( (i+1) % 8 ))
        sleep 0.1
    done
    wait "$pid"
    local exit_code=$?
    printf "\r\033[K"
    tput cnorm 2>/dev/null
    if [ "$exit_code" -eq 0 ]; then
        echo -e "${GREEN}  ✔ ${WHITE}${msg} ${DIM}[ROOTFAHIM]${NC}"
    else
        echo -e "${RED}  ✘ ${WHITE}${msg} ${DIM}[ROOTFAHIM]${NC}"
        return "$exit_code"
    fi
}

# ------- PROGRESS BAR -------
progress_bar() {
    local duration=$1
    local label="$2"
    local width=40
    echo -e "${MAGENTA}  ${label} ${DIM}(ROOTFAHIM)${NC}"
    for ((i=0; i<=width; i++)); do
        local filled=$(printf "%${i}s" | tr ' ' '█')
        local empty=$(printf "%$((width-i))s" | tr ' ' '░')
        local percent=$((i * 100 / width))
        printf "\r  ${GREEN}[${filled}${DIM}${empty}${NC}${GREEN}] ${WHITE}${percent}%%${NC}"
        sleep "$(echo "scale=3; $duration/$width" | bc)"
    done
    echo
}

# ------- WATERMARK FOOTER -------
wm_footer() {
    echo -e "${DIM}  ─────────────────────────────────────────────${NC}"
    echo -e "${DIM}   Powered by ${MAGENTA}${BOLD}ROOTFAHIM${NC}${DIM}  |  GitHub: /RootFahim${NC}"
    echo -e "${DIM}  ─────────────────────────────────────────────${NC}"
}

# ------- ASCII BANNER -------
show_banner() {
    clear
    echo -e "${MAGENTA}"
    cat << "EOF"
    ██████╗  ██████╗  ██████╗ ████████╗███████╗ █████╗ ██╗  ██╗██╗███╗   ███╗
    ██╔══██╗██╔═══██╗██╔═══██╗╚══██╔══╝██╔════╝██╔══██╗██║  ██║██║████╗ ████║
    ██████╔╝██║   ██║██║   ██║   ██║   █████╗  ███████║███████║██║██╔████╔██║
    ██╔══██╗██║   ██║██║   ██║   ██║   ██╔══╝  ██╔══██║██╔══██║██║██║╚██╔╝██║
    ██║  ██║╚██████╔╝╚██████╔╝   ██║   ██║     ██║  ██║██║  ██║██║██║ ╚═╝ ██║
    ╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝
EOF
    echo -e "${CYAN}"
    cat << "EOF"
        ███████╗██╗   ██╗███╗   ███╗    ██████╗  █████╗ ███╗   ██╗███████╗██╗
        ██╔════╝██║   ██║████╗ ████║    ██╔══██╗██╔══██╗████╗  ██║██╔════╝██║
        █████╗  ██║   ██║██╔████╔██║    ██████╔╝███████║██╔██╗ ██║█████╗  ██║
        ██╔══╝  ╚██╗ ██╔╝██║╚██╔╝██║    ██╔═══╝ ██╔══██║██║╚██╗██║██╔══╝  ██║
        ██║      ╚████╔╝ ██║ ╚═╝ ██║    ██║     ██║  ██║██║ ╚████║███████╗███████╗
        ╚═╝       ╚═══╝  ╚═╝     ╚═╝    ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝
EOF
    echo -e "${NC}"
    echo -e "${DIM}  ─────────────────────────────────────────────────────────────────────${NC}"
    echo -e "${YELLOW}      ⚡ Automated Installer ${DIM}|${NC} ${MAGENTA}Made by ROOTFAHIM${NC} ${DIM}|${NC} ${CYAN}v1.0${NC}"
    echo -e "${DIM}  ─────────────────────────────────────────────────────────────────────${NC}"
    echo
    sleep 0.5
    typewriter "  ➤ Initializing ROOTFAHIM FVM Panel Deployment..." 0.015
    echo
}

# ------- CHECK ROOT -------
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}  ✘ Please run as ROOT! ${DIM}[ROOTFAHIM]${NC}"
        echo -e "${YELLOW}    Try: ${WHITE}sudo bash installer.sh${NC}"
        exit 1
    fi
}

# ------- CHECK OS -------
check_os() {
    if ! command -v apt >/dev/null 2>&1; then
        echo -e "${RED}  ✘ This installer requires APT (Debian/Ubuntu). ${DIM}[ROOTFAHIM]${NC}"
        exit 1
    fi
}

# ------- STEP HEADER -------
step() {
    local num="$1"
    local title="$2"
    echo
    echo -e "${BLUE}  ┌──────────────────────────────────────────────┐${NC}"
    echo -e "${BLUE}  │${NC} ${YELLOW}STEP ${num}${NC} ${WHITE}➤ ${title}${NC}"
    echo -e "${BLUE}  └──────────────────────────────────────────────┘${NC}"
}

# ------- INSTALL -------
main_install() {
    step "1" "Installing GIT"
    (apt-get update -y >/dev/null 2>&1 && apt-get install git -y >/dev/null 2>&1) &
    spinner $! "Installing GIT package"

    step "2" "Cloning Repository"
    if [ -d "$INSTALL_DIR" ]; then
        echo -e "${YELLOW}  ⚠ Directory exists, removing old ${DIM}[ROOTFAHIM]${NC}"
        rm -rf "$INSTALL_DIR"
    fi
    (git clone "$REPO_URL" "$INSTALL_DIR" >/dev/null 2>&1) &
    spinner $! "Cloning RootFahim/fvm-panel"

    cd "$INSTALL_DIR" || { echo -e "${RED}  ✘ Cannot cd to $INSTALL_DIR ${DIM}[ROOTFAHIM]${NC}"; exit 1; }

    step "3" "Installing Python3 & PIP"
    (apt-get install python3 python3-pip -y >/dev/null 2>&1) &
    spinner $! "Installing Python3 & PIP"

    step "4" "Installing Requirements"
    if [ -f "requirements.txt" ]; then
        (pip3 install -r requirements.txt --break-system-packages >/dev/null 2>&1 || pip3 install -r requirements.txt >/dev/null 2>&1) &
        spinner $! "Installing Python dependencies"
    else
        echo -e "${YELLOW}  ⚠ requirements.txt not found, skipping ${DIM}[ROOTFAHIM]${NC}"
    fi

    step "5" "Configuring .env"
    if [ -f "test.env" ]; then
        cp test.env .env
        echo -e "${GREEN}  ✔ Copied test.env ➤ .env ${DIM}[ROOTFAHIM]${NC}"
    elif [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${GREEN}  ✔ Copied .env.example ➤ .env ${DIM}[ROOTFAHIM]${NC}"
    else
        echo -e "${YELLOW}  ⚠ No env template found ${DIM}[ROOTFAHIM]${NC}"
    fi

    step "6" "Creating Systemd Service"
    cat > "$SERVICE_FILE" << EOF
# ============================================
#   ROOTFAHIM FVM Panel Service
#   Managed by ROOTFAHIM
# ============================================
[Unit]
Description=FVM Panel Service - ROOTFAHIM
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/fvm.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
    echo -e "${GREEN}  ✔ Service file written ${DIM}[ROOTFAHIM]${NC}"

    step "7" "Reloading & Enabling Service"
    (systemctl daemon-reload >/dev/null 2>&1) &
    spinner $! "Reloading systemd daemon"
    (systemctl enable "$SERVICE_NAME" >/dev/null 2>&1) &
    spinner $! "Enabling ${SERVICE_NAME} service"

    step "8" "Starting Service"
    (systemctl restart "$SERVICE_NAME" >/dev/null 2>&1) &
    spinner $! "Starting ${SERVICE_NAME} service"

    sleep 1

    step "9" "Service Status"
    systemctl status "$SERVICE_NAME" --no-pager -l
}

# ------- SUCCESS BANNER -------
success_banner() {
    echo
    echo -e "${GREEN}"
    cat << "EOF"
    ╔══════════════════════════════════════════════════════╗
    ║                                                      ║
    ║     ✔  INSTALLATION COMPLETED SUCCESSFULLY!  ✔       ║
    ║                                                      ║
    ║          FVM PANEL IS NOW RUNNING - ROOTFAHIM        ║
    ║                                                      ║
    ╚══════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
    echo -e "${CYAN}  📌 Useful Commands:${NC}"
    echo -e "${DIM}     ROOTFAHIM ▸${NC} ${WHITE}systemctl status ${SERVICE_NAME}${NC}    ${DIM}# Check status${NC}"
    echo -e "${DIM}     ROOTFAHIM ▸${NC} ${WHITE}systemctl restart ${SERVICE_NAME}${NC}   ${DIM}# Restart${NC}"
    echo -e "${DIM}     ROOTFAHIM ▸${NC} ${WHITE}systemctl stop ${SERVICE_NAME}${NC}      ${DIM}# Stop${NC}"
    echo -e "${DIM}     ROOTFAHIM ▸${NC} ${WHITE}journalctl -u ${SERVICE_NAME} -f${NC}    ${DIM}# Live logs${NC}"
    echo
    wm_footer
    echo
    typewriter "  💀 Thank you for using ROOTFAHIM Installer 💀" 0.02
    echo
}

# ------- ENTRY -------
show_banner
check_root
check_os
progress_bar 1.2 "Preparing environment"
main_install
success_banner
