#!/usr/bin/env bash
# Local checks for the wrappers. Does not run install-host.sh, deploy.sh, or install.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/lib.sh
source "$SCRIPT_DIR/lib.sh"

fail() {
  printf 'self-check failed: %s\n' "$*" >&2
  exit 1
}

assert_ok() {
  if ! "$@"; then
    fail "expected success: $*"
  fi
}

assert_fail() {
  if "$@"; then
    fail "expected failure: $*"
  fi
}

[[ "$(id -u)" -ne 0 ]] || fail "self-check refuses to run as root"

TMP="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

assert_ok valid_host "example.invalid"
assert_ok valid_host "setup.example.invalid"
assert_fail valid_host "YOUR_HOST"
assert_fail valid_host "192.0.2.1"
assert_fail valid_host "127.0.0.1"
assert_fail valid_host "example.invalid."

os_good="$TMP/os-good"
os_bad="$TMP/os-bad"
printf 'VERSION_ID="24.04"\n' > "$os_good"
printf 'VERSION_ID="20.04"\n' > "$os_bad"
assert_ok ubuntu_supported "$os_good"
assert_fail ubuntu_supported "$os_bad"
assert_ok ubuntu_supported /etc/os-release

ROOT="$TMP/root"
export ASSISTANT_ROOT="$ROOT"
export ASSISTANT_BACKUP_DIR="$TMP/backups"
mkdir -p "$ROOT/opt/agent/data" "$ROOT/usr/local/sbin"
printf 'original\n' > "$ROOT/opt/agent/data/keep.txt"
chmod 600 "$ROOT/opt/agent/data/keep.txt"
printf 'ctl-v1\n' > "$ROOT/usr/local/sbin/agentsctl"
chmod 755 "$ROOT/usr/local/sbin/agentsctl"

archive="$(bash "$SCRIPT_DIR/backup.sh" rehearsal \
  --path /opt/agent \
  --path /usr/local/sbin/agentsctl \
  --path /opt/agent-onboarding \
  --unit assistant-status.timer)"
[[ -f "$archive" ]] || fail "backup did not write an archive"
[[ "$(stat -c '%a' "$archive")" == "600" ]] || fail "archive mode is not 600"
[[ "$(stat -c '%a' "$ASSISTANT_BACKUP_DIR")" == "700" ]] || fail "backup dir mode is not 700"

printf 'changed\n' > "$ROOT/opt/agent/data/keep.txt"
printf 'extra\n' > "$ROOT/opt/agent/data/extra.txt"
printf 'ctl-v2\n' > "$ROOT/usr/local/sbin/agentsctl"
mkdir -p "$ROOT/opt/agent-onboarding"
printf 'new\n' > "$ROOT/opt/agent-onboarding/x"

rollback_out="$(bash "$SCRIPT_DIR/rollback.sh" "$archive" 2>&1)"
printf '%s\n' "$rollback_out" | grep -q 'restored files only' || fail "rollback did not say it restored files only"
printf '%s\n' "$rollback_out" | grep -q 'assistant-status.timer' || fail "rollback did not list the recorded unit"
grep -q 'original' "$ROOT/opt/agent/data/keep.txt" || fail "file contents were not restored"
[[ ! -e "$ROOT/opt/agent/data/extra.txt" ]] || fail "extra file survived rollback"
[[ ! -e "$ROOT/opt/agent-onboarding" ]] || fail "path that was absent was not removed"
grep -q 'ctl-v1' "$ROOT/usr/local/sbin/agentsctl" || fail "agentsctl contents were not restored"
[[ "$(stat -c '%a' "$ROOT/opt/agent/data/keep.txt")" == "600" ]] || fail "file mode was not restored"

bash "$SCRIPT_DIR/rollback.sh" "$archive" >/dev/null 2>&1
grep -q 'original' "$ROOT/opt/agent/data/keep.txt" || fail "second rollback changed the file"
[[ ! -e "$ROOT/opt/agent-onboarding" ]] || fail "second rollback recreated an absent path"

archive_two="$(bash "$SCRIPT_DIR/backup.sh" rehearsal --path /opt/agent)"
[[ "$archive_two" != "$archive" ]] || fail "second backup overwrote the first archive"

set +e
bash "$SCRIPT_DIR/backup.sh" bad --path /etc/passwd >/dev/null 2>"$TMP/bad-path.err"
bad_status=$?
set -e
[[ "$bad_status" -eq 2 ]] || fail "disallowed path did not exit 2"
grep -q 'allowlist' "$TMP/bad-path.err" || fail "disallowed path was not explained"

canary="$ROOT/etc/passwd"
mkdir -p "$ROOT/etc"
printf 'safe\n' > "$canary"
bad_stage="$TMP/bad-stage"
mkdir -p "$bad_stage/payload/etc"
printf '1\n' > "$bad_stage/format.txt"
printf '/etc/passwd\tfile\t644\n' > "$bad_stage/manifest.tsv"
: > "$bad_stage/units.txt"
printf 'hacked\n' > "$bad_stage/payload/etc/passwd"
tar -czf "$TMP/bad.tar.gz" -C "$bad_stage" format.txt manifest.tsv units.txt payload
set +e
bash "$SCRIPT_DIR/rollback.sh" "$TMP/bad.tar.gz" >"$TMP/bad-roll.out" 2>"$TMP/bad-roll.err"
roll_status=$?
set -e
[[ "$roll_status" -ne 0 ]] || fail "malicious archive was restored"
grep -q 'safe' "$canary" || fail "canary was overwritten"

unset ASSISTANT_ROOT
plan="$(bash "$SCRIPT_DIR/apply-tenant-host.sh" shared --onboard-host example.invalid --print-only)"
printf '%s\n' "$plan" | grep -q 'pkg/host/install-host.sh' || fail "shared plan missed install-host.sh"
printf '%s\n' "$plan" | grep -q -- '--onboard-host example.invalid' || fail "shared plan missed --onboard-host"
printf '%s\n' "$plan" | grep -q 'no installer was run' || fail "shared plan did not say it was print-only"
if printf '%s\n' "$plan" | grep -q -- '--setup-token\|--force\|--yes'; then
  fail "shared plan invented a flag"
fi

bare="$(bash "$SCRIPT_DIR/apply-tenant-host.sh" shared --print-only)"
printf '%s\n' "$bare" | grep -E -q 'run: bash .*pkg/host/install-host.sh$' || fail "shared plan without a host passed extra flags"

dedicated="$(bash "$SCRIPT_DIR/apply-tenant-host.sh" dedicated --domain example.invalid --print-only)"
printf '%s\n' "$dedicated" | grep -q 'pkg/install.sh' || fail "dedicated plan missed install.sh"
printf '%s\n' "$dedicated" | grep -q -- '--yes' || fail "dedicated plan missed --yes"
printf '%s\n' "$dedicated" | grep -q -- '--domain example.invalid' || fail "dedicated plan missed --domain"
if printf '%s\n' "$dedicated" | grep -q -- '--setup-token\|ipify'; then
  fail "dedicated plan included a secret flag or address lookup"
fi

set +e
bash "$SCRIPT_DIR/apply-tenant-host.sh" dedicated --print-only >"$TMP/no-domain.out" 2>"$TMP/no-domain.err"
no_domain=$?
bash "$SCRIPT_DIR/apply-tenant-host.sh" shared --onboard-host example.invalid --force >"$TMP/force.out" 2>"$TMP/force.err"
force_status=$?
bash "$SCRIPT_DIR/apply-tenant-host.sh" dedicated --domain example.invalid >"$TMP/root.out" 2>"$TMP/root.err"
root_status=$?
bash "$SCRIPT_DIR/apply-onboarding-host.sh" --host example.invalid --print-only >"$TMP/onboard.out" 2>"$TMP/onboard.err"
onboard_status=$?
set -e
[[ "$no_domain" -eq 2 ]] || fail "dedicated plan without --domain did not exit 2"
[[ "$force_status" -eq 2 ]] || fail "unknown flag did not exit 2"
[[ "$root_status" -eq 2 ]] || fail "apply without root did not exit 2"
grep -q 'not started' "$TMP/root.err" || fail "apply without root did not say the installer was not started"
[[ "$onboard_status" -eq 2 ]] || fail "onboarding plan on the baseline tree should be blocked"
grep -q '192.0.2.1' "$TMP/onboard.err" || fail "onboarding plan did not report the placeholder bind"
grep -q 'tools/package.sh' "$TMP/onboard.err" || fail "onboarding plan did not report the missing packager"
if grep -q '^run:' "$TMP/onboard.out"; then
  fail "blocked onboarding plan still printed an installer command"
fi
[[ ! -e /opt/agent && ! -e /opt/agents && ! -e /opt/agent-onboarding ]] || fail "a wrapper created a live install path"

printf 'VERSION_ID="20.04"\n' > "$TMP/os-release"
set +e
ASSISTANT_REHEARSE=1 ASSISTANT_OS_RELEASE="$TMP/os-release" \
  bash "$SCRIPT_DIR/apply-tenant-host.sh" dedicated --domain example.invalid >"$TMP/rehearse-apply.out" 2>"$TMP/rehearse-apply.err"
rehearse_apply=$?
set -e
[[ "$rehearse_apply" -eq 2 ]] || fail "apply accepted rehearsal variables"
grep -q 'not started' "$TMP/rehearse-apply.err" || fail "rehearsal apply did not say it was not started"

cat > "$TMP/unit" <<'EOF'
[Service]
Environment=ONBOARD_PORT=8840
Environment=ONBOARD_BIND=127.0.0.1
EOF
: > "$TMP/template.tar.gz"
ready="$(ASSISTANT_REHEARSE=1 ASSISTANT_UNIT_FILE="$TMP/unit" ASSISTANT_TEMPLATE_TAR="$TMP/template.tar.gz" \
  bash "$SCRIPT_DIR/apply-onboarding-host.sh" --host example.invalid --print-only)"
printf '%s\n' "$ready" | grep -q 'central/deploy.sh example.invalid' || fail "ready plan missed deploy.sh"
printf '%s\n' "$ready" | grep -q 'tools/package.sh is not in this repository' || fail "ready plan hid the missing packager"
printf '%s\n' "$ready" | grep -q 'print-caddy-snippet.sh --host example.invalid' || fail "ready plan missed the snippet step"
if printf '%s\n' "$ready" | grep -q -- '--setup-token\|--yes\|--domain'; then
  fail "onboarding plan invented a deploy.sh flag"
fi

cat > "$TMP/unit-any" <<'EOF'
[Service]
Environment=ONBOARD_PORT=8840
Environment=ONBOARD_BIND=0.0.0.0
EOF
set +e
ASSISTANT_REHEARSE=1 ASSISTANT_UNIT_FILE="$TMP/unit-any" ASSISTANT_TEMPLATE_TAR="$TMP/template.tar.gz" \
  bash "$SCRIPT_DIR/apply-onboarding-host.sh" --host example.invalid --print-only >"$TMP/any.out" 2>"$TMP/any.err"
any_status=$?
set -e
[[ "$any_status" -eq 2 ]] || fail "0.0.0.0 bind was accepted"
grep -q '0.0.0.0' "$TMP/any.err" || fail "0.0.0.0 refusal was not explained"

snippet="$(bash "$SCRIPT_DIR/print-caddy-snippet.sh" --host example.invalid 2>"$TMP/snippet.err")"
printf '%s\n' "$snippet" | grep -q '^example.invalid {$' || fail "snippet host was not substituted"
printf '%s\n' "$snippet" | grep -q '192.0.2.1:8840' || fail "snippet lost the documented reverse_proxy"
if printf '%s\n' "$snippet" | grep -q '__ONBOARD_HOST__'; then
  fail "snippet placeholder was left in place"
fi
grep -q 'does not edit Caddy' "$TMP/snippet.err" || fail "snippet script did not say it leaves Caddy alone"

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x "$SCRIPT_DIR"/*.sh
fi

printf 'self-check ok\n'
