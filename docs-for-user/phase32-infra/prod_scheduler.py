#!/usr/bin/env python3
"""Production operational loop: metrics, alert evaluation, scheduled backups.

This host has no cron and no init system, so the recurring operational work runs
as a supervised program — the same lifecycle, logging and crash handling as the
application processes. Each tick:

  1. sample metrics into run/metrics/ (series + latest)
  2. evaluate the alert policy against that sample
  3. when a backup is due per etc/backup-policy.json, take a verified backup
     (SQLite online backup API + configuration + file storage)

A failed step is logged and counted, never hidden; the process exits non-zero
after repeated failures so supervisord's own FATAL state and the alert evaluator
both see it.

    prod_scheduler.py [--interval SECONDS] [--once] [--skip-backups]
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_alerts  # noqa: E402
import prod_config as cfg  # noqa: E402
import prod_metrics  # noqa: E402

LOG = cfg.RUN / "scheduler.log"


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def backup_policy() -> dict:
    path = Path(cfg.value("PROD_BACKUP_POLICY", str(cfg.ETC / "backup-policy.json")))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def backup_due(interval_hours: float) -> bool:
    latest = None
    if cfg.BACKUPS.exists():
        candidates = [path for path in cfg.BACKUPS.iterdir() if (path / "manifest.json").exists()]
        if candidates:
            latest = max(path.stat().st_mtime for path in candidates)
    if latest is None:
        return True
    return (time.time() - latest) >= interval_hours * 3600


def run_backup(label: str) -> bool:
    command = [
        str(cfg.PROD_ROOT / "ops-venv/bin/python"),
        str(Path(__file__).resolve().parent / "prod_backup.py"),
        "--label",
        label,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        log(f"backup: {line}")
    if result.returncode != 0:
        log(f"backup FAILED (exit {result.returncode}): {result.stderr.strip()[:400]}")
        return False
    log(f"backup ok ({label})")
    return True


def tick(skip_backups: bool) -> dict:
    sample = prod_metrics.collect()
    prod_metrics._write(sample, as_json=False)
    document = prod_alerts.evaluate()
    prod_alerts.write(document)
    summary = {
        "sampled_at": sample["sampled_at"],
        "programs": {name: data.get("state") for name, data in sample["programs"].items()},
        "alerts_firing": document["firing"],
        "critical_firing": document["critical_firing"],
        "backup_taken": False,
        "backup_ok": None,
    }
    if not skip_backups:
        interval = float(backup_policy().get("schedule", {}).get("backup_interval_hours", 6))
        if backup_due(interval):
            summary["backup_taken"] = True
            summary["backup_ok"] = run_backup("scheduled")
    log(
        f"tick: programs={summary['programs']} alerts_firing={summary['alerts_firing']} "
        f"(critical {summary['critical_firing']}) backup={summary['backup_ok']}"
    )
    return summary


def main() -> int:
    interval = 60.0
    if "--interval" in sys.argv:
        interval = float(sys.argv[sys.argv.index("--interval") + 1])
    skip_backups = "--skip-backups" in sys.argv
    if "--once" in sys.argv:
        tick(skip_backups)
        return 0
    log(
        f"scheduler starting (interval {interval}s, backups {'disabled' if skip_backups else 'enabled'})"
    )
    failures = 0
    while True:
        try:
            tick(skip_backups)
            failures = 0
        except Exception as exc:
            failures += 1
            log(f"scheduler tick FAILED ({failures}): {type(exc).__name__}: {exc}")
            if failures >= 5:
                log("scheduler exiting after 5 consecutive failures so supervisor marks it FATAL")
                return 1
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
