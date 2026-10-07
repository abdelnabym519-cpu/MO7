#!/usr/bin/env bash
# =============================================================================
# MO7 production rollback
# =============================================================================
# Promotes a previously staged release again and verifies it. Rollback is a real
# operation, not the existence of an older artifact:
#
#   1. the target release directory is resolved (given --to, else the release
#      recorded as `previous` in run/deployments.log)
#   2. its manifest and artifact hash are re-verified against releases/<id>/
#   3. a pre-rollback backup is taken (the same pre-migration control a forward
#      deployment uses)
#   4. the symlink is flipped atomically and the processes are restarted
#   5. health + smoke are re-checked; persistent data is verified afterwards by
#      `prod.sh validate --phase post-restart`
#
# usage:
#   prod_rollback.sh [--to <release-id>] [--no-backup] [--no-verify]
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_ROOT="${PROD_ROOT:-/home/user/mo7-prod}"
# shellcheck disable=SC1090
set -a; . "$PROD_ROOT/etc/production.env"; set +a

TO=""
DO_BACKUP=1
DO_VERIFY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TO="$2"; shift 2 ;;
    --no-backup) DO_BACKUP=0; shift ;;
    --no-verify) DO_VERIFY=0; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
sup() { "$PROD_SUPERVISORCTL" -c "$PROD_ROOT/etc/supervisord.conf" "$@"; }
http_code() { curl -s -k -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null || echo 000; }

CURRENT="$(readlink "$PROD_CURRENT_LINK" 2>/dev/null || true)"
case "$CURRENT" in "$PROD_RELEASES_DIR"/*) [ -d "$CURRENT" ] || CURRENT="" ;; *) CURRENT="" ;; esac

if [ -z "$TO" ]; then
  TO="$(python3 - "$PROD_DEPLOY_LOG" <<'PYEOF'
import sys
from pathlib import Path

path = Path(sys.argv[1])
previous = ""
if path.exists():
    for line in path.read_text().splitlines():
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        if fields.get("previous"):
            previous = fields["previous"]
print(previous)
PYEOF
)"
  [ -n "$TO" ] || { echo "FATAL: no previous release recorded; pass --to <release-id>" >&2; exit 2; }
  log "no --to given; using the release recorded as previous in the deploy log: $TO"
fi

TARGET="$PROD_RELEASES_DIR/$TO"
[ -d "$TARGET" ] || { echo "FATAL: release directory not found: $TARGET" >&2; exit 2; }
[ "$(readlink -f "$TARGET")" != "$CURRENT" ] || { echo "FATAL: $TO is already the current release" >&2; exit 2; }

# --- 2. re-verify the target release ----------------------------------------
log "verifying release $TO"
python3 - "$TARGET" <<'PYEOF'
import hashlib
import json
import sys
from pathlib import Path

release = Path(sys.argv[1])
manifest = json.loads((release / "manifest.json").read_text())
artifact = release / manifest["artifact"]
digest = hashlib.sha256()
with artifact.open("rb") as handle:
    while chunk := handle.read(1 << 20):
        digest.update(chunk)
actual = digest.hexdigest()
if actual != manifest["artifact_sha256"]:
    raise SystemExit(f"FATAL: {artifact} is {actual}, manifest says {manifest['artifact_sha256']}")
recorded = (release / "wheel.sha256").read_text().split()[0]
if recorded != actual:
    raise SystemExit(f"FATAL: wheel.sha256 says {recorded}, computed {actual}")
if not (release / manifest["web_bundle"]).joinpath("server.js").exists() and not (release / "web" / "server.js").exists():
    raise SystemExit("FATAL: release web bundle is incomplete")
print(f"  release {manifest['release_id']} artifact {actual} commit {manifest['source_commit']} verified")
PYEOF

# One log per rollback, beside the promotion logs, so the rehearsal's checks are
# recorded the way the promotion's are.
ROLLBACK_LOG="$PROD_ROOT/run/rollback-$(date -u +%Y%m%dT%H%M%SZ).log"

if [ "$DO_BACKUP" = "1" ]; then
  log "taking a pre-rollback backup"
  "$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_backup.py" --label "pre-rollback-$TO" \
    || { echo "FATAL: pre-rollback backup failed" >&2; exit 1; }

  # The control this rollback just took must be proven restorable, for the same
  # reason a promotion rehearses its own: the deployment verification requires
  # the *newest* backup to have been rehearsed, so taking one without rehearsing
  # it makes a healthy rollback report one red check (observed: `rehearsed=
  # …-pre-deploy-… newest=…-pre-rollback-…` immediately after a correct rollback).
  # The release being restored is what boots the restored data.
  BACKUP_DIR="$(ls -1dt "$PROD_BACKUPS"/*-pre-rollback-"$TO" 2>/dev/null | head -1)"
  [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] || { echo "FATAL: the pre-rollback backup cannot be found" >&2; exit 1; }
  REHEARSAL_ROOT="$(mktemp -d /tmp/mo7-rollback-rehearsal-XXXXXX)"
  log "MIGRATION validating the pre-rollback backup is restorable ($(basename "$BACKUP_DIR"))"
  if "$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_restore.py" \
       --backup "$BACKUP_DIR" --target "$REHEARSAL_ROOT" --release-dir "$TARGET" >>"$ROLLBACK_LOG" 2>&1; then
    log "MIGRATION restore rehearsal passed on the backup this rollback took"
  else
    rm -rf "$REHEARSAL_ROOT"
    echo "FATAL: the pre-rollback backup could not be restored; refusing to roll back" >&2
    exit 1
  fi
  rm -rf "$REHEARSAL_ROOT"
fi

# --- 4. flip and restart -----------------------------------------------------
ln -sfn "$TARGET" "$PROD_ROOT/current.new"
mv -T "$PROD_ROOT/current.new" "$PROD_CURRENT_LINK"
log "current -> $TO (was ${CURRENT##*/})"
sup restart backend frontend

for _ in $(seq 1 90); do
  if [ "$(http_code "http://$PROD_BACKEND_HOST:$PROD_BACKEND_PORT/health/ready")" = "200" ] &&
     [ "$(http_code "http://127.0.0.1:$PROD_FRONTEND_PORT/login")" = "200" ]; then
    log "HEALTH restored on $TO"
    break
  fi
  sleep 1
done
[ "$(http_code "http://$PROD_BACKEND_HOST:$PROD_BACKEND_PORT/health/ready")" = "200" ] \
  || { echo "FATAL: $TO did not become ready" >&2; exit 1; }

printf '%s rollback release=%s previous=%s reason=operator\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TO" "${CURRENT##*/}" >>"$PROD_DEPLOY_LOG"

if [ "$DO_VERIFY" = "1" ]; then
  log "post-rollback smoke"
  "$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_smoke.py" || {
    echo "FATAL: post-rollback smoke failed" >&2; exit 1; }
fi
log "ROLLBACK complete: running $TO"

# The record of the release that is running is written by prod_promote.sh on the
# way forward, so a rollback has to write it too: `run/current-release.json` is
# what `prod.sh identity`, the contract helper and the recovery tooling read, and
# leaving it naming the release that was just rolled back would make every later
# report wrong. The write is atomic (temp file + rename), like the promotion's.
python3 - "$PROD_ROOT" "$TO" "$PROD_CURRENT_RELEASE_FILE" <<'PYEOF'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root, release_id, out = sys.argv[1:4]
manifest = json.loads((Path(root) / "releases" / release_id / "manifest.json").read_text())
manifest["current"] = str(Path(root) / "current")
manifest["rolled_back_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
target = Path(out)
temporary = target.with_suffix(".tmp")
temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
temporary.replace(target)
print(f"recorded {release_id} as the running release")
PYEOF

# The contract follows the release that is actually running: after a rollback
# the recovery and validation tooling must report on the restored release.
"$PROD_ROOT/ops-venv/bin/python" "$PROD_ROOT/harness/prod_contract_identity.py" | while read -r line; do log "CONTRACT $line"; done
