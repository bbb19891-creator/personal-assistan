#!/bin/bash
# Public installer for one friend's own server (Ubuntu 22.04 / 24.04).
# SELF_HOST=1 is the default and the only mode. This script never writes a manager
# URL, a status reporter, a heartbeat hook, or a team-request hook.
# It does not accept an API token.
# Re-running updates code and keeps .env, data/, WhatsApp auth, and Google tokens,
# then strips manager hooks again.
#   sudo bash install.sh --yes
#   sudo bash install.sh --yes --setup-token SECRET --domain HOST
#   sudo bash install.sh --yes --restore BOT-EXPORT.tar.gz
# Rootless proof (no packages, no root):
#   bash install.sh --dry-run --yes --render-dir /tmp/assistant-selfhost
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
H=/opt/agent
YES=0
TOKEN="${SETUP_TOKEN:-}"
DOMAIN=""
RESTORE=""
DRY_RUN=0
RENDER_DIR=""
SELF_HOST="${SELF_HOST:-1}"

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --setup-token)
      if [ $# -lt 2 ]; then echo "missing value" >&2; exit 1; fi
      TOKEN="$2"
      shift
      ;;
    --domain)
      if [ $# -lt 2 ]; then echo "missing value" >&2; exit 1; fi
      DOMAIN="$2"
      shift
      ;;
    --restore)
      if [ $# -lt 2 ]; then echo "missing value" >&2; exit 1; fi
      RESTORE="$2"
      shift
      ;;
    --dry-run) DRY_RUN=1 ;;
    --render-dir)
      if [ $# -lt 2 ]; then echo "missing value" >&2; exit 1; fi
      RENDER_DIR="$2"
      shift
      ;;
    --hetzner-token|--hcloud-token|--api-token)
      echo "refusing: this installer does not accept an API token" >&2
      exit 1
      ;;
    *)
      echo "unknown option" >&2
      exit 1
      ;;
  esac
  shift
done

# Never carry an API token into a file or a child process.
unset HETZNER_API_TOKEN HCLOUD_TOKEN HETZNER_TOKEN HETZNER_API_KEY || true

if [ "$SELF_HOST" != "1" ]; then
  echo "refusing: install.sh is the public self-host installer and only runs with SELF_HOST=1" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }

hook_line() { printf '%s=%s\n' "$1" "$2"; }

strip_hook_keys() {
  local envfile="$1"
  local tmp home
  [ -f "$envfile" ] || return 0
  if [ -L "$envfile" ]; then
    echo "refusing to edit a symlinked env file" >&2
    exit 1
  fi
  tmp="$(mktemp)"
  awk '
    BEGIN {
      split("MANAGER_URL MANAGER_TOKEN MANAGER_ENDPOINT STATUS_REPORTER_URL STATUS_REPORT_URL HEARTBEAT_URL HEARTBEAT_ENDPOINT TEAM_REQUEST_URL TEAM_REQUEST_HOOK TEAM_REQUEST_TOKEN", banned, " ")
      for (i in banned) drop[banned[i]] = 1
    }
    {
      line = $0
      sub(/^[ \t]+/, "", line)
      if (line ~ /^#/ || index(line, "=") == 0) { print; next }
      key = line
      sub(/=.*/, "", key)
      if (drop[key]) next
      print
    }
  ' "$envfile" > "$tmp"
  cat "$tmp" > "$envfile"
  rm -f "$tmp"
  home="$(dirname "$envfile")"
  rm -f "$home/.manager_token" "$home/manager.url" "$home/manager-endpoint" "$home/.manager_endpoint"
}

ensure_self_host_flag() {
  local envfile="$1"
  local tmp
  tmp="$(mktemp)"
  awk '
    BEGIN { seen = 0 }
    {
      line = $0
      sub(/^[ \t]+/, "", line)
      if (line ~ /^SELF_HOST=/) { print "SELF_HOST=1"; seen = 1; next }
      print
    }
    END { if (!seen) print "SELF_HOST=1" }
  ' "$envfile" > "$tmp"
  cat "$tmp" > "$envfile"
  rm -f "$tmp"
}

write_self_host_dropin() {
  local dest_root="$1"
  local unit dir
  for unit in assistant-bot assistant-bridge assistant-setup; do
    dir="${dest_root}/${unit}.service.d"
    mkdir -p "$dir"
    cat > "${dir}/self-host.conf" <<'EOF'
[Service]
Environment=SELF_HOST=1
UnsetEnvironment=MANAGER_URL MANAGER_TOKEN MANAGER_ENDPOINT STATUS_REPORTER_URL STATUS_REPORT_URL HEARTBEAT_URL HEARTBEAT_ENDPOINT TEAM_REQUEST_URL TEAM_REQUEST_HOOK TEAM_REQUEST_TOKEN
EOF
  done
}

render_units() {
  local dest="$1"
  mkdir -p "$dest"
  cp "$SRC/deploy/assistant-bot.service" "$SRC/deploy/assistant-bridge.service" "$SRC/deploy/assistant-setup.service" "$dest/"
  write_self_host_dropin "$dest"
}

rnd() {
  local n="${1:-32}" raw="" chunk
  while [ "${#raw}" -lt "$n" ]; do
    chunk="$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    if [ -z "$chunk" ]; then
      echo "could not generate a local secret" >&2
      exit 1
    fi
    raw="${raw}${chunk}"
  done
  printf '%s' "${raw:0:n}"
}

if [ "$DRY_RUN" = 1 ]; then
  if [ -z "$RENDER_DIR" ]; then
    RENDER_DIR="$(mktemp -d)"
  fi
  mkdir -p "$RENDER_DIR/units" "$RENDER_DIR/env"
  render_units "$RENDER_DIR/units"
  umask 077
  cp "$SRC/.env.example" "$RENDER_DIR/env/env"
  {
    hook_line MANAGER_URL "http://127.0.0.1:9"
    hook_line MANAGER_TOKEN "dry-run"
    hook_line MANAGER_ENDPOINT "http://127.0.0.1:9"
    hook_line STATUS_REPORTER_URL "http://127.0.0.1:9"
    hook_line STATUS_REPORT_URL "http://127.0.0.1:9"
    hook_line HEARTBEAT_URL "http://127.0.0.1:9"
    hook_line HEARTBEAT_ENDPOINT "http://127.0.0.1:9"
    hook_line TEAM_REQUEST_URL "http://127.0.0.1:9/intake"
    hook_line TEAM_REQUEST_HOOK "http://127.0.0.1:9/intake"
    hook_line TEAM_REQUEST_TOKEN "dry-run"
  } >> "$RENDER_DIR/env/env"
  printf '%s\n' 'dry-run' > "$RENDER_DIR/env/.manager_token"
  strip_hook_keys "$RENDER_DIR/env/env"
  ensure_self_host_flag "$RENDER_DIR/env/env"
  if [ -e "$RENDER_DIR/env/.manager_token" ]; then
    echo "dry-run: manager token file still present" >&2
    exit 1
  fi
  if grep -E '^(MANAGER_URL|MANAGER_TOKEN|MANAGER_ENDPOINT|STATUS_REPORTER_URL|STATUS_REPORT_URL|HEARTBEAT_URL|HEARTBEAT_ENDPOINT|TEAM_REQUEST_URL|TEAM_REQUEST_HOOK|TEAM_REQUEST_TOKEN)=' "$RENDER_DIR/env/env" >/dev/null 2>&1; then
    echo "dry-run: manager hook key still present" >&2
    exit 1
  fi
  awk -F= '/^[A-Za-z_][A-Za-z0-9_]*=/ { print $1 }' "$RENDER_DIR/env/env" | sort -u > "$RENDER_DIR/env/keys"
  printf '%s\n' 'SELF_HOST=1' > "$RENDER_DIR/self-host-mode"
  printf 'dry-run rendered: %s\n' "$RENDER_DIR"
  exit 0
fi

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
# shellcheck disable=SC1091
. /etc/os-release
case "${VERSION_ID:-}" in
  22.04|24.04) ;;
  *)
    echo "Ubuntu 22.04/24.04 required (found ${PRETTY_NAME:-unknown})" >&2
    [ "$YES" = 1 ] || exit 1
    ;;
esac
if [ "$YES" = 0 ]; then
  read -r -p "Install the assistant into $H ? [y/N] " a
  [[ "$a" =~ ^[yY]$ ]] || exit 1
fi
export DEBIAN_FRONTEND=noninteractive

log "system packages"
apt-get update -y
apt-get install -y ca-certificates curl gnupg python3 python3-venv python3-pip poppler-utils debian-keyring debian-archive-keyring apt-transport-https ufw
need_node=0
if ! command -v node >/dev/null 2>&1; then
  need_node=1
else
  node_major="$(node -v | sed 's/^v\([0-9]*\).*/\1/')"
  if [ "$node_major" -lt 20 ]; then
    need_node=1
  fi
fi
if [ "$need_node" = 1 ]; then
  log "Node.js 20"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi
if ! command -v caddy >/dev/null 2>&1; then
  log "Caddy (automatic HTTPS)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

log "files -> $H"
mkdir -p "$H/data" "$H/logs" "$H/wa-bridge"
chmod 700 "$H" "$H/data"
cp "$SRC"/agent/*.py "$SRC/agent/requirements.txt" "$H/"
cp "$SRC/wa-bridge/bridge.mjs" "$SRC/wa-bridge/package.json" "$SRC/wa-bridge/package-lock.json" "$H/wa-bridge/"
install -m 755 "$SRC/deploy/agentctl" /usr/local/bin/agentctl

if [ -n "$RESTORE" ]; then
  log "restore bot from archive"
  [ -f "$RESTORE" ] || { echo "no such file" >&2; exit 1; }
  [ -f "$H/data/memory.db" ] && { echo "refusing: this server already has a bot (data/memory.db)" >&2; exit 1; }
  if tar tzf "$RESTORE" | grep -vqE '^(\./)?(\.env|persona\.txt|data(/.*)?|wa-bridge(/auth(/.*)?)?)$'; then
    echo "unexpected files in export" >&2
    exit 1
  fi
  (umask 077; tar xzf "$RESTORE" -C "$H" --no-same-owner)
  rm -f "$H/wa-bridge/.env"
  sed -i -e 's#^WA_BRIDGE_URL=.*#WA_BRIDGE_URL=http://127.0.0.1:8795#' -e 's#^WA_INBOUND_PORT=.*#WA_INBOUND_PORT=8794#' "$H/.env"
fi

log "python venv"
[ -x "$H/venv/bin/python" ] || python3 -m venv "$H/venv"
"$H/venv/bin/pip" install -q --upgrade pip
"$H/venv/bin/pip" install -q -r "$H/requirements.txt"

log "WhatsApp bridge dependencies"
(cd "$H/wa-bridge" && npm ci --omit=dev --no-audit --no-fund)

log "configuration"
umask 077
[ -f "$H/.env" ] || cp "$SRC/.env.example" "$H/.env"
if [ ! -f "$H/wa-bridge/.env" ]; then
  BT="$(rnd 40)"
  printf 'WA_BRIDGE_TOKEN=%s\nWA_BRIDGE_PORT=8795\nWA_INBOUND_URL=http://127.0.0.1:8794/wa/inbound\n' "$BT" > "$H/wa-bridge/.env"
  if grep -q '^WA_BRIDGE_TOKEN=' "$H/.env"; then
    sed -i "s/^WA_BRIDGE_TOKEN=.*/WA_BRIDGE_TOKEN=${BT}/" "$H/.env"
  else
    printf 'WA_BRIDGE_TOKEN=%s\n' "$BT" >> "$H/.env"
  fi
fi
if [ -n "$TOKEN" ]; then
  printf '%s' "$TOKEN" | tr -dc 'A-Za-z0-9_-' > "$H/.setup_token"
fi
if [ ! -s "$H/.setup_token" ]; then
  rnd 32 > "$H/.setup_token"
fi
if [ -z "$DOMAIN" ]; then
  IP="$(curl -fsS4 --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')"
  DOMAIN="$(printf '%s' "$IP" | tr '.' '-').sslip.io"
fi
printf '%s' "$DOMAIN" > "$H/.public_host"
if grep -q '^PUBLIC_BASE_URL=' "$H/.env"; then
  sed -i "s#^PUBLIC_BASE_URL=.*#PUBLIC_BASE_URL=https://${DOMAIN}#" "$H/.env"
else
  printf 'PUBLIC_BASE_URL=https://%s\n' "$DOMAIN" >> "$H/.env"
fi
strip_hook_keys "$H/.env"
ensure_self_host_flag "$H/.env"
if [ -f "$H/wa-bridge/.env" ]; then
  strip_hook_keys "$H/wa-bridge/.env"
fi
chmod 600 "$H/.env" "$H/wa-bridge/.env" "$H/.setup_token"
umask 022

log "Caddy + systemd"
sed "s#__HOST__#${DOMAIN}#" "$SRC/deploy/Caddyfile.tmpl" > /etc/caddy/Caddyfile
render_units /etc/systemd/system
systemctl daemon-reload
systemctl enable --now assistant-bridge assistant-setup
systemctl restart assistant-bridge assistant-setup caddy
if grep -q '^OPENAI_API_KEY=sk-' "$H/.env" && grep -q '^OWNER_PHONE=[0-9]' "$H/.env"; then
  systemctl enable --now assistant-bot
  systemctl restart assistant-bot
fi

log "firewall (22, 80, 443)"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null || true

LINK="https://${DOMAIN}/?t=$(cat "$H/.setup_token")"
mkdir -p /root
printf '%s\n' "$LINK" > /root/SETUP_LINK.txt
chmod 600 /root/SETUP_LINK.txt
printf '\n'
printf '%s\n' "======================================================"
printf '%s\n' " Installed. Open the setup page (keep this link private):"
printf '   %s\n' "$LINK"
printf '%s\n' " (first HTTPS certificate can take a minute)"
printf '%s\n' " Later: 'agentctl link' prints it again, 'agentctl status' shows services."
printf '%s\n' "======================================================"
