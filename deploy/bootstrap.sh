#!/usr/bin/env bash
# bootstrap.sh — one-command setup for an Ampersand vault droplet.
#
# Run as root on a fresh Ubuntu 22.04+ box. Re-running is safe — every step
# checks before doing.
#
#   git clone https://github.com/zkid18/ampersand-core /opt/ampersand/ampersand-core
#   cd /opt/ampersand/ampersand-core
#   sudo bash deploy/bootstrap.sh
#
# What this does (in order):
#   1. apt install python3, caddy, ufw, etc.
#   2. Create the `ampersand` system user.
#   3. Generate AMPERSAND_API_KEY (or keep existing).
#   4. Optionally prompt for OPENAI_API_KEY (Enter to skip).
#   5. Clone the ampersand CLI from GitHub (if absent).
#   6. Build venv, install ampersand-core + ampersand CLI editable.
#   7. Install + enable systemd units (server, vault-watcher, feed-sync.timer).
#      Email-watch unit is installed but stays disabled until you run
#      `sudo -u ampersand ampersand email setup` to configure IMAP creds.
#   8. Configure Caddy (http-only by default; bundled Caddyfile.tls for later).
#   9. Open ports 22/80/443 in UFW.
#  10. Start ampersand-server, wait for /health, print summary.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/ampersand}"
CORE_REPO="${REPO_ROOT}/ampersand-core"
CLI_REPO="${REPO_ROOT}/ampersand"
SERVICE_USER="ampersand"
DATA_DIR="/var/lib/ampersand/vault"
ENV_FILE="/etc/ampersand/env"
VENV_DIR="${REPO_ROOT}/venv"
PORT="${PORT:-8765}"
CLI_REPO_URL="${CLI_REPO_URL:-https://github.com/zkid18/ampersand.git}"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (try: sudo bash $0)" >&2
    exit 1
fi

# ── 1. apt + caddy ─────────────────────────────────────────────────

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

# ── 2. service user ────────────────────────────────────────────────

echo "==> Creating service user '${SERVICE_USER}' (if absent)"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin \
        --home-dir /var/lib/ampersand "$SERVICE_USER"
fi

# ── 3. data dir ────────────────────────────────────────────────────

echo "==> Preparing data dir ${DATA_DIR}"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$(dirname "$DATA_DIR")"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"

# ── 4. env file (AMPERSAND_API_KEY + optional OPENAI_API_KEY) ──────

echo "==> Preparing env file ${ENV_FILE}"
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

# Prompt for OPENAI_API_KEY if not set. Skipping is fine — semantic search,
# hybrid search, rerank, /chat, and audio-fallback transcription will all
# respond 503 until you set it. The README has the cost breakdown.
if ! grep -q '^OPENAI_API_KEY=' "$ENV_FILE"; then
    echo
    echo "==> OpenAI API key (optional — enables /chat, semantic + hybrid search, rerank)"
    echo "    Without it, those endpoints return 503. BM25 search via /vault/search still works."
    echo "    Paste your key now, or press Enter to skip and add later by editing ${ENV_FILE}."
    if [ -t 0 ]; then
        # Interactive run — actually prompt.
        printf "    OPENAI_API_KEY (sk-...): "
        read -r OPENAI_KEY_INPUT
        if [ -n "${OPENAI_KEY_INPUT:-}" ]; then
            printf '\nOPENAI_API_KEY=%s\n' "$OPENAI_KEY_INPUT" >> "$ENV_FILE"
            echo "    OPENAI_API_KEY added to ${ENV_FILE}."
        else
            echo "    Skipped — /chat + semantic + hybrid will 503 until you set it."
        fi
    else
        # Non-interactive (e.g. piped install) — skip silently.
        echo "    non-interactive run — skipped. Add OPENAI_API_KEY to ${ENV_FILE} later."
    fi
fi

# ── 5. clone CLI repo ──────────────────────────────────────────────

echo "==> Fetching ampersand CLI from ${CLI_REPO_URL}"
if [ ! -d "$CLI_REPO/.git" ]; then
    git clone --quiet "$CLI_REPO_URL" "$CLI_REPO"
else
    echo "    CLI repo already present at ${CLI_REPO}"
fi

# ── 6. venv + editable installs ────────────────────────────────────

echo "==> Building venv at ${VENV_DIR}"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "$CORE_REPO"
"${VENV_DIR}/bin/pip" install --quiet -e "$CLI_REPO"

echo "==> Setting ownership on ${REPO_ROOT}"
chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${REPO_ROOT}"

# ── 7. systemd units ───────────────────────────────────────────────

echo "==> Installing systemd units"
for unit in \
    ampersand-server.service \
    ampersand-vault-watcher.service \
    ampersand-feed-sync.service \
    ampersand-feed-sync.timer \
    ampersand-email-watch.service
do
    install -m 0644 "${CORE_REPO}/deploy/systemd/${unit}" "/etc/systemd/system/${unit}"
done
systemctl daemon-reload

echo "==> Enabling units (server, vault-watcher, feed-sync.timer)"
systemctl enable ampersand-server.service
systemctl enable ampersand-vault-watcher.service
systemctl enable ampersand-feed-sync.timer

# Email-watch deliberately left disabled — needs IMAP creds first.
# User enables manually after running `ampersand email setup`.

# ── 8. caddy ───────────────────────────────────────────────────────

echo "==> Configuring Caddy (HTTP-only default)"
if [ ! -f /etc/caddy/Caddyfile.bootstrap-backup ] && [ -f /etc/caddy/Caddyfile ]; then
    cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bootstrap-backup
fi
install -m 0644 "${CORE_REPO}/deploy/Caddyfile.no-tls" /etc/caddy/Caddyfile
systemctl reload caddy || systemctl start caddy

echo "    Caddyfile.tls (with auto-HTTPS) is at ${CORE_REPO}/deploy/Caddyfile.tls"
echo "    Swap it in after pointing a domain at this box."

# ── 9. firewall ────────────────────────────────────────────────────

echo "==> Configuring firewall (UFW)"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# ── 10. start + verify ─────────────────────────────────────────────

echo "==> Starting ampersand-server"
systemctl restart ampersand-server.service
systemctl start ampersand-vault-watcher.service
systemctl start ampersand-feed-sync.timer

echo "==> Waiting for /health (up to 30s)"
for i in $(seq 1 30); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/health"; then
        echo "    healthy after ${i}s"
        break
    fi
    sleep 1
    if [ "$i" = "30" ]; then
        echo "    /health didn't come up in 30s — check: sudo journalctl -u ampersand-server -n 50"
        exit 1
    fi
done

echo
echo "✓ Bootstrap complete."
echo
echo "  Public URL:  http://$(curl -fsS https://ipv4.icanhazip.com 2>/dev/null || echo "this-droplet"):80/health"
echo "  Local:       curl -s http://127.0.0.1:${PORT}/health"
echo "  Logs:        sudo journalctl -u ampersand-server -f"
echo "  Admin:       sudo -u ${SERVICE_USER} ${VENV_DIR}/bin/ampersand-admin stats"
echo
echo "  Optional:"
echo "    Email watcher (newsletters → vault):"
echo "      sudo -u ${SERVICE_USER} ${VENV_DIR}/bin/ampersand email setup"
echo "      sudo systemctl enable --now ampersand-email-watch"
echo
echo "    TLS / domain (when you have one):"
echo "      sudo cp ${CORE_REPO}/deploy/Caddyfile.tls /etc/caddy/Caddyfile"
echo "      sudo \$EDITOR /etc/caddy/Caddyfile     # replace YOUR_DOMAIN_HERE"
echo "      sudo systemctl reload caddy"
echo
