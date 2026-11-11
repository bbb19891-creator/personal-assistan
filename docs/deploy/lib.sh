#!/usr/bin/env bash
# Helpers for the operator wrappers in this directory.
# Source this file. Do not execute it.

# Paths a backup is allowed to read and a rollback is allowed to replace.
# Keep this list equal to the files the baseline installers write.
path_allowed() {
  case "$1" in
    /opt/agent|/opt/agent/*) return 0 ;;
    /opt/agent-onboarding|/opt/agent-onboarding/*) return 0 ;;
    /opt/agents|/opt/agents/*) return 0 ;;
    /var/lib/assistant-onboarding|/var/lib/assistant-onboarding/*) return 0 ;;
    /usr/local/sbin/agentsctl) return 0 ;;
    /usr/local/bin/agentctl) return 0 ;;
    /etc/caddy/Caddyfile) return 0 ;;
    /etc/systemd/system/agent-onboarding.service) return 0 ;;
    /etc/systemd/system/assistant-bot.service) return 0 ;;
    /etc/systemd/system/assistant-bridge.service) return 0 ;;
    /etc/systemd/system/assistant-setup.service) return 0 ;;
    /etc/systemd/system/assistant-bot@.service) return 0 ;;
    /etc/systemd/system/assistant-bridge@.service) return 0 ;;
    /etc/systemd/system/assistant-setup@.service) return 0 ;;
    /etc/systemd/system/assistant-provision.path) return 0 ;;
    /etc/systemd/system/assistant-provision.service) return 0 ;;
    /etc/systemd/system/assistant-restart@.path) return 0 ;;
    /etc/systemd/system/assistant-restart@.service) return 0 ;;
    /etc/systemd/system/assistant-status.service) return 0 ;;
    /etc/systemd/system/assistant-status.timer) return 0 ;;
    /root/SETUP_LINK.txt) return 0 ;;
    *) return 1 ;;
  esac
}

# Units named in a backup manifest. Rollback does not stop them; it only records them.
unit_allowed() {
  case "$1" in
    agent-onboarding|assistant-bridge|assistant-setup|assistant-bot) return 0 ;;
    assistant-status.timer|assistant-provision.path|assistant-provision.service) return 0 ;;
    *) return 1 ;;
  esac
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

blocked() {
  printf '%s\n' "$*" >&2
  exit 2
}

# ASSISTANT_ROOT is a rehearsal prefix. Apply mode refuses it.
# Unset means the real filesystem.
live_path() {
  local logical="$1"
  if [[ -n "${ASSISTANT_ROOT:-}" ]]; then
    printf '%s%s\n' "${ASSISTANT_ROOT%/}" "$logical"
  else
    printf '%s\n' "$logical"
  fi
}

valid_host() {
  local host="$1"
  [[ "${#host}" -le 253 ]] || return 1
  [[ "$host" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]] || return 1
  [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && return 1
  return 0
}

onboard_bind() {
  local unit="$1"
  local bind=""
  bind="$(sed -n 's/^Environment=ONBOARD_BIND=//p' "$unit" | head -n 1)"
  if [[ -z "$bind" ]]; then
    # Same default as central/deploy.sh when the line is empty.
    bind="127.0.0.1"
  fi
  printf '%s\n' "$bind"
}

onboard_port() {
  local unit="$1"
  sed -n 's/^Environment=ONBOARD_PORT=//p' "$unit" | head -n 1
}

# Print blockers for central/deploy.sh. Return 2 when the installer must not start.
onboard_blockers() {
  local unit="$1"
  local archive="$2"
  local bind="" port=""
  local -a found=()
  bind="$(onboard_bind "$unit")"
  port="$(onboard_port "$unit")"
  if [[ -z "$port" ]]; then
    found+=("ONBOARD_PORT is missing in ${unit}. central/deploy.sh reads that line and exits when it is empty. central/deploy.sh was not started.")
  fi
  if [[ "$bind" == "0.0.0.0" ]]; then
    found+=("ONBOARD_BIND is 0.0.0.0. central/deploy.sh refuses that value. central/deploy.sh was not started.")
  fi
  if [[ "$bind" == "192.0.2.1" ]]; then
    found+=("ONBOARD_BIND is the documentation placeholder 192.0.2.1 in the unit file. Replace it on the server checkout with the address Caddy can reach. Do not commit that edit. central/deploy.sh was not started.")
  fi
  if [[ ! -f "$archive" ]]; then
    found+=("tools/package.sh is not in this repository. central/deploy.sh requires ${archive} and its error text says to run tools/package.sh first. That script does not exist. This wrapper does not build the archive. central/deploy.sh was not started.")
  fi
  if ((${#found[@]} > 0)); then
    printf '%s\n' "${found[@]}" >&2
    return 2
  fi
  return 0
}

# VERSION_ID from an os-release file. pkg/install.sh accepts 22.04 and 24.04.
ubuntu_supported() {
  local file="${1:-}"
  local version=""
  if [[ -z "$file" ]]; then
    file="${ASSISTANT_OS_RELEASE:-/etc/os-release}"
  fi
  [[ -f "$file" ]] || return 1
  version="$(sed -n 's/^VERSION_ID="\([^"]*\)".*/\1/p' "$file" | head -n 1)"
  case "$version" in
    22.04|24.04) return 0 ;;
    *) return 1 ;;
  esac
}

rehearsal_env_set() {
  [[ -n "${ASSISTANT_REHEARSE:-}${ASSISTANT_ROOT:-}${ASSISTANT_UNIT_FILE:-}${ASSISTANT_TEMPLATE_TAR:-}${ASSISTANT_OS_RELEASE:-}" ]]
}

refuse_rehearsal_on_apply() {
  if rehearsal_env_set; then
    blocked "Unset ASSISTANT_REHEARSE, ASSISTANT_ROOT, ASSISTANT_UNIT_FILE, ASSISTANT_TEMPLATE_TAR, and ASSISTANT_OS_RELEASE before apply. The installer was not started."
  fi
}

# Print-only reads overrides only when ASSISTANT_REHEARSE=1.
refuse_partial_rehearsal() {
  if [[ -z "${ASSISTANT_REHEARSE:-}" ]] && [[ -n "${ASSISTANT_ROOT:-}${ASSISTANT_UNIT_FILE:-}${ASSISTANT_TEMPLATE_TAR:-}${ASSISTANT_OS_RELEASE:-}" ]]; then
    blocked "Set ASSISTANT_REHEARSE=1 together with rehearsal paths, or unset them. Nothing was started."
  fi
}
