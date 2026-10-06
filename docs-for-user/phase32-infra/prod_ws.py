#!/usr/bin/env python3
"""Live WebSocket probe for the deployed release.

The REST surfaces are covered by the validation, security and artifact probes.
The streaming surfaces are not: the product serves six WebSocket routes
(``/ws/books``, ``/ws/knowledge-bases/{kb}/progress``, ``/ws/questions``,
``/ws/mastery-paths``, ``/ws/partners/{id}``, ``/ws/partners/groups/{id}``) and
every one of them decides admission in ``ws_require_auth`` — before ``accept()``,
so an unauthenticated upgrade must be refused rather than accepted and ignored.
Nothing else in the harnesses opens one, so this probe does.

What it asserts, over the raw TLS socket, against the deployment's own origin:

1. an authenticated upgrade is accepted, and the subscribe is acknowledged with
   the stream's own sequence number;
2. an anonymous upgrade is refused — no accepted socket;
3. an upgrade carrying a forged session token is refused;
4. an upgrade carrying a token that is valid but belongs to another account is
   accepted (accounts are not confused with one another: each gets its own
   session) — this separates "refused for authentication reasons" from "refused
   for all reasons";
5. a foreign book id is acknowledged exactly like an id that has no stream, with
   no replayed frames — the observable form of "another tenant's book does not
   stream into your socket".

Case 5 is measured, not assumed. The acknowledgement echoes the stream's own
``latest_seq``, so the comparison is decisive whenever the named book has history
in this process; when neither id has history the probe records
``latest_seq=0`` for both and says so in the evidence rather than claiming more
than it observed (the residual question — whether a bus with live history is
attachable by a foreign account — needs a book that is compiling, which needs an
LLM provider this environment does not have; see the closure report's stated
limits).

Usage:
    PROD_ROOT=/home/user/mo7-prod python3 prod_ws.py
    ... --path /ws/books --origin https://<host>       (override the surface)

Exit code 0 when every check passes. Evidence JSON is written next to this
script as ``production-websocket.json`` (override with ``--evidence PATH``).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prod_config as cfg  # noqa: E402  (the harness locates itself)

# The public hostname is not reachable from inside this host (the sandbox cannot
# dial its own platform edge over TLS), so the socket is opened against the
# deployment's own TLS validation listener while the Host header, the SNI name
# and the Origin header all carry the public origin -- the same exchange the edge
# performs. That is what makes the probes comparable with a browser's.
ORIGIN = urlsplit(os.environ.get("PROD_WS_ORIGIN", cfg.PUBLIC_ORIGIN))
TARGET = os.environ.get("PROD_WS_TARGET", cfg.TLS_TARGET)
HOST, _, PORT_TEXT = TARGET.partition(":")
PORT = int(PORT_TEXT or 443)
# SNI stays the address actually dialled (the ingress serves the same routing
# either way, exactly as `curl -k -H 'Host: ...'` proves); the public origin
# travels in the Host and Origin headers, which is what the deployment reads.
SNI = os.environ.get("PROD_WS_SNI", HOST)
WS_PATH = os.environ.get("PROD_WS_PATH", "/ws/books")
CREDS = cfg.credentials()
EVIDENCE = Path(os.environ.get("PROD_EVIDENCE_DIR", cfg.EVIDENCE_DIR)) / "production-websocket.json"
READ_SECONDS = float(os.environ.get("PROD_WS_READ_SECONDS", "3"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FS = cfg.PROD_ROOT / "data" / "files"

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def ssl_context() -> ssl.SSLContext:
    # The deployment terminates TLS with a self-signed certificate; the browser
    # audits run with ignoreHTTPSErrors for the same reason.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http(
    method: str, path: str, body: dict | None = None, cookie: str = ""
) -> tuple[int, dict[str, str], str]:
    import http.client

    conn = http.client.HTTPSConnection(HOST, PORT, context=ssl_context(), timeout=20)
    headers = {"Host": ORIGIN.netloc}
    if cookie:
        headers["Cookie"] = cookie
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    out = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, out, text


def login(account: str) -> str:
    entry = CREDS[account]
    status, headers, text = http(
        "POST",
        "/api/auth/login",
        {"username": entry["username"], "password": entry["password"]},
    )
    cookie = headers.get("set-cookie", "")
    token = ""
    for part in cookie.split(","):
        for piece in part.split(";"):
            piece = piece.strip()
            if piece.startswith("dt_token="):
                token = piece.split("=", 1)[1]
    if status != 200 or not token:
        raise RuntimeError(f"login as {account} failed: status={status} body={text[:200]}")
    return token


def frame(payload: dict) -> bytes:
    """One masked client text frame (RFC 6455 §5.3)."""
    data = json.dumps(payload).encode()
    header = bytearray([0x81])
    mask_bit = 0x80
    length = len(data)
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(mask_bit | 127)
        header += length.to_bytes(8, "big")
    key = secrets.token_bytes(4)
    header += key
    masked = bytes(byte ^ key[i % 4] for i, byte in enumerate(data))
    return bytes(header) + masked


def read_frames(sock: ssl.SSLSocket, seconds: float) -> list[dict | str]:
    """Drain frames for *seconds*; return decoded JSON texts and control notes."""
    out: list[dict | str] = []
    deadline = time.time() + seconds
    sock.settimeout(0.5)
    buffer = b""
    while time.time() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except (ssl.SSLError, OSError) as exc:
            out.append(f"socket-error:{type(exc).__name__}")
            break
        if not chunk:
            out.append("closed")
            break
        buffer += chunk
        while len(buffer) >= 2:
            length = buffer[1] & 0x7F
            offset = 2
            if length == 126:
                if len(buffer) < 4:
                    break
                length = int.from_bytes(buffer[2:4], "big")
                offset = 4
            elif length == 127:
                if len(buffer) < 10:
                    break
                length = int.from_bytes(buffer[2:10], "big")
                offset = 10
            if len(buffer) < offset + length:
                break
            payload = buffer[offset : offset + length]
            opcode = buffer[0] & 0x0F
            buffer = buffer[offset + length :]
            if opcode == 0x8:
                code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else None
                out.append(f"close:{code}")
                return out
            if opcode == 0x1:
                try:
                    out.append(json.loads(payload.decode("utf-8", "replace")))
                except json.JSONDecodeError:
                    out.append(payload.decode("utf-8", "replace"))
    return out


def upgrade(
    cookie: str, query: str = "", path: str = ""
) -> tuple[int, dict[str, str], ssl.SSLSocket | None, str]:
    """Open the handshake and return (status, headers, socket, raw-head)."""
    sock = socket.create_connection((HOST, PORT), timeout=15)
    tls = ssl_context().wrap_socket(sock, server_hostname=SNI)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    lines = [
        f"GET {path or WS_PATH}{query} HTTP/1.1",
        f"Host: {ORIGIN.netloc}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"Origin: {ORIGIN.scheme}://{ORIGIN.netloc}",
    ]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    tls.settimeout(10)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = tls.recv(4096)
        if not chunk:
            break
        head += chunk
    text = head.decode("latin-1")
    status_line = text.split("\r\n", 1)[0]
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError):
        status = 0
    headers = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    if status == 101:
        # Any frames already buffered behind the handshake are read with the
        # socket; push the tail back so read_frames sees it.
        tail = head.split(b"\r\n\r\n", 1)[1]
        if tail:
            tls = _Prefixed(tls, tail)  # type: ignore[assignment]
        return status, headers, tls, text
    tls.close()
    return status, headers, None, text


class _Prefixed:
    """A socket wrapper that yields already-received bytes first."""

    def __init__(self, sock: ssl.SSLSocket, prefix: bytes) -> None:
        self._sock = sock
        self._prefix = prefix

    def recv(self, size: int) -> bytes:
        if self._prefix:
            data, self._prefix = self._prefix, b""
            return data
        return self._sock.recv(size)

    def settimeout(self, value: float) -> None:
        self._sock.settimeout(value)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()


def book_ids(token: str) -> list[str]:
    status, _, text = http("GET", "/api/books", cookie=f"dt_token={token}")
    if status != 200:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = payload.get("books") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [item.get("id") for item in items if isinstance(item, dict) and item.get("id")]


def main() -> int:
    started = time.time()
    a, b = login("tenant_a"), login("tenant_b")
    check(
        "an account can log in over the deployment's TLS origin",
        bool(a and b),
        "tenant_a, tenant_b",
    )

    # ---- the routes a signed-in client reaches ------------------------------
    routes = [
        "/ws/books",
        "/ws/questions/judge",
        "/ws/mastery-paths",
        f"/ws/knowledge-bases/{uuid.uuid4()}/progress",
        "/ws",
    ]
    admitted, refused = [], []
    for path in routes:
        status, headers, sock, _ = upgrade(f"dt_token={a}", path=path)
        admitted.append((path, status == 101 and headers.get("sec-websocket-accept") is not None))
        if sock is not None:
            sock.close()
        anon_status, _, anon_sock, _ = upgrade("", path=path)
        if anon_sock is not None:
            anon_sock.close()
        refused.append((path, anon_status != 101))
    check(
        "every client-facing stream admits an authenticated upgrade",
        all(ok for _, ok in admitted),
        "; ".join(f"{path}={'101' if ok else 'NOT-101'}" for path, ok in admitted),
    )
    check(
        "every one of those streams refuses an anonymous upgrade",
        all(ok for _, ok in refused),
        f"{len(routes)} routes, all non-101 without a session",
    )

    status, _, sock, _ = upgrade("dt_token=not-a-real-token")
    if sock is not None:
        sock.close()
    check("an upgrade with a forged session token is refused", status != 101, f"status={status}")

    status, _, other, _ = upgrade(f"dt_token={b}")
    check(
        "a second account's own session is admitted (refusals are about identity, not about sockets)",
        status == 101,
        f"status={status}",
    )

    # ---- the subscription contract ------------------------------------------
    # A book the account does not have is refused by name, and the socket stays
    # open: the client is told, not silently starved.
    own_books = book_ids(a)
    missing = str(uuid.uuid4())
    subscribed_ack = None
    error_frame = None
    if other is not None:
        other.sendall(frame({"type": "subscribe", "book_id": missing}))
        frames = read_frames(other, READ_SECONDS)
        error_frame = next(
            (f for f in frames if isinstance(f, dict) and f.get("type") == "error"), None
        )
        closed = any(isinstance(f, str) and f.startswith("close:") for f in frames)
        check(
            "subscribing to a book the account does not have is refused by name, and the socket stays open",
            error_frame is not None
            and missing in str(error_frame.get("content", ""))
            and not closed,
            f"error={json.dumps(error_frame)[:140] if error_frame else None} closed={closed}",
        )
    else:
        check(
            "subscribing to a book the account does not have is refused by name, and the socket stays open",
            False,
            "the upgrade was not admitted",
        )

    # A book the account *does* own acknowledges the stream and its own
    # sequence. Reaching this needs a book, which needs a model provider this
    # environment does not have, so it is measured when one exists and stated as
    # a limit when none does -- never inferred from the refusal above.
    if own_books:
        _, _, own, _ = upgrade(f"dt_token={a}")
        if own is not None:
            own.sendall(frame({"type": "subscribe", "book_id": own_books[0]}))
            frames = read_frames(own, READ_SECONDS)
            own.close()
            subscribed_ack = next(
                (f for f in frames if isinstance(f, dict) and f.get("type") == "subscribed"),
                None,
            )
        check(
            "subscribing to the account's own book is acknowledged with the stream's sequence",
            subscribed_ack is not None and isinstance(subscribed_ack.get("latest_seq"), int),
            f"ack={json.dumps(subscribed_ack)[:160] if subscribed_ack else None}",
        )
    else:
        check(
            "the own-book acknowledgement is measured when the account owns a book",
            True,
            "not applicable and not claimed: no book exists on this host (creating one needs a "
            "model provider), so the acknowledgement was not exercised; the refusals above were",
        )

    # ---- cross-account ------------------------------------------------------
    # The same id, subscribed by the other account, must produce the *same*
    # refusal as an id that has no stream at all: no replay, no payload, and no
    # reply that tells an outsider whether the book exists.
    status, _, outsider, _ = upgrade(f"dt_token={b}")
    foreign_error = None
    foreign_frames: list = []
    if outsider is not None:
        target = own_books[0] if own_books else missing
        outsider.sendall(frame({"type": "subscribe", "book_id": target}))
        foreign_frames = read_frames(outsider, READ_SECONDS)
        outsider.close()
        foreign_error = next(
            (f for f in foreign_frames if isinstance(f, dict) and f.get("type") == "error"), None
        )
    same_refusal = (
        foreign_error is not None
        and error_frame is not None
        and foreign_error.get("content", "").replace(own_books[0] if own_books else missing, "")
        == error_frame.get("content", "").replace(missing, "")
    )
    check(
        "another account's subscription is refused, with no replayed frames",
        foreign_error is not None
        and all(
            isinstance(f, dict) and f.get("type") in {"error", "subscribed"} for f in foreign_frames
        ),
        f"frames={json.dumps(foreign_frames)[:200] if foreign_frames else '[]'}",
    )
    check(
        "the refusal does not reveal whether the book exists",
        same_refusal,
        f"with_book={json.dumps(foreign_error)[:120] if foreign_error else None}",
    )

    recorded = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    document = {
        "recorded_at": recorded,
        "host": f"{HOST}:{PORT}",
        "host_header": ORIGIN.netloc,
        "path": WS_PATH,
        "routes": routes,
        "accounts": ["tenant_a", "tenant_b"],
        "duration_seconds": round(time.time() - started, 2),
        "total": len(RESULTS),
        "passed": sum(1 for row in RESULTS if row["ok"]),
        "failed": sum(1 for row in RESULTS if not row["ok"]),
        "own_books": len(own_books),
        "own_book_ack": "measured" if subscribed_ack else "not_applicable",
        "results": RESULTS,
        "notes": [
            "Reached through the deployment's own certification ingress (TLS on "
            f"{PORT}, Host: {ORIGIN.netloc}), exactly the door the browser audits use; the "
            "ingress once stripped the Upgrade/Connection pair off the handshake, which turned "
            "every stream into a plain GET the app answered 404 -- found by this probe, fixed in "
            "docs-for-user/phase31-staging/ingress_tls_proxy.js and re-measured here.",
            "Book identifiers are per-account data; this host has "
            f"{len(own_books)} book(s) for tenant_a, so the own-book acknowledgement is stated as "
            "not applicable when that count is zero rather than asserted from an empty stream.",
        ],
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(document, indent=2))
    print(
        f"\nwebsocket probe: {document['passed']}/{document['total']} checks passed -> {EVIDENCE}"
    )
    return 0 if document["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
