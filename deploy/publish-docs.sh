#!/usr/bin/env bash
# publish-docs.sh — build the Mintlify docs and publish them to the droplet.
#
# Run this from your Mac, not from the box. Re-running is safe; the new build
# is staged beside the live one and swapped in only after it unpacks cleanly.
#
#   export AMPERSTAND_DOCS_HOST=your.droplet.ip
#   ./deploy/publish-docs.sh                      # publish origin/main
#   ./deploy/publish-docs.sh --ref HEAD           # publish your working branch
#   ./deploy/publish-docs.sh --host 10.0.0.5      # override the host once
#
# What this does (in order):
#   1. Check out REF into a throwaway worktree, so your working tree is untouched.
#   2. `mintlify validate` (strict) — refuses to publish a build with warnings.
#   3. `mintlify export` — a static Next.js site, ~60 MB unpacked.
#   4. scp the zip up, unpack to a staging dir, swap it into place.
#
# The exported site uses root-absolute asset paths (/_next/...), which is why
# it gets its own port instead of living under a subpath of the API on :80.
# Caddy serves it straight off disk — see deploy/Caddyfile.docs.

set -euo pipefail

REF="origin/main"
HOST="${AMPERSTAND_DOCS_HOST:-}"
SSH_USER="${AMPERSTAND_DOCS_USER:-root}"
WEBROOT="/var/www/amperstand-docs"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)  REF="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --user) SSH_USER="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$HOST" ]]; then
    echo "error: no target host. Pass --host, or set AMPERSTAND_DOCS_HOST." >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE="$(mktemp -d)/docs-build"
ZIP="$(mktemp -d)/amperstand-docs.zip"
REMOTE="${SSH_USER}@${HOST}"

cleanup() {
    git -C "$REPO_ROOT" worktree remove --force "$WORKTREE" 2>/dev/null || true
    rm -rf "$(dirname "$WORKTREE")" "$(dirname "$ZIP")"
}
trap cleanup EXIT

# The Mintlify CLI refuses to run on Node 25+. Prefer an LTS from nvm if the
# active node is too new, rather than making the caller manage versions.
node_major() { node --version 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/'; }
if [[ "$(node_major)" -ge 25 ]] 2>/dev/null; then
    if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
        # shellcheck disable=SC1091
        . "$HOME/.nvm/nvm.sh"
        nvm use --lts >/dev/null 2>&1 || nvm use 22 >/dev/null 2>&1 || true
    fi
    if [[ "$(node_major)" -ge 25 ]] 2>/dev/null; then
        echo "error: mintlify needs Node < 25; active is $(node --version)" >&2
        exit 1
    fi
fi

echo "==> building docs from ${REF} (node $(node --version))"
git -C "$REPO_ROOT" fetch --quiet origin 2>/dev/null || true
git -C "$REPO_ROOT" worktree add --detach "$WORKTREE" "$REF" >/dev/null

cd "$WORKTREE/docs"
mintlify validate
mintlify export --output "$ZIP"

echo "==> uploading to ${HOST}"
scp -q "$ZIP" "${REMOTE}:/tmp/amperstand-docs.zip"

echo "==> swapping in"
ssh "$REMOTE" WEBROOT="$WEBROOT" 'bash -s' <<'REMOTE_SCRIPT'
set -euo pipefail
command -v unzip >/dev/null || apt-get install -y unzip >/dev/null

rm -rf "${WEBROOT}.new"
mkdir -p "${WEBROOT}.new"
unzip -q /tmp/amperstand-docs.zip -d "${WEBROOT}.new"

# Helpers for viewing the export offline; dead weight behind a web server.
rm -f "${WEBROOT}.new/Start Docs.command" "${WEBROOT}.new/Start Docs.bat" "${WEBROOT}.new/serve.js"

test -f "${WEBROOT}.new/index.html" || { echo "export has no index.html — refusing to swap" >&2; exit 1; }

chown -R www-data:www-data "${WEBROOT}.new"
chmod -R a+rX "${WEBROOT}.new"

rm -rf "${WEBROOT}.old"
[ -d "$WEBROOT" ] && mv "$WEBROOT" "${WEBROOT}.old"
mv "${WEBROOT}.new" "$WEBROOT"
rm -rf "${WEBROOT}.old" /tmp/amperstand-docs.zip

echo "    $(find "$WEBROOT" -type f | wc -l) files live"
REMOTE_SCRIPT

code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "http://${HOST}:8080/")
echo "==> http://${HOST}:8080/ returned ${code}"
[[ "$code" == "200" ]] || exit 1
