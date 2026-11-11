#!/usr/bin/env bash
# Backup, then run central/deploy.sh with its one positional host argument.
# Does not edit Caddy. Does not pass any flags to deploy.sh.
# Does not run pkg/host/install-host.sh; that installer is apply-tenant-host.sh shared.
#
# Usage: apply-onboarding-host.sh --host DNS-NAME [--print-only]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"

host=""
print_only=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || blocked "usage: apply-onboarding-host.sh --host DNS-NAME [--print-only]"
      host="$2"
      shift 2
      ;;
    --print-only)
      print_only=1
      shift
      ;;
    *)
      blocked "unknown argument: $1"
      ;;
  esac
done

[[ -n "$host" ]] || blocked "usage: apply-onboarding-host.sh --host DNS-NAME [--print-only]"
valid_host "$host" || blocked "host must be a dotted DNS name such as example.invalid"

if [[ "$print_only" -eq 1 ]]; then
  refuse_partial_rehearsal
else
  refuse_rehearsal_on_apply
  [[ "$(id -u)" -eq 0 ]] || blocked "run as root. central/deploy.sh was not started."
fi

unit="$REPO_ROOT/central/agent-onboarding.service"
deploy_sh="$REPO_ROOT/central/deploy.sh"
if [[ "$print_only" -eq 1 && -n "${ASSISTANT_REHEARSE:-}" ]]; then
  unit="${ASSISTANT_UNIT_FILE:-$unit}"
  deploy_sh="${ASSISTANT_DEPLOY_SH:-$deploy_sh}"
fi

onboard_blockers "$unit" "$deploy_sh"

paths=(
  /opt/agent-onboarding
  /etc/systemd/system/agent-onboarding.service
  /etc/caddy/Caddyfile
)
units=(agent-onboarding)

if [[ "$print_only" -eq 1 ]]; then
  printf '%s\n' "note: central/deploy.sh takes one host and no flags. It does not edit Caddy. If PUBLIC_INSTALL_URL is already set, that script may write it into the unit. This wrapper does not set PUBLIC_INSTALL_URL. The example value in the unit is https://example.invalid/install.sh."
  printf '%s\n' "# print-only; no files were changed and no installer was run"
  printf '%s\n' "check: id assistant-onboard"
  printf 'backup label=onboarding paths=%s units=%s\n' "$(IFS=,; printf '%s' "${paths[*]}")" "$(IFS=,; printf '%s' "${units[*]}")"
  printf 'run: bash %s/central/deploy.sh %s\n' "$REPO_ROOT" "$host"
  printf 'next: bash %s/print-caddy-snippet.sh --host %s\n' "$SCRIPT_DIR" "$host"
  exit 0
fi

if ! id assistant-onboard >/dev/null 2>&1; then
  blocked "user assistant-onboard does not exist. Run apply-tenant-host.sh shared --onboard-host ${host} first. central/deploy.sh was not started."
fi

backup_cmd=(bash "$SCRIPT_DIR/backup.sh" onboarding)
for path in "${paths[@]}"; do
  backup_cmd+=(--path "$path")
done
for unit_name in "${units[@]}"; do
  backup_cmd+=(--unit "$unit_name")
done
archive="$("${backup_cmd[@]}")"
printf 'backup archive: %s\n' "$archive" >&2

status=0
bash "$REPO_ROOT/central/deploy.sh" "$host" || status=$?
if [[ "$status" -ne 0 ]]; then
  printf 'central/deploy.sh exited %s. Files may already have changed. Rollback files with:\n' "$status" >&2
  printf 'bash %s/rollback.sh %s\n' "$SCRIPT_DIR" "$archive" >&2
  printf 'central/deploy.sh does not edit Caddy. This wrapper did not edit Caddy.\n' >&2
  exit "$status"
fi

printf 'central/deploy.sh finished. This wrapper did not edit Caddy.\n' >&2
printf 'Print the block with: bash %s/print-caddy-snippet.sh --host %s\n' "$SCRIPT_DIR" "$host" >&2
printf 'File rollback, if you still need it: bash %s/rollback.sh %s\n' "$SCRIPT_DIR" "$archive" >&2
