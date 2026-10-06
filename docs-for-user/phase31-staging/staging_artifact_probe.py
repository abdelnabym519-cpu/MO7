#!/usr/bin/env python3
"""Wire-level certification probe for the deployed release artifact.

Runs against the running deployment (TLS ingress by default, loopback backend
with ``STAGING_ARTIFACT_TARGET``) and asserts the properties that only the raw
HTTP exchange can show: cookie flags exactly as sent, header hardening on every
response class, CORS decisions for an origin that is not configured, request
ambiguity (Content-Length vs Transfer-Encoding), oversized headers, method
handling, path normalisation, and response framing.

Usage:
    STAGING_ARTIFACT_TARGET=127.0.0.1:8443 python3 staging_artifact_probe.py
    STAGING_ARTIFACT_TARGET=127.0.0.1:8101 python3 staging_artifact_probe.py

Exit code 0 when every check passes. Evidence JSON is written next to this
script as ``staging-artifact-probe.json`` (override with ``--evidence PATH``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import ssl
import sys

HERE = Path(__file__).resolve().parent
TARGET = os.environ.get("STAGING_ARTIFACT_TARGET", "127.0.0.1:8443")
HOST, _, PORT = TARGET.partition(":")
PORT = int(PORT or 443)
TLS = PORT == 8443 or os.environ.get("STAGING_ARTIFACT_TLS") == "1"
# The public origin terminates the frontend and the API; the loopback target is
# the API alone, so frontend-surface contracts are asserted only on the former.
DOCUMENT_SURFACE = (
    os.environ.get("STAGING_ARTIFACT_SURFACE", "public" if TLS else "api") == "public"
)
ALLOWED_ORIGIN = os.environ.get("STAGING_ALLOWED_ORIGIN", "https://127.0.0.1:8443")
STAGING_ROOT = Path(os.environ.get("STAGING_ROOT", "/home/user/mo7-staging"))
EVIDENCE = Path(os.environ.get("STAGING_EVIDENCE_DIR", STAGING_ROOT / "evidence"))
CREDS_PATH = Path(
    os.environ.get("STAGING_CREDENTIALS", STAGING_ROOT / "secrets/staging-credentials.json")
)

RESULTS: list[dict] = []
STACK_MARKERS = (
    "Traceback (most recent call last)",
    'File "',
    "site-packages",
    "sqlalchemy",
    "uvicorn",
)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def raw(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    send_chunked: bool = False,
    timeout: float = 30.0,
) -> tuple[int, list[tuple[str, str]], bytes]:
    connection = socket.create_connection((HOST, PORT), timeout=timeout)
    if TLS:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = context.wrap_socket(connection, server_hostname=HOST)
    request = [f"{method} {path} HTTP/1.1", f"Host: {HOST}:{PORT}"]
    for key, value in (headers or {}).items():
        request.append(f"{key}: {value}")
    if send_chunked:
        request.append("Transfer-Encoding: chunked")
    payload = "\r\n".join(request).encode() + b"\r\n\r\n"
    if body is not None:
        payload += body
    elif send_chunked:
        payload += b"0\r\n\r\n"
    connection.sendall(payload)

    buffer = b""
    status = 0
    response_headers: list[tuple[str, str]] = []
    while b"\r\n\r\n" not in buffer:
        chunk = connection.recv(65536)
        if not chunk:
            break
        buffer += chunk
    head, _, rest = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    if lines and lines[0].startswith("HTTP/"):
        try:
            status = int(lines[0].split(" ")[1])
        except (IndexError, ValueError):
            status = 0
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            response_headers.append((key.strip().lower(), value.strip()))

    response_body = rest
    framed = any(key == "content-length" for key, _ in response_headers)
    if framed:
        expected = int(dict(response_headers)["content-length"])
        while len(response_body) < expected:
            chunk = connection.recv(65536)
            if not chunk:
                break
            response_body += chunk
    connection.close()
    return status, response_headers, response_body


def header(headers: list[tuple[str, str]], name: str) -> str:
    for key, value in headers:
        if key == name:
            return value
    return ""


def login_token() -> tuple[str, str]:
    credentials = json.loads(CREDS_PATH.read_text())
    body = json.dumps(
        {"username": credentials["admin"]["username"], "password": credentials["admin"]["password"]}
    ).encode()
    status, headers, _ = raw(
        "POST",
        "/api/auth/login",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )
    cookie = header(headers, "set-cookie")
    token = ""
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "dt_token":
            token = value
    if status != 200 or not token:
        raise SystemExit(f"staging admin login failed: status={status} set-cookie={cookie[:120]}")
    return token, cookie


def main() -> int:
    token, cookie = login_token()

    # --- cookie transmission exactly as the wire sees it ------------------
    check("session cookie is HttpOnly", "httponly" in cookie.lower(), cookie[:160])
    check("session cookie is Secure", "secure" in cookie.lower(), cookie[:160])
    check("session cookie declares SameSite", "samesite" in cookie.lower(), cookie[:160])
    check(
        "session cookie is host-only (no Domain attribute)",
        "domain=" not in cookie.lower(),
        cookie[:160],
    )
    check("session cookie is scoped to the site root", "path=/" in cookie.lower(), cookie[:160])
    check(
        "session cookie is not a wildcard-partitioned value",
        "partitioned" not in cookie.lower(),
        cookie[:160],
    )

    # --- authentication failures ------------------------------------------
    status, headers, body = raw(
        "GET", "/api/notebooks", headers={"Authorization": "Bearer not-a-real-token"}
    )
    text = body.decode("utf-8", "replace")
    check("a forged bearer token is refused", status == 401, f"status={status}")
    check(
        "the refusal is not a framework error page",
        "text/html" not in header(headers, "content-type"),
        header(headers, "content-type"),
    )
    check(
        "no debug stack is leaked in the refusal body",
        not any(marker in text for marker in STACK_MARKERS),
        text[:120],
    )
    check(
        "a refused response sets no session cookie",
        header(headers, "set-cookie") == "",
        header(headers, "set-cookie")[:80],
    )

    # --- header hardening on the document surface -------------------------
    # The deployment contract scopes the hardened header set to non-API routes
    # (web/next.config.ts: source "/((?!api/).*)"); the API's own byte-serving
    # endpoints set nosniff themselves and are asserted by staging_security.py.
    for label, path in (
        ("an HTML page", "/login"),
        ("a gated page", "/learning/reading"),
        ("a page that does not exist", "/no-such-page"),
    ):
        status, headers, _ = raw("GET", path)
        hardened = (
            header(headers, "x-content-type-options").lower() == "nosniff"
            and bool(header(headers, "referrer-policy"))
            and (
                bool(header(headers, "x-frame-options"))
                or "frame-ancestors" in header(headers, "content-security-policy")
            )
        )
        if DOCUMENT_SURFACE:
            check(
                f"{label} carries the hardened headers",
                hardened,
                f"status={status} nosniff={header(headers, 'x-content-type-options')} "
                f"referrer={header(headers, 'referrer-policy')} "
                f"xfo={header(headers, 'x-frame-options')} csp={header(headers, 'content-security-policy')[:48]}",
            )
        else:
            check(
                f"{label} is not served by the API target",
                status == 404,
                f"status={status} (frontend surface; asserted on the public origin)",
            )

    # --- response framing --------------------------------------------------
    credentials_blob = json.dumps(
        {
            "username": "framing-probe",
            "password": "not-a-real-password",
        }
    ).encode()
    status, headers, body = raw(
        "POST",
        "/api/auth/login",
        headers={"Content-Type": "application/json", "Content-Length": str(len(credentials_blob))},
        body=credentials_blob,
    )
    length = header(headers, "content-length")
    check(
        "a framed JSON response matches its Content-Length",
        bool(length) and int(length) == len(body),
        f"status={status} declared={length} received={len(body)}",
    )
    check(
        "a rejected sign-in does not disclose whether the account exists",
        status in (400, 401, 403) and "framing-probe" not in body.decode("utf-8", "replace"),
        f"status={status}",
    )

    # --- CORS decisions ----------------------------------------------------
    status, headers, _ = raw(
        "OPTIONS",
        "/api/notebooks",
        headers={
            "Origin": "https://not-configured.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    allow_origin = header(headers, "access-control-allow-origin")
    check(
        "an unconfigured origin receives no CORS grant",
        allow_origin not in ("*", "https://not-configured.example"),
        f"status={status} allow-origin={allow_origin!r}",
    )

    status, headers, _ = raw(
        "OPTIONS",
        "/api/notebooks",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    allow_origin = header(headers, "access-control-allow-origin")
    check(
        "the configured origin is granted exactly (never a wildcard)",
        allow_origin == ALLOWED_ORIGIN,
        f"status={status} allow-origin={allow_origin!r}",
    )

    # --- request ambiguity -------------------------------------------------
    status, headers, _ = raw(
        "POST",
        "/api/auth/login",
        headers={
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Transfer-Encoding": "chunked",
        },
        body=b"{}",
        send_chunked=True,
    )
    check(
        "a request carrying both Content-Length and Transfer-Encoding is refused",
        400 <= status < 500,
        f"status={status}",
    )

    # --- oversized header --------------------------------------------------
    status, _, _ = raw("GET", "/health/live", headers={"X-Staging-Oversized": "a" * (64 * 1024)})
    if DOCUMENT_SURFACE:
        check(
            "an oversized request header is refused at the public edge without a 5xx",
            400 <= status < 500 or status == 0,
            f"status={status}",
        )
    else:
        # uvicorn/h11 accepts large-but-bounded headers on the loopback bind; the
        # edge (public origin) is where the request-size boundary is asserted.
        check(
            "the loopback API reports its oversized-header behaviour without a 5xx",
            status < 500,
            f"status={status} (edge boundary asserted on the public origin)",
        )

    # --- method handling ---------------------------------------------------
    status, headers, _ = raw("PUT", "/api/auth/login")
    allow = header(headers, "allow")
    check(
        "an unsupported method on an API route is refused",
        status in (404, 405) and (status == 404 or "POST" in allow.upper()),
        f"status={status} allow={allow!r}",
    )
    status, _, override_body = raw(
        "GET",
        "/api/notebooks",
        headers={"Authorization": f"Bearer {token}", "X-HTTP-Method-Override": "DELETE"},
    )
    check(
        "a method override header cannot change the served method",
        status == 200 and "notebooks" in override_body.decode("utf-8", "replace"),
        f"status={status} body={override_body[:80]!r}",
    )

    # --- path normalisation ------------------------------------------------
    for path in (
        "/api/../api/notebooks",
        "/api/%2e%2e/%2e%2e/etc/passwd",
        "/api/notebooks/../../etc/passwd",
        "/files/outputs/..%2f..%2f..%2fetc%2fpasswd",
    ):
        status, _, body = raw("GET", path)
        text = body.decode("utf-8", "replace")
        check(
            f"path {path!r} never escapes the API surface",
            status in (307, 308, 400, 401, 403, 404) and "root:" not in text,
            f"status={status}",
        )

    # --- body size ceiling -------------------------------------------------
    big = b"x" * (2 * 1024 * 1024)
    status, _, _ = raw(
        "POST",
        "/api/auth/login",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(big)),
            "Authorization": f"Bearer {token}",
        },
        body=big,
    )
    check(
        "an oversized JSON body is refused without a 5xx", 400 <= status < 500, f"status={status}"
    )

    # --- unauthenticated static surface ------------------------------------
    for path in ("/api/settings", "/files/library/", "/api/multi-user/users"):
        status, headers, body = raw("GET", path)
        text = body.decode("utf-8", "replace")
        check(
            f"{path} is refused to an anonymous caller",
            status in (401, 403, 307, 308) and not any(marker in text for marker in STACK_MARKERS),
            f"status={status}",
        )

    passed = sum(1 for row in RESULTS if row["ok"])
    failed = len(RESULTS) - passed
    print(
        f"\nstaging artifact probe [{TARGET}{' tls' if TLS else ' loopback'}]: "
        f"total={len(RESULTS)} passed={passed} failed={failed}"
    )
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence = EVIDENCE / "staging-artifact-probe.json"
    if "--evidence" in sys.argv:
        evidence = Path(sys.argv[sys.argv.index("--evidence") + 1])
    evidence.write_text(
        json.dumps(
            {
                "target": TARGET,
                "tls": TLS,
                "total": len(RESULTS),
                "passed": passed,
                "failed": failed,
                "checks": RESULTS,
            },
            indent=2,
        )
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
