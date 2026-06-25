#!/usr/bin/env bash
# scripts/deploy.sh
# Deploys SunBlockCore-LL on an Ubuntu server on port 3707.
# Run from the project root: bash scripts/deploy.sh

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}→${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}!${NC} $*"; }
die()     { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

PORT=3707
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="$(whoami)"
VENV="$PROJECT_DIR/.venv"
SERVICE_NAME="sunblock"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  SunBlockCore-LL Deployment Script   ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""
info "Project root : $PROJECT_DIR"
info "Deploy user  : $DEPLOY_USER"
info "Port         : $PORT"
echo ""

# ── Checks ───────────────────────────────────────────────────────────────────
[[ "$EUID" -eq 0 ]] && die "Do not run as root. Run as the service user with sudo access."
command -v sudo &>/dev/null || die "sudo not found."

# ── 1. System packages ───────────────────────────────────────────────────────
info "Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip curl git
success "System packages ready."

# ── 2. Python virtual environment ────────────────────────────────────────────
info "Setting up Python virtual environment..."
if [[ ! -d "$VENV" ]]; then
    python3 -m venv "$VENV"
    success "Virtual environment created."
else
    success "Virtual environment already exists, skipping."
fi

info "Installing Python dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
    fastapi \
    uvicorn \
    python-socketio \
    epevermodbus \
    python-dotenv \
    "python-jose[cryptography]" \
    bcrypt \
    slowapi \
    jinja2 \
    openpyxl
success "Python dependencies installed."

# ── 3. Vendor frontend assets ────────────────────────────────────────────────
info "Vendoring frontend dependencies..."
bash "$PROJECT_DIR/scripts/vendor.sh"
success "Frontend assets vendored."

# ── 4. Configure .env ────────────────────────────────────────────────────────
ENV_FILE="$PROJECT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists. Skipping interactive config."
    warn "Edit $ENV_FILE manually if changes are needed."
else
    info "Configuring .env..."
    cp "$PROJECT_DIR/sample.env" "$ENV_FILE"

    # Controller
    read -rp "  Controller port [/dev/ttyACM0]: " CTRL_PORT
    CTRL_PORT="${CTRL_PORT:-/dev/ttyACM0}"

    read -rp "  Controller slave ID [1]: " CTRL_SLAVE
    CTRL_SLAVE="${CTRL_SLAVE:-1}"

    # Data directory
    DEFAULT_DATA_DIR="/home/$DEPLOY_USER/SunblockData/"
    read -rp "  Data directory [$DEFAULT_DATA_DIR]: " DATA_DIR
    DATA_DIR="${DATA_DIR:-$DEFAULT_DATA_DIR}"
    mkdir -p "$DATA_DIR"

    # Admin username
    read -rp "  Admin username [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"

    # Admin password (hashed)
    while true; do
        read -rsp "  Admin password: " ADMIN_PASS; echo
        read -rsp "  Confirm password: " ADMIN_PASS2; echo
        [[ "$ADMIN_PASS" == "$ADMIN_PASS2" ]] && break
        warn "Passwords do not match. Try again."
    done
    ADMIN_HASH=$(printf '%s' "$ADMIN_PASS" | "$VENV/bin/python3" -c \
        "import bcrypt, sys; pw = sys.stdin.buffer.read(); print(bcrypt.hashpw(pw, bcrypt.gensalt()).decode())")

    # Secret key
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    # Admin path — random slug, kept out of public-facing links
    ADMIN_PATH=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")

    # Write .env
    cat > "$ENV_FILE" <<EOF
CONTROLLER_PORT=$CTRL_PORT
CONTROLLER_SLAVE=$CTRL_SLAVE

DATA_DIRECTORY=$DATA_DIR

DATA_MAN=true
READ_INTERVAL=1
PORT=$PORT

ADMIN_USERNAME=$ADMIN_USER
ADMIN_PASSWORD_HASH=$ADMIN_HASH
SECRET_KEY=$SECRET_KEY
TOKEN_EXPIRE_HOURS=24
SECURE_COOKIES=false
ADMIN_PATH=$ADMIN_PATH
EOF
    chmod 600 "$ENV_FILE"
    success ".env written and permissions set to 600."
fi

# ── 5. Passwordless sudo for powerprofilesctl + RAPL power-draw reads ─────────
# The cat entry is for hardware.py's direct read of Intel RAPL powercap
# energy_uj counters (CPUPowerDraw) — those files are root-only on most distros.
# hardware.py tries a direct read first and only falls back to this sudo `cat`
# if that fails, so this entry is harmless (just unused) on non-Intel hardware.
#
# IMPORTANT: this intentionally lists exact literal paths, NOT a wildcard like
# /sys/devices/virtual/powercap/*/energy_uj. In sudoers, wildcards inside a
# command *argument* (as opposed to the command path itself) match across `/`
# — see `man sudoers` "Wildcards in command arguments". A wildcarded rule here
# would let the deploy user run e.g.
#   sudo cat /sys/devices/virtual/powercap/../../../../tmp/evil/energy_uj
# where tmp/evil/energy_uj is an attacker-created symlink to /etc/shadow —
# cat follows symlinks, so this would be a local root-read-any-file bug.
# Enumerating the real paths at deploy time (they're fixed by the hardware,
# discovered the same way hardware.py's own _scan_power_caps() does) avoids
# wildcards entirely, so no such traversal is possible.
RAPL_PATHS=$("$VENV/bin/python3" -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
import hardware
for p in hardware._scan_power_caps():
    print(p)
" 2>/dev/null)

SUDOERS_FILE="/etc/sudoers.d/sunblock"
if [[ ! -f "$SUDOERS_FILE" ]]; then
    info "Configuring passwordless sudo for powerprofilesctl + RAPL power reads..."
    {
        echo "$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/powerprofilesctl"
        if [[ -n "$RAPL_PATHS" ]]; then
            CAT_CMDS=""
            while IFS= read -r p; do
                [[ -z "$p" ]] && continue
                CAT_CMDS+="/usr/bin/cat $p, "
            done <<< "$RAPL_PATHS"
            echo "$DEPLOY_USER ALL=(ALL) NOPASSWD: ${CAT_CMDS%, }"
        else
            warn "No Intel RAPL power counters found on this machine — skipping the cat sudoers entry (CPUPowerDraw will report 0)."
        fi
    } | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 440 "$SUDOERS_FILE"
    success "Sudoers entry written to $SUDOERS_FILE."
else
    success "Sudoers entry already exists, skipping."
fi

# ── 6. systemd service ────────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
info "Installing systemd service..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=SunBlockCore-LL Admin Server
After=network.target

[Service]
Type=simple
User=$DEPLOY_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

sleep 2

if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    success "Service $SERVICE_NAME is running."
else
    die "Service failed to start. Check logs: journalctl -u $SERVICE_NAME -n 50"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Deployment complete!           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
# Read ADMIN_PATH from the written .env for the final summary
_ADMIN_PATH=$(grep '^ADMIN_PATH=' "$ENV_FILE" | cut -d= -f2)
_HOST=$(hostname -I | awk '{print $1}')

echo -e "  Live view   : ${CYAN}http://$_HOST:$PORT${NC}"
echo -e "  Admin login : ${CYAN}http://$_HOST:$PORT/$_ADMIN_PATH${NC}  ${YELLOW}(keep this private)${NC}"
echo -e "  Service     : ${CYAN}sudo systemctl status $SERVICE_NAME${NC}"
echo -e "  Logs        : ${CYAN}journalctl -u $SERVICE_NAME -f${NC}"
echo -e "  Config      : ${CYAN}$ENV_FILE${NC}"
echo ""
