#!/usr/bin/env bash
# =============================================================================
# MO7 production deployment pipeline
# =============================================================================
# VERIFY -> STAGE -> PRE-PROMOTE SMOKE -> BACKUP -> PROMOTE -> RESTART
#        -> HEALTH -> POST-PROMOTE SMOKE -> RELEASE
#
# Every step is real: the artifact hash is recomputed and compared, the staged
# release is started on a scratch port and probed before it can take traffic,
# a consistent backup is taken before the switch (the pre-migration control),
# promotion is an atomic symlink flip, the outage window is measured, and a
# failed health check rolls the symlink back automatically and re-verifies the
# previous release.
#
# usage:
#   prod_deploy.sh --artifact <wheel> [--release-id ID] [--commit SHA]
#                  [--sha256 HASH] [--skip-promote] [--no-backup] [--no-rollback]
#                  [--restage]
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_ROOT="${PROD_ROOT:-/home/user/mo7-prod}"
CONTRACT="$PROD_ROOT/etc/production.env"
[ -f "$CONTRACT" ] || { echo "FATAL: missing contract $CONTRACT (run prod_bootstrap.sh first)" >&2; exit 78; }
# shellcheck disable=SC1090
set -a; . "$CONTRACT"; set +a

ARTIFACT=""
RELEASE_ID=""
COMMIT=""
EXPECTED_SHA=""
SKIP_PROMOTE=0
DO_BACKUP=1
DO_ROLLBACK=1
RESTAGE=0
SMOKE_PORT="${PROD_SMOKE_PORT:-8099}"

while [ $# -gt 0 ]; do
  case "$1" in
    --artifact) ARTIFACT="$2"; shift 2 ;;
    --release-id) RELEASE_ID="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --sha256) EXPECTED_SHA="$2"; shift 2 ;;
    --skip-promote) SKIP_PROMOTE=1; shift ;;
    --no-backup) DO_BACKUP=0; shift ;;
    --no-rollback) DO_ROLLBACK=0; shift ;;
    --restage) RESTAGE=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

SUPERVISORCTL="$PROD_ROOT/ops-venv/bin/supervisorctl"
SMOKE_LOG="$PROD_ROOT/run/deploy-smoke.log"
PHASE_LOG="$PROD_ROOT/run/deploy-$(date -u +%Y%m%dT%H%M%SZ).log"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$PHASE_LOG"; }
die() { log "FATAL: $*"; exit 1; }

sup() { "$SUPERVISORCTL" -c "$PROD_ROOT/etc/supervisord.conf" "$@"; }

# One HTTP status code, always three digits: curl writes 000 when it cannot
# connect, and must not also be followed by the fallback.
http_code() {
  local code
  code="$(curl -s -k -o /dev/null -m 4 -w '%{http_code}' "$1" 2>/dev/null)" || true
  printf '%s' "${code:-000}"
}

wait_healthy() { # wait_healthy <deadline-seconds>
  local deadline="$1" start
  start=$(date +%s)
  while [ $(( $(date +%s) - start )) -lt "$deadline" ]; do
    if [ "$(http_code "http://$PROD_BACKEND_HOST:$PROD_BACKEND_PORT/health/ready")" = "200" ] &&
       [ "$(http_code "http://127.0.0.1:$PROD_FRONTEND_PORT/login")" = "200" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# --- 0. resolve identity -----------------------------------------------------
[ -n "$ARTIFACT" ] || die "no --artifact given"
ARTIFACT="$(readlink -f "$ARTIFACT")"
[ -f "$ARTIFACT" ] || die "artifact not found: $ARTIFACT"
ARTIFACT_NAME="$(basename "$ARTIFACT")"
VERSION="$(printf '%s' "$ARTIFACT_NAME" | sed -E 's/^deeptutor-([0-9][^-]*(\.post[0-9]+)?)-py3-none-any\.whl$/\1/')"
[ "$VERSION" != "$ARTIFACT_NAME" ] || die "cannot derive the version from $ARTIFACT_NAME"
[ -n "$COMMIT" ] || COMMIT="$PROD_SOURCE_COMMIT"
[ -n "$RELEASE_ID" ] || RELEASE_ID="${VERSION}-$(printf '%s' "$COMMIT" | cut -c1-7)"
RELEASE_DIR="$PROD_RELEASES_DIR/$RELEASE_ID"
# Releases are immutable and only promotable once fully staged, so the tree is
# built under a scratch name and renamed into place at the end. A failure
# anywhere in staging therefore cannot leave a half-built release directory that
# a later promotion could switch to.
STAGING_DIR="$PROD_RELEASES_DIR/.staging-$RELEASE_ID-$$"
# A deployment is all-or-nothing: until the release is published, any exit
# (failure, signal, refusal at the smoke gate) leaves the host exactly as it
# was — the incomplete staging tree is removed and the contract, which the
# operator updates to name the artifact under test, is restored byte for byte.
# Without this a refused deployment would leave the contract pointing at a
# release that is not running, and every later identity/health report would be
# wrong for reasons that have nothing to do with production.
PUBLISHED=0
restore_contract_identity() {
  # The contract names the release the host is *supposed* to be running, so a
  # refused deployment must restore it from the promoted release's own manifest
  # (`run/current-release.json`, written by prod_promote.sh). Otherwise the
  # contract would keep naming an artifact that never took traffic, and every
  # later identity/health report would be wrong for unrelated reasons.
  [ -x "$PROD_ROOT/ops-venv/bin/python" ] || return 0
  "$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_contract_identity.py"
}


cleanup_staging() {
  local status=$?
  if [ -n "${STAGING_DIR:-}" ] && [ -d "${STAGING_DIR:-}" ]; then
    log "STAGE failed; removing the incomplete staging directory $STAGING_DIR"
    rm -rf "$STAGING_DIR"
  fi
  if [ "$PUBLISHED" != "1" ]; then
    local restored
    restored="$(restore_contract_identity 2>&1 || true)"
    [ -n "$restored" ] && log "DEPLOY refused: $restored"
  fi
  return "$status"
}
trap cleanup_staging EXIT

# --- 1. VERIFY ---------------------------------------------------------------
ACTUAL_SHA="$(sha256sum "$ARTIFACT" | awk '{print $1}')"
if [ -z "$EXPECTED_SHA" ] && [ "$RELEASE_ID" = "$PROD_RELEASE_ID" ]; then
  EXPECTED_SHA="$PROD_ARTIFACT_SHA256"
fi
if [ -n "$EXPECTED_SHA" ] && [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  die "artifact hash mismatch: $ACTUAL_SHA != $EXPECTED_SHA"
fi
log "VERIFY artifact=$ARTIFACT_NAME sha256=$ACTUAL_SHA version=$VERSION commit=$COMMIT release=$RELEASE_ID"

WHEEL_VERSION="$(python3 - "$ARTIFACT" <<'PYEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    for name in archive.namelist():
        if name.endswith(".dist-info/METADATA"):
            for line in archive.read(name).decode("utf-8", "replace").splitlines():
                if line.startswith("Version:"):
                    print(line.split(":", 1)[1].strip())
                    raise SystemExit(0)
raise SystemExit("version not found in wheel metadata")
PYEOF
)"
[ "$WHEEL_VERSION" = "$VERSION" ] || die "wheel metadata version $WHEEL_VERSION != filename version $VERSION"
log "VERIFY wheel metadata version matches ($WHEEL_VERSION)"

for checkout in "${MO7_SOURCE_CHECKOUT:-/home/user/MO7}" /home/user/MO7; do
  if [ -d "$checkout/.git" ]; then
    if git -C "$checkout" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
      log "VERIFY commit $COMMIT exists in $checkout"
    else
      log "WARN: commit $COMMIT not found in $checkout (traceability recorded from the contract)"
    fi
    break
  fi
done

# --- 2. STAGE ----------------------------------------------------------------
if [ -d "$RELEASE_DIR" ]; then
  if [ "$RESTAGE" = "1" ]; then
    log "STAGE re-staging $RELEASE_ID (existing directory preserved as .bak)"
    rm -rf "$RELEASE_DIR.bak"
    mv "$RELEASE_DIR" "$RELEASE_DIR.bak"
  else
    die "release directory already exists: $RELEASE_DIR (releases are immutable; pass --restage to replace)"
  fi
fi
rm -rf "$STAGING_DIR"
log "STAGE building $RELEASE_ID in $STAGING_DIR"
mkdir -p "$STAGING_DIR/bin" "$STAGING_DIR/venv"
install -m 0444 "$ARTIFACT" "$STAGING_DIR/$ARTIFACT_NAME"
printf '%s  %s\n' "$ACTUAL_SHA" "$ARTIFACT_NAME" >"$STAGING_DIR/wheel.sha256"

python3 -m venv "$STAGING_DIR/venv"
"$STAGING_DIR/venv/bin/pip" install -q -U pip setuptools wheel >>"$PHASE_LOG" 2>&1
log "STAGE installing the artifact into the release virtualenv (with dependencies)"
"$STAGING_DIR/venv/bin/pip" install -q "$STAGING_DIR/$ARTIFACT_NAME" >>"$PHASE_LOG" 2>&1
INSTALLED_VERSION="$("$STAGING_DIR/venv/bin/python" -c 'from deeptutor.__version__ import __version__ as v; print(v)')"
[ "$INSTALLED_VERSION" = "$VERSION" ] || die "installed version $INSTALLED_VERSION != $VERSION"

log "STAGE materialising the web bundle from the artifact"
# -P keeps the current working directory out of sys.path. Without it, a stdin
# script runs with '' (the CWD) first on sys.path, so `import deeptutor_web`
# resolves to a *source checkout* if the operator happens to stand in one — the
# release would then materialise a web bundle that is not the one inside the
# artifact under test. Observed exactly that during Phase 33, with the checkout
# mid-rebuild. The release must be materialised from the artifact, nowhere else.
(cd "$STAGING_DIR" && "$STAGING_DIR/venv/bin/python" -P - "$STAGING_DIR" "http://$PROD_BACKEND_HOST:$PROD_BACKEND_PORT") <<'PYEOF' >>"$PHASE_LOG" 2>&1
import sys
from pathlib import Path

from deeptutor.runtime.launcher import _copy_packaged_web_if_needed, _packaged_web_dir

release = Path(sys.argv[1])
api_base = sys.argv[2]
packaged = _packaged_web_dir()
if packaged is None:
    raise SystemExit("packaged web bundle missing from the artifact")
cache = _copy_packaged_web_if_needed(packaged, home=release, api_base=api_base, auth_enabled=True)
Path(sys.argv[1], "web-dir.txt").write_text(str(cache))
print(cache)
PYEOF
WEB_DIR="$(cat "$STAGING_DIR/web-dir.txt")"
[ -f "$WEB_DIR/server.js" ] || die "web bundle missing server.js ($WEB_DIR)"
ln -sfn "$WEB_DIR" "$STAGING_DIR/web"

for program in backend frontend; do
  # The scripts reference the *published* release path, not the scratch one.
  sed -e "s|@PROD_ROOT@|$PROD_ROOT|g" -e "s|@RELEASE_DIR@|$RELEASE_DIR|g" \
      "$HERE/templates/start-${program}.sh.tmpl" >"$STAGING_DIR/bin/start-${program}.sh"
  chmod 755 "$STAGING_DIR/bin/start-${program}.sh"
done

cat >"$STAGING_DIR/manifest.json" <<JSON
{
  "release_id": "$RELEASE_ID",
  "environment": "$PROD_ENVIRONMENT",
  "version": "$VERSION",
  "source_commit": "$COMMIT",
  "artifact": "$ARTIFACT_NAME",
  "artifact_sha256": "$ACTUAL_SHA",
  "artifact_bytes": $(stat -c '%s' "$STAGING_DIR/$ARTIFACT_NAME"),
  "python": "$("$STAGING_DIR/venv/bin/python" -V 2>&1 | awk '{print $2}')",
  "web_bundle": "$WEB_DIR",
  "staged_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "staged_by": "$(id -un)",
  "host": "$(uname -n)"
}
JSON
chmod 644 "$STAGING_DIR/manifest.json"
log "STAGE complete: $RELEASE_ID (installed version $INSTALLED_VERSION)"

# --- 3. PRE-PROMOTE SMOKE ----------------------------------------------------
log "SMOKE starting the staged release on the scratch port $SMOKE_PORT"
: >"$SMOKE_LOG"
# The staged process runs with the release directory as its working directory
# (the app resolves its packaged web bundle relative to it) and its output goes
# to a log that outlives the staging directory, so a failure is readable even
# after the staging tree is removed. `exec` makes the recorded pid the python
# process itself rather than an intermediate shell.
(
  cd "$STAGING_DIR" || exit 1
  exec env DEEPTUTOR_HOME="$PROD_HOME" \
    PROD_ROOT="$PROD_ROOT" PROD_CONTRACT="$CONTRACT" \
    SMOKE_BACKEND_PORT="$SMOKE_PORT" \
    "$STAGING_DIR/venv/bin/python" -m uvicorn deeptutor.api.main:app \
      --host "$PROD_BACKEND_HOST" --port "$SMOKE_PORT" --no-access-log --no-proxy-headers
) >"$SMOKE_LOG" 2>&1 &
SMOKE_PID=$!
echo "$SMOKE_PID" >"$STAGING_DIR/smoke.pid"
# The host watchdog sweeps programs that are not descendants of the live
# supervisord. A staged release under test is such a program, so the pipeline
# records the pid it owns (and the watchdog also exempts `.staging-*` trees and
# anything under a running deployment) — otherwise the sweep would kill a
# healthy staged release while it is being probed.
echo "$SMOKE_PID" >"$PROD_RUN/deploy-smoke.pid"
smoke_ok=0
SMOKE_LAST="never answered"
for _ in $(seq 1 60); do
  # Keep the last answer: a readiness that stays non-200 is a finding, and a
  # bare "never reported ready" would leave the cause to guesswork.
  SMOKE_CODE="$(http_code "http://$PROD_BACKEND_HOST:$SMOKE_PORT/health/ready")"
  if [ "$SMOKE_CODE" = "200" ]; then smoke_ok=1; break; fi
  # `set -euo pipefail` is active, so a curl that cannot connect must not abort
  # the deploy: the connection refusal *is* the measurement here.
  SMOKE_LAST="http $SMOKE_CODE: $(curl -s -k -m 4 "http://$PROD_BACKEND_HOST:$SMOKE_PORT/health/ready" 2>/dev/null | head -c 200 || true)"
  sleep 1
done
if [ "$smoke_ok" != "1" ]; then
  # A staged release that died on its own is the clearest possible failure; the
  # exit status distinguishes "never started" from "started and crashed".
  SMOKE_EXIT="still running"
  if ! kill -0 "$SMOKE_PID" 2>/dev/null; then
    # `wait` reports the staged process's own exit status; it fails by design
    # when the process died, so `set -e` must not cut the failure report short.
    SMOKE_EXIT="exited with $(wait "$SMOKE_PID" 2>/dev/null; echo $?)"
  fi
  log "SMOKE failed: staged release never reported ready (last answer: $SMOKE_LAST, process $SMOKE_EXIT)"
  log "SMOKE staged-release log tail: $(tail -5 "$SMOKE_LOG" | tr '\n' '|')"
  kill "$SMOKE_PID" 2>/dev/null || true
  rm -f "$PROD_RUN/deploy-smoke.pid"
  die "pre-promote smoke failed"
fi
kill "$SMOKE_PID" 2>/dev/null || true
sleep 1
kill -9 "$SMOKE_PID" 2>/dev/null || true
rm -f "$STAGING_DIR/smoke.pid" "$PROD_RUN/deploy-smoke.pid"
log "SMOKE staged release reported /health/ready 200 on $SMOKE_PORT"

# --- 2b. PUBLISH -------------------------------------------------------------
# The release becomes visible under its immutable name only now, with the paths
# recorded during staging rewritten from the scratch name to the published one.
mv "$STAGING_DIR" "$RELEASE_DIR"
sed -i "s|$STAGING_DIR|$RELEASE_DIR|g" "$RELEASE_DIR/web-dir.txt" "$RELEASE_DIR/manifest.json" 2>/dev/null || true
# A virtualenv records its own absolute path: in pyvenv.cfg and in the shebang
# of every console script. The module entry points (`venv/bin/python -m ...`,
# which is what the start scripts use) resolve through sys.executable and are
# unaffected, but the scripts must not point at the scratch name.
sed -i "s|$STAGING_DIR|$RELEASE_DIR|g" "$RELEASE_DIR/venv/pyvenv.cfg" 2>/dev/null || true
for script in "$RELEASE_DIR"/venv/bin/*; do
  [ -f "$script" ] || continue
  head -c 2 "$script" 2>/dev/null | grep -q '#!' || continue
  sed -i "1s|$STAGING_DIR|$RELEASE_DIR|" "$script" 2>/dev/null || true
done
ln -sfn "$(cat "$RELEASE_DIR/web-dir.txt")" "$RELEASE_DIR/web"
[ -f "$RELEASE_DIR/web/server.js" ] || die "published release web bundle is missing server.js"
PUBLISHED=1
trap - EXIT
log "PUBLISH $RELEASE_DIR is staged, smoke-tested and immutable"

if [ "$SKIP_PROMOTE" = "1" ]; then
  log "STAGE only (--skip-promote): current release left untouched"
  exit 0
fi

# --- 4..9. BACKUP -> PROMOTE -> RESTART -> HEALTH -> SMOKE -> RELEASE --------
PROMOTE_ARGS=(--release "$RELEASE_ID")
[ "$DO_BACKUP" = "0" ] && PROMOTE_ARGS+=(--no-backup)
[ "$DO_ROLLBACK" = "0" ] && PROMOTE_ARGS+=(--no-rollback)
exec bash "$HERE/prod_promote.sh" "${PROMOTE_ARGS[@]}"
