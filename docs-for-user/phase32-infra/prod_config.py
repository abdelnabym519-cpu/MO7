#!/usr/bin/env python3
"""Shared configuration for the Phase 32 production tools and harnesses.

Everything is read from the deployment contract (`etc/production.env`) that
prod_bootstrap.sh renders, so a validation run can never silently point at a
different deployment shape, and no harness needs an environment variable the
contract does not already document. Overrides exist only for debugging:

    PROD_ROOT       deployment root (default /home/user/mo7-prod)
    PROD_CONTRACT   contract file (default <root>/etc/production.env)
    PROD_FRONTEND   frontend target (default the TLS validation origin)
    PROD_BACKEND    loopback API target (default http://127.0.0.1:<port>)
    PROD_CREDENTIALS  account credentials file
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.parse

PROD_ROOT = Path(os.environ.get("PROD_ROOT", "/home/user/mo7-prod"))
CONTRACT_PATH = Path(os.environ.get("PROD_CONTRACT", PROD_ROOT / "etc/production.env"))
EVIDENCE_DIR = Path(os.environ.get("PROD_EVIDENCE", PROD_ROOT / "evidence"))


def contract() -> dict[str, str]:
    """Parse the deployment contract into a flat dict."""
    values: dict[str, str] = {}
    if CONTRACT_PATH.exists():
        for line in CONTRACT_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_CONTRACT = contract()


def value(key: str, default: str = "") -> str:
    return _CONTRACT.get(key, default)


def number(key: str, default: int) -> int:
    """Read a numeric contract value, failing with a usable message.

    A hand-edited contract must stop the operational tooling with a clear
    diagnostic (which key, which value) instead of a ValueError traceback that
    an operator has to decode.
    """
    raw = value(key, str(default))
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"FATAL: contract value {key}={raw!r} is not a number (in {CONTRACT_PATH})"
        ) from None


def secret(key: str, default: str = "") -> str:
    """Read an ops secret from secrets/secrets.env (never logged, never printed)."""
    path = Path(value("PROD_SECRETS", str(PROD_ROOT / "secrets"))) / "secrets.env"
    if not path.exists():
        return default
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, raw = line.partition("=")
        if name.strip() == key:
            return raw.strip().strip('"').strip("'")
    return default


# --- identity -----------------------------------------------------------------
ENVIRONMENT = value("PROD_ENVIRONMENT", "production")
RELEASE_ID = value("PROD_RELEASE_ID")
VERSION = value("PROD_VERSION")
SOURCE_COMMIT = value("PROD_SOURCE_COMMIT")
ARTIFACT_NAME = value("PROD_ARTIFACT_NAME")
ARTIFACT_SHA256 = value("PROD_ARTIFACT_SHA256")

# --- serving ------------------------------------------------------------------
BACKEND_HOST = value("PROD_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = number("PROD_BACKEND_PORT", 8001)
FRONTEND_HOST = value("PROD_FRONTEND_HOST", "0.0.0.0")
FRONTEND_PORT = number("PROD_FRONTEND_PORT", 3782)
TLS_VALIDATION_PORT = number("PROD_TLS_VALIDATION_PORT", 8443)
TLS_VALIDATION_ORIGIN = value(
    "PROD_TLS_VALIDATION_ORIGIN", f"https://127.0.0.1:{TLS_VALIDATION_PORT}"
)
PUBLIC_ORIGIN = value("PROD_PUBLIC_ORIGIN", f"https://{FRONTEND_PORT}-unknown.e2b.app")

# The harness speaks to the frontend through the validation ingress (TLS, the
# same shape a browser uses through the platform edge) unless overridden.
FRONTEND = os.environ.get("PROD_FRONTEND", TLS_VALIDATION_ORIGIN)
FRONTEND_PLAIN = os.environ.get("PROD_FRONTEND_PLAIN", f"http://127.0.0.1:{FRONTEND_PORT}")
BACKEND = os.environ.get("PROD_BACKEND", f"http://{BACKEND_HOST}:{BACKEND_PORT}")
BACKEND_TARGET = os.environ.get("PROD_BACKEND_TARGET", f"{BACKEND_HOST}:{BACKEND_PORT}")
# The address the platform port bridge dials for any listening port. Nothing may
# listen here: that is what keeps the API off the published surface.
PUBLISHED_BRIDGE_TARGET = os.environ.get("PROD_BRIDGE_TARGET", f"127.0.0.1:{BACKEND_PORT}")
TLS_TARGET = os.environ.get("PROD_TLS_TARGET", f"127.0.0.1:{TLS_VALIDATION_PORT}")

# --- layout -------------------------------------------------------------------
HOME = Path(value("PROD_HOME", str(PROD_ROOT / "home")))
RUN = Path(value("PROD_RUN", str(PROD_ROOT / "run")))
ETC = Path(value("PROD_ETC", str(PROD_ROOT / "etc")))
RELEASES = Path(value("PROD_RELEASES_DIR", str(PROD_ROOT / "releases")))
CURRENT_LINK = Path(value("PROD_CURRENT_LINK", str(PROD_ROOT / "current")))
BACKUPS = Path(value("PROD_BACKUPS", str(PROD_ROOT / "backups")))
SUPERVISORD = Path(value("PROD_SUPERVISORD", str(PROD_ROOT / "ops-venv/bin/supervisord")))
SUPERVISORCTL = Path(value("PROD_SUPERVISORCTL", str(PROD_ROOT / "ops-venv/bin/supervisorctl")))
SUPERVISOR_LOG = Path(value("PROD_SUPERVISOR_LOG", str(RUN / "supervisord.log")))
SUPERVISOR_SOCK = value("PROD_SUPERVISOR_SOCK", str(RUN / "supervisor.sock"))
SUPERVISOR_PID = Path(value("PROD_SUPERVISOR_PID", str(RUN / "supervisord.pid")))
LOG_DIR = Path(value("PROD_LOG_DIR", str(RUN / "supervisor")))

#: Line the fault-injection harness appends to the backend log when a drill's
#: fault has been removed. Deliberate drill errors above the newest marker are
#: recorded in evidence/faults/ and are not unexplained production errors, so
#: the log metric counts error lines only after the newest marker in its window.
#: The text must never contain the error markers the metric looks for.
FAULT_LOG_MARKER = "phase32 fault drill complete"
METRICS_DIR = Path(value("PROD_METRICS_DIR", str(RUN / "metrics")))
ALERTS_DIR = Path(value("PROD_ALERTS_DIR", str(RUN / "alerts")))
DEPLOY_LOG = RUN / "deployments.log"
CURRENT_RELEASE_FILE = RUN / "current-release.json"
VALIDATION_STATE = RUN / "validation-state.json"
WATCHDOG_PID = RUN / "watchdog.pid"
WATCHDOG_LOG = RUN / "watchdog.log"

# --- boundary decisions -------------------------------------------------------
# The application ships FastAPI's automatic documentation surface (/docs,
# /redoc, /openapi.json) with no configuration switch, so the production
# decision is a boundary decision rather than a product change: the API is bound
# to a private loopback-range address that the platform's port bridge does not
# dial, which keeps the whole API (documentation surface included) off the
# published surface. Verified by the network boundary checks.
API_DOCS_DECISION = value("PROD_API_DOCS_DECISION", "loopback-operator-only")

# --- resource limits ----------------------------------------------------------
# Declared once in the contract and enforced by supervisord (rlimit_nofile) on
# every supervised program.
RLIMIT_NOFILE = value("PROD_RLIMIT_NOFILE", "65536")


CREDENTIALS_PATH = Path(
    os.environ.get(
        "PROD_CREDENTIALS",
        str(Path(value("PROD_SECRETS", str(PROD_ROOT / "secrets"))) / "credentials.json"),
    )
)


def credentials() -> dict:
    return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))


def public_host() -> str:
    """Host (no scheme) of the deployment's public origin."""
    configured = os.environ.get("PROD_PUBLIC_HOST")
    if configured:
        return configured
    netloc = urllib.parse.urlparse(PUBLIC_ORIGIN).netloc
    return netloc or "localhost"


def sandbox_id() -> str:
    return os.environ.get("E2B_SANDBOX_ID", "")


def settings_dir() -> Path:
    return HOME / "data" / "user" / "settings"


def auth_secret_file() -> Path:
    return HOME / "data" / "system" / "auth" / "auth_secret"
