#!/usr/bin/env bash
# Backup, then run one existing installer.
#   shared     pkg/host/install-host.sh, optional --onboard-host only
#   dedicated  pkg/install.sh --yes --domain DNS-NAME, optional --restore FILE
# Does not pass --setup-token. Does not call nodesource or apt itself.
#
# Usage:
#   apply-tenant-host.sh shared [--onboard-host DNS-NAME] [--print-only]
#   apply-tenant-host.sh dedicated --domain DNS-NAME [--restore ABSOLUTE] [--print-only]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"

if [[ $# -lt 1 ]]; then
  blocked "usage: apply-tenant-host.sh shared [--onboard-host DNS-NAME] [--print-only] | dedicated --domain DNS-NAME [--restore ABSOLUTE] [--print-only]"
fi

mode="$1"
shift
case "$mode" in
  shared|dedicated) ;;
  *) blocked "first argument must be shared or dedicated" ;;
esac

onboard_host=""
domain=""
restore=""
print_only=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --print-only)
      print_only=1
      shift
      ;;
    --onboard-host)
      [[ "$mode" == "shared" ]] || blocked "--onboard-host is only valid for shared. It is the only flag pkg/host/install-host.sh accepts."
      [[ $# -ge 2 ]] || blocked "--onboard-host needs a DNS name"
      onboard_host="$2"
      shift 2
      ;;
    --domain)
      [[ "$mode" == "dedicated" ]] || blocked "--domain is only valid for dedicated. It is passed through to pkg/install.sh."
      [[ $# -ge 2 ]] || blocked "--domain needs a DNS name"
      domain="$2"
      shift 2
      ;;
    --restore)
      [[ "$mode" == "dedicated" ]] || blocked "--restore is only valid for dedicated. It is passed through to pkg/install.sh."
      [[ $# -ge 2 ]] || blocked "--restore needs an absolute file path"
      restore="$2"
      shift 2
      ;;
    *)
      blocked "unknown argument: $1"
      ;;
  esac
done

if [[ "$print_only" -eq 1 ]]; then
  refuse_partial_rehearsal
else
  refuse_rehearsal_on_apply
  [[ "$(id -u)" -eq 0 ]] || blocked "run as root. The installer was not started."
fi

if [[ "$mode" == "shared" ]]; then
  if [[ -n "$onboard_host" ]]; then
    valid_host "$onboard_host" || blocked "onboard host must be a dotted DNS name such as example.invalid"
  fi
  paths=(
    /opt/agents
    /var/lib/assistant-onboarding
    /usr/local/sbin/agentsctl
    /etc/systemd/system/assistant-bot@.service
    /etc/systemd/system/assistant-bridge@.service
    /etc/systemd/system/assistant-setup@.service
    /etc/systemd/system/assistant-provision.path
    /etc/systemd/system/assistant-provision.service
    /etc/systemd/system/assistant-restart@.path
    /etc/systemd/system/assistant-restart@.service
    /etc/systemd/system/assistant-status.service
    /etc/systemd/system/assistant-status.timer
    /etc/systemd/system/assistant-release@.service
    /etc/systemd/system/assistant-release@.path
  )
  units=(assistant-status.timer assistant-provision.path assistant-provision.service)
  installer=(bash "$REPO_ROOT/pkg/host/install-host.sh")
  if [[ -n "$onboard_host" ]]; then
    installer+=(--onboard-host "$onboard_host")
  fi
  label="tenant-shared"
else
  [[ -n "$domain" ]] || blocked "dedicated mode requires --domain. pkg/install.sh was not started."
  valid_host "$domain" || blocked "domain must be a dotted DNS name such as example.invalid"
  if [[ -n "$restore" ]]; then
    case "$restore" in
      /*) ;;
      *) blocked "--restore path must be absolute. pkg/install.sh was not started." ;;
    esac
    case "$restore" in
      *..*|*$'\n'*|*[[:space:]]*) blocked "--restore path must not contain whitespace or .." ;;
    esac
    case "$restore" in
      /opt/agent|/opt/agent/*) blocked "--restore must not point inside /opt/agent. pkg/install.sh was not started." ;;
    esac
    [[ -f "$restore" ]] || blocked "--restore file does not exist. pkg/install.sh was not started."
  fi
  if [[ "$print_only" -eq 0 ]]; then
    os_file="${ASSISTANT_OS_RELEASE:-/etc/os-release}"
    if ! ubuntu_supported "$os_file"; then
      blocked "pkg/install.sh requires Ubuntu 22.04 or 24.04. The installer was not started."
    fi
  fi
  paths=(
    /opt/agent
    /usr/local/bin/agentctl
    /etc/caddy/Caddyfile
    /etc/systemd/system/assistant-bot.service
    /etc/systemd/system/assistant-bridge.service
    /etc/systemd/system/assistant-setup.service
    /root/SETUP_LINK.txt
  )
  units=(assistant-bridge assistant-setup assistant-bot)
  installer=(bash "$REPO_ROOT/pkg/install.sh" --yes --domain "$domain")
  if [[ -n "$restore" ]]; then
    installer+=(--restore "$restore")
  fi
  label="tenant-dedicated"
fi

if [[ "$print_only" -eq 1 ]]; then
  printf '%s\n' "# print-only; no files were changed and no installer was run"
  printf 'backup label=%s paths=%s units=%s\n' "$label" "$(IFS=,; printf '%s' "${paths[*]}")" "$(IFS=,; printf '%s' "${units[*]}")"
  printf 'run:'
  printf ' %q' "${installer[@]}"
  printf '\n'
  exit 0
fi

backup_cmd=(bash "$SCRIPT_DIR/backup.sh" "$label")
for path in "${paths[@]}"; do
  backup_cmd+=(--path "$path")
done
for unit_name in "${units[@]}"; do
  backup_cmd+=(--unit "$unit_name")
done
archive="$("${backup_cmd[@]}")"
printf 'backup archive: %s\n' "$archive" >&2

status=0
"${installer[@]}" || status=$?
if [[ "$status" -ne 0 ]]; then
  printf 'installer exited %s. Files may already have changed. Rollback files with:\n' "$status" >&2
  printf 'bash %s/rollback.sh %s\n' "$SCRIPT_DIR" "$archive" >&2
  exit "$status"
fi

printf 'installer finished. File rollback, if you still need it: bash %s/rollback.sh %s\n' "$SCRIPT_DIR" "$archive" >&2
