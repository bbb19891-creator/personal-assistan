#!/usr/bin/env bash
# Print central/Caddyfile.snippet with the host substituted.
# Uses the same sed replacement as central/deploy.sh. Does not edit Caddy.
#
# Usage: print-caddy-snippet.sh --host DNS-NAME
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"

host=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || blocked "usage: print-caddy-snippet.sh --host DNS-NAME"
      host="$2"
      shift 2
      ;;
    *)
      blocked "unknown argument: $1"
      ;;
  esac
done

[[ -n "$host" ]] || blocked "usage: print-caddy-snippet.sh --host DNS-NAME"
valid_host "$host" || blocked "host must be a dotted DNS name such as example.invalid"
refuse_partial_rehearsal
snippet="$REPO_ROOT/central/Caddyfile.snippet"
unit="$REPO_ROOT/central/agent-onboarding.service"
if [[ -n "${ASSISTANT_REHEARSE:-}" && -n "${ASSISTANT_UNIT_FILE:-}" ]]; then
  unit="$ASSISTANT_UNIT_FILE"
fi
[[ -f "$snippet" ]] || die "missing $snippet"
[[ -f "$unit" ]] || die "missing $unit"
bind="$(onboard_bind "$unit")"
port="$(onboard_port "$unit")"

printf '%s\n' "The block below is the snippet with the host, bind, and port substituted the same way central/deploy.sh prints them. If it still contains 192.0.2.1, replace that documentation address before adding the block to Caddy. This script does not edit Caddy." >&2
sed -e "s#__ONBOARD_HOST__#${host}#" -e "s#__ONBOARD_BIND__#${bind}#" -e "s#__ONBOARD_PORT__#${port}#" "$snippet"
