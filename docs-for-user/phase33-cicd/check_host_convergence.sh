#!/usr/bin/env bash
# Is the host running the deployment tooling that is in the repository?
#
# The operational entry points on the host are *copies*: `bin/prod.sh` dispatches
# to `bin/prod_promote.sh`, `bin/prod_deploy.sh` and `bin/prod_rollback.sh`, and
# the python helpers are called from `harness/`. Patching `harness/` alone changes
# nothing, and a committed fix that was never installed is not a fix that is
# running -- observed: the smoke-failure rollback existed in the repository for
# two fault runs while the host kept dying where it stood (P33-M20).
#
# Run this before certifying anything against a host. Exit 0 means every file the
# host runs is byte-identical to the repository's copy.
set -euo pipefail

CHECKOUT=${MO7_CHECKOUT:-/home/user/MO7}
PROD_ROOT=${PROD_ROOT:-/home/user/mo7-prod}
SRC="$CHECKOUT/docs-for-user/phase32-infra"

# The files the host executes for deployment, promotion, rollback and their
# helpers. `bin/` holds the shell entry points; `harness/` holds the same files
# plus the python helpers the entry points call.
SHELL_FILES="prod_deploy.sh prod_promote.sh prod_rollback.sh prod_bootstrap.sh prod_restore_ws.sh"
PY_FILES="prod_backup.py prod_restore.py prod_smoke.py prod_ws.py prod_health.py ingress_tls_proxy.js"

missing=0
drifted=0
checked=0

compare() {
  local source_file="$1" installed_file="$2" label="$3"
  if [ ! -f "$source_file" ]; then
    printf 'skip   %-34s (not in the repository)\n' "$label"
    return 0
  fi
  if [ ! -f "$installed_file" ]; then
    printf 'MISSING %-33s %s\n' "$label" "$installed_file"
    missing=$((missing + 1))
    return 0
  fi
  checked=$((checked + 1))
  if cmp -s "$source_file" "$installed_file"; then
    printf 'ok     %-34s\n' "$label"
  else
    printf 'DRIFT  %-34s %s differs from the repository\n' "$label" "$installed_file"
    drifted=$((drifted + 1))
  fi
}

for f in $SHELL_FILES $PY_FILES; do
  compare "$SRC/$f" "$PROD_ROOT/harness/$f" "harness/$f"
done
for f in $SHELL_FILES; do
  compare "$SRC/$f" "$PROD_ROOT/bin/$f" "bin/$f"
done

printf '\n%d file(s) compared, %d missing, %d drifted\n' "$checked" "$missing" "$drifted"
if [ "$missing" != "0" ] || [ "$drifted" != "0" ]; then
  echo "FATAL: the host is not running the deployment tooling from the repository" >&2
  exit 1
fi
echo "the host's deployment tooling matches the repository"
