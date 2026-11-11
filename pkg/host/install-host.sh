#!/usr/bin/env bash
# Prepare THIS server to host isolated assistant bots, one slug per friend.
# Re-running is idempotent: shared code and unit files are refreshed.
# Tenant homes, registry.json, invite codes, and per-tenant secrets are kept.
# Does not start bots. Does not edit Caddy or unrelated services.
# Does not accept an API token. Secrets are never copied from one slug to another.
#   sudo bash install-host.sh --onboard-host setup.example.com
# Source check without root or package installs:
#   bash install-host.sh --dry-run
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
R=/opt/agents
S="$R/_shared"
ONBOARD_HOST=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --onboard-host)
      if [ $# -lt 2 ]; then echo "missing value" >&2; exit 1; fi
      ONBOARD_HOST="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --hetzner-token|--hcloud-token|--api-token)
      echo "refusing: this installer does not accept an API token" >&2
      exit 1
      ;;
    *)
      echo "unknown arg" >&2
      exit 1
      ;;
  esac
done

unset HETZNER_API_TOKEN HCLOUD_TOKEN HETZNER_TOKEN HETZNER_API_KEY || true

if [ -n "$ONBOARD_HOST" ]; then
  if [[ ! "$ONBOARD_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]; then
    echo "invalid onboard host" >&2
    exit 1
  fi
fi

if [ "$DRY_RUN" = 1 ]; then
  missing=0
  for unit in assistant-bot@.service assistant-bridge@.service assistant-setup@.service; do
    file="$SRC/host/systemd/$unit"
    if [ ! -f "$file" ]; then
      echo "missing unit $unit" >&2
      missing=1
      continue
    fi
    if ! grep -q 'User=agent-%i' "$file"; then
      echo "unit $unit has no per-tenant user" >&2
      missing=1
    fi
    if ! grep -q '/opt/agents/%i' "$file"; then
      echo "unit $unit is not scoped to the tenant directory" >&2
      missing=1
    fi
  done
  if [ ! -f "$SRC/host/systemd/assistant-release@.service" ] || ! grep -q 'agentsctl release-access %i' "$SRC/host/systemd/assistant-release@.service"; then
    echo "missing per-tenant release unit" >&2
    missing=1
  fi
  if [ ! -f "$SRC/host/systemd/assistant-release@.path" ] || ! grep -q '/opt/agents/%i/.release-request' "$SRC/host/systemd/assistant-release@.path"; then
    echo "missing per-tenant release path" >&2
    missing=1
  fi
  leaked="$(find "$SRC" \( -name '.env' -o -name 'id_rsa' -o -name 'id_ed25519' -o -name '*.pem' \) -print)"
  if [ -n "$leaked" ]; then
    echo "secret-like file in package" >&2
    printf '%s\n' "$leaked" >&2
    missing=1
  fi
  if [ "$missing" -ne 0 ]; then
    exit 1
  fi
  echo "install-host dry-run: ok"
  exit 0
fi

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

for u in assistant-bot@ assistant-bridge@ assistant-setup@; do
  if [ -e "/etc/systemd/system/${u}.service" ] && ! grep -q '/opt/agents/' "/etc/systemd/system/${u}.service"; then
    echo "refusing: /etc/systemd/system/${u}.service exists and is not ours" >&2
    exit 1
  fi
done

export DEBIAN_FRONTEND=noninteractive
need=()
command -v node >/dev/null 2>&1 || need+=(nodejs npm)
python3 -c 'import venv' >/dev/null 2>&1 || need+=(python3-venv)
command -v pdftotext >/dev/null 2>&1 || need+=(poppler-utils)
command -v sqlite3 >/dev/null 2>&1 || need+=(sqlite3)
if [ "${#need[@]}" -gt 0 ]; then
  apt-get update -q
  apt-get install -yq "${need[@]}"
fi
NODE_MAJ="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJ" -ge 20 ] || { echo "need Node.js 20+ (found $NODE_MAJ)" >&2; exit 1; }

mkdir -p "$S/agent" "$S/wa-bridge" "$R/_status" "$R/_archive" "$R/_release"
chmod 755 "$R" "$S" "$R/_status" "$R/_release"
chmod 700 "$R/_archive"
install -m 644 "$SRC"/agent/*.py "$SRC/agent/requirements.txt" "$S/agent/"
install -m 644 "$SRC/wa-bridge/bridge.mjs" "$SRC/wa-bridge/package.json" "$SRC/wa-bridge/package-lock.json" "$S/wa-bridge/"
install -m 644 "$SRC/.env.example" "$S/.env.example"
if [ ! -d "$S/venv" ]; then
  python3 -m venv "$S/venv"
fi
"$S/venv/bin/pip" install -q --upgrade pip
"$S/venv/bin/pip" install -q -r "$S/agent/requirements.txt"
(cd "$S/wa-bridge" && npm ci --omit=dev --no-audit --no-fund -s)
# Shared code is readable. Invite codes and operator public keys stay private
# even when this script is run again.
chmod -R a+rX "$S"
if [ ! -f "$S/invites.txt" ]; then
  install -m 600 /dev/null "$S/invites.txt"
fi
chmod 600 "$S/invites.txt"
if [ -f "$S/operator_ssh_keys" ]; then
  chmod 600 "$S/operator_ssh_keys"
fi
if [ -n "$ONBOARD_HOST" ]; then
  printf 'ONBOARD_HOST=%s\n' "$ONBOARD_HOST" > "$S/host.env"
  chmod 644 "$S/host.env"
fi
if [ ! -f "$R/registry.json" ]; then
  (umask 077; printf '%s\n' '{}' > "$R/registry.json")
fi

id assistant-onboard >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin assistant-onboard
mkdir -p /var/lib/assistant-onboarding/requests /var/lib/assistant-onboarding/results
chown root:assistant-onboard /var/lib/assistant-onboarding /var/lib/assistant-onboarding/results /var/lib/assistant-onboarding/requests
chmod 750 /var/lib/assistant-onboarding /var/lib/assistant-onboarding/results
chmod 770 /var/lib/assistant-onboarding/requests

install -m 755 "$SRC/host/agentsctl" /usr/local/sbin/agentsctl
install -m 644 "$SRC"/host/systemd/assistant-* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now assistant-status.timer assistant-provision.path
if [ -f "$R/registry.json" ]; then
  python3 - "$R/registry.json" <<'PY'
import json, re, subprocess, sys
reg = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(reg, dict):
    raise SystemExit(0)
ok = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
for slug in reg:
    if not ok.fullmatch(str(slug)):
        continue
    subprocess.run(["systemctl", "enable", "--now", f"assistant-release@{slug}.path"], check=False)
PY
fi
/usr/local/sbin/agentsctl status --write >/dev/null || true
printf '\n'
/usr/local/sbin/agentsctl capacity
printf '\nready. create a bot:  agentsctl create <name>    | invite codes for the site:  agentsctl invite --count 3\n'
