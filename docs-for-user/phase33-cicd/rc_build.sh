#!/usr/bin/env bash
# =============================================================================
# MO7 release build — the single build recipe
# =============================================================================
# Both the CI pipeline and the local release controller build the release
# artifact with THIS script, so "what CI built" and "what the deployment holds"
# come from one recipe instead of two that drift.
#
#   rc_build.sh --checkout DIR --out DIR [--version V] [--source-date-epoch N]
#               [--skip-web] [--json]
#
# Steps (each fails the build closed):
#   1. clear the generated deeptutor_web package data
#   2. build the web bundle (next build) using the offline font responses
#   3. materialise deeptutor_web from web/.next/standalone
#   4. build the wheel
#   5. write build-report.json (identity + hashes) next to the artifact
#
# Determinism: SOURCE_DATE_EPOCH is pinned to the commit timestamp unless given,
# so a build does not inherit "now" as an input.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKOUT="${MO7_CHECKOUT:-/home/user/MO7}"
OUT=""
VERSION=""
SDE=""
SKIP_WEB=0
FONT_MOCK="${MO7_FONT_MOCK:-/home/user/mo7-cicd/font-mock.cjs}"
PY="${MO7_BUILD_PYTHON:-/home/user/mo7-cicd/venv/bin/python}"

while [ $# -gt 0 ]; do
  case "$1" in
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --source-date-epoch) SDE="$2"; shift 2 ;;
    --skip-web) SKIP_WEB=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ -n "$OUT" ] || { echo "usage: rc_build.sh --out DIR [--checkout DIR]" >&2; exit 2; }
[ -d "$CHECKOUT/.git" ] || { echo "FATAL: $CHECKOUT is not a git checkout" >&2; exit 2; }
mkdir -p "$OUT"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { printf '%s  FATAL: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; exit 1; }

COMMIT="$(git -C "$CHECKOUT" rev-parse HEAD)"
SHORT="$(git -C "$CHECKOUT" rev-parse --short=7 HEAD)"
if [ -z "$VERSION" ]; then
  VERSION="$("$PY" - "$CHECKOUT" <<'PYEOF'
import re, sys, pathlib
src = pathlib.Path(sys.argv[1]) / "deeptutor/__version__.py"
m = re.search(r'__version__\s*=\s*"([^"]+)"', src.read_text(encoding="utf-8"))
print(m.group(1) if m else "")
PYEOF
)"
fi
[ -n "$VERSION" ] || die "cannot determine the version from deeptutor/__version__.py"

# Commit timestamp is the deterministic build epoch.
if [ -z "$SDE" ]; then
  SDE="$(git -C "$CHECKOUT" log -1 --format=%ct)"
fi
export SOURCE_DATE_EPOCH="$SDE"

log "build checkout=$CHECKOUT commit=$COMMIT version=$VERSION SOURCE_DATE_EPOCH=$SDE"

# --- 1. clear generated package data ----------------------------------------
# Everything except the tracked __init__.py, which is source rather than build
# output (a plain `*` glob would delete it too).
[ -d "$CHECKOUT/deeptutor_web" ] || die "deeptutor_web/ is missing from the source tree"
[ -f "$CHECKOUT/deeptutor_web/__init__.py" ] || die "deeptutor_web/__init__.py is missing from the source tree"
find "$CHECKOUT/deeptutor_web" -mindepth 1 -maxdepth 1 ! -name __init__.py -exec rm -rf {} + 2>/dev/null || true
[ -f "$CHECKOUT/deeptutor_web/__init__.py" ] || die "clearing the generated package data removed __init__.py"

# --- 2. web bundle ----------------------------------------------------------
if [ "$SKIP_WEB" = "1" ]; then
  [ -f "$CHECKOUT/web/.next/standalone/server.js" ] || die "--skip-web but web/.next/standalone/server.js does not exist"
  log "web build skipped (reusing $CHECKOUT/web/.next)"
else
  [ -f "$FONT_MOCK" ] || die "offline font mock not found at $FONT_MOCK (Google Fonts is TLS-blocked here)"
  [ "$SOURCE_DATE_EPOCH" = "$SDE" ] || die "SOURCE_DATE_EPOCH was not pinned"
  # Remove the previous build's output first. Next.js leaves build-id-named
  # chunk directories behind, and a build that inherits them ships the previous
  # build's assets alongside its own — observed as extra entries in the wheel
  # manifest, and a real reproducibility hazard for a release artifact.
  rm -rf "$CHECKOUT/web/.next"
  log "web build: npm run build (offline font responses)"
  ( cd "$CHECKOUT/web" && NEXT_FONT_GOOGLE_MOCKED_RESPONSES="$FONT_MOCK" \
      NEXT_TELEMETRY_DISABLED=1 npm run build ) >"$OUT/web-build.log" 2>&1 \
    || { tail -30 "$OUT/web-build.log" >&2; die "web build failed"; }
fi

# --- 3. materialise deeptutor_web -------------------------------------------
log "packaging deeptutor_web from web/.next/standalone"
( cd "$CHECKOUT" && "$PY" scripts/prepare_web_package.py --skip-build ) >>"$OUT/web-build.log" 2>&1 \
  || { tail -20 "$OUT/web-build.log" >&2; die "prepare_web_package.py failed"; }
[ -f "$CHECKOUT/deeptutor_web/server.js" ] || die "deeptutor_web/server.js was not materialised"

# --- 4. wheel ---------------------------------------------------------------
# setuptools stages the wheel in build/lib and never deletes a file whose source
# has disappeared, so a stale staging directory ships the PREVIOUS build's
# assets inside the next wheel (observed: four web build ids in one artifact).
# The staging directory is build output, so it is cleared rather than trusted.
rm -rf "$CHECKOUT/build"
log "building the wheel"
( cd "$CHECKOUT" && "$PY" -m build --wheel --outdir "$OUT" ) >"$OUT/wheel-build.log" 2>&1 \
  || { tail -30 "$OUT/wheel-build.log" >&2; die "wheel build failed"; }

WHEEL="$(ls -1 "$OUT"/deeptutor-"$VERSION"-*.whl 2>/dev/null | head -1)"
[ -n "$WHEEL" ] || die "no wheel for version $VERSION in $OUT"
WHEEL_NAME="$(basename "$WHEEL")"
SHA="$(sha256sum "$WHEEL" | awk '{print $1}')"
BYTES="$(stat -c%s "$WHEEL")"

# --- 5. build report --------------------------------------------------------
"$PY" "$HERE/rc_manifest.py" --wheel "$WHEEL" --json >"$OUT/manifest.json"
MANIFEST_DIGEST="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['manifest_sha256'])" "$OUT/manifest.json")"
ENTRIES="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['entries'])" "$OUT/manifest.json")"

cat >"$OUT/build-report.json" <<JSON
{
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "repository": "$(git -C "$CHECKOUT" config --get remote.origin.url)",
  "checkout": "$CHECKOUT",
  "branch": "$(git -C "$CHECKOUT" rev-parse --abbrev-ref HEAD)",
  "commit": "$COMMIT",
  "commit_short": "$SHORT",
  "version": "$VERSION",
  "artifact": "$WHEEL_NAME",
  "artifact_bytes": $BYTES,
  "artifact_sha256": "$SHA",
  "artifact_entries": $ENTRIES,
  "artifact_manifest_sha256": "$MANIFEST_DIGEST",
  "source_date_epoch": $SDE,
  "python": "$("$PY" -c 'import platform;print(platform.python_version())')",
  "setuptools": "$("$PY" -c 'import setuptools;print(setuptools.__version__)')",
  "node": "$(node --version 2>/dev/null || echo unknown)",
  "web_build": "$([ "$SKIP_WEB" = "1" ] && echo reused || echo executed)"
}
JSON

log "artifact $WHEEL_NAME"
log "  sha256   $SHA"
log "  bytes    $BYTES"
log "  entries  $ENTRIES"
log "  manifest $MANIFEST_DIGEST"
log "build report $OUT/build-report.json"
echo "$WHEEL"
