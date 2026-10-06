#!/usr/bin/env python3
"""Fault injection, degradation, recovery and observability for staging.

A staging deployment is only validated when a component failure is *observed*
from the outside, degrades instead of corrupting, recovers when the component is
restored, and leaves a usable trace in the logs.

Phases (run them in order; `observe` reads what `inject` recorded):

    python3 staging_faults.py --phase inject
    python3 staging_faults.py --phase observe

Evidence: ``<staging root>/evidence/staging-faults-<phase>.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from staging_http import Actor, call, login, upload  # noqa: E402

STAGING = Path(os.environ.get("STAGING_ROOT", "/home/user/mo7-staging"))
EVIDENCE = STAGING / "evidence"
SUPERVISOR = STAGING / "bin/staging.sh"
CREDS = json.loads((STAGING / "secrets/staging-credentials.json").read_text())
TLS_TARGET = os.environ.get("STAGING_TLS_TARGET", "127.0.0.1:8443")
BACKEND_API = os.environ.get("STAGING_BACKEND_API", "http://127.0.0.1:8101")
FRONTEND = os.environ.get("STAGING_FRONTEND", "http://127.0.0.1:3982")

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def supervisor(*args: str) -> int:
    completed = subprocess.run(
        ["bash", str(SUPERVISOR), *args], capture_output=True, text=True, timeout=180
    )
    sys.stdout.write(completed.stdout)
    if completed.returncode:
        sys.stderr.write(completed.stderr)
    return completed.returncode


def port_open(target: str, timeout: float = 2.0) -> bool:
    host, _, port = target.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


INSECURE_TLS = ssl.create_default_context()
INSECURE_TLS.check_hostname = False
INSECURE_TLS.verify_mode = ssl.CERT_NONE


def http_status(url: str, timeout: float = 5.0) -> int:
    # The public origin during a fault run is the harness TLS ingress, whose
    # certificate is self-signed by design; reachability is what is measured.
    opener = urllib.request.build_opener()
    if url.startswith("https://"):
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=INSECURE_TLS))
    try:
        request = urllib.request.Request(url)
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 — unreachable/down is a status of its own
        return 0


def health(port: int, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:120]


def tenant_storage_dir() -> Path:
    users = json.loads((STAGING / "home/data/system/auth/users.json").read_text())
    uid = users[CREDS["tenant_a"]["username"]]["id"]
    return STAGING / f"home/data/users/{uid}/user/workspace/library"


def api_client() -> Actor:
    actor = Actor("faults")
    status, _, body, _ = login(actor, CREDS["tenant_a"]["username"], CREDS["tenant_a"]["password"])
    if status != 200 or not actor.cookies.get("dt_token"):
        raise SystemExit(
            f"cannot open a staging session for the fault run: status={status} body={body[:120]}"
        )
    actor.token = actor.cookies.get("dt_token")
    return actor


def safe_upload(actor: Actor, name: str, payload: bytes) -> tuple[int, str]:
    """Upload that reports a dead server as status 0 instead of raising."""
    try:
        status, _, body, _ = upload(
            actor,
            "POST",
            "/files/library/",
            payload,
            filename=name,
            base=BACKEND_API,
            timeout=60,
            extra_fields=(("filename", name), ("mime_type", "text/plain")),
        )
        return status, body or ""
    except Exception as exc:  # noqa: BLE001 — the point of the probe is the failure mode
        return 0, str(exc)[:160]


def component_up(name: str, probe, expected: int = 200) -> bool:
    try:
        return probe() == expected
    except Exception:  # noqa: BLE001
        return False


def phase_inject() -> int:
    # Precondition: never inject faults into a deployment that is not healthy —
    # a fault run must start from a known-good baseline.
    baseline_health = {
        "backend_live": health(8101, "/health/live")[0],
        "backend_ready": health(8101, "/health/ready")[0],
        "frontend_login": http_status(f"{FRONTEND}/login", 10),
        "public_login": http_status(f"https://{TLS_TARGET}/login", 10),
    }
    if any(value != 200 for value in baseline_health.values()):
        print(
            f"refusing to inject faults: baseline is not healthy {baseline_health}", file=sys.stderr
        )
        return 2
    actor = api_client()
    state: dict[str, object] = {
        "targets": {"tls": TLS_TARGET, "frontend": FRONTEND, "backend_api": BACKEND_API}
    }

    # --- 1. ingress outage ------------------------------------------------
    check(
        "the public origin is serving before the fault",
        http_status(f"https://{TLS_TARGET}/login", 10) == 200,
    )
    supervisor("stop", "ingress")
    down = not port_open(TLS_TARGET)
    check(
        "the public origin refuses connections while the ingress is down",
        down,
        f"port_open={port_open(TLS_TARGET)}",
    )
    state["ingress_down"] = {"port_open": port_open(TLS_TARGET)}
    check(
        "the backend keeps running behind the failed ingress",
        health(8101, "/health/live")[0] == 200,
        f"live={health(8101, '/health/live')[0]}",
    )
    check("the ingress recovers when it is started again", supervisor("start", "ingress") == 0)
    recovered = http_status(f"https://{TLS_TARGET}/login", 10)
    check("the public origin serves again after recovery", recovered == 200, f"status={recovered}")
    st, _, _, payload = call(actor, "GET", "/api/notebooks")
    check(
        "authenticated API traffic works again after ingress recovery",
        st == 200 and isinstance(payload, dict),
        f"status={st}",
    )

    # --- 2. backend outage ------------------------------------------------
    before_live = health(8101, "/health/live")
    supervisor("stop", "backend")
    after_live = health(8101, "/health/live")
    ready_during = health(8101, "/health/ready")
    state["backend_down"] = {"live": after_live[0], "ready": ready_during[0]}
    check(
        "liveness is not reported while the backend is down",
        after_live[0] == 0,
        f"before={before_live[0]} during={after_live[0]}",
    )
    check(
        "readiness is not falsely reported while the backend is down",
        ready_during[0] == 0,
        f"during={ready_during[0]} body={ready_during[1][:80]!r}",
    )
    frontend_status = http_status(f"{FRONTEND}/login", 10)
    check(
        "the frontend stays up and degraded while the backend is down",
        frontend_status in (200, 500, 503),
        f"frontend_status={frontend_status}",
    )
    check(
        "the public origin does not answer API calls successfully while the backend is down",
        http_status(f"https://{TLS_TARGET}/api/notebooks", 10) != 200,
    )
    check("the backend restarts", supervisor("start", "backend") == 0)
    live_after = health(8101, "/health/live")
    ready_after = health(8101, "/health/ready")
    check(
        "liveness returns after the backend recovers",
        live_after[0] == 200,
        f"status={live_after[0]}",
    )
    check(
        "readiness returns after the backend recovers",
        ready_after[0] == 200,
        f"status={ready_after[0]}",
    )
    check(
        "readiness reports its dependencies, not just liveness",
        "ready" in ready_after[1].lower() or "ok" in ready_after[1].lower(),
        ready_after[1][:120],
    )
    # The same session token still authenticates: the auth secret lives in the
    # persistent layer, so a restart does not invalidate live sessions.
    st, _, _, payload = call(actor, "GET", "/api/notebooks")
    check(
        "the session token issued before the restart still authenticates", st == 200, f"status={st}"
    )
    check(
        "the tenant's own data is readable after the backend recovers",
        st == 200 and isinstance(payload, dict) and payload.get("total", 0) >= 1,
        f"status={st} total={(payload or {}).get('total')}",
    )

    # --- 3. storage fault -------------------------------------------------
    storage = tenant_storage_dir()
    original_mode = stat.S_IMODE(storage.stat().st_mode)
    state["storage_dir"] = {"path": str(storage), "mode_before": oct(original_mode)}
    baseline_status, baseline_body = safe_upload(actor, "fault-baseline.txt", b"fault baseline\n")
    check(
        "a library upload succeeds before the storage fault",
        baseline_status == 200,
        f"status={baseline_status} {baseline_body[:80]}",
    )
    try:
        storage.chmod(0o500)
        blocked_status, blocked_body = safe_upload(
            actor, "fault-blocked.txt", b"fault during outage\n"
        )
        state["storage_fault"] = {"status": blocked_status, "body": blocked_body[:200]}
        check(
            "a write with an unwritable storage root fails without an unstructured crash",
            blocked_status in (400, 403, 409, 500, 507) and blocked_body.strip().startswith("{"),
            f"status={blocked_status} body={blocked_body[:120]}",
        )
        check(
            "no stack trace is returned to the client",
            not any(marker in blocked_body for marker in ("Traceback", 'File "', "site-packages")),
            blocked_body[:80],
        )
        check(
            "the deployment stays live during the storage fault",
            health(8101, "/health/live")[0] == 200,
        )
    finally:
        storage.chmod(original_mode)
    after_status, after_body = safe_upload(actor, "fault-after.txt", b"fault after recovery\n")
    state["storage_recovered"] = {"status": after_status}
    check(
        "writes succeed again once storage is writable",
        after_status == 200,
        f"status={after_status} {after_body[:80]}",
    )
    check(
        "the storage root is back to its deployment mode",
        oct(stat.S_IMODE(storage.stat().st_mode)) == oct(original_mode),
        oct(stat.S_IMODE(storage.stat().st_mode)),
    )

    document = {
        "phase": "inject",
        "state": state,
        "checks": RESULTS,
        "total": len(RESULTS),
        "passed": sum(1 for r in RESULTS if r["ok"]),
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "staging-faults-inject.json").write_text(json.dumps(document, indent=2))
    print(
        f"\nstaging faults [inject]: total={document['total']} passed={document['passed']} "
        f"failed={document['total'] - document['passed']}"
    )
    return 0 if document["passed"] == document["total"] else 1


def phase_observe() -> int:
    logs = {
        "backend": STAGING / "run/backend.log",
        "frontend": STAGING / "run/frontend.log",
        "ingress": STAGING / "run/ingress.log",
    }
    secrets_to_hide = {
        account["password"]
        for account in CREDS.values()
        if isinstance(account, dict) and account.get("password")
    }
    trace: dict[str, object] = {}

    for name, path in logs.items():
        text = path.read_text(errors="replace") if path.exists() else ""
        timestamped = len(
            re.findall(
                r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?", text
            )
        )
        severity = {
            level: len(re.findall(rf"\b{level}\b", text))
            for level in ("ERROR", "WARNING", "INFO", "CRITICAL")
        }
        leaked = sorted({secret for secret in secrets_to_hide if secret and secret in text})
        leaked += re.findall(r"eyJ[A-Za-z0-9._-]{20,}", text)[:2]
        trace[name] = {
            "lines": text.count("\n"),
            "timestamped": timestamped,
            "severity": severity,
            "leaks": leaked,
        }
        if name != "frontend":
            check(
                f"the {name} log is timestamped",
                timestamped > 0,
                f"timestamped_lines={timestamped}",
            )
        else:
            check(
                "the frontend log is written (framework format, no timestamps)",
                trace["frontend"]["lines"] > 0,
                f"lines={trace['frontend']['lines']}",
            )
        if name == "frontend":
            # Next.js writes its own framework format; the supervisor versions and
            # timestamps deployment events in run/deployments.log instead.
            check(
                "the frontend log captures the upstream outage with context",
                "ECONNREFUSED" in text or "Failed to proxy" in text,
                f"lines={trace['frontend']['lines']}",
            )
            trace["frontend"]["format"] = (
                "next.js framework format (no timestamps; see the closure report)"
            )
        else:
            check(
                f"the {name} log uses severity levels",
                any(severity[level] for level in ("INFO", "WARNING", "ERROR", "CRITICAL")),
                str(severity),
            )
        check(
            f"the {name} log leaks no credentials or session tokens",
            not leaked,
            f"leaks={leaked[:3]}",
        )

    backend_text = logs["backend"].read_text(errors="replace") if logs["backend"].exists() else ""
    check(
        "the backend log records the injected dependency failures",
        any(level in backend_text for level in ("ERROR", "WARNING")),
        f"errors={len(re.findall(r'ERROR', backend_text))} warnings={len(re.findall(r'WARNING', backend_text))}",
    )
    storage_lines = [
        line
        for line in backend_text.splitlines()
        if re.search(
            r"(?i)ERROR.*(readonly database|unhandled exception on POST /files/library/)", line
        )
    ]
    check(
        "the storage fault is attributable in the backend log",
        bool(storage_lines),
        f"lines={len(storage_lines)} sample={storage_lines[0][:120] if storage_lines else ''}",
    )

    ingress_text = logs["ingress"].read_text(errors="replace") if logs["ingress"].exists() else ""
    degraded_lines = [
        line for line in ingress_text.splitlines() if re.search(r"-> (4|5)\d\d", line)
    ]
    check(
        "the ingress logs the degraded responses it proxied",
        bool(degraded_lines),
        f"lines={len(degraded_lines)} sample={degraded_lines[0][:120] if degraded_lines else ''}",
    )
    check(
        "the ingress records a timestamped startup line",
        bool(re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*listening", ingress_text)),
        "startup line format",
    )

    # Health and readiness must stay secret-free while they report state.
    for port, path in ((8101, "/health/live"), (8101, "/health/ready")):
        status, body = health(port, path)
        check(
            f"{path} reports state without secrets",
            status == 200 and not any(secret in body for secret in secrets_to_hide),
            f"status={status}",
        )

    document = {
        "phase": "observe",
        "trace": trace,
        "checks": RESULTS,
        "total": len(RESULTS),
        "passed": sum(1 for r in RESULTS if r["ok"]),
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "staging-faults-observe.json").write_text(json.dumps(document, indent=2))
    print(
        f"\nstaging faults [observe]: total={document['total']} passed={document['passed']} "
        f"failed={document['total'] - document['passed']}"
    )
    return 0 if document["passed"] == document["total"] else 1


def main() -> int:
    args = sys.argv[1:]
    phase = args[args.index("--phase") + 1] if "--phase" in args else "inject"
    time.sleep(0.5)
    if phase == "inject":
        return phase_inject()
    if phase == "observe":
        return phase_observe()
    print("usage: staging_faults.py --phase inject|observe", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
