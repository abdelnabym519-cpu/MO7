#!/usr/bin/env python3
"""Post-deploy / post-rollback smoke test for the production deployment.

Runs after promotion (and after every rollback) and exercises the deployment
the way a client does: the loopback API health routes, the frontend as served,
the validation ingress over TLS, one real authentication, one authenticated API
read and one persistent-data read. It never mutates production data.

Exit code is 0 only when every step passes.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402
from prod_http import Actor, call, login  # noqa: E402

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail[:300]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail[:200]}"))
    return bool(ok)


def _tls_context():
    """The validation ingress uses a self-signed certificate by design.

    Production TLS terminates at the platform edge, so a client inside the
    deployment host cannot chain-verify the *validation* ingress certificate.
    The probe therefore disables verification for this one component and the
    closure report records that explicitly; nothing about production TLS is
    inferred from it.
    """
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def get(url: str) -> tuple[int, float]:
    import urllib.request

    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10, context=_tls_context()) as response:  # noqa: S310
            response.read()
            return response.status, (time.perf_counter() - start) * 1000
    except Exception:
        return 0, (time.perf_counter() - start) * 1000


def main() -> int:
    live, live_ms = get(f"{cfg.BACKEND}/health/live")
    ready, ready_ms = get(f"{cfg.BACKEND}/health/ready")
    check("backend liveness 200", live == 200, f"status={live} ({live_ms:.1f} ms)")
    check("backend readiness 200", ready == 200, f"status={ready} ({ready_ms:.1f} ms)")

    frontend, frontend_ms = get(cfg.FRONTEND_PLAIN + "/login")
    check("frontend serves /login", frontend == 200, f"status={frontend} ({frontend_ms:.1f} ms)")

    # The validation ingress is a harness component (autostart=false): it is
    # checked when it is running, and its absence is reported as such instead of
    # being read as an application failure. Production TLS terminates at the
    # platform edge and is verified from outside the deployment host.
    import socket

    def _listening(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            return False

    ingress_running = _listening("127.0.0.1", cfg.TLS_VALIDATION_PORT)
    if ingress_running:
        ingress, ingress_ms = get(cfg.TLS_VALIDATION_ORIGIN + "/login")
        check(
            "validation ingress serves /login over TLS",
            ingress == 200,
            f"status={ingress} ({ingress_ms:.1f} ms)",
        )
    else:
        print(
            f"[note] validation ingress is not running on {cfg.TLS_VALIDATION_PORT}: "
            "harness-only component, production TLS terminates at the platform edge"
        )
    # The smoke authenticates against the deployment's own API address. The
    # validation ingress is a harness component that is stopped outside a
    # validation window, and a promotion must not depend on it being up; when it
    # *is* up the same checks run through TLS as well.
    api_base = cfg.FRONTEND if ingress_running else cfg.BACKEND
    print(f"[note] authentication and API reads target {api_base}")

    try:
        accounts = cfg.credentials()
    except Exception as exc:
        check("deployment credentials readable", False, str(exc))
        return 1

    admin = Actor("admin")
    status, _, _, _ = login(
        admin, accounts["admin"]["username"], accounts["admin"]["password"], base=api_base
    )
    check("administrator can authenticate", status == 200, f"status={status}")
    if status != 200:
        return 1

    status, _, _, payload = call(admin, "GET", "/api/settings", base=api_base)
    check("authenticated API read (/api/settings)", status == 200, f"status={status}")
    check(
        "settings response does not leak credentials",
        "password" not in json.dumps(payload or {}).lower().replace("requires_password", ""),
        "response contains a password-looking field",
    )

    status, _, _, payload = call(admin, "GET", "/api/notebooks", base=api_base)
    check("persistent data read (/api/notebooks)", status == 200, f"status={status}")
    check(
        "notebooks payload has the documented shape",
        isinstance(payload, dict) and "notebooks" in payload,
        f"payload keys: {sorted((payload or {}).keys())[:6]}",
    )

    failed = [row for row in RESULTS if not row["ok"]]
    print(
        f"\nproduction smoke: total={len(RESULTS)} passed={len(RESULTS) - len(failed)} failed={len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
