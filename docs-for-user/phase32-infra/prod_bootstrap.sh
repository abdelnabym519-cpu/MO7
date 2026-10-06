#!/usr/bin/env bash
# =============================================================================
# MO7 production host bootstrap
# =============================================================================
# Creates (or repairs) a production deployment host from a release artifact:
#
#   1. the directory contract                    etc/, run/, releases/, backups/, ...
#   2. the deployment contract                   etc/production.env
#   3. supervisord daemon + program definitions  etc/supervisord.conf, etc/supervisord.d/
#   4. policy files                              etc/alerts.json, etc/backup-policy.json
#   5. secrets                                   secrets/secrets.env (0600),
#                                                secrets/credentials.json (0600),
#                                                home/data/system/auth/auth_secret (0600)
#   6. validation instruments                    harness/ (Phase 32 tools + TLS ingress)
#   7. the first release                         releases/<release-id>/ via prod_deploy.sh
#
# It is idempotent: re-running it never overwrites an existing contract, secret
# or release directory, and never touches production data.
#
# usage:
#   prod_bootstrap.sh <wheel> [--release-id ID] [--commit SHA] [--public-origin URL]
#                     [--root DIR] [--no-deploy]
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES="$HERE/templates"

WHEEL=""
RELEASE_ID=""
COMMIT=""
PUBLIC_ORIGIN=""
PROD_ROOT="${PROD_ROOT:-/home/user/mo7-prod}"
DO_DEPLOY=1

while [ $# -gt 0 ]; do
  case "$1" in
    --release-id) RELEASE_ID="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --public-origin) PUBLIC_ORIGIN="$2"; shift 2 ;;
    --root) PROD_ROOT="$2"; shift 2 ;;
    --no-deploy) DO_DEPLOY=0; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) WHEEL="$1"; shift ;;
  esac
done

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ -z "$WHEEL" ] || [ ! -f "$WHEEL" ]; then
  echo "usage: prod_bootstrap.sh <wheel> [options]  (wheel not found: ${WHEEL:-<empty>})" >&2
  exit 2
fi
WHEEL="$(readlink -f "$WHEEL")"

ARTIFACT_NAME="$(basename "$WHEEL")"
ARTIFACT_SHA256="$(sha256sum "$WHEEL" | awk '{print $1}')"
VERSION="$(printf '%s' "$ARTIFACT_NAME" | sed -E 's/^deeptutor-([0-9][^-]*(\.post[0-9]+)?)-py3-none-any\.whl$/\1/')"
if [ "$VERSION" = "$ARTIFACT_NAME" ]; then
  echo "FATAL: cannot derive the version from $ARTIFACT_NAME" >&2
  exit 2
fi

# Source commit: either given, or read from the checkout the deploy will use.
if [ -z "$COMMIT" ]; then
  for candidate in "${MO7_SOURCE_CHECKOUT:-/home/user/MO7}" /home/user/MO7; do
    if [ -d "$candidate/.git" ]; then
      COMMIT="$(git -C "$candidate" rev-parse HEAD)"
      break
    fi
  done
fi
if [ -z "$COMMIT" ]; then
  echo "FATAL: source commit unknown; pass --commit SHA" >&2
  exit 2
fi
if [ -z "$RELEASE_ID" ]; then
  RELEASE_ID="${VERSION}-$(printf '%s' "$COMMIT" | cut -c1-7)"
fi
case "$RELEASE_ID" in
  *[!A-Za-z0-9._-]*) echo "FATAL: release id contains unsupported characters: $RELEASE_ID" >&2; exit 2 ;;
esac

if [ -z "$PUBLIC_ORIGIN" ]; then
  SANDBOX_ID="${E2B_SANDBOX_ID:-}"
  if [ -n "$SANDBOX_ID" ]; then
    PUBLIC_ORIGIN="https://3782-${SANDBOX_ID}.e2b.app"
  else
    PUBLIC_ORIGIN="https://localhost"
  fi
fi

log "bootstrap root=$PROD_ROOT release=$RELEASE_ID version=$VERSION commit=${COMMIT:0:12}"
log "artifact $ARTIFACT_NAME sha256=$ARTIFACT_SHA256"

# --- 1. layout ---------------------------------------------------------------
mkdir -p "$PROD_ROOT"/{bin,etc/supervisord.d,secrets,releases,home,run,run/supervisor,run/metrics,run/alerts,backups,evidence,harness}
chmod 700 "$PROD_ROOT" "$PROD_ROOT/secrets" "$PROD_ROOT/run"
chmod 750 "$PROD_ROOT/backups"

# --- 2. deployment contract --------------------------------------------------
if [ -f "$PROD_ROOT/etc/production.env" ]; then
  log "contract exists; leaving it untouched ($PROD_ROOT/etc/production.env)"
else
  sed -e "s|@PROD_ROOT@|$PROD_ROOT|g" \
      -e "s|@RELEASE_ID@|$RELEASE_ID|g" \
      -e "s|@VERSION@|$VERSION|g" \
      -e "s|@SOURCE_COMMIT@|$COMMIT|g" \
      -e "s|@ARTIFACT_NAME@|$ARTIFACT_NAME|g" \
      -e "s|@ARTIFACT_SHA256@|$ARTIFACT_SHA256|g" \
      -e "s|@PUBLIC_ORIGIN@|$PUBLIC_ORIGIN|g" \
      "$TEMPLATES/production.env.tmpl" >"$PROD_ROOT/etc/production.env"
  chmod 644 "$PROD_ROOT/etc/production.env"
  log "wrote contract $PROD_ROOT/etc/production.env"
fi
# shellcheck disable=SC1090
set -a; . "$PROD_ROOT/etc/production.env"; set +a

# --- 2b. ops tooling virtualenv ---------------------------------------------
if [ ! -x "$PROD_ROOT/ops-venv/bin/supervisord" ]; then
  log "creating the ops virtualenv and installing the process manager (supervisor)"
  python3 -m venv "$PROD_ROOT/ops-venv"
  "$PROD_ROOT/ops-venv/bin/pip" install -q -U pip setuptools wheel
  "$PROD_ROOT/ops-venv/bin/pip" install -q supervisor
fi
"$PROD_ROOT/ops-venv/bin/supervisord" --version | sed 's/^/  supervisor /'

# --- 3. supervisord ----------------------------------------------------------
sed -e "s|@PROD_ROOT@|$PROD_ROOT|g" \
    -e "s|@PROD_MIN_FDS@|${PROD_MIN_FDS:-65536}|g" \
    -e "s|@PROD_UID@|$(id -u)|g" \
    -e "s|@PROD_GID@|$(id -g)|g" \
    "$TEMPLATES/supervisord.conf.tmpl" >"$PROD_ROOT/etc/supervisord.conf"
for program in backend frontend scheduler; do
  sed -e "s|@PROD_ROOT@|$PROD_ROOT|g" \
      -e "s|@PROD_RLIMIT_NOFILE@|${PROD_RLIMIT_NOFILE:-65536}|g" \
      "$TEMPLATES/program-${program}.conf.tmpl" >"$PROD_ROOT/etc/supervisord.d/${program}.conf"
done
sed -e "s|@PROD_ROOT@|$PROD_ROOT|g" \
    -e "s|@PROD_RLIMIT_NOFILE@|${PROD_RLIMIT_NOFILE:-65536}|g" \
    -e "s|@PROD_TLS_VALIDATION_PORT@|${PROD_TLS_VALIDATION_PORT:-8443}|g" \
    -e "s|@PROD_FRONTEND_PORT@|${PROD_FRONTEND_PORT:-3782}|g" \
    "$TEMPLATES/program-ingress-validation.conf.tmpl" >"$PROD_ROOT/etc/supervisord.d/ingress-validation.conf"
chmod 644 "$PROD_ROOT"/etc/supervisord.conf "$PROD_ROOT"/etc/supervisord.d/*.conf
log "rendered supervisord configuration ($(ls "$PROD_ROOT/etc/supervisord.d" | tr '\n' ' '))"

# --- 4. policies -------------------------------------------------------------
for policy in alerts.json backup-policy.json; do
  if [ ! -f "$PROD_ROOT/etc/$policy" ]; then
    cp "$TEMPLATES/$policy" "$PROD_ROOT/etc/$policy"
    log "installed policy $PROD_ROOT/etc/$policy"
  fi
done

# --- 5. secrets --------------------------------------------------------------
if [ ! -f "$PROD_ROOT/secrets/secrets.env" ]; then
  cat >"$PROD_ROOT/secrets/secrets.env" <<'EOF'
# MO7 production ops secrets. Mode 0600, owned by the deployment user, never
# committed, never logged. Keys here are exported into the release start
# scripts; the application reads its own settings from the data root.
#
# MO7_MONITORING_TOKEN — reserved for the metrics/alerting probes if a
# read-only monitoring account is later introduced. Unused today: the collectors
# probe public health endpoints and the app's own logs.
EOF
  chmod 600 "$PROD_ROOT/secrets/secrets.env"
  log "created $PROD_ROOT/secrets/secrets.env (0600)"
fi

if [ ! -f "$PROD_ROOT/secrets/credentials.json" ]; then
  python3 - "$PROD_ROOT/secrets/credentials.json" <<'PYEOF'
import json
import secrets
import sys

path = sys.argv[1]
payload = {
    "note": "Non-production deployment accounts. Generated by prod_bootstrap.sh; mode 0600.",
    "admin": {"username": "prod-admin", "password": secrets.token_urlsafe(24)},
    "tenant_a": {"username": "prod-tenant-a", "password": secrets.token_urlsafe(24)},
    "tenant_b": {"username": "prod-tenant-b", "password": secrets.token_urlsafe(24)},
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
PYEOF
  chmod 600 "$PROD_ROOT/secrets/credentials.json"
  log "generated deployment accounts (0600): prod-admin, prod-tenant-a, prod-tenant-b"
fi

# Auth secret: materialised here (0600) so the release start script can require
# it and the alert evaluator can watch it, instead of the app quietly
# generating one on first start.
SECRET_FILE="$PROD_ROOT/home/data/system/auth/auth_secret"
mkdir -p "$(dirname "$SECRET_FILE")"
if [ ! -s "$SECRET_FILE" ]; then
  python3 -c "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_hex(32))" "$SECRET_FILE"
  log "materialised auth secret $SECRET_FILE (0600)"
fi
chmod 600 "$SECRET_FILE"

# --- 6. validation instruments ----------------------------------------------
cp -f "$HERE"/prod_*.py "$PROD_ROOT/harness/"
chmod 755 "$PROD_ROOT/harness/"prod_*.py
if [ ! -e "$PROD_ROOT/harness/ingress_tls_proxy.js" ]; then
  cp -f "$HERE/../phase31-staging/ingress_tls_proxy.js" "$PROD_ROOT/harness/"
fi
if [ ! -e "$PROD_ROOT/harness/playwright.production.config.ts" ] && [ -f "$HERE/playwright.production.config.ts" ]; then
  cp -f "$HERE/playwright.production.config.ts" "$PROD_ROOT/harness/"
fi
mkdir -p "$PROD_ROOT/etc/systemd" && cp -f "$HERE/templates/systemd/mo7-production.service" "$PROD_ROOT/etc/systemd/"
# The browser config resolves @playwright/test from its own directory, and the
# repository keeps the only installed copy.
if [ ! -e "$PROD_ROOT/harness/node_modules" ] && [ -d "${MO7_SOURCE_CHECKOUT:-/home/user/MO7}/web/node_modules" ]; then
  ln -sfn "${MO7_SOURCE_CHECKOUT:-/home/user/MO7}/web/node_modules" "$PROD_ROOT/harness/node_modules"
fi
# Self-signed material for the *validation* ingress (never presented as
# production TLS; production TLS terminates at the platform edge).
if [ ! -f "$PROD_ROOT/harness/tls/ingress.crt" ]; then
  mkdir -p "$PROD_ROOT/harness/tls"
  PUBLIC_HOST="$(printf '%s' "$PUBLIC_ORIGIN" | sed -E 's|^https?://||; s|/.*$||; s|:.*$||')"
  openssl req -x509 -newkey rsa:2048 -sha256 -days 90 -nodes \
    -keyout "$PROD_ROOT/harness/tls/ingress.key" -out "$PROD_ROOT/harness/tls/ingress.crt" \
    -subj "/CN=${PUBLIC_HOST}" \
    -addext "subjectAltName=DNS:${PUBLIC_HOST},DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
  chmod 600 "$PROD_ROOT/harness/tls/ingress.key"
  chmod 644 "$PROD_ROOT/harness/tls/ingress.crt"
  log "issued validation ingress certificate for ${PUBLIC_HOST} (90 days, self-signed)"
fi

# --- 6b. operational entry points --------------------------------------------
# The deployment pipeline renders the release start scripts from these
# templates, so they ship with the operational entry points.
mkdir -p "$PROD_ROOT/bin/templates" "$PROD_ROOT/bin/templates/systemd"
cp -f "$HERE"/templates/*.tmpl "$PROD_ROOT/bin/templates/"
cp -f "$HERE"/templates/systemd/* "$PROD_ROOT/bin/templates/systemd/" 2>/dev/null || true
harden_permissions() {
  # Documented modes for a production host: the deployment user owns the data,
  # the secrets and the operational state; no group/other access is granted to
  # either. Applied on every bootstrap so a repaired host cannot be left more
  # permissive than a freshly created one.
  chmod 700 "$PROD_ROOT/etc" "$PROD_ROOT/secrets" "$PROD_ROOT/backups" \
            "$PROD_ROOT/home" "$PROD_ROOT/home/data" "$PROD_ROOT/run" \
            "$PROD_ROOT/run/supervisor" "$PROD_ROOT/run/metrics" "$PROD_ROOT/run/alerts" \
            "$PROD_ROOT/evidence" 2>/dev/null || true
  chmod 600 "$PROD_ROOT/etc/production.env" "$PROD_ROOT/etc/alerts.json" \
            "$PROD_ROOT/etc/backup-policy.json" 2>/dev/null || true
  chmod 600 "$PROD_ROOT/secrets/"* 2>/dev/null || true
  chmod 600 "$PROD_ROOT/home/data/system/auth/auth_secret" 2>/dev/null || true
  find "$PROD_ROOT/home/data" -type d -exec chmod 700 {} + 2>/dev/null || true
  log "applied the documented modes to data, secrets and operational state"
}
harden_permissions

install -m 755 "$HERE/prod.sh" "$PROD_ROOT/bin/prod.sh"
install -m 755 "$HERE/prod_deploy.sh" "$PROD_ROOT/bin/prod_deploy.sh"
install -m 755 "$HERE/prod_promote.sh" "$PROD_ROOT/bin/prod_promote.sh"
install -m 755 "$HERE/prod_rollback.sh" "$PROD_ROOT/bin/prod_rollback.sh"
log "installed operational entry points in $PROD_ROOT/bin/"

# --- 7. first release --------------------------------------------------------
if [ "$DO_DEPLOY" = "1" ]; then
  log "staging the first release through the deployment pipeline"
  PROD_ROOT="$PROD_ROOT" bash "$HERE/prod_deploy.sh" --artifact "$WHEEL" \
    --release-id "$RELEASE_ID" --commit "$COMMIT" --skip-promote
  log "bootstrap complete; run: $PROD_ROOT/bin/prod.sh promote --release $RELEASE_ID"
else
  log "bootstrap complete (no release staged)"
fi
