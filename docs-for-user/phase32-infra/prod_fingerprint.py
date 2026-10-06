#!/usr/bin/env python3
"""Phase 32 production capability fingerprint.

A machine-readable digest of what this deployment can and cannot prove, so a
reader of the closure report does not have to reconstruct it from prose. It
records the deployment's own identity, the process/port boundary, the
environment's infrastructure limits (container runtime, DNS, TLS trust,
optional services) and the resulting classification. Honest by construction: it
reports the absence of infrastructure rather than pretending it exists.

Writes evidence/production-fingerprint.json and always exits 0 (it is evidence,
not a gate).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import socket
import ssl
import subprocess
import time

import prod_config as cfg

STAGING = cfg.PROD_ROOT
OUT = cfg.EVIDENCE_DIR / "production-fingerprint.json"


def exists(command: str) -> bool:
    return shutil.which(command) is not None


def read_config() -> dict[str, str]:
    config: dict[str, str] = {}
    path = STAGING / "etc/production.env"
    if not path.exists():
        return config
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    return config


def listening() -> list[str]:
    result = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, check=False)
    rows = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            rows.append(parts[3])
    return sorted(set(rows))


def tls_probe(host: str, port: int) -> dict:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=5) as raw_socket:
            with context.wrap_socket(raw_socket, server_hostname=host) as tls:
                cert = tls.getpeercert()
                return {
                    "verified_by_system_store": True,
                    "protocol": tls.version(),
                    "subject": str(cert.get("subject")) if cert else None,
                }
    except ssl.SSLCertVerificationError as exc:
        return {"verified_by_system_store": False, "verification_error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def main() -> int:
    config = read_config()
    sandbox = os.environ.get("E2B_SANDBOX_ID", "")
    document = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "deployment": {
            "environment": config.get("PROD_ENVIRONMENT"),
            "release_id": config.get("PROD_RELEASE_ID"),
            "source_commit": config.get("PROD_SOURCE_COMMIT"),
            "artifact_sha256": config.get("PROD_ARTIFACT_SHA256"),
            "public_origin": config.get("PROD_PUBLIC_ORIGIN"),
            "tls_origin": config.get("PROD_TLS_ORIGIN"),
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "sandbox_id": sandbox,
            "public_preview_origin": f"https://{config.get('PROD_FRONTEND_PORT')}-{sandbox}.e2b.app" if sandbox else None,
        },
        "infrastructure": {
            "container_runtime": exists("docker") or exists("podman"),
            "compose": exists("docker-compose") or exists("docker compose"),
            "reverse_proxy": exists("nginx") or exists("caddy") or exists("traefik"),
            "supervisor": exists("supervisord") or exists("systemctl"),
            "sqlite_cli": exists("sqlite3"),
            "redis_server": exists("redis-server"),
            "postgres": exists("psql"),
            "notes": (
                "No container runtime, packaged reverse proxy, init system or "
                "database server CLI exists in this environment. The production "
                "deployment therefore runs the release the supported way for a "
                "single host (release wheel -> venv -> uvicorn + standalone "
                "Next.js), with a local TLS ingress standing in for the platform "
                "edge. Anything that requires those components is reported as an "
                "infrastructure blocker, not as an application defect."
            ),
        },
        "tls": {
            "public_origin": tls_probe("3982-itod6pemom6o9uh7nwe24.e2b.app" if sandbox else "localhost", 443)
            if sandbox
            else None,
            "local_ingress_self_signed": tls_probe("127.0.0.1", 8443),
        },
        "listening_sockets": listening(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, indent=2))
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
