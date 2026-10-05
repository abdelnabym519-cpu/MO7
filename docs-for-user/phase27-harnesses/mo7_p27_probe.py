#!/usr/bin/env python3
"""Focused Phase 27 probes: raw Set-Cookie headers, CORS preflight, workspace
PATCH authorization, deleted-user token validity, login latency."""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path("/home/user/MO7")
PY = REPO / ".venv/bin/python"
HOME = Path(tempfile.mkdtemp(prefix="mo7-p27-probe-"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


PORT = free_port()
HOST = "127.0.0.1"


def raw(method: str, path: str, *, body=None, headers=None) -> tuple[int, list, bytes]:
    conn = http.client.HTTPConnection(HOST, PORT, timeout=60)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(data))
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    payload = resp.read()
    status, resp_headers = resp.status, resp.getheaders()
    conn.close()
    return status, resp_headers, payload


settings = HOME / "data/user/settings"
settings.mkdir(parents=True, exist_ok=True)
(settings / "auth.json").write_text(json.dumps({
    "version": 1, "enabled": True, "username": "admin", "password_hash": "",
    "token_expire_hours": 12, "cookie_secure": False, "private_login_hosts": [],
}), encoding="utf-8")
log = (Path("/tmp") / "mo7-p27-probe.log").open("ab")
proc = subprocess.Popen(
    [str(PY), "-m", "uvicorn", "deeptutor.api.main:app", "--host", HOST, "--port", str(PORT)],
    cwd=str(REPO), env=dict(os.environ, DEEPTUTOR_HOME=str(HOME), PYTHONPATH=str(REPO),
                            PYTHONUNBUFFERED="1"),
    stdout=log, stderr=subprocess.STDOUT,
)
for _ in range(150):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health/live", timeout=2) as r:
            if r.status == 200:
                break
    except Exception:
        time.sleep(0.4)
PROBE_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    PROBE_RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name} ({detail})", flush=True)


print(f"home={HOME} port={PORT}\n")

# ---------------------------------------------------------------- 1. cookies
raw("POST", "/api/auth/register", body={"username": "p27admin", "password": "P27-Admin-Passw0rd!"})
t0 = time.time()
status, headers, payload = raw("POST", "/api/auth/login",
                               body={"username": "p27admin", "password": "P27-Admin-Passw0rd!"})
print("== 1. login response headers ==")
print(f"  status={status} elapsed={time.time() - t0:.3f}s")
for name, value in headers:
    if name.lower() in ("set-cookie", "cache-control", "pragma"):
        print(f"  {name}: {value[:200]}")
token = None
for name, value in headers:
    if name.lower() == "set-cookie" and "dt_token" in value:
        token = value.split("dt_token=", 1)[1].split(";", 1)[0]
auth = {"Cookie": f"dt_token={token}"}

status, headers, payload = raw("POST", "/api/auth/logout", headers=auth)
print("== logout ==")
print(f"  status={status}")
for name, value in headers:
    if name.lower() == "set-cookie":
        print(f"  {name}: {value[:200]}")

# ------------------------------------------------------------------ 2. CORS
print("\n== 2. CORS preflight ==")
for origin in ("http://localhost:3000", "https://evil.example"):
    status, headers, payload = raw("OPTIONS", "/api/sessions", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "content-type",
    })
    acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
    acac = [v for k, v in headers if k.lower() == "access-control-allow-credentials"]
    print(f"  origin={origin} status={status} ACAO={acao} ACAC={acac}")
status, headers, payload = raw("GET", "/api/settings", headers={**auth, "Origin": "https://evil.example"})
acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
print(f"  simple GET with evil origin: status={status} ACAO={acao}")

# --------------------------------------------------- 3. workspace PATCH authz
print("\n== 3. workspace PATCH ==")
status, _, payload = raw("POST", "/api/auth/users", headers=auth,
                         body={"username": "p27user2", "password": "P27-User2-Passw0rd!"})
print(f"  create user2: {status}")
status, headers, payload = raw("POST", "/api/auth/login",
                               body={"username": "p27user2", "password": "P27-User2-Passw0rd!"})
u2_token = None
for name, value in headers:
    if name.lower() == "set-cookie" and "dt_token" in value:
        u2_token = value.split("dt_token=", 1)[1].split(";", 1)[0]
u2_auth = {"Cookie": f"dt_token={u2_token}"}

status, _, payload = raw("POST", "/api/reading/workspaces", headers=auth,
                         body={"title": "probe collection", "material_ids": []})
ws_id = json.loads(payload)["workspace"]["workspace_id"]
print(f"  admin created workspace {ws_id}")
status, _, payload = raw("PATCH", f"/api/reading/workspaces/{ws_id}", headers=auth,
                         body={"title": "renamed by owner"})
print(f"  owner PATCH: status={status} body={payload[:120]}")
status, _, payload = raw("PATCH", f"/api/reading/workspaces/{ws_id}", headers=u2_auth,
                         body={"title": "stolen"})
print(f"  cross-tenant PATCH: status={status} body={payload[:160]}")
status, _, payload = raw("PATCH", f"/api/reading/workspaces/{ws_id}", headers=u2_auth, body={})
print(f"  cross-tenant empty PATCH: status={status} body={payload[:160]}")
status, _, payload = raw("GET", f"/api/reading/workspaces/{ws_id}", headers=auth)
print(f"  owner GET after: {status} title={json.loads(payload).get('workspace', {}).get('title')!r}")

# --------------------------------------------- 4. deleted-user token validity
print("\n== 4. deleted account token ==")
status, _, payload = raw("POST", "/api/auth/users", headers=auth,
                         body={"username": "p27third", "password": "P27-Third-Passw0rd!"})
print(f"  create third: {status} {payload[:120]}")
status, headers, payload = raw("POST", "/api/auth/login",
                               body={"username": "p27third", "password": "P27-Third-Passw0rd!"})
third = None
for name, value in headers:
    if name.lower() == "set-cookie" and "dt_token" in value:
        third = value.split("dt_token=", 1)[1].split(";", 1)[0]
status, _, payload = raw("GET", "/api/settings", headers={"Cookie": f"dt_token={third}"})
before = status
print(f"  third token before deletion: {status}")
status, _, payload = raw("DELETE", "/api/auth/users/p27third", headers=auth)
deleted = status
print(f"  delete third: {status} {payload[:120]}")
status, _, payload = raw("GET", "/api/settings", headers={"Cookie": f"dt_token={third}"})
after = status
print(f"  third token AFTER deletion: {status} {payload[:160]}")
status, _, payload = raw("GET", "/api/multi-user/users", headers={"Cookie": f"dt_token={third}"})
admin_after = status
print(f"  third token on admin route after deletion: {status}")
check("deleted account token is revoked",
      before == 200 and deleted == 200 and after in (401, 403) and admin_after in (401, 403),
      f"before={before} delete={deleted} after={after} admin_route={admin_after}")

# ------------------------------------------------------------------ 4b. demotion
print("\n== 4b. demoted admin token ==")
status, _, payload = raw("POST", "/api/auth/users", headers=auth,
                         body={"username": "p27demoted", "password": "P27-Demoted-Passw0rd!"})
status, _, payload = raw("PUT", "/api/auth/users/p27demoted/role", headers=auth,
                         body={"role": "admin"})
print(f"  promote: {status}")
status, headers, payload = raw("POST", "/api/auth/login",
                               body={"username": "p27demoted", "password": "P27-Demoted-Passw0rd!"})
demoted_token = None
for name, value in headers:
    if name.lower() == "set-cookie" and "dt_token" in value:
        demoted_token = value.split("dt_token=", 1)[1].split(";", 1)[0]
status, _, _ = raw("GET", "/api/multi-user/users", headers={"Cookie": f"dt_token={demoted_token}"})
as_admin = status
status, _, payload = raw("PUT", "/api/auth/users/p27demoted/role", headers=auth,
                         body={"role": "user"})
print(f"  demote: {status}")
status, _, _ = raw("GET", "/api/multi-user/users", headers={"Cookie": f"dt_token={demoted_token}"})
after_demote = status
status, _, _ = raw("GET", "/api/settings", headers={"Cookie": f"dt_token={demoted_token}"})
own_settings = status
print(f"  token as admin={as_admin} demoted={after_demote} own settings={own_settings}")
check("demoted admin token loses the admin route",
      as_admin == 200 and after_demote in (401, 403) and own_settings == 200,
      f"as_admin={as_admin} demoted={after_demote} own_settings={own_settings}")

# ------------------------------------------------------- 5. login attempt cost
print("\n== 5. failed-login cost ==")
times = []
for _ in range(5):
    t0 = time.time()
    status, _, _ = raw("POST", "/api/auth/login",
                       body={"username": "p27admin", "password": "wrong-password"})
    times.append((status, round((time.time() - t0) * 1000)))
print(f"  5 failed logins (status, ms): {times}")

proc.terminate()
try:
    proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()

failed = [r for r in PROBE_RESULTS if not r[1]]
print(f"\ntotal={len(PROBE_RESULTS)} passed={len(PROBE_RESULTS) - len(failed)} failed={len(failed)}")
for name, _, detail in failed:
    print(f"  - {name} ({detail})")
print(f"log: /tmp/mo7-p27-probe.log  home: {HOME}")
sys.exit(1 if failed else 0)
