#!/usr/bin/env bash
# Copy allowlisted paths into a new archive. Does not modify those paths.
# Repeating this command writes another archive and leaves older archives in place.
#
# Usage:
#   backup.sh LABEL --path ABSOLUTE [--path ABSOLUTE ...] [--unit UNIT ...]
# Optional:
#   ASSISTANT_BACKUP_DIR   directory for archives (default /var/backups/assistant-deploy)
#   ASSISTANT_ROOT         rehearsal prefix; do not set this on the live server
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  printf '%s\n' "usage: backup.sh LABEL --path ABSOLUTE [--path ABSOLUTE ...] [--unit UNIT ...]" >&2
  exit 2
}

if [[ $# -lt 1 ]]; then
  usage
fi

label="$1"
shift
if [[ ! "$label" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]]; then
  blocked "backup label must be lowercase letters, digits, and hyphens"
fi

paths=()
units=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --path)
      [[ $# -ge 2 ]] || usage
      paths+=("$2")
      shift 2
      ;;
    --unit)
      [[ $# -ge 2 ]] || usage
      units+=("$2")
      shift 2
      ;;
    *)
      blocked "unknown argument: $1"
      ;;
  esac
done

if [[ ${#paths[@]} -eq 0 ]]; then
  usage
fi

for path in "${paths[@]}"; do
  case "$path" in
    /*) ;;
    *) blocked "path must be absolute: $path" ;;
  esac
  case "$path" in
    *..*|*$'\n'*|*[[:space:]]*) blocked "refusing unusual path: $path" ;;
  esac
  if ! path_allowed "$path"; then
    blocked "path is outside the deploy allowlist: $path"
  fi
done

for unit in "${units[@]}"; do
  if ! unit_allowed "$unit"; then
    blocked "unit is outside the deploy allowlist: $unit"
  fi
done

backup_dir="${ASSISTANT_BACKUP_DIR:-/var/backups/assistant-deploy}"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${backup_dir}/${label}-${stamp}.tar.gz"
if [[ -e "$archive" ]]; then
  archive="${backup_dir}/${label}-${stamp}-$$.tar.gz"
fi

stage="$(mktemp -d)"
cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT

printf '1\n' > "$stage/format.txt"
: > "$stage/manifest.tsv"
: > "$stage/units.txt"
mkdir -p "$stage/payload"

for unit in "${units[@]}"; do
  printf '%s\n' "$unit" >> "$stage/units.txt"
done

for path in "${paths[@]}"; do
  live="$(live_path "$path")"
  if [[ ! -e "$live" && ! -L "$live" ]]; then
    printf '%s\tabsent\t-\n' "$path" >> "$stage/manifest.tsv"
    continue
  fi
  if [[ -L "$live" ]]; then
    kind="link"
  elif [[ -d "$live" ]]; then
    kind="dir"
  elif [[ -f "$live" ]]; then
    kind="file"
  else
    die "refusing to back up a special file: $path"
  fi
  mode="$(stat -c '%a' "$live")"
  rel="${path#/}"
  parent="$(dirname "$rel")"
  mkdir -p "$stage/payload/$parent"
  cp -a "$live" "$stage/payload/$rel"
  printf '%s\t%s\t%s\n' "$path" "$kind" "$mode" >> "$stage/manifest.tsv"
done

umask 077
tar -czf "$archive" -C "$stage" format.txt manifest.tsv units.txt payload
chmod 600 "$archive"
printf '%s\n' "$archive"
