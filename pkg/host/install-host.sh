#!/usr/bin/env bash
# Prepare THIS server to host many isolated assistant bots (multi-bot mode).
# Touches only: /opt/agents, /var/lib/assistant-onboarding, /usr/local/sbin/agentsctl,
# /etc/systemd/system/assistant-*  (and installs node/python packages if missing).
# Does NOT start any bot. Does NOT touch Caddy or any existing service.
#   sudo bash install-host.sh --onboard-host setup.example.com
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"      # package root (has agent/, wa-bridge/, host/)
R=/opt/agents; S=$R/_shared; ONBOARD_HOST=""
while [ $# -gt 0 ]; do case "$1" in --onboard-host) ONBOARD_HOST="$2"; shift 2;; *) echo "unknown arg $1"; exit 1;; esac; done

for u in assistant-bot@ assistant-bridge@ assistant-setup@; do
  if [ -e "/etc/systemd/system/$u.service" ] && ! grep -q '/opt/agents/' "/etc/systemd/system/$u.service"; then
    echo "refusing: /etc/systemd/system/$u.service exists and is not ours"; exit 1; fi
done

export DEBIAN_FRONTEND=noninteractive
need=""; command -v node >/dev/null || need="$need nodejs npm"; python3 -c 'import venv' 2>/dev/null || need="$need python3-venv"
command -v pdftotext >/dev/null || need="$need poppler-utils"; command -v sqlite3 >/dev/null || need="$need sqlite3"
[ -n "$need" ] && apt-get update -q && apt-get install -yq $need
NODE_MAJ=$(node -p 'process.versions.node.split(".")[0]')
[ "$NODE_MAJ" -ge 20 ] || { echo "need Node.js 20+ (found $NODE_MAJ)"; exit 1; }

mkdir -p $S/agent $S/wa-bridge $R/_status $R/_archive
chmod 755 $R $S $R/_status; chmod 700 $R/_archive
install -m 644 "$SRC"/agent/*.py "$SRC"/agent/requirements.txt $S/agent/
install -m 644 "$SRC"/wa-bridge/bridge.mjs "$SRC"/wa-bridge/package.json "$SRC"/wa-bridge/package-lock.json $S/wa-bridge/
install -m 644 "$SRC"/.env.example $S/.env.example
[ -d $S/venv ] || python3 -m venv $S/venv
$S/venv/bin/pip install -q --upgrade pip && $S/venv/bin/pip install -q -r $S/agent/requirements.txt
(cd $S/wa-bridge && npm ci --omit=dev --no-audit --no-fund -s)
chmod -R a+rX $S
[ -f $S/invites.txt ] || install -m 600 /dev/null $S/invites.txt
if [ -n "$ONBOARD_HOST" ]; then printf 'ONBOARD_HOST=%s\n' "$ONBOARD_HOST" > $S/host.env; chmod 644 $S/host.env; fi
[ -f $R/registry.json ] || { echo '{}' > $R/registry.json; chmod 600 $R/registry.json; }

# onboarding site user + spool (requests in, results out)
id assistant-onboard >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin assistant-onboard
mkdir -p /var/lib/assistant-onboarding/requests /var/lib/assistant-onboarding/results
chown root:assistant-onboard /var/lib/assistant-onboarding /var/lib/assistant-onboarding/results /var/lib/assistant-onboarding/requests
chmod 750 /var/lib/assistant-onboarding /var/lib/assistant-onboarding/results
chmod 770 /var/lib/assistant-onboarding/requests

install -m 755 "$SRC"/host/agentsctl /usr/local/sbin/agentsctl
install -m 644 "$SRC"/host/systemd/assistant-* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now assistant-status.timer assistant-provision.path
/usr/local/sbin/agentsctl status --write >/dev/null || true
echo; /usr/local/sbin/agentsctl capacity
echo; echo "ready. create a bot:  agentsctl create <name>    | invite codes for the site:  agentsctl invite --count 3"
