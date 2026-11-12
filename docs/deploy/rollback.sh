#!/usr/bin/env bash
# Restore paths from an archive written by backup.sh.
# Repeating this command on the same archive restores the same files.
# This script does not call systemctl, does not delete users, and does not uninstall packages.
#
# Usage:
#   rollback.sh ARCHIVE.tar.gz
# Optional:
#   ASSISTANT_ROOT   rehearsal prefix; do not set this on the live server
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"

if [[ $# -ne 1 ]]; then
  blocked "usage: rollback.sh ARCHIVE.tar.gz"
fi

archive="$1"
[[ -f "$archive" ]] || die "no such archive: $archive"

stage="$(mktemp -d)"
cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT

tar -xzf "$archive" -C "$stage"
[[ -f "$stage/format.txt" && -f "$stage/manifest.tsv" && -f "$stage/units.txt" ]] || die "archive is missing backup metadata"
[[ "$(head -n 1 "$stage/format.txt")" == "1" ]] || die "unknown backup format"

# Validate every row before changing any live path.
declare -a rows=()
while IFS=$'\t' read -r path kind mode; do
  [[ -z "$path" ]] && continue
  case "$path" in
    /*) ;;
    *) die "archive path is not absolute: $path" ;;
  esac
  case "$path" in
    *..*|*$'\n'*|*[[:space:]]*) die "archive path is unusual: $path" ;;
  esac
  if ! path_allowed "$path"; then
    die "archive path is outside the deploy allowlist: $path"
  fi
  case "$kind" in
    file|dir|link|absent) ;;
    *) die "unknown manifest state for $path" ;;
  esac
  if [[ "$kind" != "absent" ]]; then
    rel="${path#/}"
    if [[ ! -e "$stage/payload/$rel" && ! -L "$stage/payload/$rel" ]]; then
      die "archive is missing payload for $path"
    fi
  fi
  rows+=("${path}"$'\t'"${kind}"$'\t'"${mode}")
done < "$stage/manifest.tsv"

if [[ -s "$stage/units.txt" ]]; then
  while IFS= read -r unit; do
    [[ -z "$unit" ]] && continue
    if ! unit_allowed "$unit"; then
      die "archive unit is outside the deploy allowlist: $unit"
    fi
  done < "$stage/units.txt"
fi

for row in "${rows[@]}"; do
  path="${row%%$'\t'*}"
  rest="${row#*$'\t'}"
  kind="${rest%%$'\t'*}"
  live="$(live_path "$path")"
  case "$kind" in
    absent)
      if [[ -e "$live" || -L "$live" ]]; then
        rm -rf -- "$live"
      fi
      ;;
    file|dir|link)
      rel="${path#/}"
      mkdir -p -- "$(dirname "$live")"
      rm -rf -- "$live"
      cp -a "$stage/payload/$rel" "$live"
      ;;
  esac
done

printf 'restored %s\n' "$archive" >&2
printf 'rollback restored files only. It did not stop units, delete users, uninstall packages, or change the firewall.\n' >&2
if [[ -s "$stage/units.txt" ]]; then
  printf 'units recorded for this step:\n' >&2
  cat "$stage/units.txt" >&2
fi
