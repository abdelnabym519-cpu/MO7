#!/usr/bin/env bash
# =============================================================================
# MO7 production promotion
# =============================================================================
# Activates a staged release: BACKUP -> PROMOTE (atomic symlink flip) -> RESTART
# (with a measured outage window) -> HEALTH (with automatic rollback) ->
# SMOKE -> RELEASE record. Used by prod_deploy.sh after staging and available
# directly as `prod.sh promote --release <id>`.
#
# usage: prod_promote.sh --release <release-id> [--no-backup] [--no-rollback]
#                        [--skip-smoke]
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_ROOT="${PROD_ROOT:-/home/user/mo7-prod}"
# shellcheck disable=SC1090
set -a; . "$PROD_ROOT/etc/production.env"; set +a

RELEASE_ID=""
DO_BACKUP=1
DO_ROLLBACK=1
DO_SMOKE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE_ID="$2"; shift 2 ;;
    --no-backup) DO_BACKUP=0; shift ;;
    --no-rollback) DO_ROLLBACK=0; shift ;;
    --skip-smoke) DO_SMOKE=0; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$RELEASE_ID" ] || { echo "usage: prod_promote.sh --release <release-id>" >&2; exit 2; }
[ -d "$PROD_RELEASES_DIR/$RELEASE_ID" ] || { echo "FATAL: release not staged: $PROD_RELEASES_DIR/$RELEASE_ID" >&2; exit 2; }

PHASE_LOG="$PROD_RUN/promote-$(date -u +%Y%m%dT%H%M%SZ).log"
log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$PHASE_LOG"; }
die() { log "FATAL: $*"; exit 1; }
sup() { "$PROD_SUPERVISORCTL" -c "$PROD_ROOT/etc/supervisord.conf" "$@"; }
# Liveness is supervisord's own pid: `status` exits non-zero whenever any
# program is stopped (the validation ingress is deliberately stopped outside a
# validation window), which must not be mistaken for a dead supervisor.
sup_running() { "$PROD_SUPERVISORCTL" -c "$PROD_ROOT/etc/supervisord.conf" pid >/dev/null 2>&1; }
# One HTTP status code, always three digits: curl writes 000 when it cannot
# connect, and must not also be followed by the fallback.
http_code() {
  local code
  code="$(curl -s -k -o /dev/null -m 4 -w '%{http_code}' "$1" 2>/dev/null)" || true
  printf '%s' "${code:-000}"
}
wait_healthy() {
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

# --- 0. RELEASE COMPLETENESS --------------------------------------------------
# Promotion is the moment a release starts serving, so it verifies that the
# release directory is a *complete* staging result before anything is switched:
# manifest, artifact hash, start scripts, virtualenv and the materialised web
# bundle. A partial or hand-edited release is refused here rather than becoming
# a running deployment.
RELEASE_DIR="$PROD_RELEASES_DIR/$RELEASE_ID"
MANIFEST="$RELEASE_DIR/manifest.json"
[ -f "$MANIFEST" ] || die "release manifest missing: $MANIFEST"
[ -f "$RELEASE_DIR/wheel.sha256" ] || die "release artifact hash missing: $RELEASE_DIR/wheel.sha256"
for program in backend frontend; do
  [ -x "$RELEASE_DIR/bin/start-$program.sh" ] || die "release start script missing or not executable: bin/start-$program.sh"
done
[ -x "$RELEASE_DIR/venv/bin/python" ] || die "release virtualenv missing: $RELEASE_DIR/venv/bin/python"
[ -f "$RELEASE_DIR/web/server.js" ] || die "release web bundle missing: $RELEASE_DIR/web/server.js"
ACTUAL_ARTIFACT_SHA="$(sha256sum "$RELEASE_DIR"/*.whl 2>/dev/null | awk '{print $1}' | head -1)"
RECORDED_ARTIFACT_SHA="$(awk '{print $1}' "$RELEASE_DIR/wheel.sha256")"
[ -n "$ACTUAL_ARTIFACT_SHA" ] || die "no artifact present in $RELEASE_DIR"
[ "$ACTUAL_ARTIFACT_SHA" = "$RECORDED_ARTIFACT_SHA" ] || die "artifact in the release does not match its recorded hash"
ARTIFACT_SHA="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['artifact_sha256'])" "$MANIFEST")"
[ "$ARTIFACT_SHA" = "$RECORDED_ARTIFACT_SHA" ] || die "manifest artifact hash disagrees with wheel.sha256"
INSTALLED_VERSION="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$MANIFEST")"
RELEASE_VERSION="$("$RELEASE_DIR/venv/bin/python" -c 'from deeptutor.__version__ import __version__ as v; print(v)')"
[ "$INSTALLED_VERSION" = "$RELEASE_VERSION" ] || die "release virtualenv reports $RELEASE_VERSION, manifest says $INSTALLED_VERSION"
COMMIT="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['source_commit'])" "$MANIFEST")"
# A previous release exists only when the symlink resolves to a real release
# directory; a dangling or self-referential symlink must never become the
# rollback target (that would create current -> current and every program would
# fail with ELOOP).
PREVIOUS_RELEASE=""
if [ -L "$PROD_CURRENT_LINK" ]; then
  target="$(readlink "$PROD_CURRENT_LINK")"
  case "$target" in
    "$PROD_RELEASES_DIR"/*) [ -d "$target" ] && PREVIOUS_RELEASE="$target" ;;
  esac
fi

if [ "$DO_BACKUP" = "1" ]; then
  log "BACKUP taking a consistent pre-deploy backup (pre-migration control)"
  "$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_backup.py" --label "pre-deploy-$RELEASE_ID" \
    >>"$PHASE_LOG" 2>&1 || die "pre-deploy backup failed; refusing to promote"
  log "BACKUP complete"
else
  log "BACKUP skipped (--no-backup)"
fi

ln -sfn "$PROD_RELEASES_DIR/$RELEASE_ID" "$PROD_ROOT/current.new"
mv -T "$PROD_ROOT/current.new" "$PROD_CURRENT_LINK"
log "PROMOTE current -> $RELEASE_ID (previous: ${PREVIOUS_RELEASE:-none})"

OUTAGE_LOG="$PROD_RUN/outage-$(date -u +%Y%m%dT%H%M%SZ).log"
DOWN_MS=0
if sup_running; then
  (
    while true; do
      printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)" \
        "$(http_code "http://$PROD_BACKEND_HOST:$PROD_BACKEND_PORT/health/ready")" \
        "$(http_code "http://127.0.0.1:$PROD_FRONTEND_PORT/login")" >>"$OUTAGE_LOG"
      sleep 0.2
    done
  ) &
  PROBE_PID=$!
  log "RESTART restarting backend and frontend through supervisord"
  sup restart backend frontend scheduler >>"$PHASE_LOG" 2>&1 || log "WARN: supervisorctl restart reported an error"
  sleep 1
  kill "$PROBE_PID" 2>/dev/null || true
  wait "$PROBE_PID" 2>/dev/null || true
  DOWN_MS="$(python3 - "$OUTAGE_LOG" <<'PYEOF'
import sys
from datetime import datetime

rows = [line.split() for line in open(sys.argv[1]) if line.strip()]
first = last = None
for row in rows:
    if not (row[1] == "200" and row[2] == "200"):
        first = first or row[0]
        last = row[0]
if first and last:
    parse = lambda value: datetime.strptime(value[:23], "%Y-%m-%dT%H:%M:%S.%f")
    print(int((parse(last) - parse(first)).total_seconds() * 1000))
else:
    print(0)
PYEOF
)"
  log "RESTART measured outage window: ${DOWN_MS} ms (probe every 200 ms)"
else
  log "RESTART supervisord is not running; starting the stack"
  "$PROD_ROOT/bin/prod.sh" start >>"$PHASE_LOG" 2>&1 || true
fi

if wait_healthy 90; then
  log "HEALTH backend ready and frontend serving (release $RELEASE_ID)"
else
  log "HEALTH failed after promotion"
  if [ "$DO_ROLLBACK" = "1" ] && [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ]; then
    log "ROLLBACK promoting ${PREVIOUS_RELEASE##*/} back"
    ln -sfn "$PREVIOUS_RELEASE" "$PROD_ROOT/current.new"
    mv -T "$PROD_ROOT/current.new" "$PROD_CURRENT_LINK"
    sup restart backend frontend scheduler >>"$PHASE_LOG" 2>&1 || true
    if wait_healthy 90; then
      log "ROLLBACK verified: $(readlink -f "$PROD_CURRENT_LINK") is healthy again"
    else
      log "ROLLBACK FAILED: the previous release is not healthy either"
    fi
  fi
  die "promotion of $RELEASE_ID failed its health gate"
fi

if [ "$DO_SMOKE" = "1" ]; then
  if PROD_ROOT="$PROD_ROOT" "$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_smoke.py" >>"$PHASE_LOG" 2>&1; then
    log "SMOKE post-promote smoke passed (health, auth, settings, data read)"
  else
    die "post-promote smoke failed (see $PHASE_LOG)"
  fi
fi

printf '%s deploy release=%s artifact=%s commit=%s previous=%s outage_ms=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RELEASE_ID" "$ARTIFACT_SHA" "$COMMIT" \
  "${PREVIOUS_RELEASE##*/}" "$DOWN_MS" >>"$PROD_DEPLOY_LOG"
# The marker is written first, and the contract is refreshed from it afterwards.
# The marker is what `prod_contract_identity.py` reads, so refreshing the contract
# before this point wrote the *previous* release's identity into the contract --
# observed on the second promotion of a host: the release was live and serving,
# and every identity-bearing check read the old release. Order matters here.
python3 - "$PROD_ROOT" "$RELEASE_ID" "$PROD_CURRENT_RELEASE_FILE" "${PREVIOUS_RELEASE##*/}" <<'PYEOF'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root, release_id, out, previous = sys.argv[1:5]
manifest = json.loads((Path(root) / "releases" / release_id / "manifest.json").read_text())
manifest["promoted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
manifest["current"] = str(Path(root) / "current")
# The release this promotion replaced, recorded where a rollback and an auditor
# both look for it. `prod_rollback.sh` resolves its target from the deploy log;
# a caller that decides to roll back later needs the same fact in one place.
manifest["previous_release"] = previous or None
Path(out).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PYEOF

# The contract must name the release that is now running; a forward deployment
# that reached this point has been health- and smoke-verified.
"$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_contract_identity.py" | while read -r line; do log "CONTRACT $line"; done
log "RELEASE $RELEASE_ID is live"
