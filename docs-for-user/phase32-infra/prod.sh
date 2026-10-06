#!/usr/bin/env bash
# =============================================================================
# MO7 production operations CLI
# =============================================================================
# One documented entry point for every operational action on a Phase 32 host.
# Nothing here re-implements the application: processes go through supervisord
# (the repository's documented production process model), the application is
# started by the release's own start scripts, and data operations use the tools
# in harness/ that the closure report and runbook reference.
#
# commands:
#   start | stop | restart [component]   supervisord lifecycle (+ watchdog)
#   status                               supervisor states, listeners, watchdog
#   health [--json]                      liveness, readiness, frontend, ingress
#   identity                             release -> artifact -> commit traceability
#   env                                  rendered runtime environment (no secrets)
#   logs <component> [lines]             supervisor log tail
#   deploy <wheel> [args...]             the deployment pipeline
#   rollback [--to <release-id>]         promote a previous release and verify
#   releases                             list staged releases and which is current
#   backup [--label L] | backups | restore --backup DIR --target DIR
#   metrics [--once|--loop] | alerts [--json]
#   verify [--quick]                     infrastructure verification harness
#   artifact                             raw-endpoint artifact probe
#   security | validate | session | provision | faults
#   faults --phase inject|observe        failure injection and recovery
#   session <account>                    mint the browser harness session state
#   browser [playwright args...]         repository Playwright suites vs production
#   provision                            create the deployment accounts
#   harden                               re-apply the documented filesystem modes
#   watchdog start|stop|status
#   ingress start|stop|status            validation-only TLS ingress (harness)
# =============================================================================
set -euo pipefail

PROD_ROOT="${PROD_ROOT:-/home/user/mo7-prod}"
CONTRACT="$PROD_ROOT/etc/production.env"

usage() { sed -n '2,32p' "$0"; }

if [ ! -f "$CONTRACT" ]; then
  echo "FATAL: missing deployment contract $CONTRACT (run prod_bootstrap.sh first)" >&2
  exit 78
fi
# shellcheck disable=SC1090
set -a; . "$CONTRACT"; set +a

PY="$PROD_ROOT/ops-venv/bin/python"
SUPERVISORCTL="${PROD_ROOT}/ops-venv/bin/supervisorctl"
HARNESS="$PROD_ROOT/harness"

command="${1:-status}"
shift || true

sup() { "$SUPERVISORCTL" -c "$PROD_ROOT/etc/supervisord.conf" "$@"; }
# Liveness is supervisord's own pid: `status` exits non-zero whenever *any*
# program is stopped (the validation ingress is deliberately stopped outside a
# validation window), which must not be mistaken for a dead supervisor.
sup_running() { "$SUPERVISORCTL" -c "$PROD_ROOT/etc/supervisord.conf" pid >/dev/null 2>&1; }
http_code() { curl -s -k -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null || echo 000; }

clear_orphans() {
  # Programs left behind by a supervisord that died hold the API and frontend
  # ports; a fresh supervisord would then fail every start with EADDRINUSE (and
  # give up after startretries, leaving the program FATAL). They are terminated
  # first — they belong to a supervisor generation that no longer exists.
  local cleared
  cleared="$("$PY" "$HARNESS/prod_watchdog.py" clear-orphans 2>/dev/null || echo 0)"
  case "$cleared" in
    0\ *) ;;
    *) echo "cleared $cleared" ;;
  esac
}

start_stack() {
  if sup_running; then
    echo "supervisord already running"
  else
    clear_orphans
    echo "starting supervisord ($PROD_SUPERVISORD)"
    "$PROD_SUPERVISORD" -c "$PROD_ROOT/etc/supervisord.conf"
    for _ in $(seq 1 30); do sup_running && break; sleep 1; done
  fi
  sup status || true
  "$0" watchdog start || true
}

stop_stack() {
  "$0" watchdog stop || true
  if sup_running; then
    sup shutdown || true
    for _ in $(seq 1 30); do sup_running || break; sleep 1; done
  fi
  echo "supervisord stopped (stray children, if any, are reported by 'prod.sh status')"
}

case "$command" in
  start)
    if [ $# -gt 0 ]; then sup start "$@"; else start_stack; fi
    ;;
  stop)
    if [ $# -gt 0 ]; then sup stop "$@"; else stop_stack; fi
    ;;
  restart)
    if [ $# -gt 0 ]; then sup restart "$@"; else
      sup_running || { echo "supervisord is not running; use 'prod.sh start'" >&2; exit 1; }
      sup restart backend frontend
    fi
    ;;
  status)
    if sup_running; then sup status; else echo "supervisord: NOT RUNNING"; fi
    echo "--- listeners ---"
    ss -ltn 2>/dev/null | awk 'NR==1 || /:(8001|3782|8443)\b/'
    echo "--- watchdog ---"
    if [ -f "$PROD_WATCHDOG_PID" ] && kill -0 "$(cat "$PROD_WATCHDOG_PID")" 2>/dev/null; then
      echo "watchdog: running (pid $(cat "$PROD_WATCHDOG_PID"))"
    else
      echo "watchdog: not running"
    fi
    echo "--- current release ---"
    readlink -f "$PROD_CURRENT_LINK" 2>/dev/null || echo "none"
    ;;
  health)
    "$PY" "$HARNESS/prod_healthcheck.py" "$@"
    ;;
  identity)
    "$PY" "$HARNESS/prod_identity.py" "$@"
    ;;
  fingerprint)
    "$PY" "$HARNESS/prod_fingerprint.py" "$@"
    ;;
  env)
    "$PY" - <<PYEOF
import sys
from pathlib import Path
sys.path.insert(0, "$HARNESS")
import prod_config as cfg
for key in ("PROD_ENVIRONMENT", "PROD_RELEASE_ID", "PROD_VERSION", "PROD_SOURCE_COMMIT",
            "PROD_ARTIFACT_NAME", "PROD_ARTIFACT_SHA256", "PROD_BACKEND_HOST", "PROD_BACKEND_PORT",
            "PROD_BACKEND_WORKERS", "PROD_FRONTEND_HOST", "PROD_FRONTEND_PORT",
            "PROD_PUBLIC_ORIGIN", "PROD_TLS_VALIDATION_ORIGIN", "PROD_HOME", "PROD_DATA_MODE",
            "PROD_REQUIRED_SECRETS", "PROD_SERVICE_MANAGER"):
    print(f"{key}={cfg.value(key)}")
PYEOF
    ;;
  logs)
    component="${1:-backend}"; lines="${2:-80}"
    case "$component" in
      backend|frontend|scheduler) tail -n "$lines" "$PROD_LOG_DIR/$component.log" ;;
      supervisor) tail -n "$lines" "$PROD_SUPERVISOR_LOG" ;;
  watchdog) tail -n "$lines" "$PROD_WATCHDOG_LOG" ;;
      *) echo "unknown component $component (backend|frontend|scheduler|supervisor|watchdog)" >&2; exit 2 ;;
    esac
    ;;
  deploy)
    # `deploy <wheel> [args...]` is the documented form; prod_deploy.sh takes
    # --artifact explicitly, so translate a leading positional path.
    if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
      set -- --artifact "$@"
    fi
    PROD_ROOT="$PROD_ROOT" bash "$PROD_ROOT/bin/prod_deploy.sh" "$@"
    ;;
  promote)
    PROD_ROOT="$PROD_ROOT" bash "$PROD_ROOT/bin/prod_promote.sh" "$@"
    ;;
  rollback)
    exec "$PROD_ROOT/bin/prod_rollback.sh" "$@"
    ;;
  releases)
    for dir in "$PROD_RELEASES_DIR"/*/; do
      [ -d "$dir" ] || continue
      marker=""
      [ "$(readlink -f "$dir")" = "$(readlink -f "$PROD_CURRENT_LINK" 2>/dev/null)" ] && marker=" <- current"
      printf '%s%s\n' "${dir%/}" "$marker"
    done
    ;;
  backup)
    "$PY" "$HARNESS/prod_backup.py" "$@"
    ;;
  restore)
    "$PY" "$HARNESS/prod_restore.py" "$@"
    ;;
  backups)
    "$PY" "$HARNESS/prod_backup.py" --list
    ;;
  metrics)
    "$PY" "$HARNESS/prod_metrics.py" "$@"
    ;;
  alerts)
    "$PY" "$HARNESS/prod_alerts.py" "$@"
    ;;
  artifact)
    # Raw-endpoint artifact probe (the certification instrument): TLS validation
    # origin by default, PROD_ARTIFACT_TARGET=127.0.0.1:<port> to probe directly.
    "$PY" "$HARNESS/prod_artifact_probe.py" "$@"
    ;;
  verify)
    "$PY" "$HARNESS/prod_verify.py" "$@"
    ;;
  faults)
    "$PY" "$HARNESS/prod_faults.py" "$@"
    ;;
  validate)
    "$PY" "$HARNESS/prod_validate.py" "$@"
    ;;
  security)
    "$PY" "$HARNESS/prod_security.py" "$@"
    ;;
  session)
    "$PY" "$HARNESS/prod_session.py" "$@"
    ;;
  browser)
    # Repository Playwright suites against the deployed production frontend
    # through the validation ingress; the browser build and its libraries live
    # outside the deployment root, so the wrapper script owns that setup.
    PROD_ROOT="$PROD_ROOT" bash "$HARNESS/prod_browser.sh" "$@"
    ;;
  provision)
    "$PY" "$HARNESS/prod_validate.py" --provision
    ;;
  harden)
    # Re-apply the documented filesystem modes (the bootstrap does this too; an
    # operator needs a way to restore them after a manual change).
    chmod 700 "$PROD_ROOT/etc" "$PROD_ROOT/secrets" "$PROD_ROOT/backups" \
              "$PROD_ROOT/home" "$PROD_ROOT/home/data" "$PROD_ROOT/run" \
              "$PROD_ROOT/run/supervisor" "$PROD_ROOT/run/metrics" "$PROD_ROOT/run/alerts" \
              "$PROD_ROOT/evidence" 2>/dev/null || true
    chmod 600 "$PROD_ROOT/etc/production.env" "$PROD_ROOT/etc/alerts.json" \
              "$PROD_ROOT/etc/backup-policy.json" 2>/dev/null || true
    chmod 600 "$PROD_ROOT/secrets/"* 2>/dev/null || true
    chmod 600 "$PROD_ROOT/home/data/system/auth/auth_secret" 2>/dev/null || true
    find "$PROD_ROOT/home/data" -type d -exec chmod 700 {} + 2>/dev/null || true
    echo "applied the documented modes to data, secrets and operational state"
    ;;
  ingress)
    # Validation-only component, supervised by supervisord (autostart=false) so a
    # validation window gets the same crash handling as everything else. It is
    # not part of the production edge: production TLS terminates at the platform
    # edge, and its own certificate is self-signed and used only by the local
    # harnesses.
    case "${1:-status}" in
      start) sup start ingress-validation ;;
      stop) sup stop ingress-validation ;;
      status)
        if sup_running; then sup status ingress-validation; else echo "supervisord is not running"; fi
        ;;
      *) echo "ingress start|stop|status" >&2; exit 2 ;;
    esac
    ;;
  watchdog)
    case "${1:-start}" in
      start) exec "$PY" "$HARNESS/prod_watchdog.py" start ;;
      stop) exec "$PY" "$HARNESS/prod_watchdog.py" stop ;;
      status) exec "$PY" "$HARNESS/prod_watchdog.py" status ;;
      *) echo "watchdog start|stop|status" >&2; exit 2 ;;
    esac
    ;;
  init-config)
    # The settings must be rendered by the application's own settings service,
    # so this one command uses the promoted release's interpreter.
    [ -x "$PROD_CURRENT_LINK/venv/bin/python" ] || { echo "FATAL: no promoted release; run prod.sh promote --release <id>" >&2; exit 78; }
    DEEPTUTOR_HOME="$PROD_HOME" "$PROD_CURRENT_LINK/venv/bin/python" "$HARNESS/prod_init_config.py"
    # Settings are read once, at import. A deployment that has already started
    # keeps serving the settings it started with, so applying the configuration
    # to a running stack must restart the processes that read it — otherwise a
    # freshly promoted host keeps auth disabled and provisioning fails with
    # "Auth is disabled — user creation is not available" (observed).
    if sup_running; then
      echo "configuration written; restarting the stack so the release reads it"
      sup restart backend frontend
      for _ in $(seq 1 60); do
        if curl -fsS "http://$PROD_BACKEND_HOST:$PROD_BACKEND_PORT/health/ready" >/dev/null 2>&1; then
          echo "backend ready with the new configuration"
          break
        fi
        sleep 1
      done
      curl -fsS "http://$PROD_BACKEND_HOST:$PROD_BACKEND_PORT/health/ready" >/dev/null 2>&1 \
        || { echo "FATAL: backend did not become ready after applying the configuration" >&2; exit 1; }
    fi
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "unknown command: $command" >&2
    usage
    exit 2
    ;;
esac
