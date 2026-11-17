#!/usr/bin/env bash
#
# ╔══════════════════════════════════════════════════════════════╗
# ║   FVM PANEL AUTO INSTALLER  •  Powered by ROOTFAHIM          ║
# ║   Author  : RootFahim                                        ║
# ║   GitHub  : https://github.com/RootFahim                     ║
# ║   Watermark: ROOTFAHIM                                       ║
# ╚══════════════════════════════════════════════════════════════╝

set -euo pipefail

# ───────────────────────── COLORS ─────────────────────────
RED='\033[1;31m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
MAGENTA='\033[1;35m'
CYAN='\033[1;36m'
WHITE='\033[1;37m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

# ───────────────────── WATERMARK BANNER ───────────────────
print_banner() {
    clear
    echo -e "${CYAN}"
    cat << "EOF"
  ██████╗  ██████╗  ██████╗ ████████╗███████╗ █████╗ ██╗  ██╗██╗███╗   ███╗
  ██╔══██╗██╔═══██╗██╔═══██╗╚══██╔══╝██╔════╝██╔══██╗██║  ██║██║████╗ ████║
  ██████╔╝██║   ██║██║   ██║   ██║   █████╗  ███████║███████║██║██╔████╔██║
  ██╔══██╗██║   ██║██║   ██║   ██║   ██╔══╝  ██╔══██║██╔══██║██║██║╚██╔╝██║
  ██║  ██║╚██████╔╝╚██████╔╝   ██║   ██║     ██║  ██║██║  ██║██║██║ ╚═╝ ██║
  ╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝
EOF
    echo -e "${MAGENTA}${BOLD}"
    echo "     ███████╗██╗   ██╗███╗   ███╗    ██████╗  █████╗ ███╗   ██╗███████╗██╗     "
    echo "     ██╔════╝██║   ██║████╗ ████║    ██╔══██╗██╔══██╗████╗  ██║██╔════╝██║     "
    echo "     █████╗  ██║   ██║██╔████╔██║    ██████╔╝███████║██╔██╗ ██║█████╗  ██║     "
    echo "     ██╔══╝  ╚██╗ ██╔╝██║╚██╔╝██║    ██╔═══╝ ██╔══██║██║╚██╗██║██╔══╝  ██║     "
    echo "     ██║      ╚████╔╝ ██║ ╚═╝ ██║    ██║     ██║  ██║██║ ╚████║███████╗███████╗"
    echo "     ╚═╝       ╚═══╝  ╚═╝     ╚═╝    ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝"
    echo -e "${RESET}"
    echo -e "${YELLOW}${DIM}          ══════════ [ WATERMARK: ROOTFAHIM ] ══════════${RESET}"
    echo -e "${GREEN}${BOLD}             >> FVM PANEL AUTOMATED BASH INSTALLER <<${RESET}"
    echo
}

# ───────────────────── ANIMATED TYPING ────────────────────
type_text() {
    local text="$1"
    local color="${2:-$WHITE}"
    local delay="${3:-0.02}"
    printf "%b" "$color"
    for ((i=0; i<${#text}; i++)); do
        printf "%s" "${text:$i:1}"
        sleep "$delay"
    done
    printf "%b\n" "$RESET"
}

# ─────────────────── SPINNER / LOADER ─────────────────────
spinner() {
    local pid=$1
    local msg="$2"
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local colors=("$CYAN" "$BLUE" "$MAGENTA")
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        local c="${colors[$((i % ${#colors[@]}))]}"
        printf "\r  ${c}%s${RESET}  ${WHITE}%s${RESET}   ${DIM}[ROOTFAHIM]${RESET}" \
            "${frames[$((i % ${#frames[@]}))]}" "$msg"
        i=$((i + 1))
        sleep 0.08
    done
    printf "\r  ${GREEN}✔${RESET}  ${WHITE}%s${RESET}   ${DIM}[ROOTFAHIM]${RESET}\n" "$msg"
}

# ─────────────────── STEP HEADER ──────────────────────────
step() {
    local num="$1"
    local title="$2"
    echo
    echo -e "${MAGENTA}╔══════════════════════════════════════════════════════════════╗${RESET}"
    printf "${MAGENTA}║${RESET} ${YELLOW}${BOLD}STEP %s${RESET}  ${WHITE}%s${RESET}" "$num" "$title"
    local used=$((8 + ${#num} + ${#title}))
    for ((p=used; p<62; p++)); do printf " "; done
    echo -e "${MAGENTA}║${RESET}"
    echo -e "${MAGENTA}╚══════════════════════════════════════════════════════════════╝${RESET}"
}

# ─────────────────── RUN WITH SPINNER ─────────────────────
run_spin() {
    local msg="$1"
    shift
    ( "$@" >/dev/null 2>&1 ) &
    local pid=$!
    spinner "$pid" "$msg"
    wait "$pid"
}

# ─────────────────── ERROR TRAP ───────────────────────────
on_error() {
    echo
    echo -e "${RED}╔══════════════════════════════════════════════════════════════╗${RESET}"
    echo -e "${RED}║  ✖ INSTALLATION FAILED at line $1                            ${RESET}"
    echo -e "${RED}║  Please check the error above. ROOTFAHIM                     ${RESET}"
    echo -e "${RED}╚══════════════════════════════════════════════════════════════╝${RESET}"
    exit 1
}
trap 'on_error $LINENO' ERR

# ─────────────────── ROOT CHECK ───────────────────────────
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}${BOLD}✖ This installer must be run as ROOT!${RESET}"
        echo -e "${YELLOW}  Try: ${WHITE}sudo bash installer.sh${RESET}"
        exit 1
    fi
}

# ─────────────────── OS DETECT ────────────────────────────
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        echo "$PRETTY_NAME"
    else
        echo "Unknown OS"
    fi
}

# ─────────────────── SYSTEMD SERVICE ──────────────────────
SERVICE_FILE="/etc/systemd/system/rfrs-bot.service"
SERVICE_NAME="fvm"

write_service() {
    cat > "$SERVICE_FILE" << 'EOF'
# ────────────────────────────────────────────────
#   FVM Panel Service  •  Managed by ROOTFAHIM
#   Watermark: ROOTFAHIM
# ────────────────────────────────────────────────
[Unit]
Description=FVM Panel Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/fvm.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
}

# ─────────────────── INSTALL FUNCTION ─────────────────────
install_fvm() {
    print_banner
    type_text "  >> Initializing FVM Panel installation..." "$CYAN" 0.01
    type_text "  >> Watermark: ROOTFAHIM" "$MAGENTA" 0.01
    sleep 0.5

    # STEP 1: System update + deps
    step "1/8" "Installing git..."
    export DEBIAN_FRONTEND=noninteractive
    run_spin "apt-get update" apt-get update -y
    run_spin "apt-get install git" apt-get install -y git
    type_text "  >> Git installed ✔" "$GREEN" 0.005

    # STEP 2: Clone repo
    step "2/8" "Cloning fvm-panel repository..."
    cd /root
    rm -rf fvm-panel
    run_spin "git clone RootFahim/fvm-panel" \
        git clone https://github.com/RootFahim/fvm-panel.git fvm-panel
    cd fvm-panel
    type_text "  >> Repository cloned successfully ✔" "$GREEN" 0.005

    # STEP 3: Python pip
    step "3/8" "Installing python3-pip..."
    run_spin "apt-get install python3-pip" apt-get install -y python3-pip
    type_text "  >> python3-pip installed ✔" "$GREEN" 0.005

    # STEP 4: Requirements
    step "4/8" "Installing Python requirements..."
    if [[ -f requirements.txt ]]; then
        ( pip3 install -r requirements.txt >/dev/null 2>&1 ) &
        spinner "$!" "pip install -r requirements.txt"
        type_text "  >> All Python requirements installed ✔" "$GREEN" 0.005
    else
        type_text "  !! requirements.txt not found, skipping." "$YELLOW" 0.005
    fi

    # STEP 5: Env setup
    step "5/8" "Configuring environment (.env)..."
    if [[ -f test.env ]]; then
        cp test.env .env
        type_text "  >> Copied test.env -> .env ✔" "$GREEN" 0.005
    else
        type_text "  !! test.env not found, skipping .env" "$YELLOW" 0.005
    fi

    # STEP 6: Systemd service (Aapki nayi file ke hisaab se)
    step "6/8" "Writing systemd service..."
    write_service
    type_text "  >> Service file created: $SERVICE_FILE ✔" "$GREEN" 0.005
    type_text "  >> (Watermark: ROOTFAHIM embedded)" "$DIM" 0.003

    # STEP 7: Enable + start
    step "7/8" "Reloading systemd & enabling service..."
    run_spin "systemctl daemon-reload" systemctl daemon-reload
    run_spin "systemctl enable $SERVICE_NAME" systemctl enable "$SERVICE_NAME"
    run_spin "systemctl restart $SERVICE_NAME" systemctl restart "$SERVICE_NAME"
    type_text "  >> Service enabled and started ✔" "$GREEN" 0.005

    # STEP 8: Status
    step "8/8" "Service status:"
    echo
    systemctl status "$SERVICE_NAME" --no-pager || true

    # ─── Final animated banner ───
    echo
    echo -e "${GREEN}${BOLD}"
    cat << "EOF"
   ┌──────────────────────────────────────────────────────────┐
   │                                                          │
   │      ✔  FVM PANEL INSTALLED SUCCESSFULLY!                │
   │                                                          │
   │      Service : fvm.service                               │
   │      Path    : /root/fvm-panel                           │
   │      Env     : /root/fvm-panel/.env                      │
   │                                                          │
   │      Watermark : ROOTFAHIM                               │
   │                                                          │
   └──────────────────────────────────────────────────────────┘
EOF
    echo -e "${RESET}"
    type_text "  >> Panel is now live. Enjoy! (ROOTFAHIM)" "$CYAN" 0.02
    echo
    echo -e "${DIM}  Quick commands:${RESET}"
    echo -e "    ${YELLOW}systemctl status fvm${RESET}    - check status"
    echo -e "    ${YELLOW}systemctl restart fvm${RESET}   - restart"
    echo -e "    ${YELLOW}journalctl -u fvm -f${RESET}    - live logs"
    echo
    echo -e "${MAGENTA}${BOLD}         ══════ [ ROOTFAHIM • FVM PANEL ] ══════${RESET}"
    echo
}

# ─────────────────── MAIN MENU ────────────────────────────
main_menu() {
    print_banner
    # Screenshot wala "Welcome" text hata diya gaya hai
    echo -e "  ${GREEN}[1]${RESET} Install FVM Panel (fresh setup)"
    echo -e "  ${GREEN}[2]${RESET} Update / Re-clone repository"
    echo -e "  ${GREEN}[3]${RESET} Restart service"
    echo -e "  ${GREEN}[4]${RESET} View service status"
    echo -e "  ${GREEN}[5]${RESET} Uninstall FVM Panel"
    echo -e "  ${RED}[0]${RESET} Exit"
    echo
    read -rp "  $(echo -e "${CYAN}Select [0-5]: ${RESET}")" choice

    case "$choice" in
        1) install_fvm ;;
        2)
            step "→" "Updating repository..."
            cd /root
            rm -rf fvm-panel
            run_spin "Re-cloning repo" git clone https://github.com/RootFahim/fvm-panel.git fvm-panel
            cd fvm-panel
            [[ -f requirements.txt ]] && ( pip3 install -r requirements.txt >/dev/null 2>&1 ) &
            spinner "$!" "Reinstalling requirements"
            [[ -f test.env ]] && cp test.env .env
            run_spin "Restarting service" systemctl restart "$SERVICE_NAME"
            type_text "  >> Update complete ✔ (ROOTFAHIM)" "$GREEN"
            ;;
        3)
            step "→" "Restarting service..."
            run_spin "systemctl restart $SERVICE_NAME" systemctl restart "$SERVICE_NAME"
            type_text "  >> Service restarted ✔" "$GREEN"
            ;;
        4)
            step "→" "Service status"
            systemctl status "$SERVICE_NAME" --no-pager || true
            ;;
        5)
            step "→" "Uninstalling FVM Panel..."
            read -rp "  ${RED}Are you sure? [y/N]: ${RESET}" ans
            if [[ "$ans" =~ ^[Yy]$ ]]; then
                systemctl stop "$SERVICE_NAME" 2>/dev/null || true
                systemctl disable "$SERVICE_NAME" 2>/dev/null || true
                rm -f "$SERVICE_FILE"
                rm -rf /root/fvm-panel
                systemctl daemon-reload
                type_text "  >> FVM Panel uninstalled. (ROOTFAHIM)" "$GREEN"
            else
                type_text "  >> Cancelled." "$YELLOW"
            fi
            ;;
        0|"") exit 0 ;;
        *) type_text "  !! Invalid choice!" "$RED" ;;
    esac
}

# ─────────────────── ENTRYPOINT ───────────────────────────
check_root
type_text "  Detected OS: $(detect_os)" "$DIM" 0.005
sleep 0.4

if [[ "${1:-}" == "--auto" ]]; then
    install_fvm
else
    main_menu
fi
