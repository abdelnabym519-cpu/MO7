#!/usr/bin/env bash
# =============================================================================
# MO7 staging supervisor
# =============================================================================
# Production-shaped process layout for a single staging host, using the
# repository's own documented start commands:
#
#   backend   : `python -m uvicorn deeptutor.api.main:app` — the same entrypoint
#               the container image uses, bound to loopback so the API is never
#               published by the ingress.
#   frontend  : `node server.js` from the packaged Next.js standalone bundle,
#               with PORT/HOSTNAME from the deployment contract.
#   ingress   : TLS reverse proxy in front of the frontend (the local stand-in
#               for the platform edge; see harness/ingress_tls_proxy.js).
#
# Environment is rendered from data/user/settings/*.json through the app's own
# exporter (export_runtime_settings_to_env), exactly like the container
# entrypoint; nothing is hand-exported except the deployment's bind hosts.
#
# Commands: init-config | deploy | start [component] | stop [component]
#           | restart [component] | status | health | identity | session | env
#           | logs [component] [lines]
#           | identity | env | logs
# =============================================================================
set -euo pipefail

STAGING_ROOT=/home/user/mo7-staging
# shellcheck source=/home/user/mo7-staging/etc/staging.env
source "$STAGING_ROOT/etc/staging.env"

PY="$STAGING_VENV/bin/python"
RUNTIME_WEB="$STAGING_HOME/data/user/runtime/web"
BACKEND_LOG="$STAGING_RUN/backend.log"
FRONTEND_LOG="$STAGING_RUN/frontend.log"
INGRESS_LOG="$STAGING_RUN/ingress.log"
BACKEND_PID="$STAGING_RUN/backend.pid"
FRONTEND_PID="$STAGING_RUN/frontend.pid"
INGRESS_PID="$STAGING_RUN/ingress.pid"
DEPLOY_LOG="$STAGING_RUN/deployments.log"

mkdir -p "$STAGING_RUN" "$STAGING_EVIDENCE"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

pid_alive() {
  local file="$1"
  [ -f "$file" ] || return 1
  local pid
  pid="$(cat "$file" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

want_url() {
  local url="$1" deadline="${2:-60}" start
  start=$(date +%s)
  while [ $(( $(date +%s) - start )) -lt "$deadline" ]; do
    if curl -fsS -k -o /dev/null -m 4 "$url" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

# --- env rendering (mirrors the container entrypoint) ------------------------
render_env() {
  export DEEPTUTOR_HOME="$STAGING_HOME"
  export DEEPTUTOR_IGNORE_PROCESS_ENV_OVERRIDES=1
  unset BACKEND_PORT BACKEND_WORKERS FRONTEND_PORT NEXT_PUBLIC_API_BASE_EXTERNAL \
        NEXT_PUBLIC_API_BASE CORS_ORIGIN CORS_ORIGINS DISABLE_SSL_VERIFY \
        CHAT_ATTACHMENT_DIR AUTH_ENABLED NEXT_PUBLIC_AUTH_ENABLED AUTH_USERNAME \
        AUTH_PASSWORD_HASH AUTH_TOKEN_EXPIRE_HOURS AUTH_COOKIE_SECURE 2>/dev/null || true
  local rendered
  rendered="$("$PY" - <<'PY'
import shlex
from deeptutor.services.config import export_runtime_settings_to_env
for key, value in export_runtime_settings_to_env(overwrite=True).items():
    print(f"export {key}={shlex.quote(str(value))}")
PY
)"
  # shellcheck disable=SC1090
  eval "$rendered"
  export DEEPTUTOR_API_BASE_URL="http://127.0.0.1:$STAGING_BACKEND_PORT"
  export DEEPTUTOR_AUTH_ENABLED="true"
  export BACKEND_HOST="$STAGING_BACKEND_HOST"
  export BACKEND_PORT="$STAGING_BACKEND_PORT"
  export FRONTEND_HOST="$STAGING_FRONTEND_HOST"
  export FRONTEND_PORT="$STAGING_FRONTEND_PORT"
}

# --- 1. settings --------------------------------------------------------------
cmd_init_config() {
  render_env
  STAGING_PUBLIC_ORIGIN="$STAGING_PUBLIC_ORIGIN" \
  STAGING_TLS_ORIGIN="$STAGING_TLS_ORIGIN" \
  STAGING_BACKEND_PORT="$STAGING_BACKEND_PORT" \
  STAGING_FRONTEND_PORT="$STAGING_FRONTEND_PORT" \
    "$PY" "$STAGING_ROOT/bin/staging_init_config.py"
}

# --- 2. deploy ---------------------------------------------------------------
cmd_deploy() {
  if [ ! -f "$STAGING_WHEEL" ]; then
    echo "FATAL: missing release artifact $STAGING_WHEEL" >&2
    return 1
  fi
  local actual
  actual="$(sha256sum "$STAGING_WHEEL" | awk '{print $1}')"
  if [ "$actual" != "$STAGING_ARTIFACT_SHA256" ]; then
    echo "FATAL: artifact hash mismatch: $actual != $STAGING_ARTIFACT_SHA256" >&2
    return 1
  fi
  log "artifact sha256 verified: $actual"

  # install when this artifact is not the one already installed
  local installed_marker="$STAGING_RUN/installed-artifact.sha256"
  if [ ! -f "$installed_marker" ] || [ "$(cat "$installed_marker")" != "$actual" ]; then
    log "installing $STAGING_ARTIFACT into the staging venv (artifact $actual)"
    "$STAGING_VENV/bin/pip" install --force-reinstall --no-deps "$STAGING_WHEEL" \
      >>"$STAGING_EVIDENCE/pip-reinstall.log" 2>&1
    printf '%s\n' "$actual" >"$installed_marker"
  else
    log "installed artifact already current: $actual"
  fi

  # materialise the packaged web cache through the supported launcher when the
  # cache does not belong to this artifact
  export DEEPTUTOR_HOME="$STAGING_HOME"
  local web_marker="$STAGING_RUN/runtime-web-artifact.sha256"
  if [ ! -f "$web_marker" ] || [ "$(cat "$web_marker")" != "$actual" ] || \
     ! "$PY" - <<'PY'
import json, sys
from pathlib import Path
cache = Path("/home/user/mo7-staging/home/data/user/runtime/web")
try:
    payload = json.loads((cache / ".deeptutor-web-runtime.json").read_text())
except Exception:
    sys.exit(1)
sys.exit(0 if (cache / "server.js").exists() and payload.get("auth_enabled") is True else 1)
PY
  then
    log "preparing packaged runtime web cache via 'deeptutor start' (supported launcher)"
    "$STAGING_VENV/bin/deeptutor" start --home "$STAGING_HOME" --no-browser --detach \
      >>"$STAGING_EVIDENCE/launcher-prep.log" 2>&1 || true
    want_url "http://127.0.0.1:$STAGING_BACKEND_PORT/health/live" 90 \
      || log "WARN: launcher-prep backend did not report healthy"
    want_url "http://127.0.0.1:$STAGING_FRONTEND_PORT/login" 90 \
      || log "WARN: launcher-prep frontend did not report healthy"
    "$STAGING_VENV/bin/deeptutor" stop --home "$STAGING_HOME" \
      >>"$STAGING_EVIDENCE/launcher-prep.log" 2>&1 || true
    sleep 2
  fi
  if [ ! -f "$RUNTIME_WEB/server.js" ]; then
    echo "FATAL: runtime web cache missing at $RUNTIME_WEB" >&2
    return 1
  fi
  printf '%s\n' "$actual" >"$web_marker"

  printf '%s deploy release=%s artifact=%s commit=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STAGING_RELEASE_ID" "$actual" "$STAGING_SOURCE_COMMIT" \
    >>"$DEPLOY_LOG"
  log "deploy complete: runtime web at $RUNTIME_WEB"
}

# --- 3. start / stop ----------------------------------------------------------
port_owner() {
  # The recorded pid is authoritative. A port held by anything else means a
  # stray process from an earlier run, which must be stopped before starting:
  # otherwise the new server dies with EADDRINUSE and the old one keeps
  # answering with the previous release's configuration.
  ss -ltnp 2>/dev/null | awk -v port=":$1" '$4 ~ port {print $0}'
}

comp_pid_file() {
  case "$1" in
    backend)  echo "$BACKEND_PID" ;;
    frontend) echo "$FRONTEND_PID" ;;
    ingress)  echo "$INGRESS_PID" ;;
    *) return 1 ;;
  esac
}

require_free_port() {
  local port="$1" holder
  holder="$(port_owner "$port")"
  if [ -n "$holder" ]; then
    echo "FATAL: port $port is already held by another process; stop it first:" >&2
    echo "  $holder" >&2
    return 1
  fi
}

stop_component() {
  local name="$1" file pid
  file="$(comp_pid_file "$name")" || { echo "unknown component $name" >&2; return 2; }
  if pid_alive "$file"; then
    pid="$(cat "$file")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    log "stopped $name (pid $pid)"
  fi
  rm -f "$file"
}

# Start one component on its own. Used by fault-injection/recovery runs; a full
# `start` still comes up in contract order (backend, frontend, ingress).
start_component() {
  local name="$1"
  render_env
  case "$name" in
    backend)
      require_free_port "$STAGING_BACKEND_PORT" || return 1
      : >"$BACKEND_LOG"
      setsid "$PY" -m uvicorn deeptutor.api.main:app \
          --host "$STAGING_BACKEND_HOST" --port "$STAGING_BACKEND_PORT" \
          --log-config "$STAGING_ROOT/harness/uvicorn-log.json" \
          >>"$BACKEND_LOG" 2>&1 < /dev/null &
      echo $! >"$BACKEND_PID"
      want_url "http://127.0.0.1:$STAGING_BACKEND_PORT/health/live" 60 \
        || { echo "backend did not become healthy; see $BACKEND_LOG" >&2; return 1; }
      ;;
    frontend)
      require_free_port "$STAGING_FRONTEND_PORT" || return 1
      : >"$FRONTEND_LOG"
      ( cd "$RUNTIME_WEB" && exec env PORT="$STAGING_FRONTEND_PORT" HOSTNAME="$STAGING_FRONTEND_HOST" \
          node server.js >>"$FRONTEND_LOG" 2>&1 < /dev/null ) &
      echo $! >"$FRONTEND_PID"
      want_url "http://127.0.0.1:$STAGING_FRONTEND_PORT/login" 90 \
        || { echo "frontend did not become healthy; see $FRONTEND_LOG" >&2; return 1; }
      ;;
    ingress)
      require_free_port "$STAGING_TLS_PORT" || return 1
      setsid node "$STAGING_ROOT/harness/ingress_tls_proxy.js" \
        "$STAGING_ROOT/harness/tls" "$STAGING_TLS_PORT" "$STAGING_FRONTEND_PORT" \
        >>"$INGRESS_LOG" 2>&1 < /dev/null &
      echo $! >"$INGRESS_PID"
      want_url "https://127.0.0.1:$STAGING_TLS_PORT/login" 30 \
        || { echo "TLS ingress did not become healthy; see $INGRESS_LOG" >&2; return 1; }
      ;;
    *)
      echo "unknown component $name (backend|frontend|ingress)" >&2
      return 2
      ;;
  esac
  log "started $name pid=$(cat "$(comp_pid_file "$name")")"
}

cmd_start() {
  if [ -n "${1:-}" ]; then start_component "$1"; return $?; fi
  if pid_alive "$BACKEND_PID" || pid_alive "$FRONTEND_PID"; then
    echo "already running (use restart)"; return 0
  fi
  for port in "$STAGING_BACKEND_PORT" "$STAGING_FRONTEND_PORT" "$STAGING_TLS_PORT"; do
    local holder
    holder="$(port_owner "$port")"
    if [ -n "$holder" ]; then
      echo "FATAL: port $port is already held by another process; stop it first:" >&2
      echo "  $holder" >&2
      return 1
    fi
  done
  render_env
  : >"$BACKEND_LOG"; : >"$FRONTEND_LOG"
  # Each daemon is detached in its own session (`setsid`) with every standard
  # descriptor redirected: the supervisor must not stay attached to a process it
  # does not own, and a caller's pipe must not be held open by the servers.
  setsid "$PY" -m uvicorn deeptutor.api.main:app \
      --host "$STAGING_BACKEND_HOST" --port "$STAGING_BACKEND_PORT" \
      --log-config "$STAGING_ROOT/harness/uvicorn-log.json" \
      >>"$BACKEND_LOG" 2>&1 < /dev/null &
  echo $! >"$BACKEND_PID"
  ( cd "$RUNTIME_WEB" && exec env PORT="$STAGING_FRONTEND_PORT" HOSTNAME="$STAGING_FRONTEND_HOST" \
      node server.js >>"$FRONTEND_LOG" 2>&1 < /dev/null ) &
  echo $! >"$FRONTEND_PID"
  if ! pid_alive "$INGRESS_PID"; then
    setsid node "$STAGING_ROOT/harness/ingress_tls_proxy.js" \
      "$STAGING_ROOT/harness/tls" "$STAGING_TLS_PORT" "$STAGING_FRONTEND_PORT" \
      >>"$INGRESS_LOG" 2>&1 < /dev/null &
    echo $! >"$INGRESS_PID"
  fi
  log "start: backend $STAGING_BACKEND_HOST:$STAGING_BACKEND_PORT (loopback) | frontend $STAGING_FRONTEND_HOST:$STAGING_FRONTEND_PORT"
  want_url "http://127.0.0.1:$STAGING_BACKEND_PORT/health/live" 60 \
    || { echo "backend did not become healthy; see $BACKEND_LOG" >&2; return 1; }
  want_url "http://127.0.0.1:$STAGING_FRONTEND_PORT/login" 90 \
    || { echo "frontend did not become healthy; see $FRONTEND_LOG" >&2; return 1; }
  want_url "https://127.0.0.1:$STAGING_TLS_PORT/login" 30 \
    || { echo "TLS ingress did not become healthy; see $INGRESS_LOG" >&2; return 1; }
  log "started: backend pid=$(cat "$BACKEND_PID") frontend pid=$(cat "$FRONTEND_PID") ingress pid=$(cat "$INGRESS_PID")"
}

cmd_stop() {
  if [ -n "${1:-}" ]; then stop_component "$1"; return $?; fi
  for pair in "frontend:$FRONTEND_PID" "backend:$BACKEND_PID" "ingress:$INGRESS_PID"; do
    local name="${pair%%:*}" file="${pair##*:}"
    if pid_alive "$file"; then
      local pid; pid="$(cat "$file")"
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      log "stopped $name (pid $pid)"
    fi
  done
  rm -f "$BACKEND_PID" "$FRONTEND_PID" "$INGRESS_PID"
}

cmd_restart() {
  if [ -n "${1:-}" ]; then cmd_stop "$1" && cmd_start "$1"; return $?; fi
  cmd_stop; cmd_start
}

cmd_status() {
  printf 'staging %s (env=%s)\n' "$STAGING_RELEASE_ID" "$STAGING_ENVIRONMENT"
  for pair in "backend:$BACKEND_PID" "frontend:$FRONTEND_PID" "ingress:$INGRESS_PID"; do
    local name="${pair%%:*}" file="${pair##*:}"
    if pid_alive "$file"; then
      printf '  %-8s running pid=%s\n' "$name" "$(cat "$file")"
    else
      printf '  %-8s stopped\n' "$name"
    fi
  done
  ss -ltn 2>/dev/null | awk 'NR>1 {print "  " $4}' | sort -u
}

cmd_health() {
  local rc=0
  for url in "http://127.0.0.1:$STAGING_BACKEND_PORT/health/live" \
             "http://127.0.0.1:$STAGING_BACKEND_PORT/health/ready" \
             "http://127.0.0.1:$STAGING_FRONTEND_PORT/login" \
             "https://127.0.0.1:$STAGING_TLS_PORT/login"; do
    local code
    code="$(curl -ksS -o /dev/null -m 5 -w '%{http_code}' "$url" || echo 000)"
    printf '  %-58s %s\n' "$url" "$code"
    [ "$code" = "200" ] || rc=1
  done
  return $rc
}

cmd_identity() { "$PY" "$STAGING_ROOT/bin/staging_identity.py"; }
cmd_session() { STAGING_ROOT="$STAGING_ROOT" "$PY" "$STAGING_ROOT/bin/staging_session.py" "${1:-admin}"; }
cmd_env() { render_env; env | grep -E '^(BACKEND|FRONTEND|DEEPTUTOR_API_BASE_URL|AUTH|CORS)_' | sort; }
cmd_logs() {
  local which="${1:-all}" lines="${2:-60}"
  case "$which" in
    backend) tail -n "$lines" "$BACKEND_LOG" ;;
    frontend) tail -n "$lines" "$FRONTEND_LOG" ;;
    ingress) tail -n "$lines" "$INGRESS_LOG" ;;
    deploy) tail -n "$lines" "$DEPLOY_LOG" ;;
    all) tail -n "$lines" "$BACKEND_LOG" "$FRONTEND_LOG" "$INGRESS_LOG" ;;
  esac
}

case "${1:-status}" in
  init-config) cmd_init_config ;;
  deploy)      cmd_deploy ;;
  start)       shift; cmd_start "${1:-}" ;;
  stop)        shift; cmd_stop "${1:-}" ;;
  restart)     shift; cmd_restart "${1:-}" ;;
  status)      cmd_status ;;
  health)      cmd_health ;;
  identity)    cmd_identity ;;
  session)     shift; cmd_session "${1:-admin}" ;;
  env)         cmd_env ;;
  logs)        shift; cmd_logs "$@" ;;
  *) echo "usage: staging.sh {start|stop|restart} [backend|frontend|ingress] | {init-config|deploy|status|health|identity|session [account]|env|logs [backend|frontend|ingress|deploy|all] [lines]}" >&2; exit 2 ;;
esac
