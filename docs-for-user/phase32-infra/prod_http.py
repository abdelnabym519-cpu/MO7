#!/usr/bin/env python3
"""Shared HTTP helpers for the Phase 32 production harnesses.

The production deployment is reached over its real origins (the TLS ingress and
the platform origin), never a dev server, and every request carries the same
contracts the browser does: cookie sessions, bearer tokens where the wire
probe needs them, multipart uploads, and host headers for the public origin.
"""
from __future__ import annotations

import http.client
import http.cookiejar
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

import prod_config as cfg

FRONTEND = cfg.FRONTEND
BACKEND = cfg.BACKEND
PUBLIC_HOST = cfg.public_host()

PROD_ROOT = cfg.PROD_ROOT


def contract() -> dict[str, str]:
    """The deployment's non-secret contract (`etc/production.env`).

    Harnesses take their defaults from here so a validation run needs no
    environment variables that the deployment contract does not already
    document, and can never silently validate a different deployment shape.
    """
    path = Path(os.environ.get("PROD_CONTRACT", PROD_ROOT / "etc/production.env"))
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def public_host() -> str:
    """Host (no scheme) of the deployment's public origin."""
    configured = os.environ.get("PROD_PUBLIC_HOST")
    if configured:
        return configured
    origin = contract().get("PROD_PUBLIC_ORIGIN", "")
    if not origin:
        return PUBLIC_HOST
    return urllib.parse.urlparse(origin).netloc or PUBLIC_HOST

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


class Actor:
    """One production identity: its own cookie jar, plus a bearer token when minted."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_TLS),
        )
        self.token: str | None = None

    @property
    def cookies(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.jar}


def call(
    actor: Actor | None,
    method: str,
    path: str,
    *,
    body=None,
    base: str | None = None,
    host: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
):
    """One request. Returns (status, headers, text, parsed-json-or-None)."""
    url = (base or FRONTEND) + path
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    if host:
        hdrs["Host"] = host
    if actor is not None and actor.token:
        hdrs["Authorization"] = f"Bearer {actor.token}"
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = actor.opener if actor is not None else urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_TLS)
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            head = {k.lower(): v for k, v in response.headers.items()}
            return response.status, head, text, _json(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        head = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, head, text, _json(text)
    except Exception as exc:  # transport failure is a result too
        return 0, {}, str(exc), None


def upload(
    actor: Actor,
    method: str,
    path: str,
    content: bytes,
    *,
    filename: str,
    field: str = "file",
    extra_fields=(),
    base: str | None = None,
    timeout: int = 120,
):
    boundary = "----p32" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in extra_fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    chunks.append(content)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(chunks)
    request = urllib.request.Request(
        (base or FRONTEND) + path,
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method=method,
    )
    if actor.token:
        request.add_header("Authorization", f"Bearer {actor.token}")
    try:
        with actor.opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return response.status, {k.lower(): v for k, v in response.headers.items()}, text, _json(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, text, _json(text)


def raw(
    method: str,
    path: str,
    *,
    host_header: str,
    host: str = "127.0.0.1",
    port: int = cfg.TLS_VALIDATION_PORT,
    tls: bool = True,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
):
    """Wire-level request: repeated headers survive, so cookie attributes show."""
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    if tls:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_TLS)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers={"Host": host_header, **hdrs})
        response = conn.getresponse()
        payload = response.read()
        collected: dict[str, list[str]] = {}
        for name, value in response.getheaders():
            collected.setdefault(name.lower(), []).append(value)
        return response.status, collected, payload
    finally:
        conn.close()


def joined(headers: dict[str, list[str]], name: str) -> str:
    return "; ".join(headers.get(name.lower(), []))


def _json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def login(actor: Actor, username: str, password: str, *, base: str | None = None):
    return call(actor, "POST", "/api/auth/login", body={"username": username, "password": password}, base=base)


def login_via_raw(username: str, password: str):
    return raw(
        "POST",
        "/api/auth/login",
        host_header=PUBLIC_HOST,
        body=json.dumps({"username": username, "password": password}).encode(),
    )
