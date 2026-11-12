#!/usr/bin/env bash
# Restore one archive written by migrate-manager.sh.
# Repeating this command on the same archive restores the same files.
# This script does not call systemctl.
#
#   sudo bash docs/deploy/rollback-manager.sh ARCHIVE.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"

if [[ $# -ne 1 ]]; then
  blocked "usage: rollback-manager.sh ARCHIVE.tar.gz"
fi

archive="$1"
[[ -f "$archive" ]] || die "no such archive: $archive"
if ! tar -xOf "$archive" manifest.tsv | grep -q '^/opt/agents/_manager	'; then
  die "archive is not a manager migration backup"
fi

bash "$SCRIPT_DIR/rollback.sh" "$archive"
printf '%s\n' "restored files only. Reload systemd and restart assistant-manager yourself."
