# Deploying Ampersand to a VPS

This is a minimal runbook for getting `ampersand-server` running on a single
Linux box (DigitalOcean droplet, Hetzner CX, etc.). The shape:

```
internet  →  Caddy :443 (TLS)  →  uvicorn 127.0.0.1:8765  →  vault on disk
                                                              ~/.../var/lib/ampersand/vault
```

Admin work happens via SSH + the `ampersand-admin` CLI, not over HTTP.

## Prerequisites

- A fresh Ubuntu 22.04+ droplet
- Root SSH access
- (Recommended for production) A domain name with an A record pointing at the droplet

## 1. Lock the box down

Before anything else:

```bash
ssh root@YOUR_IP
adduser you                          # create yourself a user, set a password
usermod -aG sudo you
mkdir /home/you/.ssh
# paste your public key into /home/you/.ssh/authorized_keys
chmod 700 /home/you/.ssh
chmod 600 /home/you/.ssh/authorized_keys
chown -R you:you /home/you/.ssh
```

Edit `/etc/ssh/sshd_config` → `PermitRootLogin no` and `PasswordAuthentication no`,
then `systemctl reload ssh`. Keep the root session open until you confirm you can
log in as `you` with `sudo` from a second terminal.

## 2. Clone the repo + run bootstrap

```bash
sudo git clone <repo-url> /opt/ampersand
cd /opt/ampersand/ampersand-core
sudo bash deploy/bootstrap.sh
```

This will:

- `apt install` python, git, ufw, Caddy
- Create the `ampersand` system user
- Create `/var/lib/ampersand/vault`
- Generate a fresh API key into `/etc/ampersand/env` (mode 0640, root:ampersand)
- Build a venv at `/opt/ampersand/venv` and install both packages
- Install the systemd unit
- Open ports 22/80/443 in UFW

The script **prints the new API key once** during run. Store it in your password
manager immediately. You can rotate it later with `sudo -u ampersand ampersand-admin rotate-key --env-file /etc/ampersand/env`.

## 3. Pick a Caddyfile

### Quick proof-of-life (no domain yet, HTTP only)

⚠️ Do this only for the initial smoke test. Bearer tokens travel in cleartext.

```bash
sudo cp /opt/ampersand/ampersand-core/deploy/Caddyfile.no-tls /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl start ampersand-server

curl -s http://YOUR_IP/health
# → {"status":"ok"}

curl -s -H "Authorization: Bearer <YOUR_KEY>" http://YOUR_IP/vault
# → {"items":[],"next_cursor":null}
```

### Production (domain + TLS)

After pointing an A record at the droplet:

```bash
sudo cp /opt/ampersand/ampersand-core/deploy/Caddyfile.tls /etc/caddy/Caddyfile
sudo $EDITOR /etc/caddy/Caddyfile        # replace YOUR_DOMAIN_HERE
sudo systemctl reload caddy
sudo systemctl start ampersand-server
```

Caddy auto-provisions a Let's Encrypt cert on first request (takes 10-30s).

```bash
curl -s https://vault.example.com/health
curl -s -H "Authorization: Bearer <KEY>" https://vault.example.com/vault
```

## 4. Smoke + admin

```bash
# server logs
sudo journalctl -u ampersand-server -f

# admin CLI (run as the service user, not root)
sudo -u ampersand /opt/ampersand/venv/bin/ampersand-admin stats
sudo -u ampersand /opt/ampersand/venv/bin/ampersand-admin integrity --deep
sudo -u ampersand /opt/ampersand/venv/bin/ampersand-admin backup /tmp/vault.tar.gz
```

## 5. Backups (optional but recommended)

The simplest backup: nightly tar to off-box storage. Cron entry as root:

```cron
0 3 * * * sudo -u ampersand /opt/ampersand/venv/bin/ampersand-admin backup - \
          | aws s3 cp - s3://my-bucket/ampersand/$(date +\%F).tar.gz
```

Or pipe to a remote box via SSH:

```cron
0 3 * * * sudo -u ampersand /opt/ampersand/venv/bin/ampersand-admin backup - \
          | ssh backup-host 'cat > /backups/ampersand-$(date +\%F).tar.gz'
```

## Common operations

| Task | Command |
| --- | --- |
| Check status | `sudo systemctl status ampersand-server` |
| Restart server | `sudo systemctl restart ampersand-server` |
| Tail logs | `sudo journalctl -u ampersand-server -f` |
| Rotate key | `sudo -u ampersand /opt/ampersand/venv/bin/ampersand-admin rotate-key --env-file /etc/ampersand/env`, then `sudo systemctl restart ampersand-server` |
| Pull new code | `cd /opt/ampersand && sudo -u ampersand git pull && sudo -u ampersand /opt/ampersand/venv/bin/pip install -e ./ampersand-core && sudo systemctl restart ampersand-server` |
| Re-enable Swagger temporarily | `sudo $EDITOR /etc/ampersand/env` → add `AMPERSAND_PUBLIC_DOCS=1` → `sudo systemctl restart ampersand-server`. Remove when done. |

## What's NOT in this deploy

- **Per-user identity.** Single shared API key. Multi-tenant requires app-level changes.
- **Multi-host / HA.** One box, one process. Resize the droplet vertically when needed.
- **Server-side email fetch.** Stays on the local CLI for now (creds shouldn't live in plain env on a public box; design pending).
- **Search.** BM25 + vector indexer is a separate plan; the change-hook in `MarkdownStore` is the seam it'll plug into.
