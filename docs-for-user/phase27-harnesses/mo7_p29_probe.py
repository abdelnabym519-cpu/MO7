#!/usr/bin/env python3
"""MO7 Phase 29 security probe (re-created for the Phase 27 closure run).

Phase 29 validated the auth boundary with a 14-check probe against a live
auth-enabled server; the script itself did not survive the sandbox re-clone, so
this reproduces the exact list recorded in
``docs-for-user/PHASE_29_QA_CLOSURE_REPORT.md`` §6 (row "Security probe"),
one check per documented assertion.

Run: ``python3 mo7_p29_probe.py`` — exits non-zero if any check fails.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = Path("/home/user/MO7")
PY = REPO / ".venv/bin/python"
sys.path.insert(0, str(REPO / ".venv/lib/python3.11/site-packages"))

HOME = Path(tempfile.mkdtemp(prefix="mo7-p29-probe-"))
ADMIN = ("p29admin", "P29-Admin-Passw0rd!")
USER2 = ("p29user2", "P29-User2-Passw0rd!")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL':<5} {name:<64} {detail}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


BASE = ""


def call(op, method: str, path: str, *, body=None, token=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with op.open(req, timeout=60) as response:
            payload = response.read()
            try:
                return response.status, json.loads(payload)
            except Exception:
                return response.status, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except Exception:
            return exc.code, payload[:200]
    except Exception as exc:  # transport failure — never a pass
        return 0, str(exc)


def login(op, username: str, password: str) -> tuple[int, dict]:
    return call(op, "POST", "/api/auth/login", body={"username": username, "password": password})


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def mint(payload: dict, secret: str) -> str:
    from jose import jwt

    return jwt.encode(payload, secret, algorithm="HS256")


def main() -> int:
    global BASE
    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    # Auth is enabled by the settings file (the same switch the P27 harness
    # uses), not by an env var — a fresh home otherwise boots in single-user
    # mode and every route answers 200, which is how the first run of this
    # probe produced six false FAILs.
    settings = HOME / "data/user/settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "enabled": True,
                "username": "admin",
                "password_hash": "",
                "token_expire_hours": 12,
                "cookie_secure": False,
                "private_login_hosts": [],
            }
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "DEEPTUTOR_HOME": str(HOME), "PYTHONPATH": str(REPO)}
    log = open("/tmp/mo7-p29-probe-server.log", "wb")  # noqa: SIM115 - closed below
    proc = subprocess.Popen(
        [
            str(PY),
            "-m",
            "uvicorn",
            "deeptutor.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(REPO),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{BASE}/health/live", timeout=2):
                    break
            except Exception:
                time.sleep(0.5)
        print(f"home={HOME} port={port}\n", flush=True)

        # 1. anonymous is refused on protected routes
        protected = [
            "/api/settings",
            "/api/sessions",
            "/api/reading/materials",
            "/api/multi-user/users",
            "/files/library/",
        ]
        anon = opener()
        anon_codes = {p: call(anon, "GET", p)[0] for p in protected}
        check(
            "1. anonymous 401 on 5 protected routes",
            all(v == 401 for v in anon_codes.values()),
            f"{anon_codes}",
        )

        now = int(time.time())
        # 2-7. token shape attacks
        forged = {
            "2. HS256 token forged with a foreign key": mint(
                {"sub": "x", "role": "admin", "uid": "u_x", "exp": now + 600}, "not-the-secret"
            ),
            "3. empty token": "",
            "4. header-only token": "eyJhbGciOiJIUzI1NiJ9",
            "5. alg=none token": f"{b64(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}."
            f"{b64(json.dumps({'sub': 'x', 'role': 'admin', 'exp': now + 600}).encode())}.",
            "6. malformed token": "not-a-jwt",
            "7. expired-shaped token": None,  # real secret unknown before bootstrap
        }
        for label, token in forged.items():
            if token is None:
                continue
            status, _ = call(anon, "GET", "/api/multi-user/users", token=token)
            check(label, status in (401, 403), f"status={status}")

        # 8. bootstrap registration creates the admin
        status, created = call(
            anon, "POST", "/api/auth/register", body={"username": ADMIN[0], "password": ADMIN[1]}
        )
        check("8. bootstrap register 201", status == 201, f"status={status}")

        secret = (HOME / "data/system/auth/auth_secret").read_text(encoding="utf-8").strip()
        status, _ = call(
            anon,
            "GET",
            "/api/multi-user/users",
            token=mint(
                {"sub": ADMIN[0], "role": "admin", "uid": "u_admin", "exp": now - 60}, secret
            ),
        )
        check("7. expired-shaped token (real key) 401", status in (401, 403), f"status={status}")

        # 9. admin login succeeds, role=admin
        admin = opener()
        status, body = login(admin, *ADMIN)
        check(
            "9. admin login 200 with role=admin",
            status == 200 and (body or {}).get("role") == "admin",
            f"status={status} role={(body or {}).get('role')}",
        )

        # 10. admin can list users and provision the second account
        status_list, _ = call(admin, "GET", "/api/multi-user/users")
        status_create, created2 = call(
            admin, "POST", "/api/auth/users", body={"username": USER2[0], "password": USER2[1]}
        )
        check(
            "10. admin user list 200 and provisioning 201",
            status_list == 200 and status_create == 201,
            f"list={status_list} create={status_create}",
        )

        # 11. self-registration is closed once the first admin exists
        status, _ = call(
            anon,
            "POST",
            "/api/auth/register",
            body={"username": "p29third", "password": "P29-Third-Passw0rd!"},
        )
        check(
            "11. self-registration closed after bootstrap (403)", status == 403, f"status={status}"
        )

        # 12-13. the second account is a plain user: 403 on admin routes
        user2 = opener()
        status, body = login(user2, *USER2)
        status_admin, admin_body = call(user2, "GET", "/api/multi-user/users")
        check(
            "12. second user role=user, 403 on admin route",
            (body or {}).get("role") == "user" and status_admin == 403,
            f"role={(body or {}).get('role')} admin_route={status_admin}",
        )

        # 13-14. tenant isolation + own settings
        user2_id = (created2 or {}).get("user_id")
        status_sessions, sessions = call(user2, "GET", "/api/sessions")
        status_settings, _ = call(user2, "GET", "/api/settings")
        # ``GET /api/sessions`` answers ``{"sessions": [...]}`` for a plain user.
        rows = sessions.get("sessions") if isinstance(sessions, dict) else sessions
        isolated = isinstance(rows, list) and len(rows) == 0
        check(
            "13. second user sees an empty own session list (tenant isolation)",
            status_sessions == 200 and isolated,
            f"status={status_sessions} rows={len(rows) if isinstance(rows, list) else rows!r}",
        )
        check(
            "14. second user reads its own settings 200",
            status_settings == 200,
            f"status={status_settings}",
        )

        # 15. wrong password is refused
        status, _ = login(opener(), ADMIN[0], "wrong-password")
        check("15. wrong password 401", status == 401, f"status={status}")
        print(f"\nuser2_id={user2_id}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\ntotal={len(RESULTS)} passed={len(RESULTS) - len(failed)} failed={len(failed)}")
    for name, _, detail in failed:
        print(f"  - {name} ({detail})")
    print(f"results: {HOME}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
