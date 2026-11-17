#!/usr/bin/env bash
# ============================================================
#   FVM PANEL AUTO INSTALLER (FIXED VERSION)
#   Author  : ROOTFAHIM
#   Version : 2.1 (Venv Support)
#   GitHub  : https://github.com/RootFahim
# ============================================================
#   WATERMARK : ROOTFAHIM
# ============================================================

set -euo pipefail

# ---------- COLORS ----------
R='\033[0;31m'
G='\033[0;32m'
Y='\033[1;33m'
B='\033[0;34m'
M='\033[0;35m'
C='\033[0;36m'
W='\033[1;37m'
DIM='\033[2m'
NC='\033[0m'

# ---------- CONFIG ----------
REPO_URL="https://github.com/RootFahim/fvm-panel.git"
INSTALL_DIR="/root/fvm-panel"
SERVICE_NAME="fvm"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PY_SCRIPT="/root/fvm.py"
LOG_FILE="/var/log/rootfahim-installer.log"
VENV_DIR="$INSTALL_DIR/venv"

# ---------- HELPERS ----------
log()  { echo -e "${C}[ROOTFAHIM]${NC} $*" | tee -a "$LOG_FILE"; }
ok()   { echo -e "${G}✔ ROOTFAHIM ✔${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${Y}⚠ ROOTFAHIM ⚠${NC} $*" | tee -a "$LOG_FILE"; }
err()  { echo -e "${R}✘ ROOTFAHIM ✘${NC} $*" | tee -a "$LOG_FILE"; }

# ---------- ROOT CHECK ----------
if [[ $EUID -ne 0 ]]; then
  err "Root access required. Please run: sudo bash installer.sh"
  exit 1
fi

# ---------- ASCII ART ----------
show_banner() {
  clear
  echo -e "${M}"
  cat << "EOF"
   ██████╗  ██████╗  ██████╗ ████████╗███████╗ █████╗ ██╗  ██╗██╗███╗   ███╗
   ██╔══██╗██╔═══██╗██╔═══██╗╚══██╔══╝██╔════╝██╔══██╗██║  ██║██║████╗ ████║
   ██████╔╝██║   ██║██║   ██║   ██║   █████╗  ███████║███████║██║██╔████╔██║
   ██╔══██╗██║   ██║██║   ██║   ██║   ██╔══╝  ██╔══██║██╔══██║██║██║╚██╔╝██║
   ██║  ██║╚██████╔╝╚██████╔╝   ██║   ██║     ██║  ██║██║  ██║██║██║ ╚═╝ ██║
   ╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝
EOF
  echo -e "${NC}"
  echo -e "${C}┌──────────────────────────────────────────────────────────────────┐${NC}"
  echo -e "${C}│${W}         🔥  FVM PANEL — AUTO INSTALLER v2.1  🔥              ${C}│${NC}"
  echo -e "${C}│${DIM}              Powered by: ${M}ROOTFAHIM${DIM}  |  Watermarked Build      ${C}│${NC}"
  echo -e "${C}└──────────────────────────────────────────────────────────────────┘${NC}"
  echo ""
}

# ---------- WATERMARK ----------
watermark() {
  echo -e "${DIM}${M}──────────────────── [ ROOTFAHIM ] ────────────────────${NC}"
}

# ---------- SPINNER ----------
spinner() {
  local pid=$1
  local delay=0.1
  local spinstr='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
  local msg="${2:-Working}"
  while ps -p "$pid" > /dev/null 2>&1; do
    local temp=${spinstr#?}
    printf " ${C}[%c]${NC} ${W}%s...${NC}  ${DIM}[ROOTFAHIM]${NC}\r" "$spinstr" "$msg"
    local spinstr=$temp${spinstr%"$temp"}
    sleep $delay
  done
  printf "                                                                      \r"
}

# ---------- TYPEWRITER ----------
typewriter() {
  local text="$1"
  local delay="${2:-0.02}"
  for (( i=0; i<${#text}; i++ )); do
    printf "%s" "${text:$i:1}"
    sleep "$delay"
  done
  echo
}

# ---------- STEP HEADER ----------
step() {
  echo ""
  echo -e "${B}╔════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${B}║${W}  ▶ STEP $1: $2${NC}"
  echo -e "${B}║${DIM}  [ROOTFAHIM]${NC}"
  echo -e "${B}╚════════════════════════════════════════════════════════════════╝${NC}"
}

# ---------- RUN WITH SPINNER ----------
run_step() {
  local msg="$1"; shift
  "$@" >> "$LOG_FILE" 2>&1 &
  local pid=$!
  spinner "$pid" "$msg"
  wait "$pid" || { 
    err "Failed: $msg"
    echo -e "${R}--- Last 10 lines of log ---${NC}"
    tail -n 10 "$LOG_FILE" | sed 's/^/  /'
    echo -e "${R}----------------------------${NC}"
    exit 1
  }
  ok "$msg — Done"
}

# ============================================================
#                        MAIN INSTALL
# ============================================================
main() {
  show_banner
  typewriter "   Starting installation... Please wait 🔧" 0.03
  echo ""
  watermark
  sleep 1

  # ---------- STEP 1: Update & Prereqs ----------
  step "1/7" "Updating System & Installing Prerequisites"
  run_step "Updating package lists" bash -c "apt-get update -y"
  run_step "Installing git, python3-pip, python3-venv, nano, curl" bash -c "apt-get install -y git python3-pip python3-venv nano curl"
  ok "Prerequisites installed"

  # ---------- STEP 2: Clone Repo ----------
  step "2/7" "Cloning FVM Panel Repository"
  if [[ -d "$INSTALL_DIR" ]]; then
    warn "Directory already exists — pulling latest..."
    run_step "Pulling latest changes" bash -c "cd $INSTALL_DIR && git pull"
  else
    run_step "Cloning from $REPO_URL" git clone "$REPO_URL" "$INSTALL_DIR"
  fi
  ok "Repository ready"

  # ---------- STEP 3: Install Python deps (VENV FIX) ----------
  step "3/7" "Installing Python Requirements (Virtual Environment)"
  if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
    # Create venv if not exists
    if [[ ! -d "$VENV_DIR" ]]; then
      run_step "Creating virtual environment" python3 -m venv "$VENV_DIR"
    else
      warn "Virtual environment already exists — skipping creation"
    fi
    
    # Install using venv pip
    run_step "Installing requirements" bash -c "$VENV_DIR/bin/pip install --upgrade pip && $VENV_DIR/bin/pip install -r $INSTALL_DIR/requirements.txt"
    ok "Python dependencies installed in venv"
  else
    warn "requirements.txt not found — skipping dependencies"
  fi

  # ---------- STEP 4: Setup .env ----------
  step "4/7" "Configuring Environment"
  if [[ -f "$INSTALL_DIR/test.env" ]]; then
    cp -f "$INSTALL_DIR/test.env" "$INSTALL_DIR/.env"
    ok ".env created from test.env"
    echo ""
    warn "Please edit the .env file with your credentials:"
    echo -e "   ${Y}nano $INSTALL_DIR/.env${NC}"
  else
    warn "test.env not found — please create .env manually"
  fi

  # ---------- STEP 5: Move python script ----------
  step "5/7" "Preparing Python Entrypoint"
  if [[ -f "$INSTALL_DIR/fvm.py" ]]; then
    cp -f "$INSTALL_DIR/fvm.py" "$PY_SCRIPT"
    ok "Copied fvm.py to $PY_SCRIPT"
  elif [[ -f "$INSTALL_DIR/main.py" ]]; then
    cp -f "$INSTALL_DIR/main.py" "$PY_SCRIPT"
    ok "Copied main.py to $PY_SCRIPT"
  else
    warn "No fvm.py or main.py found — service may fail"
  fi

  # ---------- STEP 6: Systemd service ----------
  step "6/7" "Creating Systemd Service"
  cat > "$SERVICE_FILE" <<EOF
# ============================================================
#   FVM PANEL SERVICE  —  Powered by ROOTFAHIM
# ============================================================
[Unit]
Description=FVM Panel Service | ROOTFAHIM
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 $PY_SCRIPT
Restart=always
RestartSec=5
User=root
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
# ============================================================
#   [ ROOTFAHIM WATERMARK ]  —  Do not remove
# ============================================================
EOF
  ok "Service file created at $SERVICE_FILE"

  # ---------- STEP 7: Enable & Start ----------
  step "7/7" "Starting FVM Panel Service"
  run_step "Reloading systemd daemon" systemctl daemon-reload
  run_step "Enabling $SERVICE_NAME on boot" systemctl enable "$SERVICE_NAME"
  run_step "Restarting $SERVICE_NAME" systemctl restart "$SERVICE_NAME"
  sleep 3

  # ---------- FINAL STATUS ----------
  echo ""
  watermark
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo -e "${G}"
    cat << "EOF"
   ╔═══════════════════════════════════════════════════════════╗
   ║   ✅  INSTALLATION COMPLETE — FVM PANEL IS RUNNING  ✅    ║
   ╚═══════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
    echo -e "${W}📋 Service Status:${NC}"
    systemctl status "$SERVICE_NAME" --no-pager -l | head -n 12
    echo ""
    echo -e "${C}🔧 Useful commands:${NC}"
    echo -e "   ${Y}systemctl status $SERVICE_NAME${NC}    — Check status"
    echo -e "   ${Y}systemctl restart $SERVICE_NAME${NC}   — Restart panel"
    echo -e "   ${Y}systemctl stop $SERVICE_NAME${NC}      — Stop panel"
    echo -e "   ${Y}journalctl -u $SERVICE_NAME -f${NC}    — Live logs"
    echo -e "   ${Y}nano $INSTALL_DIR/.env${NC}            — Edit config"
  else
    err "Service failed to start. Check logs:"
    echo -e "   ${Y}journalctl -u $SERVICE_NAME -n 50${NC}"
    echo -e "   ${Y}tail -n 20 /var/log/rootfahim-installer.log${NC}"
  fi
  echo ""
  watermark
  echo -e "${M}${DIM}   Copyright © ROOTFAHIM  |  GitHub: https://github.com/RootFahim${NC}"
  watermark
  echo ""
}

# ---------- TRAP ----------
trap 'err "Installation interrupted by user"; exit 130' INT TERM

main "$@"
