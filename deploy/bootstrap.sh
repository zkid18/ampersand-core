#!/usr/bin/env bash
# bootstrap.sh — idempotent first-time setup for an Ampersand vault droplet.
#
# Run as root on a fresh Ubuntu 22.04+ box, AFTER you've cloned the repo to
# /opt/ampersand. Re-running is safe: every step checks before doing.
#
#   git clone <repo-url> /opt/ampersand
#   cd /opt/ampersand/ampersand-core
#   sudo bash deploy/bootstrap.sh
#
# Read the script before running. It installs packages, creates a system user,
# generates a secret, and installs a systemd unit.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/ampersand}"
SERVICE_USER="ampersand"
DATA_DIR="/var/lib/ampersand/vault"
ENV_FILE="/etc/ampersand/env"
VENV_DIR="${REPO_ROOT}/venv"
PORT="${PORT:-8765}"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (try: sudo bash $0)" >&2
    exit 1
fi

echo "==> Updating apt and installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ufw \
    debian-keyring debian-archive-keyring apt-transport-https

echo "==> Installing Caddy (if absent)"
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy
fi

echo "==> Creating service user '${SERVICE_USER}' (if absent)"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin --home-dir /var/lib/ampersand "$SERVICE_USER"
fi

echo "==> Preparing data dir ${DATA_DIR}"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$(dirname "$DATA_DIR")"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"

echo "==> Preparing env file ${ENV_FILE}"
# Dir must be group-traversable by the service user so `ampersand-admin` can read it.
install -d -m 0750 -o root -g "$SERVICE_USER" /etc/ampersand
if [ ! -f "$ENV_FILE" ]; then
    KEY="$(openssl rand -hex 32)"
    cat > "$ENV_FILE" <<EOF
AMPERSAND_API_KEY=${KEY}
AMPERSAND_DATA_DIR=${DATA_DIR}
EOF
    chmod 0640 "$ENV_FILE"
    chown root:"$SERVICE_USER" "$ENV_FILE"
    echo
    echo "    NEW API KEY GENERATED — store this somewhere safe NOW."
    echo "    AMPERSAND_API_KEY=${KEY}"
    echo
else
    echo "    env file already exists, leaving it alone"
fi

echo "==> Building venv at ${VENV_DIR}"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "${REPO_ROOT}/ampersand-core"

echo "==> Setting ownership on ${REPO_ROOT}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${REPO_ROOT}"

echo "==> Installing systemd unit"
install -m 0644 "${REPO_ROOT}/ampersand-core/deploy/systemd/ampersand-server.service" \
    /etc/systemd/system/ampersand-server.service
systemctl daemon-reload
systemctl enable ampersand-server

echo "==> Configuring firewall (UFW)"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status verbose

echo
echo "✓ Bootstrap complete. Next steps:"
echo
echo "  1. Pick a Caddyfile:"
echo "       sudo cp ${REPO_ROOT}/ampersand-core/deploy/Caddyfile.no-tls /etc/caddy/Caddyfile   # http only, for testing"
echo "       # OR after pointing a domain at this box:"
echo "       sudo cp ${REPO_ROOT}/ampersand-core/deploy/Caddyfile.tls /etc/caddy/Caddyfile"
echo "       sudo \$EDITOR /etc/caddy/Caddyfile     # replace YOUR_DOMAIN_HERE"
echo
echo "  2. Reload Caddy:    sudo systemctl reload caddy"
echo "  3. Start ampersand: sudo systemctl start ampersand-server"
echo "  4. Verify:          sudo journalctl -u ampersand-server -n 20"
echo "                      curl -s http://127.0.0.1:${PORT}/health"
echo
