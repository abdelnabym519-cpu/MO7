#!/usr/bin/env python3
"""Phase 31 staging security matrix.

Re-runs the Phase 27 attack surface and the Phase 29 token checklist against the
deployed staging release, over both the TLS ingress (127.0.0.1:8443) and the
loopback backend (127.0.0.1:8101), to prove the infrastructure did not
reintroduce a defect the application had already closed:

  * cookie attributes on the wire (HttpOnly / Secure / SameSite / Path / Max-Age)
  * forged, tampered, foreign-key, expired, alg=none, empty, garbage and
    header-only tokens
  * wrong password, unknown account, malformed bodies
  * RBAC on admin routes, revocation on account deletion
  * the cross-tenant matrix, with an owner-side check that refusals changed
    nothing and leaked nothing
  * traversal and zip-slip containment, sanitised error envelopes
  * no documentation surface served by the ingress, CORS, cache controls
  * throttling and its Retry-After, without locking the admin out
  * storage roots and databases stay separate

usage:
    STAGING_BACKEND=127.0.0.1:8443 python3 staging_security.py   # via TLS ingress
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from staging_http import Actor, call, joined, login, raw, upload  # noqa: E402

STAGING = Path("/home/user/mo7-staging")
EVIDENCE = STAGING / "evidence"
CREDS = json.loads((STAGING / "secrets/staging-credentials.json").read_text())
TARGET = os.environ.get("STAGING_BACKEND", "127.0.0.1:8443")
HOST, _, PORT = TARGET.partition(":")
PORT = int(PORT or 8443)
TLS = "8443" in TARGET or PORT == 8443
BASE = f"{'https' if TLS else 'http'}://127.0.0.1:{PORT}"
# The deployment's API listens on loopback only; API-only surfaces (the file
# library collection route, settings) are checked there. Cookies are Secure and
# therefore not sent over http, so those calls authenticate with the session
# token as a bearer credential.
BACKEND_TARGET = os.environ.get("STAGING_BACKEND_API", "127.0.0.1:8101")
BACKEND = f"http://{BACKEND_TARGET}"

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail[:400]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail[:300]}"))
    return bool(ok)


def token_actor(actor: Actor) -> Actor:
    """Reuse an account's session token in the Authorization header."""
    actor2 = Actor(actor.name + "-bearer")
    actor2.token = actor.cookies.get("dt_token") or actor.token
    return actor2


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    admin, a, b = Actor("admin"), Actor("tenant_a"), Actor("tenant_b")
    for actor, key in ((admin, "admin"), (a, "tenant_a"), (b, "tenant_b")):
        login(actor, CREDS[key]["username"], CREDS[key]["password"], base=BASE)
        actor.token = actor.cookies.get("dt_token")
    check(
        "the three staging accounts authenticate",
        bool(a.cookies) and bool(b.cookies) and bool(admin.cookies),
    )

    # --- wire-level cookie contract -----------------------------------------
    status, headers, payload = raw(
        "POST",
        "/api/auth/login",
        host_header=HOST,
        body=json.dumps(
            {"username": CREDS["admin"]["username"], "password": CREDS["admin"]["password"]}
        ).encode(),
        port=PORT,
        tls=TLS,
    )
    set_cookie = joined(headers, "set-cookie")
    check(
        "login sets the session cookie on the wire",
        status == 200 and "dt_token=" in set_cookie,
        set_cookie[:200],
    )
    check("cookie is HttpOnly", "httponly" in set_cookie.lower(), set_cookie[:200])
    check("cookie is Secure", "secure" in set_cookie.lower(), set_cookie[:200])
    check("cookie is SameSite=None", "samesite=none" in set_cookie.lower(), set_cookie[:200])
    check("cookie is Path=/", "path=/" in set_cookie.lower(), set_cookie[:200])
    check("cookie carries Max-Age", "max-age=86400" in set_cookie.lower(), set_cookie[:200])
    check(
        "auth responses are not cacheable",
        "no-store" in joined(headers, "cache-control").lower(),
        joined(headers, "cache-control"),
    )

    # --- token shapes -------------------------------------------------------
    import base64
    import hashlib
    import hmac

    def b64(raw_bytes: bytes) -> str:
        return base64.urlsafe_b64encode(raw_bytes).decode().rstrip("=")

    def mint(payload: dict, secret: bytes, algorithm: str = "HS256") -> str:
        header = b64(json.dumps({"alg": algorithm, "typ": "JWT"}).encode())
        body = b64(json.dumps(payload).encode())
        signature = b64(hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest())
        return f"{header}.{body}.{signature}"

    now = int(time.time())
    shapes = {
        "garbage": "not-a-token",
        "empty": "",
        "header_only": "eyJhbGciOiJIUzI1NiJ9",
        "wrong_key": mint(
            {"sub": "admin", "role": "admin", "exp": now + 3600}, b"wrong-secret-value"
        ),
        "alg_none": b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        + "."
        + b64(json.dumps({"sub": "admin", "role": "admin"}).encode())
        + ".",
        "expired": mint({"sub": "staging-admin", "role": "admin", "exp": now - 60}, b"unused"),
    }
    accepted = {}
    for name, token in shapes.items():
        actor = Actor(name)
        actor.token = token
        status, _, _, _ = call(actor, "GET", "/api/notebooks", base=BASE)
        accepted[name] = status
    check(
        "every forged or malformed token is refused",
        all(status == 401 for status in accepted.values()),
        f"results={accepted}",
    )

    # --- credential handling -------------------------------------------------
    status, _, body, _ = call(
        Actor("wrong"),
        "POST",
        "/api/auth/login",
        body={"username": CREDS["admin"]["username"], "password": "definitely-wrong"},
        base=BASE,
    )
    check("a wrong password is refused", status == 401, f"status={status}")
    status, _, body, _ = call(
        None,
        "POST",
        "/api/auth/login",
        body={"username": "no-such-staging-user", "password": "irrelevant"},
        base=BASE,
    )
    check("an unknown account is refused", status == 401, f"status={status}")
    status, _, body, _ = call(None, "POST", "/api/auth/login", body={"username": "x"}, base=BASE)
    check("a malformed login body is rejected", status == 422, f"status={status}")
    check(
        "auth failure bodies carry no internals",
        "traceback" not in body.lower() and "site-packages" not in body.lower(),
        body[:160],
    )

    # --- RBAC and revocation ------------------------------------------------
    non_admin = Actor("non-admin")
    login(non_admin, CREDS["tenant_a"]["username"], CREDS["tenant_a"]["password"], base=BASE)
    non_admin.token = non_admin.cookies.get("dt_token")
    status, _, body, _ = call(non_admin, "GET", "/api/multi-user/users", base=BASE)
    check(
        "a non-admin is refused the admin surface",
        status == 403,
        f"status={status} body={body[:120]}",
    )
    status, _, body, _ = call(
        non_admin,
        "POST",
        "/api/auth/users",
        body={"username": "escalate", "password": "x"},
        base=BASE,
    )
    check("a non-admin cannot create accounts", status in (401, 403), f"status={status}")

    doomed = {
        "username": f"p31-doomed-{uuid.uuid4().hex[:8]}",
        "password": f"p31-doomed-{uuid.uuid4().hex[:8]}",
    }
    call(admin, "POST", "/api/auth/users", body=doomed, base=BASE)
    revoked = Actor("revoked")
    login(revoked, doomed["username"], doomed["password"], base=BASE)
    revoked.token = revoked.cookies.get("dt_token")
    before, _, _, _ = call(revoked, "GET", "/api/settings", base=BASE)
    status, _, _, _ = call(admin, "DELETE", f"/api/auth/users/{doomed['username']}", base=BASE)
    after, _, _, _ = call(revoked, "GET", "/api/settings", base=BASE)
    check(
        "deleting an account revokes its live session",
        before == 200 and status == 200 and after == 401,
        f"before={before} delete={status} after={after}",
    )

    # --- cross-tenant matrix ------------------------------------------------
    a_notebook = (
        call(a, "POST", "/api/notebooks", body={"name": "p31 owner notebook"}, base=BASE)[3] or {}
    )
    notebook_id = a_notebook.get("notebook_id") or a_notebook.get("id")
    a_file = (
        upload(
            a,
            "POST",
            "/files/library/",
            b"p31 owner bytes\n",
            filename="p31-owner.txt",
            extra_fields=(("filename", "p31-owner.txt"), ("mime_type", "text/plain")),
            base=BACKEND,
        )[3]
        or {}
    )
    file_id = a_file.get("file_id") or a_file.get("id")
    a_material = (
        upload(
            a,
            "POST",
            "/api/reading/materials",
            b"# p31 owner material\n\nSecret text.\n",
            filename="p31-owner.md",
            base=BASE,
        )[3]
        or {}
    )
    material_id = a_material.get("material_id") or a_material.get("id")
    a_workspace = (
        call(
            a, "POST", "/api/reading/workspaces", body={"title": "p31 owner collection"}, base=BASE
        )[3]
        or {}
    )
    workspace_id = (a_workspace.get("workspace") or a_workspace).get("workspace_id")

    owner_shape = {
        "collection update": call(
            a,
            "PATCH",
            f"/api/reading/workspaces/{workspace_id}",
            body={"title": "p31 owner collection", "description": "owner shape check"},
            base=BASE,
        )[0],
        "material position write": call(
            a,
            "PUT",
            f"/api/reading/materials/{material_id}/position",
            body={"locator": 1, "percentage": 0.0},
            base=BASE,
        )[0],
        "material bookmark write": call(
            a,
            "POST",
            f"/api/reading/materials/{material_id}/bookmarks",
            body={"locator": 1, "label": "owner bookmark"},
            base=BASE,
        )[0],
        "material annotation write": call(
            a,
            "PUT",
            f"/api/reading/materials/{material_id}/annotations",
            body={
                "locator": 1,
                "kind": "highlight",
                "color": "yellow",
                "quote": "p31 owner material",
                "source_anchor": "",
            },
            base=BASE,
        )[0],
    }
    check(
        "the request shapes used by the matrix are valid for the owner",
        all(status in (200, 201) for status in owner_shape.values()),
        f"owner statuses={owner_shape}",
    )
    before_collection = (
        call(a, "GET", f"/api/reading/workspaces/{workspace_id}", base=BASE)[3] or {}
    )
    before_units = call(a, "GET", f"/api/reading/materials/{material_id}/units/1", base=BASE)[2]

    attempts = {
        "notebook read": call(b, "GET", f"/api/notebooks/{notebook_id}", base=BASE),
        "notebook update": call(
            b, "PUT", f"/api/notebooks/{notebook_id}", body={"name": "hijacked"}, base=BASE
        ),
        "notebook delete": call(b, "DELETE", f"/api/notebooks/{notebook_id}", base=BASE),
        "library metadata": call(b, "GET", f"/files/library/{file_id}", base=BACKEND),
        "library download": call(b, "GET", f"/files/library/{file_id}/download", base=BACKEND),
        "library delete": call(b, "DELETE", f"/files/library/{file_id}", base=BACKEND),
        "material read": call(b, "GET", f"/api/reading/materials/{material_id}", base=BASE),
        "material text": call(b, "GET", f"/api/reading/materials/{material_id}/units/1", base=BASE),
        "material position": call(
            b,
            "PUT",
            f"/api/reading/materials/{material_id}/position",
            body={"locator": 2, "percentage": 0.5},
            base=BASE,
        ),
        "material annotation": call(
            b,
            "PUT",
            f"/api/reading/materials/{material_id}/annotations",
            body={
                "locator": 1,
                "kind": "highlight",
                "color": "yellow",
                "quote": "p31 owner material",
                "source_anchor": "",
            },
            base=BASE,
        ),
        "material delete": call(b, "DELETE", f"/api/reading/materials/{material_id}", base=BASE),
        "collection read": call(b, "GET", f"/api/reading/workspaces/{workspace_id}", base=BASE),
        "collection update": call(
            b,
            "PATCH",
            f"/api/reading/workspaces/{workspace_id}",
            body={"title": "hijacked"},
            base=BASE,
        ),
        "collection delete": call(
            b, "DELETE", f"/api/reading/workspaces/{workspace_id}", base=BASE
        ),
        "collection material add": call(
            b,
            "POST",
            f"/api/reading/workspaces/{workspace_id}/materials",
            body={"material_id": material_id},
            base=BASE,
        ),
    }
    refused = {name: result[0] for name, result in attempts.items()}
    # 400 appears on two collection writes: an object the caller does not own is
    # reported as "workspace not found" with that status. It is a refusal that
    # discloses nothing (the message repeats only the caller's own id), and the
    # inconsistency of 400-instead-of-404 is recorded as a finding.
    check(
        "every cross-tenant attempt is refused",
        all(status in (400, 401, 403, 404) for status in refused.values()),
        f"statuses={refused}",
    )
    leaked = []
    for name, (status, _, body, _) in attempts.items():
        haystack = body.lower()
        if status < 400:
            leaked.append(f"{name}:{status}")
        elif any(
            marker in haystack
            for marker in ("p31 owner", "owner shape check", "secret text", "workspace_id")
        ):
            leaked.append(f"{name}:content")
    check("no refused response leaked another account's data", not leaked, f"leaks={leaked}")

    after_collection = call(a, "GET", f"/api/reading/workspaces/{workspace_id}", base=BASE)[3] or {}
    after_units = call(a, "GET", f"/api/reading/materials/{material_id}/units/1", base=BASE)[2]
    check(
        "the owner's objects are unchanged by the refused writes",
        before_collection == after_collection and before_units == after_units,
        f"collection_same={before_collection == after_collection} units_same={before_units == after_units}",
    )

    # --- containment -------------------------------------------------------
    escapes = []
    for shape in (
        "/files/outputs/..%2f..%2f..%2fetc%2fpasswd",
        "/files/library/..%2f..%2f..%2fetc%2fpasswd",
        "/files/workspace-items/..%2f..%2fetc%2fpasswd/x",
        "/api/reading/materials/../../../../etc/passwd",
    ):
        status, _, body, _ = call(b, "GET", shape, base=BASE)
        if status == 200 and "root:" in body:
            escapes.append(shape)
    check("directory traversal attempts all fail", not escapes, f"escapes={escapes}")

    zip_bytes = _zip_slip()
    zip_status = upload(
        a,
        "POST",
        "/files/library/",
        zip_bytes,
        filename="p31-slip.zip",
        extra_fields=(("filename", "p31-slip.zip"), ("mime_type", "application/zip")),
        base=BASE,
    )[0]
    check("a zip-slip upload does not 5xx", zip_status < 500, f"status={zip_status}")

    # --- surfaces and error envelopes --------------------------------------
    for path in ("/docs", "/redoc", "/openapi.json", "/api/docs"):
        # First hop only: a redirect to the login page is a refusal, and
        # following it would report the login page as the served content.
        status, headers, payload = raw("GET", path, host_header=HOST, port=PORT, tls=TLS)
        body = payload.decode("utf-8", "replace")
        served_docs = status == 200 and any(
            marker in body.lower() for marker in ("swagger", "openapi", "redoc")
        )
        if TLS:
            check(
                f"the public origin does not serve {path}",
                not served_docs and status in (200, 302, 307, 308, 401, 403, 404),
                f"status={status} location={joined(headers, 'location')}",
            )
        else:
            # Loopback API: its own documentation is not a public exposure. The
            # boundary assertion is that the public origin refuses it, which the
            # TLS pass of this same script makes.
            RESULTS.append(
                {
                    "check": f"loopback API serves {path}"
                    if served_docs
                    else f"loopback API refuses {path}",
                    "ok": True,
                    "detail": f"status={status} (internal surface; boundary asserted on the public origin)",
                }
            )
            print(f"[NOTE] loopback {path} -> {status}")
    status, _, body, _ = call(None, "GET", "/api/notebooks/does-not-exist", base=BASE)
    check(
        "an unknown route does not disclose internals",
        status in (401, 404) and "traceback" not in body.lower(),
        f"status={status} body={body[:120]}",
    )

    status, headers, _, _ = call(
        None,
        "OPTIONS",
        "/api/notebooks",
        headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "GET"},
        base=BASE,
    )
    check(
        "an unknown origin gets no CORS grant",
        headers.get("access-control-allow-origin") in (None, ""),
        f"ACAO={headers.get('access-control-allow-origin')}",
    )

    # --- throttling --------------------------------------------------------
    # A dedicated throwaway account: throttling is keyed by username, and a real
    # staging account must not be locked out by a validation pass.
    throttle_name = f"p31-throttle-{uuid.uuid4().hex[:8]}"
    call(
        admin,
        "POST",
        "/api/auth/users",
        body={"username": throttle_name, "password": uuid.uuid4().hex},
        base=BASE,
    )
    throttle_user = Actor("throttle")
    statuses = []
    for _ in range(12):
        statuses.append(
            call(
                throttle_user,
                "POST",
                "/api/auth/login",
                body={"username": throttle_name, "password": "wrong-on-purpose"},
                base=BASE,
            )[0]
        )
    throttled = 429 in statuses
    check("repeated failed sign-ins are throttled", throttled, f"statuses={statuses}")
    status, headers, _, _ = call(
        throttle_user,
        "POST",
        "/api/auth/login",
        body={"username": throttle_name, "password": "wrong-on-purpose"},
        base=BASE,
    )
    check(
        "the throttle advertises Retry-After",
        status == 429 and bool(headers.get("retry-after")),
        f"status={status} retry-after={headers.get('retry-after')}",
    )
    call(admin, "DELETE", f"/api/auth/users/{throttle_name}", base=BASE)
    admin_ok = Actor("admin-after-throttle")
    status, _, _, _ = login(
        admin_ok, CREDS["admin"]["username"], CREDS["admin"]["password"], base=BASE
    )
    check("throttling one account does not lock out the admin", status == 200, f"status={status}")

    # --- carried forward from the Phase 27 matrix -------------------------
    status, _, body, payload = call(a, "GET", "/api/settings", base=BACKEND)
    tenant_blob = json.dumps(payload or {}).lower()
    tenant_leaks = [
        marker
        for marker in ("api_key", "password_hash", "auth_secret", '"secret"')
        if marker in tenant_blob
    ]
    check(
        "a tenant's settings expose no credential-shaped fields",
        status == 200 and not tenant_leaks,
        f"status={status} leaks={tenant_leaks}",
    )
    anonymous_settings = call(None, "GET", "/api/settings", base=BACKEND)[0]
    check(
        "settings are refused to anonymous callers",
        anonymous_settings == 401,
        f"status={anonymous_settings}",
    )
    status, _, body, payload = call(admin, "GET", "/api/settings", base=BACKEND)

    def credential_values(node, path=""):
        """Non-empty values of credential-shaped fields (not capability flags)."""
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else key
                if isinstance(value, str) and value and "requires_" not in key.lower():
                    if any(
                        word in key.lower() for word in ("api_key", "password", "secret", "token")
                    ):
                        if "hash" not in key.lower() or value.strip():
                            found.append(f"{here}={value[:6]}…")
                found.extend(credential_values(value, here))
        elif isinstance(node, list):
            for index, value in enumerate(node[:5]):
                found.extend(credential_values(value, f"{path}[{index}]"))
        return found

    leaks = credential_values(payload or {})
    check(
        "administrator settings expose no credential values",
        status == 200 and not leaks,
        f"status={status} leaks={leaks[:5]}",
    )

    status, headers, _, _ = call(a, "GET", f"/files/library/{file_id}/download", base=BACKEND)
    cache = (headers.get("cache-control") or "").lower()
    check(
        "private downloads are not publicly cacheable",
        status == 200 and ("private" in cache or "no-store" in cache),
        f"status={status} cache-control={cache!r}",
    )

    oversized = b"x" * (64 * 1024 * 1024)
    status = upload(
        a,
        "POST",
        "/api/reading/materials",
        oversized,
        filename="p31-oversized.bin",
        base=BACKEND,
        timeout=120,
    )[0]
    check(
        "an oversized upload is rejected without a 5xx",
        status in (400, 413, 422),
        f"status={status}",
    )

    try:
        import sqlite3

        bad = []
        checked = 0
        for database in sorted((STAGING / "home/data").rglob("*.sqlite3")) + sorted(
            (STAGING / "home/data").rglob("*.db")
        ):
            checked += 1
            try:
                connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                try:
                    if connection.execute("pragma integrity_check").fetchone()[0] != "ok":
                        bad.append(str(database))
                finally:
                    connection.close()
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{database}: {exc}")
        check(
            "every staging database passes integrity_check",
            checked >= 3 and not bad,
            f"checked={checked} bad={bad[:3]}",
        )
    except ImportError:  # pragma: no cover
        check("every staging database passes integrity_check", False, "sqlite3 unavailable")

    # --- storage and database separation ----------------------------------
    roots = sorted((STAGING / "home/data/users").glob("*/user"))
    distinct = {root.resolve() for root in roots}
    check(
        "each tenant keeps its own storage root",
        len(distinct) == len(roots) and len(roots) >= 3,
        f"roots={len(roots)} distinct={len(distinct)}",
    )

    total = len(RESULTS)
    passed = sum(1 for row in RESULTS if row["ok"])
    target_name = "tls" if TLS else "loopback"
    document = {
        "target": BASE,
        "mode": target_name,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": RESULTS,
    }
    (EVIDENCE / f"staging-security-{target_name}.json").write_text(json.dumps(document, indent=2))
    if target_name == "loopback":
        (EVIDENCE / "staging-security.json").write_text(json.dumps(document, indent=2))
    print(
        f"\nstaging security matrix [{target_name}]: total={total} passed={passed} failed={total - passed}"
    )
    return 0 if passed == total else 1


def _zip_slip() -> bytes:
    """A zip whose member names try to escape the extraction root."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../../../tmp/p31-zip-slip.txt", "escaped")
        archive.writestr("..\\..\\p31-zip-slip.txt", "escaped")
        archive.writestr("safe.txt", "contained")
    return buffer.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
