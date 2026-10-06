#!/usr/bin/env python3
"""Production health check.

Complements the application's own `/health/live` and `/health/ready` (the
Dockerfile's HEALTHCHECK semantics) with the deployment-level facts an operator
or an alert rule needs:

  * liveness  — the API process answers
  * readiness — dependencies are ready (database, settings, storage)
  * frontend  — the Next.js server serves the app
  * ingress   — the validation ingress reaches the frontend over TLS
  * storage   — the data root is writable *now* (not just at startup)
  * database  — every SQLite store passes PRAGMA integrity_check
  * disk      — free space on the data root filesystem

    prod_healthcheck.py [--json] [--skip-db] [--require-ready]

Exit code is 0 only when every required probe passes; --json prints the full
report for the runbook and the alert evaluator.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


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


def timed_get(url: str) -> dict:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10, context=_tls_context()) as response:  # noqa: S310
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        status = exc.code
    except Exception as exc:
        return {"status": 0, "latency_ms": None, "error": type(exc).__name__}
    return {"status": status, "latency_ms": round((time.perf_counter() - start) * 1000, 2)}


def database_report() -> dict:
    stores, bad = [], []
    if cfg.HOME.exists():
        for path in sorted(cfg.HOME.rglob("*")):
            if not path.is_file() or path.suffix not in DB_SUFFIXES or "-wal" in path.name or "-shm" in path.name:
                continue
            try:
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
                try:
                    result = connection.execute("pragma integrity_check").fetchone()[0]
                finally:
                    connection.close()
                ok = result == "ok"
                stores.append({"path": str(path.relative_to(cfg.HOME)), "bytes": path.stat().st_size, "integrity": result})
                if not ok:
                    bad.append(str(path))
            except Exception as exc:
                stores.append({"path": str(path.relative_to(cfg.HOME)), "integrity": f"error: {type(exc).__name__}"})
                bad.append(str(path))
    return {"count": len(stores), "unhealthy": bad, "stores": stores}


def storage_report() -> dict:
    root = cfg.HOME / "data"
    report = {"root": str(root), "exists": root.exists(), "writable": False, "error": None}
    if root.exists():
        probe = root / ".healthcheck-write-probe"
        try:
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
            report["writable"] = True
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def disk_report() -> dict:
    usage = shutil.disk_usage(str(cfg.HOME if cfg.HOME.exists() else cfg.PROD_ROOT))
    return {
        "total_gb": round(usage.total / 2**30, 2),
        "free_gb": round(usage.free / 2**30, 2),
        "percent_used": round(usage.used / usage.total * 100, 2),
    }


def main() -> int:
    as_json = "--json" in sys.argv
    skip_db = "--skip-db" in sys.argv
    require_ready = "--require-ready" in sys.argv

    report = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "release": cfg.RELEASE_ID,
        "live": timed_get(f"{cfg.BACKEND}/health/live"),
        "ready": timed_get(f"{cfg.BACKEND}/health/ready"),
        "frontend": timed_get(f"{cfg.FRONTEND_PLAIN}/login"),
        "ingress": timed_get(f"{cfg.TLS_VALIDATION_ORIGIN}/login"),
        "storage": storage_report(),
        "disk": disk_report(),
    }
    report["database"] = {"skipped": True} if skip_db else database_report()

    failures: list[str] = []
    if report["live"]["status"] != 200:
        failures.append("live")
    if report["ready"]["status"] != 200:
        failures.append("ready")
    if report["frontend"]["status"] != 200:
        failures.append("frontend")
    if report["ingress"]["status"] != 200:
        failures.append("ingress")
    if not report["storage"]["writable"]:
        failures.append("storage")
    if not skip_db and report["database"]["unhealthy"]:
        failures.append("database")
    if not skip_db and report["database"]["count"] == 0:
        failures.append("database-empty")

    report["failures"] = failures
    report["healthy"] = not failures

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        for name in ("live", "ready", "frontend", "ingress"):
            entry = report[name]
            print(f"{name:9s} status={entry['status']:<3} latency_ms={entry.get('latency_ms')}")
        print(f"storage   writable={report['storage']['writable']}")
        if not skip_db:
            print(f"database  stores={report['database']['count']} unhealthy={len(report['database']['unhealthy'])}")
        print(f"disk      used={report['disk']['percent_used']}% free={report['disk']['free_gb']} GiB")
        print(f"healthy   {report['healthy']}" + (f" (failures: {', '.join(failures)})" if failures else ""))

    if require_ready and report["ready"]["status"] != 200:
        return 1
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
