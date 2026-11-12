#!/bin/bash
# Deploy the central onboarding site. NOT run automatically. Usage: sudo bash deploy.sh setup.example.com
# Needs multi-bot mode first (host/install-host.sh creates the assistant-onboard user and the request spool).
# Copies files to /opt/agent-onboarding, installs + starts agent-onboarding.service on ONBOARD_BIND:ONBOARD_PORT from the unit (do not use port 8791, that is Tokyo's),
# and prints the Caddy block to add by hand (this script does not edit Caddy).
set -euo pipefail
HOST="${1:?usage: deploy.sh <onboarding host, e.g. setup.example.com>}"
SRC="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SRC/../agent-template.tar.gz" ] || { echo "missing ../agent-template.tar.gz (run tools/package.sh first)"; exit 1; }
id assistant-onboard >/dev/null 2>&1 || { echo "run pkg/host/install-host.sh --onboard-host $HOST first"; exit 1; }
PORT=$(sed -n "s/^Environment=ONBOARD_PORT=//p" "$SRC/agent-onboarding.service"); BIND=$(sed -n "s/^Environment=ONBOARD_BIND=//p" "$SRC/agent-onboarding.service"); BIND=${BIND:-127.0.0.1}
[ -n "$PORT" ] || { echo "ONBOARD_PORT missing in unit"; exit 1; }
[ "$BIND" = "0.0.0.0" ] && { echo "refusing ONBOARD_BIND=0.0.0.0"; exit 1; }
if ! systemctl is-active -q agent-onboarding; then ss -ltn | grep -q ":$PORT " && { echo "port $PORT already in use - set ONBOARD_PORT in the unit"; exit 1; }; fi
install -d -m 755 /opt/agent-onboarding
install -m 644 "$SRC/onboard.py" /opt/agent-onboarding/onboard.py
install -m 644 "$SRC/../agent-template.tar.gz" /opt/agent-onboarding/agent-template.tar.gz
sed "s#__ONBOARD_HOST__#$HOST#" "$SRC/agent-onboarding.service" > /etc/systemd/system/agent-onboarding.service
systemctl daemon-reload && systemctl enable --now agent-onboarding
sleep 1; systemctl restart agent-onboarding; sleep 1; curl -fsS http://$BIND:$PORT/health && echo
echo "Now add to /etc/caddy/Caddyfile:"; sed "s#__ONBOARD_HOST__#$HOST#" "$SRC/Caddyfile.snippet"
