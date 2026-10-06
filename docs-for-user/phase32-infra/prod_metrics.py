#!/usr/bin/env python3
"""Production metrics collector.

There is no external monitoring system in this environment, so the phase builds
the production-observable local baseline the prompt asks for: one JSON document
per sample, appended to a series, plus a `latest.json` the alert evaluator and
the runbook read. Every value is measured, never estimated:

  * process supervision — state, pid, uptime, RSS, CPU, open file descriptors
    (from `supervisorctl status` and /proc), restart counters
  * watchdog — running, restart count (it is what keeps supervisord alive)
  * blackbox probes — liveness, readiness, frontend and TLS ingress status and
    latency, measured by the collector itself
  * database — store count, bytes, WAL bytes, integrity failures
  * disk — usage of the deployment root, data root and filesystem
  * backups — count, newest backup age, whether it verified
  * logs — recent ERROR/Traceback lines in the supervisor-captured logs
  * secrets — modes of the deployment's secret files (never their contents)
  * identity — release, commit, artifact hash, current symlink target

    prod_metrics.py [--once|--loop] [--interval N] [--skip-db] [--json]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SERIES = cfg.METRICS_DIR / "series.ndjson"
LATEST = cfg.METRICS_DIR / "latest.json"
PROGRAMS = ("backend", "frontend", "scheduler")


def supervisorctl(*args: str) -> str:
    command = [str(cfg.PROD_ROOT / "ops-venv/bin/supervisorctl"), "-c", str(cfg.ETC / "supervisord.conf"), *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def proc_metrics(pid: int) -> dict:
    metrics: dict = {"pid": pid}
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        metrics["state"] = stat[2]
        metrics["rss_kb"] = int(stat[23]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
        metrics["threads"] = int(stat[19])
        ticks = os.sysconf("SC_CLK_TCK")
        metrics["cpu_seconds"] = round((int(stat[13]) + int(stat[14])) / ticks, 3)
    except Exception as exc:
        metrics["error"] = type(exc).__name__
    try:
        metrics["fds"] = len(os.listdir(f"/proc/{pid}/fd"))
    except Exception:
        pass
    try:
        uplink = Path(f"/proc/{pid}/limits").read_text()
        for line in uplink.splitlines():
            if line.startswith("Max open files"):
                metrics["max_open_files"] = int(line.split()[3])
    except Exception:
        pass
    return metrics


def program_metrics() -> dict:
    output = supervisorctl("status")
    programs: dict = {}
    for line in output.splitlines():
        if " " not in line:
            continue
        name, _, rest = line.partition(" ")
        if name not in PROGRAMS:
            continue
        entry: dict = {"state": rest.split()[0] if rest else "UNKNOWN"}
        if ", pid " in rest:
            try:
                entry["pid"] = int(rest.split(", pid ")[1].split(",")[0])
                entry.update(proc_metrics(entry["pid"]))
            except Exception:
                pass
        if "uptime " in rest:
            uptime = rest.split("uptime ")[-1].strip().split(":")
            try:
                parts = [int(part) for part in uptime[-3:]]
                while len(parts) < 3:
                    parts.insert(0, 0)
                entry["uptime_seconds"] = parts[0] * 3600 + parts[1] * 60 + parts[2]
            except Exception:
                pass
        programs[name] = entry
    for name in PROGRAMS:
        programs.setdefault(name, {"state": "NOT-RUNNING"})
    return programs


def restart_counts() -> dict:
    counts: dict = {"backend": 0, "frontend": 0, "scheduler": 0}
    try:
        text = cfg.SUPERVISOR_LOG.read_text(errors="replace")
    except Exception:
        return counts
    for line in text.splitlines():
        if "exited:" in line or "gave up" in line:
            for name in PROGRAMS:
                if f":{name}" in line and ("exited:" in line or "gave up" in line):
                    counts[name] += 1
    return counts


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


def probe(url: str) -> dict:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=8, context=_tls_context()) as response:  # noqa: S310
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        return {"status": 0, "latency_ms": None, "error": type(exc).__name__}
    return {"status": status, "latency_ms": round((time.perf_counter() - start) * 1000, 2)}


def database_metrics(check_integrity: bool) -> dict:
    import sqlite3

    stores = 0
    total = 0
    wal = 0
    unhealthy: list[str] = []
    for path in cfg.HOME.rglob("*") if cfg.HOME.exists() else []:
        if not path.is_file() or path.suffix not in DB_SUFFIXES:
            continue
        if path.name.endswith(("-wal", "-shm")):
            continue
        stores += 1
        total += path.stat().st_size
        for suffix in ("-wal", "-shm"):
            companion = Path(str(path) + suffix)
            if companion.exists():
                wal += companion.stat().st_size
        if check_integrity:
            try:
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
                try:
                    result = connection.execute("pragma integrity_check").fetchone()[0]
                finally:
                    connection.close()
                if result != "ok":
                    unhealthy.append(str(path.relative_to(cfg.HOME)))
            except Exception:
                unhealthy.append(str(path.relative_to(cfg.HOME)))
    return {"stores": stores, "bytes": total, "wal_bytes": wal, "unhealthy": unhealthy, "integrity_checked": check_integrity}


def disk_metrics() -> dict:
    import shutil

    usage = shutil.disk_usage(str(cfg.PROD_ROOT))
    data_usage = shutil.disk_usage(str(cfg.HOME if cfg.HOME.exists() else cfg.PROD_ROOT))
    return {
        "total_gb": round(usage.total / 2**30, 2),
        "free_gb": round(usage.free / 2**30, 2),
        "percent_used": round(usage.used / usage.total * 100, 2),
        "data_percent_used": round(data_usage.used / data_usage.total * 100, 2),
    }


def memory_metrics() -> dict:
    fields = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            fields[key] = int(value.split()[0]) // 1024
    except Exception:
        return {}
    return {
        "total_mb": fields.get("MemTotal"),
        "available_mb": fields.get("MemAvailable"),
        "swap_total_mb": fields.get("SwapTotal"),
    }


def load_metrics() -> dict:
    try:
        one, five, fifteen = os.getloadavg()
    except Exception:
        return {}
    return {"load1": round(one, 2), "load5": round(five, 2), "load15": round(fifteen, 2), "cpus": os.cpu_count()}


def backup_metrics() -> dict:
    entries = []
    if cfg.BACKUPS.exists():
        for path in sorted(cfg.BACKUPS.iterdir()):
            manifest = path / "manifest.json"
            if path.is_dir() and manifest.exists():
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
                entries.append(
                    {
                        "backup_id": path.name,
                        "age_hours": round((time.time() - manifest.stat().st_mtime) / 3600, 2),
                        "verified": bool(payload.get("verified")),
                    }
                )
    latest = entries[-1] if entries else None
    return {
        "count": len(entries),
        "latest_age_hours": latest["age_hours"] if latest else None,
        "latest_verified": latest["verified"] if latest else None,
        "latest_id": latest["backup_id"] if latest else None,
    }


def log_metrics() -> dict:
    result = {}
    for component in ("backend", "frontend"):
        path = cfg.LOG_DIR / f"{component}.log"
        errors = 0
        if path.exists():
            with path.open("rb") as handle:
                try:
                    handle.seek(-200_000, os.SEEK_END)
                except OSError:
                    handle.seek(0)
                text = handle.read().decode("utf-8", "replace")
            for line in text.splitlines()[-400:]:
                if any(marker in line for marker in ("ERROR", "CRITICAL", "Traceback", "FATAL")):
                    errors += 1
        result[component] = {"recent_error_lines": errors, "exists": path.exists(), "bytes": path.stat().st_size if path.exists() else 0}
    return result


def secret_metrics() -> dict:
    files = {
        "secrets_env": cfg.PROD_ROOT / "secrets/secrets.env",
        "credentials": cfg.CREDENTIALS_PATH,
        "auth_secret": cfg.auth_secret_file(),
    }
    report = {}
    for name, path in files.items():
        report[name] = {
            "exists": path.exists(),
            "mode": oct(path.stat().st_mode & 0o777) if path.exists() else None,
        }
    return report


def tls_metrics() -> dict:
    """Days until the *validation ingress* certificate expires.

    The production certificate belongs to the platform edge and cannot be
    inspected from inside the sandbox; the alert policy records that dependency
    explicitly (see etc/alerts.json, rule cert-expiry-production-edge).
    """
    crt = cfg.PROD_ROOT / "harness/tls/ingress.crt"
    if not crt.exists():
        return {"validation_cert_present": False, "validation_cert_days": None}
    try:
        import ssl

        info = ssl._ssl._test_decode_cert(str(crt))  # type: ignore[attr-defined]
        not_after = info.get("notAfter")
        expiry = time.mktime(time.strptime(not_after, "%b %d %H:%M:%S %Y %Z"))
        return {
            "validation_cert_present": True,
            "validation_cert_days": round((expiry - time.time()) / 86400, 2),
            "validation_cert_subject": info.get("subject"),
        }
    except Exception as exc:
        return {"validation_cert_present": True, "validation_cert_days": None, "error": type(exc).__name__}


def collect(skip_db: bool = False) -> dict:
    started = time.time()
    sample = {
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_epoch": started,
        "environment": cfg.ENVIRONMENT,
        "release": {
            "id": cfg.RELEASE_ID,
            "version": cfg.VERSION,
            "commit": cfg.SOURCE_COMMIT,
            "artifact_sha256": cfg.ARTIFACT_SHA256,
            "current_target": os.path.realpath(cfg.CURRENT_LINK) if cfg.CURRENT_LINK.exists() else None,
        },
        "programs": program_metrics(),
        "restarts": restart_counts(),
        "watchdog": watchdog_metrics(),
        "probes": {
            "live": probe(f"{cfg.BACKEND}/health/live"),
            "ready": probe(f"{cfg.BACKEND}/health/ready"),
            "frontend": probe(f"{cfg.FRONTEND_PLAIN}/login"),
            "ingress": probe(f"{cfg.TLS_VALIDATION_ORIGIN}/login"),
        },
        "database": database_metrics(not skip_db),
        "disk": disk_metrics(),
        "memory": memory_metrics(),
        "load": load_metrics(),
        "backups": backup_metrics(),
        "logs": log_metrics(),
        "secrets": secret_metrics(),
        "tls": tls_metrics(),
    }
    sample["collection_ms"] = round((time.time() - started) * 1000, 1)
    return sample


def watchdog_metrics() -> dict:
    state_file = cfg.RUN / "watchdog-state.json"
    pid_file = cfg.RUN / "watchdog.pid"
    pid = None
    for candidate in (state_file, pid_file):
        if not candidate.exists():
            continue
        try:
            if candidate.name == "watchdog-state.json":
                pid = int(json.loads(candidate.read_text()).get("pid", 0)) or pid
            else:
                pid = int(candidate.read_text().strip()) or pid
        except Exception:
            continue
    running = False
    if pid:
        try:
            os.kill(pid, 0)
            running = "prod_watchdog" in Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        except Exception:
            running = False
    restarts = 0
    if cfg.WATCHDOG_LOG.exists():
        restarts = sum(1 for line in cfg.WATCHDOG_LOG.read_text(errors="replace").splitlines() if "restart #" in line)
    return {"running": running, "pid": pid if running else None, "restarts": restarts}


def main() -> int:
    as_json = "--json" in sys.argv
    skip_db = "--skip-db" in sys.argv
    if "--loop" in sys.argv:
        interval = 60
        if "--interval" in sys.argv:
            interval = int(sys.argv[sys.argv.index("--interval") + 1])
        cfg.METRICS_DIR.mkdir(parents=True, exist_ok=True)
        while True:
            sample = collect(skip_db=skip_db)
            _write(sample, as_json)
            time.sleep(interval)
    sample = collect(skip_db=skip_db)
    _write(sample, as_json)
    return 0


def _write(sample: dict, as_json: bool) -> None:
    cfg.METRICS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(sample, indent=2) + "\n", encoding="utf-8")
    with SERIES.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample) + "\n")
    if as_json:
        print(json.dumps(sample, indent=2))
    else:
        programs = " ".join(f"{name}={data.get('state')}" for name, data in sample["programs"].items())
        probes = " ".join(f"{name}={data['status']}" for name, data in sample["probes"].items())
        print(f"{sample['sampled_at']} release={sample['release']['id']} programs[{programs}] probes[{probes}] "
              f"db={sample['database']['stores']} unhealthy={len(sample['database']['unhealthy'])} "
              f"disk={sample['disk']['percent_used']}% backups={sample['backups']['count']} "
              f"collection_ms={sample['collection_ms']}")


if __name__ == "__main__":
    raise SystemExit(main())
