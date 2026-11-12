#!/bin/bash
# Deploy the central onboarding site. NOT run automatically. Usage: sudo bash deploy.sh setup.example.com
# Needs multi-bot mode first (host/install-host.sh creates the assistant-onboard user and the request spool).
# Copies files to /opt/agent-onboarding and starts agent-onboarding.service.
# Listen address is ONBOARD_BIND:ONBOARD_PORT from the unit (examples are in that unit).
# Prints the Caddy block to add by hand (this script does not edit Caddy).
set -euo pipefail
HOST="${1:?usage: deploy.sh <onboarding host, e.g. setup.example.com>}"
SRC="$(cd "$(dirname "$0")" && pwd)"
id assistant-onboard >/dev/null 2>&1 || { echo "run pkg/host/install-host.sh --onboard-host $HOST first"; exit 1; }
if [ -n "${PUBLIC_INSTALL_URL:-}" ]; then
  if ! printf '%s' "$PUBLIC_INSTALL_URL" | grep -Eq '^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~-]+)*/install\.sh$'; then
    echo "PUBLIC_INSTALL_URL must be an https install.sh URL"
    exit 1
  fi
fi
PORT=$(sed -n "s/^Environment=ONBOARD_PORT=//p" "$SRC/agent-onboarding.service"); BIND=$(sed -n "s/^Environment=ONBOARD_BIND=//p" "$SRC/agent-onboarding.service"); BIND=${BIND:-127.0.0.1}
[ -n "$PORT" ] || { echo "ONBOARD_PORT missing in unit"; exit 1; }
[ "$BIND" = "0.0.0.0" ] && { echo "refusing ONBOARD_BIND=0.0.0.0"; exit 1; }
if ! systemctl is-active -q agent-onboarding; then ss -ltn | grep -q ":$PORT " && { echo "port $PORT already in use - set ONBOARD_PORT in the unit"; exit 1; }; fi
install -d -m 755 /opt/agent-onboarding
install -m 644 "$SRC/onboard.py" /opt/agent-onboarding/onboard.py
sed "s#__ONBOARD_HOST__#$HOST#" "$SRC/agent-onboarding.service" > /etc/systemd/system/agent-onboarding.service
if [ -n "${PUBLIC_INSTALL_URL:-}" ]; then
  sed -i "s#^Environment=PUBLIC_INSTALL_URL=.*#Environment=PUBLIC_INSTALL_URL=${PUBLIC_INSTALL_URL}#" /etc/systemd/system/agent-onboarding.service
  echo "PUBLIC_INSTALL_URL written into the unit"
else
  echo "PUBLIC_INSTALL_URL left as the example in the unit"
fi
systemctl daemon-reload && systemctl enable --now agent-onboarding
sleep 1; systemctl restart agent-onboarding; sleep 1; curl -fsS http://$BIND:$PORT/health && echo
echo "Now add to /etc/caddy/Caddyfile:"
sed -e "s#__ONBOARD_HOST__#$HOST#" -e "s#__ONBOARD_BIND__#$BIND#" -e "s#__ONBOARD_PORT__#$PORT#" "$SRC/Caddyfile.snippet"
