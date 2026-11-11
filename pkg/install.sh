#!/bin/bash
# Installer for the personal WhatsApp assistant on a fresh Ubuntu 22.04 / 24.04 server (e.g. Hetzner CX23).
# Usage: sudo bash install.sh [--yes] [--setup-token TOKEN] [--domain HOST] [--restore BOT-EXPORT.tar.gz]
# --restore: move a bot from a multi-bot host (agentsctl export) to this server; stop it on the old host first.
# Safe to re-run (updates code, keeps .env, data/, WhatsApp auth and Google tokens).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
H=/opt/agent
YES=0; TOKEN="${SETUP_TOKEN:-}"; DOMAIN=""; RESTORE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1;;
    --setup-token) TOKEN="$2"; shift;;
    --domain) DOMAIN="$2"; shift;;
    --restore) RESTORE="$2"; shift;;
    *) echo "unknown option: $1"; exit 1;;
  esac; shift
done
log(){ echo -e "\n==> $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
. /etc/os-release
case "${VERSION_ID:-}" in 22.04|24.04) ;; *) echo "Ubuntu 22.04/24.04 required (found ${PRETTY_NAME:-unknown})"; [ $YES = 1 ] || exit 1;; esac
if [ $YES = 0 ]; then read -r -p "Install the assistant into $H ? [y/N] " a; [[ "$a" =~ ^[yY] ]] || exit 1; fi
export DEBIAN_FRONTEND=noninteractive

log "system packages"
apt-get update -y
apt-get install -y ca-certificates curl gnupg python3 python3-venv python3-pip poppler-utils debian-keyring debian-archive-keyring apt-transport-https ufw
if ! command -v node >/dev/null || [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 20 ]; then
  log "Node.js 20"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi
if ! command -v caddy >/dev/null; then
  log "Caddy (automatic HTTPS)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

log "files -> $H"
mkdir -p $H/data $H/logs $H/wa-bridge
chmod 700 $H $H/data
cp "$SRC"/agent/*.py "$SRC"/agent/requirements.txt $H/
cp "$SRC"/wa-bridge/bridge.mjs "$SRC"/wa-bridge/package.json "$SRC"/wa-bridge/package-lock.json $H/wa-bridge/
install -m 755 "$SRC"/deploy/agentctl /usr/local/bin/agentctl

if [ -n "$RESTORE" ]; then
  log "restore bot from $RESTORE"
  [ -f "$RESTORE" ] || { echo "no such file: $RESTORE"; exit 1; }
  [ -f $H/data/memory.db ] && { echo "refusing: $H already has a bot (data/memory.db)"; exit 1; }
  if tar tzf "$RESTORE" | grep -vqE "^(\./)?(\.env|persona\.txt|data(/.*)?|wa-bridge(/auth(/.*)?)?)$"; then echo "unexpected files in export"; exit 1; fi
  (umask 077; tar xzf "$RESTORE" -C $H --no-same-owner)
  rm -f $H/wa-bridge/.env
  sed -i -e "s#^WA_BRIDGE_URL=.*#WA_BRIDGE_URL=http://127.0.0.1:8795#" -e "s#^WA_INBOUND_PORT=.*#WA_INBOUND_PORT=8794#" $H/.env
fi

log "python venv"
[ -x $H/venv/bin/python ] || python3 -m venv $H/venv
$H/venv/bin/pip install -q --upgrade pip
$H/venv/bin/pip install -q -r $H/requirements.txt

log "WhatsApp bridge dependencies"
(cd $H/wa-bridge && npm ci --omit=dev --no-audit --no-fund)

log "configuration"
umask 077
rnd(){ head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }
[ -f $H/.env ] || cp "$SRC"/.env.example $H/.env
if [ ! -f $H/wa-bridge/.env ]; then
  BT=$(rnd 40)
  printf "WA_BRIDGE_TOKEN=%s\nWA_BRIDGE_PORT=8795\nWA_INBOUND_URL=http://127.0.0.1:8794/wa/inbound\n" "$BT" > $H/wa-bridge/.env
  sed -i "s/^WA_BRIDGE_TOKEN=.*/WA_BRIDGE_TOKEN=$BT/" $H/.env
fi
if [ -n "$TOKEN" ]; then printf "%s" "$TOKEN" | tr -dc 'A-Za-z0-9_-' > $H/.setup_token; fi
[ -s $H/.setup_token ] || rnd 32 > $H/.setup_token
if [ -z "$DOMAIN" ]; then
  IP=$(curl -fsS4 --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')
  DOMAIN="$(echo "$IP" | tr . -).sslip.io"
fi
echo -n "$DOMAIN" > $H/.public_host
grep -q '^PUBLIC_BASE_URL=' $H/.env && sed -i "s#^PUBLIC_BASE_URL=.*#PUBLIC_BASE_URL=https://$DOMAIN#" $H/.env || echo "PUBLIC_BASE_URL=https://$DOMAIN" >> $H/.env
chmod 600 $H/.env $H/wa-bridge/.env $H/.setup_token
umask 022

log "Caddy + systemd"
sed "s#__HOST__#$DOMAIN#" "$SRC"/deploy/Caddyfile.tmpl > /etc/caddy/Caddyfile
cp "$SRC"/deploy/assistant-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now assistant-bridge assistant-setup
systemctl restart assistant-bridge assistant-setup caddy
if grep -q '^OPENAI_API_KEY=sk-' $H/.env && grep -q '^OWNER_PHONE=[0-9]' $H/.env; then systemctl enable --now assistant-bot; systemctl restart assistant-bot; fi

log "firewall (22, 80, 443)"
ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null || true

LINK="https://$DOMAIN/?t=$(cat $H/.setup_token)"
echo "$LINK" > /root/SETUP_LINK.txt; chmod 600 /root/SETUP_LINK.txt
echo
echo "======================================================"
echo " Installed. Open the setup page (keep this link private):"
echo "   $LINK"
echo " (first HTTPS certificate can take ~1 minute)"
echo " Later: 'agentctl link' prints it again, 'agentctl status' shows services."
echo "======================================================"
