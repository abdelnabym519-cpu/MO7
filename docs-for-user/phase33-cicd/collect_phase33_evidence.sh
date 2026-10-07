#!/usr/bin/env bash
# Copy the phase's runtime evidence into the repository.
#
# The artefacts under /home/user/mo7-cicd and /home/user/mo7-prod live outside
# the checkout, and this environment recycles. Anything a reader of the report
# needs to check a claim has to be committed under evidence/ (the one path the
# source-drift rule allows to change after a certified run, besides
# docs-for-user/).
set -euo pipefail

CHECKOUT=${MO7_CHECKOUT:-/home/user/MO7}
WORKDIR=${MO7_RELEASE_WORKDIR:-/home/user/mo7-cicd}
PROD_ROOT=${PROD_ROOT:-/home/user/mo7-prod}
DEST="$CHECKOUT/evidence/phase33-runtime"
RC=1.6.11-45d419a-ci37546936253

mkdir -p "$DEST"/{candidates,negative,host,ci,logs}

# candidate ledgers and their evidence documents
for d in "$WORKDIR"/state/*.json; do cp -f "$d" "$DEST/candidates/"; done
for r in "$RC"; do
  mkdir -p "$DEST/candidates/$r"
  for f in "$WORKDIR/rc/$r/evidence"/*.json; do cp -f "$f" "$DEST/candidates/$r/"; done
  cp -f "$WORKDIR/state/$r.json" "$DEST/candidates/$r/state.json"
done

# the negative and fault-injection runs (the negative suite writes its document
# into the --evidence directory it is given, which nests one level)
for f in faults-local.json faults-host.json rollback-before-rehearsal-fix.json; do
  [ -f "$WORKDIR/negative/$f" ] && cp -f "$WORKDIR/negative/$f" "$DEST/negative/"
done
[ -f "$WORKDIR/negative/negative-tests.json/negative-tests.json" ] &&
  cp -f "$WORKDIR/negative/negative-tests.json/negative-tests.json" "$DEST/negative/negative-tests.json"

# the host's own final state
for f in production-verify.json production-identity.json production-artifact-probe.json production-websocket.json; do
  [ -f "$PROD_ROOT/evidence/$f" ] && cp -f "$PROD_ROOT/evidence/$f" "$DEST/host/"
done
cp -f "$PROD_ROOT/etc/production.env" "$DEST/host/deployment-contract.env" 2>/dev/null || true
cp -f "$PROD_ROOT/run/current-release.json" "$DEST/host/current-release.json" 2>/dev/null || true
cp -f "$PROD_ROOT/run/deployments.log" "$DEST/host/deployments.log" 2>/dev/null || true

# the promotion logs of the certified cycle (the deployment's own narration)
ls -1t "$PROD_ROOT"/run/promote-*.log 2>/dev/null | head -3 | while read -r f; do
  cp -f "$f" "$DEST/logs/$(basename "$f")"
done

# the browser matrix summary, extracted from the reporter output
if [ -f /tmp/browser-final2.log ]; then
  sed 's/\x1b\[[0-9;]*[A-Za-z]//g' /tmp/browser-final2.log >"$DEST/logs/browser-matrix.log"
fi

find "$DEST" -type f | sort | sed "s|$CHECKOUT/||"
