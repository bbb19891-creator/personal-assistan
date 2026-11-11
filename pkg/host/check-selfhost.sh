#!/usr/bin/env bash
# Prove the public installer stays self-host.
#
# Always:
#   the shell linter on the host and install shell scripts
#   agentsctl self-check (per-tenant secrets, release-access, no manager endpoint)
#   install-host.sh --dry-run (template units, no packaged secrets)
#   a rootless dry-run of install.sh
#
# Rootless dry-run (no container runtime, no root, no package install):
#   env -u SELF_HOST bash pkg/install.sh --dry-run --yes --render-dir "$DIR"
#   That renders the systemd units, the self-host drop-in, and the env key
#   names. Manager hook assignments must be absent. SELF_HOST=1 must be set.
#   The same assertion runs when a container runtime is available.
#
# Container dry-run (required):
#   docker or podman, when `info` succeeds, runs the same install.sh --dry-run
#   inside ubuntu:24.04 against a mounted tree.
#   The linter and the rootless dry-run still run when no runtime answers.
#   A missing runtime is a failed check: the script exits non-zero and does
#   not call the container dry-run a pass.
#
# Exit status is non-zero when any check fails, including a missing runtime.
set -euo pipefail

HOST_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="$(cd "$HOST_DIR/.." && pwd)"

if ! command -v shellcheck >/dev/null 2>&1; then
  echo "shellcheck: missing (exit 127)" >&2
  exit 127
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

shell_files=(
  "$PKG_DIR/install.sh"
  "$HOST_DIR/install-host.sh"
  "$HOST_DIR/check-selfhost.sh"
  "$PKG_DIR/deploy/agentctl"
)

for file in "${shell_files[@]}"; do
  set +e
  shellcheck --shell=bash "$file"
  code=$?
  set -e
  if [ "$code" -ne 0 ]; then
    echo "shellcheck ${file}: fail (exit ${code})" >&2
    exit "$code"
  fi
  echo "shellcheck ${file}: pass (exit 0)"
done

set +e
python3 -m py_compile "$HOST_DIR/agentsctl"
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "agentsctl py_compile: fail (exit ${code})" >&2
  exit "$code"
fi
echo "agentsctl py_compile: pass (exit 0)"

set +e
python3 "$HOST_DIR/agentsctl" self-check
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "agentsctl self-check: fail (exit ${code})" >&2
  exit "$code"
fi
echo "agentsctl self-check: pass (exit 0)"

set +e
bash "$HOST_DIR/install-host.sh" --dry-run
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "install-host dry-run: fail (exit ${code})" >&2
  exit "$code"
fi
set +e
bash "$HOST_DIR/install-host.sh" --dry-run
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "install-host dry-run rerun: fail (exit ${code})" >&2
  exit "$code"
fi
echo "install-host dry-run: pass (exit 0)"

set +e
bash "$HOST_DIR/install-host.sh" --hetzner-token reject-me --dry-run >"$tmp/host-token.out" 2>&1
code=$?
set -e
if [ "$code" -eq 0 ]; then
  echo "install-host api token flag was accepted" >&2
  exit 1
fi
if grep -q 'reject-me' "$tmp/host-token.out"; then
  echo "install-host api token flag echoed its value" >&2
  exit 1
fi
echo "install-host api token refusal: pass (exit ${code})"

assert_rendered() {
  local root="$1"
  python3 - "$root" <<'PY'
import os, sys
root = sys.argv[1]
keys = (
    "MANAGER_URL",
    "MANAGER_TOKEN",
    "MANAGER_ENDPOINT",
    "STATUS_REPORTER_URL",
    "STATUS_REPORT_URL",
    "HEARTBEAT_URL",
    "HEARTBEAT_ENDPOINT",
    "TEAM_REQUEST_URL",
    "TEAM_REQUEST_HOOK",
    "TEAM_REQUEST_TOKEN",
)
hook_names = {".manager_token", "manager.url", "manager-endpoint", ".manager_endpoint"}
bad = False
saw_self_host = False
saw_unset = False
names = set()
for dirpath, _dirnames, filenames in os.walk(root):
    for name in filenames:
        names.add(name)
        path = os.path.join(dirpath, name)
        if name in hook_names:
            print(f"{path}:0 category=manager-hook")
            bad = True
            continue
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        if name == "keys":
            for number, line in enumerate(lines, 1):
                if line.strip() in keys:
                    print(f"{path}:{number} category=manager-hook")
                    bad = True
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("UnsetEnvironment="):
                if "MANAGER_URL" in stripped and "HEARTBEAT_URL" in stripped and "TEAM_REQUEST_URL" in stripped and "STATUS_REPORT_URL" in stripped:
                    saw_unset = True
                continue
            if stripped.startswith("#"):
                continue
            if "Environment=SELF_HOST=1" in stripped:
                saw_self_host = True
            if "x-manager-token" in stripped or "/intake" in stripped or ".manager_token" in stripped:
                print(f"{path}:{number} category=manager-hook")
                bad = True
                continue
            for key in keys:
                if (key + "=") in stripped:
                    print(f"{path}:{number} category=manager-hook")
                    bad = True
                    break
needed = ("assistant-bot.service", "assistant-bridge.service", "assistant-setup.service", "self-host.conf")
for name in needed:
    if name not in names:
        print(f"{root}:0 category=missing-{name}")
        bad = True
if not saw_self_host:
    print(f"{root}:0 category=self-host-flag-missing")
    bad = True
if not saw_unset:
    print(f"{root}:0 category=manager-unset-missing")
    bad = True
sys.exit(1 if bad else 0)
PY
}

rootless="$tmp/rootless"
mkdir -p "$rootless"
set +e
env -u SELF_HOST bash "$PKG_DIR/install.sh" --dry-run --yes --render-dir "$rootless"
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "rootless install dry-run: fail (exit ${code})" >&2
  exit "$code"
fi
set +e
assert_rendered "$rootless"
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "rootless manager-hook assertion: fail (exit ${code})" >&2
  exit "$code"
fi
echo "rootless install dry-run: pass (exit 0)"

bad="$tmp/self-host-off"
mkdir -p "$bad"
set +e
SELF_HOST=0 bash "$PKG_DIR/install.sh" --dry-run --yes --render-dir "$bad" >"$tmp/self-host-off.out" 2>&1
code=$?
set -e
if [ "$code" -eq 0 ]; then
  echo "SELF_HOST=0 was accepted" >&2
  exit 1
fi
echo "SELF_HOST=0 refusal: pass (exit ${code})"

set +e
bash "$PKG_DIR/install.sh" --hetzner-token reject-me --dry-run --yes >"$tmp/token.out" 2>&1
code=$?
set -e
if [ "$code" -eq 0 ]; then
  echo "install.sh api token flag was accepted" >&2
  exit 1
fi
if grep -q 'reject-me' "$tmp/token.out"; then
  echo "install.sh api token flag echoed its value" >&2
  exit 1
fi
echo "install.sh api token refusal: pass (exit ${code})"

runtime=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  runtime="docker"
elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
  runtime="podman"
fi

if [ -z "$runtime" ]; then
  echo "container dry-run: fail (no docker or podman)" >&2
  exit 1
fi

image="ubuntu:24.04"
if ! "$runtime" image inspect "$image" >/dev/null 2>&1; then
  set +e
  "$runtime" pull "$image"
  code=$?
  set -e
  if [ "$code" -ne 0 ]; then
    echo "container dry-run: fail (exit ${code})" >&2
    exit "$code"
  fi
fi

cout="$tmp/container"
mkdir -p "$cout"
set +e
"$runtime" run --rm --network=none \
  -v "$PKG_DIR:/pkg:ro" \
  -v "$cout:/out" \
  -e SELF_HOST=1 \
  "$image" \
  bash /pkg/install.sh --dry-run --yes --render-dir /out
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "container dry-run: fail (exit ${code})" >&2
  exit "$code"
fi
set +e
assert_rendered "$cout"
code=$?
set -e
if [ "$code" -ne 0 ]; then
  echo "container manager-hook assertion: fail (exit ${code})" >&2
  exit "$code"
fi
echo "container dry-run: pass (exit 0)"
