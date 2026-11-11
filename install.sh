#!/bin/bash
# Public bootstrap for a fresh Ubuntu machine.
# Usage, with stdin not a terminal:
#   curl -fsSL <raw url of this file> | sudo bash
# The account and repository names are stored encoded and decoded only in memory.
# REF overrides the pinned revision. BOOTSTRAP_URL and BOOTSTRAP_SHA256 are
# test overrides. A missing checksum is a failed install, not a skip.
main() {
  set -euo pipefail
  exec </dev/null

  PINNED_SHA256=""
  OWNER_B64="YmJiMTk4OTEtY3JlYXRvcg=="
  REPO_B64="cGVyc29uYWwtYXNzaXN0YW4="
  REF_B64="Y3Vyc29yL2ludGVncmF0ZWQtcHJvZHVjdA=="

  refuse_token() {
    printf '%s\n' "refusing: this installer does not accept an API token" >&2
    exit 1
  }

  local arg
  for arg in "$@"; do
    case "$arg" in
      --hetzner-token|--hcloud-token|--api-token|--hetzner-token=*|--hcloud-token=*|--api-token=*)
        refuse_token
        ;;
    esac
  done

  decode() {
    printf '%s' "$1" | base64 -d
  }

  local owner repo ref want url
  owner="$(decode "$OWNER_B64")"
  repo="$(decode "$REPO_B64")"
  if [[ -n "${REF:-}" ]]; then
    ref="$REF"
  else
    ref="$(decode "$REF_B64")"
  fi
  case "$owner" in
    ""|*[!A-Za-z0-9._-]*|*..*) printf '%s\n' "invalid repository coordinate" >&2; exit 1 ;;
  esac
  case "$repo" in
    ""|*[!A-Za-z0-9._-]*|*..*) printf '%s\n' "invalid repository coordinate" >&2; exit 1 ;;
  esac
  case "$ref" in
    ""|*[!A-Za-z0-9._/-]*|*..*|/*|*/) printf '%s\n' "invalid ref" >&2; exit 1 ;;
  esac

  want="${BOOTSTRAP_SHA256:-$PINNED_SHA256}"
  if [[ ! "$want" =~ ^[0-9a-f]{64}$ ]]; then
    printf '%s\n' "tarball checksum is not pinned" >&2
    exit 2
  fi

  if [[ -n "${BOOTSTRAP_URL:-}" ]]; then
    url="$BOOTSTRAP_URL"
    case "$url" in
      http://*|https://*) ;;
      *) printf '%s\n' "invalid archive url" >&2; exit 1 ;;
    esac
  else
    url="https://codeload.github.com/${owner}/${repo}/tar.gz/${ref}"
  fi

  if ! command -v curl >/dev/null 2>&1; then
    if [[ "$(id -u)" -ne 0 ]]; then
      printf '%s\n' "curl is required" >&2
      exit 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y ca-certificates curl
  fi

  local tarball dest tops top
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  tarball="$work/archive.tar.gz"
  dest="$work/src"
  mkdir -p "$dest"
  if ! curl -fsSL --retry 3 --retry-delay 2 -o "$tarball" "$url" 2>"$work/curl.err"; then
    printf '%s\n' "download of the pinned archive failed" >&2
    exit 1
  fi
  if ! printf '%s  %s\n' "$want" "$tarball" | sha256sum -c - >/dev/null; then
    printf '%s\n' "tarball checksum did not match" >&2
    exit 1
  fi
  tar -xzf "$tarball" -C "$dest"
  tops=()
  while IFS= read -r top; do
    tops+=("$top")
  done < <(find "$dest" -mindepth 1 -maxdepth 1 -type d)
  if [[ "${#tops[@]}" -ne 1 || ! -f "${tops[0]}/pkg/install.sh" ]]; then
    printf '%s\n' "archive layout was not a single repository tree" >&2
    exit 1
  fi
  SELF_HOST=1 bash "${tops[0]}/pkg/install.sh" --yes "$@"
}

main "$@"
