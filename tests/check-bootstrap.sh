#!/usr/bin/env bash
# Run the root bootstrap inside Ubuntu.
# A missing docker and podman is a failed check. A skip is not a pass.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v shellcheck >/dev/null 2>&1; then
  printf '%s\n' "shellcheck: missing (exit 127)" >&2
  exit 127
fi
shellcheck --shell=bash "$ROOT/install.sh"
grep -q 'tarball checksum is not pinned' "$ROOT/install.sh"
grep -q 'exit 2' "$ROOT/install.sh"
if ! grep -q '^  PINNED_SHA256="[0-9a-f]\{64\}"$' "$ROOT/install.sh"; then
  set +e
  empty_out="$(env -u BOOTSTRAP_SHA256 -u BOOTSTRAP_URL -u REF bash "$ROOT/install.sh" --dry-run </dev/null 2>&1)"
  empty_code=$?
  set -e
  if [[ "$empty_code" -ne 2 ]]; then
    printf '%s\n' "bootstrap treated a missing checksum as exit ${empty_code}" >&2
    exit 1
  fi
  if ! printf '%s\n' "$empty_out" | grep -q 'tarball checksum is not pinned'; then
    printf '%s\n' "bootstrap did not explain the missing checksum" >&2
    exit 1
  fi
  printf '%s\n' "bootstrap missing checksum: pass (exit 2)"
fi

set +e
token_out="$(bash "$ROOT/install.sh" --api-token reject-me </dev/null 2>&1)"
token_code=$?
set -e
if [[ "$token_code" -eq 0 ]]; then
  printf '%s\n' "bootstrap accepted an API token flag" >&2
  exit 1
fi
if printf '%s' "$token_out" | grep -q 'reject-me'; then
  printf '%s\n' "bootstrap echoed an API token value" >&2
  exit 1
fi
printf '%s\n' "bootstrap api token refusal: pass (exit ${token_code})"

runtime=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  runtime="docker"
elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
  runtime="podman"
fi
if [[ -z "$runtime" ]]; then
  printf '%s\n' "container bootstrap: fail (no docker or podman)" >&2
  exit 1
fi

image="ubuntu:24.04"
if ! "$runtime" image inspect "$image" >/dev/null 2>&1; then
  "$runtime" pull "$image"
fi

work="$(mktemp -d)"
# shellcheck disable=SC2317
cleanup() {
  if [[ -n "${server_pid:-}" ]]; then
    kill "$server_pid" >/dev/null 2>&1 || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

stage="$work/stage/personal-assistan-testref"
mkdir -p "$stage"
tar -C "$ROOT" --exclude .git --exclude __pycache__ -cf - . | tar -C "$stage" -xf -
tar -czf "$work/archive.tar.gz" -C "$work/stage" personal-assistan-testref
sum="$(sha256sum "$work/archive.tar.gz" | awk '{print $1}')"
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
python3 -m http.server --bind 127.0.0.1 "$port" --directory "$work" >"$work/http.log" 2>&1 &
server_pid=$!
ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS -o /dev/null "http://127.0.0.1:${port}/archive.tar.gz"; then
    ready=1
    break
  fi
  sleep 0.2
done
if [[ "$ready" -ne 1 ]]; then
  printf '%s\n' "container bootstrap: fail (archive server did not start)" >&2
  exit 1
fi

cat > "$work/guest.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
bash /install.sh --dry-run --render-dir /tmp/render </dev/null
cat /install.sh | bash -s -- --dry-run --render-dir /tmp/render-pipe
test -f /tmp/render/self-host-mode
test ! -e /tmp/render/env/.manager_token
grep -q '^SELF_HOST=1$' /tmp/render/self-host-mode
set +e
out="$(bash /install.sh --hetzner-token reject-me 2>&1)"
code=$?
set -e
test "$code" -ne 0
if printf '%s' "$out" | grep -q 'reject-me'; then
  printf '%s\n' "container echoed an API token value" >&2
  exit 1
fi
printf '%s\n' "container token refusal: pass"
EOF
set +e
"$runtime" run --rm --network=host \
  -v "$ROOT/install.sh:/install.sh:ro" \
  -v "$work/guest.sh:/guest.sh:ro" \
  -e REF=testref \
  -e BOOTSTRAP_URL="http://127.0.0.1:${port}/archive.tar.gz" \
  -e BOOTSTRAP_SHA256="$sum" \
  "$image" \
  bash /guest.sh
code=$?
set -e
if [[ "$code" -ne 0 ]]; then
  printf '%s\n' "container bootstrap: fail (exit ${code})" >&2
  exit "$code"
fi
printf '%s\n' "container bootstrap: pass"
exit 0
