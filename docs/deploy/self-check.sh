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

release_archive="$(bash "$SCRIPT_DIR/backup.sh" release-unit \
  --path /etc/systemd/system/assistant-release@.service \
  --path /var/lib/assistant-manager)"
manifest="$(tar -xOf "$release_archive" manifest.tsv)"
printf '%s\n' "$manifest" | grep -F -q '/etc/systemd/system/assistant-release@.service' || fail "release unit was not recorded"
printf '%s\n' "$manifest" | grep -F -q '/var/lib/assistant-manager' || fail "manager data dir was not recorded"
printf '%s\n' "$manifest" | grep -F -q 'absent' || fail "new paths were not recorded as absent"

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
deploy_file="$SCRIPT_DIR/../../central/deploy.sh"
if grep -q 'agent-template.tar.gz' "$deploy_file"; then
  grep -q 'agent-template.tar.gz' "$TMP/onboard.err" || fail "onboarding plan did not report the baseline archive requirement"
else
  if grep -q 'agent-template.tar.gz' "$TMP/onboard.err"; then
    fail "onboarding plan reported an archive requirement this deploy.sh does not have"
  fi
fi
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
printf '%s\n' '#!/bin/bash' 'echo deploy-stub' > "$TMP/deploy-stub.sh"
ready="$(ASSISTANT_REHEARSE=1 ASSISTANT_UNIT_FILE="$TMP/unit" ASSISTANT_DEPLOY_SH="$TMP/deploy-stub.sh" \
  bash "$SCRIPT_DIR/apply-onboarding-host.sh" --host example.invalid --print-only)"
printf '%s\n' "$ready" | grep -q 'central/deploy.sh example.invalid' || fail "ready plan missed deploy.sh"
printf '%s\n' "$ready" | grep -q 'does not set PUBLIC_INSTALL_URL' || fail "ready plan missed the install URL note"
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
ASSISTANT_REHEARSE=1 ASSISTANT_UNIT_FILE="$TMP/unit-any" ASSISTANT_DEPLOY_SH="$TMP/deploy-stub.sh" \
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

mgr="$TMP/mgr"
mkdir -p "$mgr/opt/agents/_manager" "$mgr/etc/systemd/system" "$mgr/etc/assistant-manager"
printf 'old-request\n' > "$mgr/opt/agents/_manager/requests.json"
printf 'token-value\n' > "$mgr/opt/agents/_manager/api_token"
printf 'OLD-UNIT\n' > "$mgr/etc/systemd/system/assistant-manager.service"
cat > "$mgr/etc/assistant-manager/wa.env" <<'EOF'
WA_BRIDGE_URL=http://127.0.0.1:8795
WA_BRIDGE_TOKEN=bridge-token-test
WA_ADMIN_JID=owner@s.whatsapp.net
EOF
mig_out="$(ASSISTANT_ROOT="$mgr" ASSISTANT_BACKUP_DIR="$TMP/mgr-backups" bash "$SCRIPT_DIR/migrate-manager.sh")"
printf '%s\n' "$mig_out" | grep -q '^archive: ' || fail "migration did not print an archive"
if printf '%s\n' "$mig_out" | grep -q 'bridge-token-test'; then
  fail "migration printed a bridge token"
fi
archive="$(printf '%s\n' "$mig_out" | sed -n 's/^archive: //p')"
[[ -f "$archive" ]] || fail "migration archive is missing"
grep -q 'old-request' "$mgr/var/lib/assistant-manager/requests.json" || fail "requests were not copied"
grep -q 'token-value' "$mgr/var/lib/assistant-manager/api_token" || fail "api token file was not copied"
grep -q '"clients": \[\]' "$mgr/var/lib/assistant-manager/ledger.json" || fail "ledger was not created"
grep -q 'AM_DATA=/var/lib/assistant-manager' "$mgr/etc/systemd/system/assistant-manager.service" || fail "unit missed AM_DATA"
grep -q '127.0.0.1' "$mgr/etc/systemd/system/assistant-manager.service" || fail "unit missed the local bind"
grep -q 'AM_PORT=8850' "$mgr/etc/systemd/system/assistant-manager.service" || fail "unit missed the port"
grep -q 'AM_PREFIX=/manage' "$mgr/etc/systemd/system/assistant-manager.service" || fail "unit missed the manage prefix"
if grep -q 'AM_ROOT_ENV\|AM_ROOT_DB' "$mgr/etc/systemd/system/assistant-manager.service"; then
  fail "unit brought back removed settings"
fi
grep -q 'InaccessiblePaths=/root' "$mgr/etc/systemd/system/assistant-manager.service.d/10-isolation.conf" || fail "isolation drop-in missed"
grep -q 'AM_WA_BRIDGE_URL=http://127.0.0.1:8795' "$mgr/etc/assistant-manager/wa.manager.env" || fail "bridge url was not mapped"
grep -q 'AM_WA_BRIDGE_TOKEN=bridge-token-test' "$mgr/etc/assistant-manager/wa.manager.env" || fail "bridge token was not mapped"
grep -q 'AM_WA_ADMIN_JID=owner@s.whatsapp.net' "$mgr/etc/assistant-manager/wa.manager.env" || fail "admin jid was not mapped"
printf '%s\n' '{"next_id": 2, "clients": []}' > "$mgr/var/lib/assistant-manager/ledger.json"
ASSISTANT_ROOT="$mgr" ASSISTANT_BACKUP_DIR="$TMP/mgr-backups" bash "$SCRIPT_DIR/migrate-manager.sh" >/dev/null
grep -q '"next_id": 2' "$mgr/var/lib/assistant-manager/ledger.json" || fail "second migration replaced the ledger"
ASSISTANT_ROOT="$mgr" bash "$SCRIPT_DIR/rollback-manager.sh" "$archive" >"$TMP/mgr-roll.out"
grep -q 'restored files only' "$TMP/mgr-roll.out" || fail "manager rollback did not say it restored files only"
grep -q 'OLD-UNIT' "$mgr/etc/systemd/system/assistant-manager.service" || fail "previous unit was not restored"
[[ ! -e "$mgr/var/lib/assistant-manager" ]] || fail "new data dir survived rollback of an absent path"
grep -q 'old-request' "$mgr/opt/agents/_manager/requests.json" || fail "source requests were not restored"

phone_root="$TMP/mgr-phone"
mkdir -p "$phone_root/opt/agents/_manager" "$phone_root/etc/assistant-manager"
printf '{}\n' > "$phone_root/opt/agents/_manager/state.json"
{
  printf 'WA_BRIDGE_URL=%s\n' 'http://127.0.0.1:8795'
  printf 'WA_BRIDGE_TOKEN=%s\n' 'bridge-token-test'
  printf 'WA_OWNER_PHONE=%s%s%s\n' '555' '010' '0999'
} > "$phone_root/etc/assistant-manager/wa.env"
phone_out="$(ASSISTANT_ROOT="$phone_root" ASSISTANT_BACKUP_DIR="$TMP/mgr-phone-backups" bash "$SCRIPT_DIR/migrate-manager.sh")"
phone_jid="$(printf '%s%s%s@s.whatsapp.net' '555' '010' '0999')"
grep -q "AM_WA_ADMIN_JID=${phone_jid}" "$phone_root/etc/assistant-manager/wa.manager.env" || fail "phone was not mapped to a jid"
if printf '%s\n' "$phone_out" | grep -q "$phone_jid"; then
  fail "migration printed a derived jid"
fi

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x "$SCRIPT_DIR"/*.sh
fi

printf 'self-check ok\n'
