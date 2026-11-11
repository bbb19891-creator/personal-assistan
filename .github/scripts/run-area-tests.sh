#!/usr/bin/env bash
# Run area test suites that have landed on this branch.
# Missing paths are skipped with a log line. A failing suite fails this script.
# This does not replace the gitleaks and denylist job in secrets.yml.
set -euo pipefail

ROOT="${1:-.}"
if [ ! -d "$ROOT" ]; then
  echo "test root does not exist: $ROOT" >&2
  exit 2
fi

run_pytest() {
  local dir="$1"
  if [ ! -d "$ROOT/$dir" ]; then
    echo "skip: $dir does not exist yet"
    return 0
  fi
  echo "running pytest in $dir"
  set +e
  ( cd "$ROOT" && python3 -m pytest --rootdir="$ROOT" "$dir" )
  local code=$?
  set -e
  if [ "$code" -eq 5 ]; then
    echo "skip: $dir exists but pytest collected no tests"
    return 0
  fi
  if [ "$code" -ne 0 ]; then
    echo "pytest failed in $dir (exit $code)" >&2
    return "$code"
  fi
}

run_pytest pkg/agent/tests
run_pytest central/tests
run_pytest platform/tests
run_pytest tests

if [ -f "$ROOT/pkg/host/check-selfhost.sh" ]; then
  echo "running pkg/host/check-selfhost.sh"
  ( cd "$ROOT" && bash pkg/host/check-selfhost.sh )
else
  echo "skip: pkg/host/check-selfhost.sh does not exist yet"
fi
