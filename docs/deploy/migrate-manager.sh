#!/usr/bin/env bash
# Copy the live manager data into the new AM_DATA layout.
# Takes a backup first. Repeating this command takes another backup and does
# not replace data files that are already in the new directory.
# Does not reload systemd and does not restart the service.
#
#   sudo bash docs/deploy/migrate-manager.sh
# Rehearsal only (do not set this on the live server):
#   ASSISTANT_ROOT=/tmp/rehearsal bash docs/deploy/migrate-manager.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -z "${ASSISTANT_ROOT:-}" && "$(id -u)" -ne 0 ]]; then
  blocked "run as root. The migration was not started."
fi

containerized_caddy() {
  if [[ "${AM_CADDY_CONTAINER:-}" == "1" ]]; then
    return 0
  fi
  local runtime listing
  for runtime in docker podman; do
    if ! command -v "$runtime" >/dev/null 2>&1; then
      continue
    fi
    listing="$("$runtime" ps --format '{{.Names}} {{.Image}}' 2>/dev/null || true)"
    if printf '%s\n' "$listing" | grep -qi 'caddy'; then
      return 0
    fi
  done
  return 1
}

proxy_is_loopback() {
  case "${1:-}" in
    ""|127.0.0.1|localhost|::1) return 0 ;;
  esac
  return 1
}

effective_proxy_bind() {
  if [[ -n "${AM_BIND_PROXY:-}" ]]; then
    printf '%s' "$AM_BIND_PROXY"
    return 0
  fi
  local dir conf line found
  dir="$(live_path /etc/systemd/system/assistant-manager.service.d)"
  found=""
  if [[ -d "$dir" ]]; then
    shopt -s nullglob
    for conf in "$dir"/*.conf; do
      while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
          Environment=AM_BIND_PROXY=*)
            found="${line#Environment=AM_BIND_PROXY=}"
            ;;
        esac
      done < "$conf"
    done
    shopt -u nullglob
  fi
  if [[ -n "$found" ]]; then
    printf '%s' "$found"
    return 0
  fi
  printf '%s' "127.0.0.1"
}

if containerized_caddy && proxy_is_loopback "$(effective_proxy_bind)"; then
  printf '%s\n' "containerized Caddy cannot use a loopback AM_BIND_PROXY. Set the docker bridge address in a systemd drop-in. The migration was not started." >&2
  exit 1
fi

src_logical="/opt/agents/_manager"
data_logical="/var/lib/assistant-manager"
src="$(live_path "$src_logical")"
dest="$(live_path "$data_logical")"
if [[ -L "$src" || ! -d "$src" ]]; then
  die "manager data directory is missing"
fi

archive="$(bash "$SCRIPT_DIR/backup.sh" manager-before \
  --path "$src_logical" \
  --path "$data_logical" \
  --path /etc/systemd/system/assistant-manager.service \
  --path /etc/systemd/system/assistant-manager.service.d \
  --path /usr/local/lib/assistant-manager \
  --path /etc/assistant-manager/wa.manager.env \
  --unit assistant-manager)"

mkdir -p "$dest"
chmod 700 "$dest"

copy_file() {
  local name="$1"
  local from="$src/$name"
  local to="$dest/$name"
  if [[ ! -e "$from" && ! -L "$from" ]]; then
    return 0
  fi
  if [[ -L "$from" || ! -f "$from" ]]; then
    die "refusing a non-regular file in manager data"
  fi
  if [[ -e "$to" || -L "$to" ]]; then
    printf 'kept %s\n' "$name"
    return 0
  fi
  cp -p "$from" "$to"
  chmod 600 "$to"
  printf 'copied %s\n' "$name"
}

copy_file requests.json
copy_file state.json
copy_file api_token
copy_file proxy_secret
copy_file outbox.log

write_new() {
  local path="$1"
  local body="$2"
  if [[ -e "$path" || -L "$path" ]]; then
    printf 'kept %s\n' "$(basename "$path")"
    return 0
  fi
  printf '%s\n' "$body" > "$path"
  chmod 600 "$path"
  printf 'created %s\n' "$(basename "$path")"
}

write_new "$dest/ledger.json" '{"next_id": 1, "clients": []}'
write_new "$dest/invites.json" '{"codes": {}}'
write_new "$dest/costs.json" '{}'
write_new "$dest/status_history.json" '{}'

lib="$(live_path /usr/local/lib/assistant-manager)"
unit_dir="$(live_path /etc/systemd/system)"
mkdir -p "$lib" "$unit_dir/assistant-manager.service.d"
install -m 644 "$REPO/platform/assistant_manager.py" "$lib/assistant_manager.py"
install -m 644 "$REPO/platform/systemd/assistant-manager.service" "$unit_dir/assistant-manager.service"
install -m 644 "$REPO/platform/systemd/assistant-manager.service.d/10-isolation.conf" \
  "$unit_dir/assistant-manager.service.d/10-isolation.conf"
if [[ -n "${AM_BIND_PROXY:-}" ]] && ! proxy_is_loopback "$AM_BIND_PROXY"; then
  printf '[Service]\nEnvironment=AM_BIND_PROXY=%s\n' "$AM_BIND_PROXY" \
    > "$unit_dir/assistant-manager.service.d/20-proxy-bind.conf"
  chmod 644 "$unit_dir/assistant-manager.service.d/20-proxy-bind.conf"
fi

wa_src="$(live_path /etc/assistant-manager/wa.env)"
wa_dest="$(live_path /etc/assistant-manager/wa.manager.env)"
read_key() {
  local file="$1" key="$2" line k v
  [[ -f "$file" && ! -L "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    k="${line%%=*}"
    v="${line#*=}"
    k="${k#"${k%%[![:space:]]*}"}"
    k="${k%"${k##*[![:space:]]}"}"
    if [[ "$k" != "$key" ]]; then
      continue
    fi
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    v="${v#\"}"
    v="${v%\"}"
    v="${v#\'}"
    v="${v%\'}"
    printf '%s' "$v"
    return 0
  done < "$file"
  return 1
}

if [[ -f "$wa_src" && ! -L "$wa_src" ]]; then
  bridge_url="$(read_key "$wa_src" WA_BRIDGE_URL || true)"
  bridge_token="$(read_key "$wa_src" WA_BRIDGE_TOKEN || true)"
  admin_jid="$(read_key "$wa_src" WA_ADMIN_JID || true)"
  if [[ -z "$admin_jid" ]]; then
    phone="$(read_key "$wa_src" WA_OWNER_PHONE || true)"
    digits="$(printf '%s' "$phone" | tr -cd '0-9')"
    if [[ -n "$digits" ]]; then
      admin_jid="${digits}@s.whatsapp.net"
    fi
  fi
  mkdir -p "$(dirname "$wa_dest")"
  {
    printf 'AM_WA_BRIDGE_URL=%s\n' "$bridge_url"
    printf 'AM_WA_BRIDGE_TOKEN=%s\n' "$bridge_token"
    printf 'AM_WA_ADMIN_JID=%s\n' "$admin_jid"
  } > "$wa_dest"
  chmod 600 "$wa_dest"
  if [[ -z "$bridge_url" || -z "$bridge_token" || -z "$admin_jid" ]]; then
    printf '%s\n' "whatsapp alert settings are incomplete; values were not printed"
  else
    printf '%s\n' "whatsapp alert settings were copied"
  fi
else
  printf '%s\n' "whatsapp env file was not present; alert settings were not copied"
fi

marker="$dest/.migration-archive"
if [[ ! -f "$marker" ]]; then
  printf '%s\n' "$archive" > "$marker"
  chmod 600 "$marker"
fi

printf 'archive: %s\n' "$archive"
printf '%s\n' "files are in place. This script does not reload systemd and does not restart the service."
