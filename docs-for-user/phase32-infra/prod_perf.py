#!/usr/bin/env python3
"""Phase 32 compact production performance check.

Measures what the Phase 28 baseline measured, at production scale: warm
health/API latency (p50/p95), a concurrent burst on an authenticated endpoint,
frontend HTML latency, TLS-ingress latency, and process memory sanity. Writes
evidence/production-perf.json and exits non-zero when something is pathological
(any 5xx, latency an order of magnitude past the Phase 28 baseline, or
unbounded memory growth).
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import ssl
import statistics
import threading
import time

import prod_config as cfg

STAGING = cfg.PROD_ROOT
EVIDENCE = cfg.EVIDENCE_DIR / "production-perf.json"
CREDS = cfg.credentials()
FRONTEND = ("127.0.0.1", cfg.FRONTEND_PORT)
BACKEND = ("127.0.0.1", cfg.BACKEND_PORT)
INGRESS = ("127.0.0.1", cfg.TLS_VALIDATION_PORT)
TLS = ssl.create_default_context()
TLS.check_hostname = False
TLS.verify_mode = ssl.CERT_NONE


def login() -> str:
    conn = http.client.HTTPConnection(*BACKEND, timeout=20)
    body = json.dumps({"username": CREDS["admin"]["username"], "password": CREDS["admin"]["password"]})
    conn.request("POST", "/api/auth/login", body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    cookie = response.getheader("set-cookie") or ""
    conn.close()
    return cookie.split(";")[0]


def timed(host_port, path, cookie=None, samples=30, tls=False):
    statuses, timings = [], []
    for _ in range(samples):
        started = time.perf_counter()
        conn = (
            http.client.HTTPSConnection(*host_port, timeout=20, context=TLS)
            if tls
            else http.client.HTTPConnection(*host_port, timeout=20)
        )
        headers = {"Cookie": cookie} if cookie else {}
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)
        except Exception as exc:  # noqa: BLE001
            statuses.append(str(exc))
        finally:
            conn.close()
        timings.append((time.perf_counter() - started) * 1000)
    ordered = sorted(timings)
    return {
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
        "max_ms": round(ordered[-1], 2),
        "statuses": sorted({str(status) for status in statuses}),
    }


def burst(cookie: str, count: int = 240, workers: int = 12):
    codes: list[int] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        local: list[int] = []
        for _ in range(n):
            conn = http.client.HTTPConnection(*BACKEND, timeout=20)
            try:
                conn.request("GET", "/api/settings", headers={"Cookie": cookie})
                response = conn.getresponse()
                response.read()
                local.append(response.status)
            except Exception:
                local.append(0)
            finally:
                conn.close()
        with lock:
            codes.extend(local)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(count // workers,)) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seconds = time.perf_counter() - started
    return {
        "requests": len(codes),
        "ok": sum(1 for code in codes if code == 200),
        "errors": sum(1 for code in codes if code != 200),
        "codes": {str(code): codes.count(code) for code in sorted(set(codes))},
        "seconds": round(seconds, 3),
        "rps": round(len(codes) / seconds, 1) if seconds else None,
    }


def rss_kib(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text().strip())
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        return None
    return None


def main() -> int:
    cookie = login()
    report: dict = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    report["warm"] = {
        "backend /health/live": timed(BACKEND, "/health/live"),
        "backend /health/ready": timed(BACKEND, "/health/ready"),
        "backend /api/settings (auth)": timed(BACKEND, "/api/settings", cookie=cookie),
        "frontend /login": timed(FRONTEND, "/login"),
        "ingress(https) /login": timed(INGRESS, "/login", tls=True),
    }
    before = {
        "backend": rss_kib(STAGING / "run/backend.pid"),
        "frontend": rss_kib(STAGING / "run/frontend.pid"),
    }
    report["burst"] = burst(cookie)
    after = {
        "backend": rss_kib(STAGING / "run/backend.pid"),
        "frontend": rss_kib(STAGING / "run/frontend.pid"),
    }
    report["rss_kib"] = {
        "before": before,
        "after_burst": after,
        "backend_growth": (after["backend"] or 0) - (before["backend"] or 0),
        "frontend_growth": (after["frontend"] or 0) - (before["frontend"] or 0),
    }
    pathological = False
    for name, row in report["warm"].items():
        if any(status not in ("200",) for status in row["statuses"]):
            pathological = True
        if row["p95_ms"] > 2000:
            pathological = True
    if report["burst"]["errors"]:
        pathological = True
    if report["rss_kib"]["backend_growth"] > 200_000:
        pathological = True
    report["ok"] = not pathological
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print("\nproduction perf:", "OK" if report["ok"] else "PATHOLOGICAL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
