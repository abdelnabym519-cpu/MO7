#!/usr/bin/env python3
"""MO7 Phase 27 — live security attack harness.

Runs against a real uvicorn process with auth enabled, real per-user data
roots, real SQLite stores. Every check prints PASS/FAIL with evidence and the
process exits non-zero when a security expectation is violated.

Sections: authentication, RBAC, object-level authorization, tenant isolation,
storage/path traversal, archive containment, information disclosure, CORS and
response headers, resource-exhaustion boundaries.
"""

from __future__ import annotations

import base64
import http.client
import http.cookiejar
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

REPO = Path("/home/user/MO7")
PY = REPO / ".venv/bin/python"
# The harness talks HTTP but mints its own tokens, so it needs jose from the
# project venv rather than whatever interpreter `python3` resolves to.
sys.path.insert(0, str(REPO / ".venv/lib/python3.11/site-packages"))
HOME = Path(tempfile.mkdtemp(prefix="mo7-p27-attack-"))
ADMIN = ("p27admin", "P27-Admin-Passw0rd!")
USER2 = ("p27user2", "P27-User2-Passw0rd!")
RESULTS: list[tuple[str, bool, str]] = []
BASE = ""
FRONTEND_ORIGIN = "http://localhost:3000"
#: Mirrors deeptutor.services.auth.LOGIN_ATTEMPT_LIMIT[0]; a drift here
#: shows up as a failing check rather than a silent mismatch.
auth_service_limit = 10


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL':<5} {name:<66} {detail}", flush=True)
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


JARS: list[http.cookiejar.CookieJar] = []


def opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    JARS.append(jar)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(
    op, method, path, *, body=None, raw=None, ctype=None, token=None, headers=None, timeout=90
):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", ctype or "application/json")
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with op.open(req, timeout=timeout) as r:
            payload = r.read()
            meta = {"status": r.status, "headers": dict(r.headers)}
            try:
                return meta, json.loads(payload)
            except Exception:
                return meta, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        meta = {"status": exc.code, "headers": dict(exc.headers)}
        try:
            return meta, json.loads(payload)
        except Exception:
            return meta, payload[:400]
    except Exception as exc:
        return {"status": 0, "headers": {}}, str(exc)


def code(meta) -> int:
    return int(meta.get("status", 0))


def raw_headers(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    ctype: str = "application/json",
    headers: dict | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, list[str]], bytes]:
    """Send one request over http.client so repeated headers survive.

    ``urllib`` folds every ``Set-Cookie`` into one comma-joined string, which
    cannot distinguish an attribute from a second cookie; the checks that read
    cookies and CORS preflights need the wire-level list.
    """
    parsed = urllib.parse.urlsplit(BASE + path)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", ctype)
    try:
        conn.request(method, parsed.path or "/", body=body, headers=hdrs)
        response = conn.getresponse()
        payload = response.read()
        collected: dict[str, list[str]] = {}
        for name, value in response.getheaders():
            collected.setdefault(name.lower(), []).append(value)
        return response.status, collected, payload
    finally:
        conn.close()


def _join(collected: dict[str, list[str]], name: str) -> str:
    return "; ".join(collected.get(name.lower(), []))


def _workspace_fields(payload) -> dict | None:
    """The mutable fields of a workspace response, for change detection."""
    if not isinstance(payload, dict):
        return None
    row = payload.get("workspace")
    if not isinstance(row, dict):
        return None
    return {key: row.get(key) for key in ("title", "description", "color")}


def jwt_segment(payload: dict) -> str:
    def b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return f"{b64(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}.{b64(json.dumps(payload).encode())}."


def mint(payload: dict, secret: str, algorithm: str = "HS256") -> str:
    from jose import jwt

    return jwt.encode(payload, secret, algorithm=algorithm)


def upload_multipart(
    op,
    method,
    path,
    parts,
    token=None,
    field="file",
    filename="x.md",
    extra_fields=(),
    timeout=120,
    include_file=True,
):
    boundary = "----p27" + uuid.uuid4().hex
    chunks = []
    for name, value in extra_fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    if include_file:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        chunks.append(parts)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return call(
        op,
        method,
        path,
        raw=b"".join(chunks),
        ctype=f"multipart/form-data; boundary={boundary}",
        token=token,
        timeout=timeout,
    )


def login(op, username: str, password: str):
    meta, body = call(
        op, "POST", "/api/auth/login", body={"username": username, "password": password}
    )
    return code(meta), body


def upload_reading(op, name: str, content: bytes):
    return upload_multipart(
        op, "POST", "/api/reading/materials", content, filename=name, field="file"
    )


def upload_library(op, name: str, content: bytes, mime="text/plain"):
    return upload_multipart(
        op,
        "POST",
        "/files/library/",
        content,
        filename=name,
        field="file",
        extra_fields=(("filename", name), ("mime_type", mime)),
    )


# --------------------------------------------------------------------------- setup
def start_server(port: int) -> subprocess.Popen:
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
    log = (Path("/tmp") / "mo7-p27-attack.log").open("ab")
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
        ],
        cwd=str(REPO),
        env=dict(os.environ, DEEPTUTOR_HOME=str(HOME), PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1"),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    for _ in range(200):
        try:
            with urllib.request.urlopen(f"{BASE}/health/live", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.4)
    raise SystemExit("server did not start")


def main() -> int:
    global BASE
    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    proc = start_server(port)
    print(f"home={HOME} port={port}\n", flush=True)

    admin, user2, anon = opener(), opener(), opener()
    admin_jar, user2_jar, anon_jar = JARS[-3], JARS[-2], JARS[-1]

    # ---------------------------------------------------------------- bootstrap
    meta, body = call(
        anon, "POST", "/api/auth/register", body={"username": ADMIN[0], "password": ADMIN[1]}
    )
    check(
        "bootstrap: first registration creates the admin", code(meta) == 201, f"status={code(meta)}"
    )
    meta, body = call(
        anon,
        "POST",
        "/api/auth/register",
        body={"username": "p27third", "password": "P27-Third-Passw0rd!"},
    )
    check(
        "bootstrap: self-registration closes after the first admin",
        code(meta) == 403,
        f"status={code(meta)}",
    )
    st, _ = login(admin, *ADMIN)
    check("admin login succeeds", st == 200, f"status={st}")
    meta, created = call(
        admin, "POST", "/api/auth/users", body={"username": USER2[0], "password": USER2[1]}
    )
    user2_id = (created or {}).get("user_id") if isinstance(created, dict) else None
    check("admin provisions a second account", code(meta) == 201, f"status={code(meta)}")
    st, _ = login(user2, *USER2)
    check("user2 login succeeds", st == 200, f"status={st}")

    secret = (HOME / "data/system/auth/auth_secret").read_text(encoding="utf-8").strip()
    check(
        "auth secret is generated on disk (test can mint real tokens)",
        len(secret) == 64,
        f"len={len(secret)}",
    )

    # -------------------------------------------------- authentication attacks
    protected = [
        "/api/sessions",
        "/api/settings",
        "/api/knowledge-bases",
        "/api/notebooks",
        "/api/multi-user/users",
        "/api/reading/materials",
        "/api/reading/library/materials",
        "/files/library/",
        "/api/mastery-paths/topics",
        "/api/dashboard/suggestions",
        "/api/memory/overview",
        "/api/courses",
        "/api/question-notebook/entries",
        "/api/partner-groups",
        "/files/outputs/x.txt",
        "/files/workspace-items/a/b",
        "/files/attachments/s/a/f.txt",
    ]
    codes = {}
    for path in protected:
        meta, _ = call(anon, "GET", path)
        codes[path] = code(meta)
    bad = {p: c for p, c in codes.items() if c not in (401, 403)}
    check(
        "anonymous access refused on every protected route",
        not bad,
        f"refused {len(codes) - len(bad)}/{len(codes)}; unexpected={bad}",
    )

    now = int(time.time())
    forged = {
        "garbage": "not-a-jwt",
        "empty": "",
        "header_only": "eyJhbGciOiJIUzI1NiJ9",
        "wrong_key": mint(
            {"sub": USER2[0], "role": "admin", "uid": user2_id, "exp": now + 3600},
            "attacker-key-not-the-real-one",
        ),
        "alg_none": jwt_segment(
            {"sub": USER2[0], "role": "admin", "uid": user2_id, "exp": now + 3600}
        ),
        "expired_real_key": mint(
            {"sub": USER2[0], "role": "admin", "uid": user2_id, "exp": now - 60, "iat": now - 7200},
            secret,
        ),
        # Real key, admin claims, no expiry: without ``require_exp`` this token
        # would never age out, so it has to be refused like any other forgery.
        "no_exp": mint({"sub": USER2[0], "role": "admin", "uid": user2_id}, secret),
    }
    token_results = {}
    for label, token in forged.items():
        meta, _ = call(anon, "GET", "/api/multi-user/users", token=token)
        token_results[label] = code(meta)
    bad_tokens = {k: v for k, v in token_results.items() if v not in (401, 403)}
    check(
        "forged / malformed / expired / unexpiring tokens are rejected",
        not bad_tokens,
        f"results={token_results}",
    )

    # A real user token with its payload tampered (signature no longer matches).
    meta, _ = call(user2, "GET", "/api/auth/status")
    real_token = next((c.value for c in user2_jar if c.name == "dt_token"), None)
    if real_token:
        head, payload_seg, sig = real_token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(payload_seg + "==="))
        decoded["role"] = "admin"
        tampered = ".".join(
            [head, base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("="), sig]
        )
        meta, _ = call(anon, "GET", "/api/multi-user/users", token=tampered)
        check(
            "privilege-claim tampering invalidates the token",
            code(meta) in (401, 403),
            f"status={code(meta)}",
        )
    else:
        check("privilege-claim tampering invalidates the token", False, "no cookie token found")

    st, _ = login(anon, ADMIN[0], "wrong-password")
    check("wrong password rejected", st == 401, f"status={st}")
    st, _ = login(anon, "p27ghost", "some-password")
    check("unknown user rejected", st == 401, f"status={st}")
    meta, _ = call(anon, "POST", "/api/auth/login", raw=b"{not json", ctype="application/json")
    check("malformed login body rejected", code(meta) in (400, 422), f"status={code(meta)}")
    meta, _ = call(anon, "POST", "/api/auth/login", body={"username": ADMIN[0]})
    check("missing credential fields rejected", code(meta) == 422, f"status={code(meta)}")

    # cookie hardening (wire-level headers — urllib folds Set-Cookie)
    status, hdrs, _ = raw_headers(
        "POST",
        "/api/auth/login",
        body=json.dumps({"username": USER2[0], "password": USER2[1]}).encode(),
    )
    set_cookie = _join(hdrs, "set-cookie")
    check(
        "login sets a session cookie",
        status == 200 and "dt_token=" in set_cookie,
        f"status={status} cookie={set_cookie[:90]}",
    )
    check("session cookie is HttpOnly", "httponly" in set_cookie.lower(), set_cookie[:110])
    check("session cookie declares SameSite", "samesite" in set_cookie.lower(), set_cookie[:110])
    check(
        "session cookie is scoped to the site root",
        "path=/" in set_cookie.lower(),
        set_cookie[:110],
    )
    check(
        "login response is not cacheable",
        _join(hdrs, "cache-control").startswith("no-store"),
        _join(hdrs, "cache-control"),
    )
    status, hdrs, _ = raw_headers(
        "POST",
        "/api/auth/logout",
        headers={"Cookie": set_cookie.split(";")[0], "Origin": "http://localhost:3000"},
    )
    logout_cookie = _join(hdrs, "set-cookie")
    check(
        "logout clears the session cookie",
        status in (200, 204)
        and ("max-age=0" in logout_cookie.lower() or "expires=" in logout_cookie.lower()),
        f"status={status} cookie={logout_cookie[:110]}",
    )

    # ------------------------------------------------------------ RBAC matrix
    admin_only = [
        ("GET", "/api/multi-user/users"),
        ("POST", "/api/auth/users"),
        ("GET", "/api/multi-user/audit-log"),
    ]
    rbac = {}
    for method, path in admin_only:
        meta, _ = call(
            user2,
            method,
            path,
            body={"username": "x", "password": "y"} if method == "POST" else None,
        )
        rbac[f"{method} {path}"] = code(meta)
    bad_rbac = {k: v for k, v in rbac.items() if v not in (401, 403, 404)}
    check("non-admin refused admin-only routes", not bad_rbac, f"results={rbac}")

    # role claim from a *valid* admin-signed token is honoured only when signed:
    # user2's own signed token must never satisfy the admin gate (checked above).
    meta, _ = call(user2, "GET", "/api/settings")
    check(
        "regular user keeps access to its own settings", code(meta) == 200, f"status={code(meta)}"
    )

    # --------------------------------------------- object creation for isolation
    meta, admin_mat = upload_reading(
        admin, "p27-admin-secret.md", b"# Admin doc\n\nP27-ADMIN-ONLY-MARKER\n"
    )
    admin_mat_id = (admin_mat or {}).get("material_id") if isinstance(admin_mat, dict) else None
    check(
        "admin can create a reading material",
        code(meta) == 200 and bool(admin_mat_id),
        f"status={code(meta)}",
    )
    meta, ws = call(
        admin,
        "POST",
        "/api/reading/workspaces",
        body={"title": "P27 admin collection", "material_ids": [admin_mat_id]},
    )
    admin_ws_id = (
        (ws or {}).get("workspace", {}).get("workspace_id") if isinstance(ws, dict) else None
    )
    meta, sess = call(
        admin,
        "POST",
        f"/api/reading/workspaces/{admin_ws_id}/sessions",
        body={"title": "P27 admin session"},
    )
    admin_session_id = (
        (sess or {}).get("session", {}).get("session_id") if isinstance(sess, dict) else None
    )
    check(
        "admin reading workspace + session exist",
        bool(admin_ws_id and admin_session_id),
        f"ws={admin_ws_id} session={admin_session_id}",
    )

    meta, admin_file = upload_library(admin, "p27-admin-file.txt", b"P27-ADMIN-FILE-BYTES")
    admin_file_id = (admin_file or {}).get("id") if isinstance(admin_file, dict) else None
    check(
        "admin can upload a library file",
        code(meta) in (200, 201) and bool(admin_file_id),
        f"status={code(meta)}",
    )

    meta, admin_notebook = call(
        admin, "POST", "/api/notebooks", body={"name": "P27 admin notebook"}
    )
    admin_notebook_id = None
    if isinstance(admin_notebook, dict):
        notebook = admin_notebook.get("notebook") or {}
        admin_notebook_id = (
            notebook.get("notebook_id") or notebook.get("id") or admin_notebook.get("notebook_id")
        )
    check("admin can create a notebook", code(meta) in (200, 201), f"status={code(meta)}")

    meta, admin_course = call(admin, "POST", "/api/courses", body={"name": "P27 admin course"})
    admin_course_id = (
        (admin_course or {}).get("course_id") if isinstance(admin_course, dict) else None
    )
    if admin_course_id is None and isinstance(admin_course, dict):
        course = admin_course.get("course") or {}
        admin_course_id = course.get("course_id") or course.get("id")
    check("admin can create a course", code(meta) in (200, 201), f"status={code(meta)}")

    # ``create_knowledge_base`` takes Form fields, not a JSON body.
    meta, admin_kb = upload_multipart(
        admin,
        "POST",
        "/api/knowledge-bases",
        b"",
        extra_fields=(("name", "p27-admin-kb"),),
        include_file=False,
        timeout=180,
    )
    admin_kb_id = None
    if isinstance(admin_kb, dict):
        admin_kb_id = (
            admin_kb.get("kb_id") or admin_kb.get("id") or (admin_kb.get("kb") or {}).get("kb_id")
        )
    check("admin can create a knowledge base", code(meta) in (200, 201), f"status={code(meta)}")

    # synthesize files the API stores through chat turns / generated artifacts
    admin_attach_id = "p27attach0001"
    admin_attach_name = "p27-admin-attachment.txt"
    attach_dir = HOME / "data/user/workspace/chat/attachments" / str(admin_session_id)
    attach_dir.mkdir(parents=True, exist_ok=True)
    (attach_dir / f"{admin_attach_id}_{admin_attach_name}").write_text(
        "P27-ADMIN-ATTACHMENT", encoding="utf-8"
    )
    outputs_root = HOME / "data/user/workspace/outputs"
    outputs_root.mkdir(parents=True, exist_ok=True)
    (outputs_root / "p27-admin-output.txt").write_text("P27-ADMIN-OUTPUT", encoding="utf-8")

    # ------------------------------------- object-level + cross-tenant attacks
    ADMIN_MARKERS = (
        "P27-ADMIN-ONLY-MARKER",
        "P27-ADMIN-FILE-BYTES",
        "P27-ADMIN-ATTACHMENT",
        "P27-ADMIN-OUTPUT",
    )

    def no_leak(payload) -> bool:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return not any(marker in text for marker in ADMIN_MARKERS)

    cross = [
        ("GET", f"/api/reading/materials/{admin_mat_id}", "material detail"),
        ("GET", f"/api/reading/materials/{admin_mat_id}/units/1", "material text"),
        ("GET", f"/api/reading/materials/{admin_mat_id}/raw", "material raw"),
        ("POST", f"/api/reading/materials/{admin_mat_id}/retry", "material retry"),
        ("PUT", f"/api/reading/materials/{admin_mat_id}/position", "material position write"),
        ("POST", f"/api/reading/materials/{admin_mat_id}/bookmarks", "material bookmark write"),
        ("DELETE", f"/api/reading/materials/{admin_mat_id}", "material delete"),
        ("GET", f"/api/reading/workspaces/{admin_ws_id}", "collection"),
        ("PATCH", f"/api/reading/workspaces/{admin_ws_id}", "collection update"),
        ("DELETE", f"/api/reading/workspaces/{admin_ws_id}", "collection delete"),
        ("GET", f"/api/sessions/{admin_session_id}", "session detail"),
        ("PATCH", f"/api/sessions/{admin_session_id}", "session rename"),
        ("DELETE", f"/api/sessions/{admin_session_id}", "session delete"),
        ("GET", f"/files/library/{admin_file_id}", "library metadata"),
        ("GET", f"/files/library/{admin_file_id}/download", "library download"),
        ("DELETE", f"/files/library/{admin_file_id}", "library delete"),
        ("POST", f"/files/library/{admin_file_id}/restore", "library restore"),
        (
            "GET",
            f"/files/attachments/{admin_session_id}/{admin_attach_id}/{admin_attach_name}",
            "attachment",
        ),
        ("GET", "/files/outputs/p27-admin-output.txt", "generated output"),
    ]
    if admin_notebook_id:
        cross.append(("GET", f"/api/notebooks/{admin_notebook_id}", "notebook"))
    if admin_course_id:
        cross.append(("GET", f"/api/courses/{admin_course_id}/state", "course state"))
    if admin_kb_id:
        cross.append(("GET", f"/api/knowledge-bases/{admin_kb_id}", "knowledge base"))

    # Request shapes first, as the owner: a shape that is invalid for the owner
    # would make a 400/405 below meaningless (that is what a fake "denied"
    # status code looks like). These succeed, so the cross-tenant calls are the
    # same requests sent by the wrong account.
    shape_ok = {}
    meta, _ = call(
        admin,
        "PATCH",
        f"/api/reading/workspaces/{admin_ws_id}",
        body={"description": "p27 owner shape check"},
    )
    shape_ok["collection update"] = code(meta)
    meta, _ = call(
        admin,
        "PUT",
        f"/api/reading/materials/{admin_mat_id}/position",
        body={"locator": 1, "percentage": 0.0},
    )
    shape_ok["material position write"] = code(meta)
    meta, _ = call(
        admin,
        "POST",
        f"/api/reading/materials/{admin_mat_id}/bookmarks",
        body={"locator": 1, "label": "p27 owner shape"},
    )
    shape_ok["material bookmark write"] = code(meta)
    bad_shapes = {k: v for k, v in shape_ok.items() if v not in (200, 201)}
    check(
        "request shapes used by the cross-tenant matrix are valid for the owner",
        not bad_shapes,
        f"owner statuses={shape_ok}",
    )

    meta, ws_snapshot = call(admin, "GET", f"/api/reading/workspaces/{admin_ws_id}")
    ws_before = _workspace_fields(ws_snapshot)

    cross_results = {}
    leaks = {}
    for method, path, label in cross:
        body = {"title": "x"} if method == "PATCH" else None
        if label == "material position write":
            body = {"locator": 3, "percentage": 0.5}
        if label == "material bookmark write":
            body = {"locator": 3, "label": "p27-non-owner"}
        meta, payload = call(user2, method, path, body=body)
        cross_results[label] = code(meta)
        if not no_leak(payload):
            leaks[label] = payload
    # Any non-2xx that is not a transport failure counts as a refusal, but a 5xx
    # does not: an error page is not an authorization decision. The routes this
    # matrix covers answer "not found in your scope" as either 404 or 400
    # (ReadingError), which is why both are accepted — with the owner-visible
    # data verified unchanged below.
    REFUSALS = (400, 401, 403, 404, 409)
    bad_cross = {k: v for k, v in cross_results.items() if v not in REFUSALS}
    check(
        "cross-tenant object access is denied on every route",
        not bad_cross,
        f"refused {len(cross_results) - len(bad_cross)}/{len(cross_results)}; unexpected={bad_cross}",
    )
    check(
        "no cross-tenant response leaked another account's data", not leaks, f"leaks={list(leaks)}"
    )

    # Refusals have to be refusals: the owner's objects must be exactly as they
    # were before the wrong-account writes above.
    meta, ws_snapshot = call(admin, "GET", f"/api/reading/workspaces/{admin_ws_id}")
    ws_after = _workspace_fields(ws_snapshot)
    check(
        "refused cross-tenant workspace writes changed nothing",
        ws_before is not None and ws_after == ws_before,
        f"before={ws_before} after={ws_after}",
    )
    meta, payload = call(admin, "GET", f"/api/reading/materials/{admin_mat_id}/units/1")
    check(
        "admin material content unchanged after refused writes",
        code(meta) == 200 and "P27-ADMIN-ONLY-MARKER" in json.dumps(payload),
        f"status={code(meta)}",
    )

    # user2's own resources still work (deny rules are not blanket failures)
    meta, user_mat = upload_reading(user2, "p27-user-doc.md", b"# User doc\n\nP27-USER2-BODY\n")
    user_mat_id = (user_mat or {}).get("material_id") if isinstance(user_mat, dict) else None
    meta, payload = call(user2, "GET", f"/api/reading/materials/{user_mat_id}/units/1")
    check(
        "user2 reads its own material normally",
        code(meta) == 200 and "P27-USER2-BODY" in json.dumps(payload),
        f"status={code(meta)}",
    )
    meta, payload = call(admin, "GET", f"/api/reading/materials/{user_mat_id}")
    check(
        "admin cannot read user2's material by id", code(meta) in (403, 404), f"status={code(meta)}"
    )

    # last-copy deletion by the non-owner must not destroy the owner's data
    meta, _ = call(user2, "DELETE", f"/api/reading/materials/{admin_mat_id}")
    meta, payload = call(admin, "GET", f"/api/reading/materials/{admin_mat_id}/units/1")
    check(
        "admin material survives a non-owner delete attempt",
        code(meta) == 200 and "P27-ADMIN-ONLY-MARKER" in json.dumps(payload),
        f"status={code(meta)}",
    )

    # storage roots are separate on disk
    admin_root = HOME / "data/user/workspace/reading"
    user2_root = HOME / "data/users" / str(user2_id) / "user/workspace/reading"
    admin_dirs = (
        {p.name for p in admin_root.iterdir() if p.is_dir()} if admin_root.is_dir() else set()
    )
    user_dirs = (
        {p.name for p in user2_root.iterdir() if p.is_dir()} if user2_root.is_dir() else set()
    )
    check(
        "tenant storage roots share no content directory",
        not (admin_dirs & user_dirs),
        f"shared={admin_dirs & user_dirs}",
    )

    # Phase 26 continuity: listings stay inside the caller's own store, and no
    # tenant database is corrupt after the cross-tenant attempts above.
    meta, listing = call(user2, "GET", "/api/reading/materials")
    meta_lib, lib_listing = call(user2, "GET", "/api/reading/library/materials")
    own_only = no_leak(listing) and no_leak(lib_listing)
    check(
        "user2 listings expose no admin material",
        own_only and code(meta) == 200 and code(meta_lib) == 200,
        f"materials={code(meta)} library={code(meta_lib)} leaked={not own_only}",
    )

    db_reports = {}
    for root in (admin_root, user2_root, HOME / "data/system"):
        for db in sorted(root.rglob("*.sqlite3")) if root.is_dir() else []:
            try:
                connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                result = connection.execute("PRAGMA integrity_check").fetchone()
                db_reports[str(db.relative_to(HOME))] = str(result[0])
                connection.close()
            except Exception as exc:
                db_reports[str(db.relative_to(HOME))] = f"ERROR {exc}"
    bad_dbs = {k: v for k, v in db_reports.items() if v != "ok"}
    check(
        "every tenant database passes integrity_check",
        bool(db_reports) and not bad_dbs,
        f"{len(db_reports)} databases checked; bad={bad_dbs}",
    )

    # --------------------------------------------------- path traversal attacks
    traversals = [
        "../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "....//....//etc/passwd",
        "/etc/passwd",
        "..\\..\\etc\\passwd",
        "..%5c..%5cetc%5cpasswd",
        "p27-admin-output.txt/../../../../etc/passwd",
        "....//....//....//etc/passwd",
        "%252e%252e%252fetc%252fpasswd",
    ]
    escape_evidence = []
    for attack in traversals:
        for base in ("/files/outputs/", "/files/workspace-items/w1/"):
            meta, payload = call(admin, "GET", base + attack)
            text = payload if isinstance(payload, str) else json.dumps(payload)
            if code(meta) in (200, 206) or "root:" in text or "/bin/" in text:
                escape_evidence.append(f"{base}{attack} -> {code(meta)}")
    check(
        "output and workspace-item traversal attempts all fail",
        not escape_evidence,
        f"escapes={escape_evidence}",
    )

    # reading asset/media traversal on an owned material
    asset_attacks = [
        "../../../../etc/passwd",
        "..%2f..%2f..%2fetc%2fpasswd",
        "....//....//etc/passwd",
        "%2e%2e/assets/x",
        "subdir/../../../../etc/passwd",
    ]
    asset_escapes = []
    for attack in asset_attacks:
        for suffix in (f"/assets/{attack}", f"/media/{attack}"):
            meta, payload = call(admin, "GET", f"/api/reading/materials/{admin_mat_id}{suffix}")
            text = payload if isinstance(payload, str) else json.dumps(payload)
            if code(meta) in (200, 206) or "root:" in text:
                asset_escapes.append(f"{suffix} -> {code(meta)}")
    check(
        "material asset/media traversal attempts all fail",
        not asset_escapes,
        f"escapes={asset_escapes}",
    )

    # attachment filename traversal
    meta, payload = call(
        admin,
        "GET",
        f"/files/attachments/{admin_session_id}/p27attach0001/..%2f..%2f..%2fetc%2fpasswd",
    )
    check(
        "attachment filename traversal is refused",
        code(meta) in (400, 404) and "root:" not in json.dumps(payload),
        f"status={code(meta)}",
    )

    # library download with crafted ids
    lib_attacks = ["..%2f..%2fetc%2fpasswd", "../../../../etc/passwd", "%2e%2e%2f"]
    lib_escapes = []
    for attack in lib_attacks:
        meta, payload = call(admin, "GET", f"/files/library/{attack}/download")
        if code(meta) in (200, 206) or "root:" in json.dumps(payload):
            lib_escapes.append(f"{attack} -> {code(meta)}")
    check("library id traversal attempts all fail", not lib_escapes, f"escapes={lib_escapes}")

    # ------------------------------------------------------ archive containment
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr(zipfile.ZipInfo("../../p27-escape.txt"), b"P27-ESCAPED")
        zf.writestr(zipfile.ZipInfo("/tmp/p27-absolute.txt"), b"P27-ABSOLUTE")
        zf.writestr("safe-note.md", b"# safe")
    evil_bytes = evil.getvalue()
    escape_target = HOME / "data/user/workspace/p27-escape.txt"
    meta, payload = upload_multipart(
        admin,
        "POST",
        f"/api/knowledge-bases/{admin_kb_id}/documents"
        if admin_kb_id
        else "/api/knowledge-bases/upload",
        evil_bytes,
        filename="p27-evil.zip",
    )
    escaped_anywhere = [str(p) for p in HOME.rglob("p27-escape.txt")] + [
        str(p) for p in Path("/tmp").glob("p27-absolute.txt")
    ]
    check(
        "zip-slip upload cannot write outside the extraction root",
        not escaped_anywhere,
        f"found={escaped_anywhere}",
    )
    check("zip upload did not 5xx", code(meta) not in (500, 0), f"status={code(meta)}")

    # ------------------------------------------------------ information disclosure
    meta, body = call(anon, "GET", "/api/reading/materials/not-a-valid-material-id")
    text = json.dumps(body)
    check(
        "auth failure bodies carry no internals",
        code(meta) in (401, 403) and "/home/user" not in text,
        f"status={code(meta)}",
    )

    # force an unhandled error through the JSON boundary and inspect the body
    meta, body = call(admin, "GET", "/api/system/does-not-exist")
    check(
        "unknown route does not disclose internals",
        "/home/user" not in json.dumps(body),
        f"status={code(meta)}",
    )

    corrupt = HOME / "data/user/workspace/reading/_catalog.sqlite3"
    if corrupt.exists():
        snapshot = corrupt.read_bytes()
        corrupt.write_bytes(b"garbage" * 200)
        meta, body = call(admin, "GET", "/api/reading/library/materials")
        text = json.dumps(body)
        leaked = [
            needle
            for needle in ("/home/user", "Traceback", "sqlite3", "auth_secret")
            if needle in text
        ]
        check(
            "500 bodies do not leak filesystem paths, traces or secrets",
            code(meta) >= 500 and not leaked,
            f"status={code(meta)} leaked={leaked}",
        )
        check(
            "500 body does not include an exception message string",
            "garbage" not in text and "file is not a database" not in text,
            f"detail={str(body)[:120]}",
        )
        corrupt.write_bytes(snapshot)

    meta, body = call(anon, "GET", "/api/settings/ui")
    text = json.dumps(body).lower()
    secrets_leaked = [w for w in ("password", "secret", "api_key", "token") if w in text]
    check(
        "public settings expose no credential-shaped fields",
        not secrets_leaked,
        f"fields={secrets_leaked}",
    )

    # ------------------------------------------------------------- CORS + headers
    status, hdrs, _ = raw_headers(
        "OPTIONS",
        "/api/sessions",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    allow = _join(hdrs, "access-control-allow-origin")
    check(
        "cross-origin preflight from an unknown origin is not allowed",
        allow not in ("*", "https://evil.example"),
        f"status={status} ACAO={allow!r}",
    )

    status, hdrs, _ = raw_headers(
        "OPTIONS",
        "/api/sessions",
        headers={
            "Origin": FRONTEND_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    allow_ok = _join(hdrs, "access-control-allow-origin")
    check(
        "configured frontend origin is allowed",
        allow_ok == FRONTEND_ORIGIN and _join(hdrs, "access-control-allow-credentials") == "true",
        f"status={status} ACAO={allow_ok!r}",
    )

    meta, _ = call(admin, "GET", f"/files/library/{admin_file_id}/download")
    headers = {k.lower(): v for k, v in meta.get("headers", {}).items()}
    check(
        "private downloads are marked non-cacheable or private",
        "no-store" in headers.get("cache-control", "")
        or "private" in headers.get("cache-control", ""),
        f"cache-control={headers.get('cache-control')!r}",
    )

    # ---------------------------------------------------- resource exhaustion
    # The avatar endpoint advertises a 1 MB ceiling, so the smallest honest
    # overshoot is 1 MB + 1 byte — a real streaming rejection that answers in
    # milliseconds. (The reading-material ceiling is 200 MB and enforced per
    # 1 MB chunk; its rejection path is pinned by
    # tests/api/test_reading_upload_ceiling.py rather than by a 201 MB upload.)
    avatar = b"x" * (1024 * 1024 + 1)
    t0 = time.time()
    meta, body = upload_multipart(
        admin, "PUT", "/api/auth/profile/avatar", avatar, filename="p27-huge.png", timeout=120
    )
    check(
        "oversized upload is rejected with 413",
        code(meta) == 413,
        f"status={code(meta)} elapsed={time.time() - t0:.1f}s",
    )

    # login attempt rate: record the observed behaviour (evidence, not a gate)
    stamps = []
    for _ in range(8):
        meta, _ = call(
            anon,
            "POST",
            "/api/auth/login",
            body={"username": ADMIN[0], "password": "wrong-password"},
        )
        stamps.append(code(meta))
    print(f"      [evidence] 8 consecutive failed logins -> {stamps}", flush=True)

    # ------------------------------------------- token revocation (deletion/demotion)
    meta, third = call(
        admin,
        "POST",
        "/api/auth/users",
        body={"username": "p27third2", "password": "P27-Third2-Passw0rd!"},
    )
    third_op = opener()
    st, _ = login(third_op, "p27third2", "P27-Third2-Passw0rd!")
    meta_before, _ = call(third_op, "GET", "/api/settings")
    meta_del, _ = call(admin, "DELETE", "/api/auth/users/p27third2")
    meta_after, _ = call(third_op, "GET", "/api/settings")
    check(
        "deleting an account revokes its existing token",
        code(meta_del) == 200 and code(meta_before) == 200 and code(meta_after) in (401, 403),
        f"before={code(meta_before)} delete={code(meta_del)} after={code(meta_after)}",
    )

    # A demotion has to reach a token that already exists: promote, log in, then
    # demote and replay the admin-route call with the token minted while admin.
    meta, _ = call(
        admin,
        "POST",
        "/api/auth/users",
        body={"username": "p27demoted", "password": "P27-Demoted-Passw0rd!"},
    )
    meta, _ = call(admin, "PUT", "/api/auth/users/p27demoted/role", body={"role": "admin"})
    demoted_op = opener()
    st, _ = login(demoted_op, "p27demoted", "P27-Demoted-Passw0rd!")
    meta_as_admin, _ = call(demoted_op, "GET", "/api/multi-user/users")
    meta, _ = call(admin, "PUT", "/api/auth/users/p27demoted/role", body={"role": "user"})
    meta_demoted, _ = call(demoted_op, "GET", "/api/multi-user/users")
    meta_own_settings, _ = call(demoted_op, "GET", "/api/settings")
    check(
        "demoting an account strips admin from its existing token",
        code(meta_as_admin) == 200
        and code(meta_demoted) in (401, 403)
        and code(meta_own_settings) == 200,
        f"as_admin={code(meta_as_admin)} demoted={code(meta_demoted)} "
        f"own_settings={code(meta_own_settings)}",
    )

    # Failed sign-ins are counted per account and caller, so guessing stops.
    meta, _ = call(
        admin,
        "POST",
        "/api/auth/users",
        body={"username": "p27throttle", "password": "P27-Throttle-Passw0rd!"},
    )
    throttle_codes = []
    throttle_headers = []
    for _ in range(auth_service_limit + 1):
        status, hdrs, _ = raw_headers(
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": "p27throttle", "password": "definitely-wrong"}).encode(),
        )
        throttle_codes.append(status)
        throttle_headers.append(_join(hdrs, "retry-after"))
    check(
        "repeated failed sign-ins are throttled",
        throttle_codes[-1] == 429 and bool(throttle_headers[-1]),
        f"statuses={throttle_codes} retry_after={throttle_headers[-1]!r}",
    )

    proc.terminate()
    try:
        proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"total={len(RESULTS)} passed={len(RESULTS) - len(failed)} failed={len(failed)}")
    for name, _, detail in failed:
        print(f"  - {name} ({detail})")
    print(f"\nlog: /tmp/mo7-p27-attack.log  home: {HOME}")

    report = HOME / "harness-results.json"
    report.write_text(
        json.dumps(
            {
                "total": len(RESULTS),
                "passed": len(RESULTS) - len(failed),
                "failed": len(failed),
                "checks": [
                    {"name": name, "ok": ok, "detail": detail} for name, ok, detail in RESULTS
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"results: {report}")
    sys.exit(1 if failed else 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
