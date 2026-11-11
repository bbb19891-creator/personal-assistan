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
snippet="$REPO_ROOT/central/Caddyfile.snippet"
[[ -f "$snippet" ]] || die "missing $snippet"

printf '%s\n' "The block below still contains the documentation address 192.0.2.1. Replace it with the bind you set in the unit before adding the block to Caddy. This script does not edit Caddy." >&2
sed "s#__ONBOARD_HOST__#${host}#" "$snippet"
