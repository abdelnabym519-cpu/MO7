#!/usr/bin/env python3
"""Production alert evaluation.

The policy in `etc/alerts.json` declares one rule per operational failure mode.
Every rule carries the five fields the phase requires — condition, severity,
response, owner, verification — and is evaluated against a *measured* metrics
sample (prod_metrics.py), never against a synthetic value. No external alerting
integration exists in this environment and none is faked: the evaluator writes
`run/alerts/alerts.json` (current state) and `run/alerts/alerts.log` (state
transitions), which is what a page/on-call integration would be wired to.

    prod_alerts.py [--evaluate] [--list] [--strict] [--json]

--strict exits non-zero when a critical rule is active, which is how the
verification harness proves the evaluator fires on a real injected failure.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

ALERTS_FILE = cfg.ALERTS_DIR / "alerts.json"
ALERTS_LOG = cfg.ALERTS_DIR / "alerts.log"


def policy_path() -> Path:
    return Path(cfg.value("PROD_ALERTS_POLICY", str(cfg.ETC / "alerts.json")))


def rules() -> list[dict]:
    path = policy_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("rules", [])


def derived_metrics(metrics: dict) -> dict:
    """Metrics that need more than one sample: restart rate over the last hour."""
    derived: dict = {"restarts_last_hour": {}}
    programs = list((metrics.get("restarts") or {}).keys())
    now = metrics.get("sample_epoch", time.time())
    oldest = None
    if cfg.METRICS_DIR.joinpath("series.ndjson").exists():
        for line in (
            cfg.METRICS_DIR.joinpath("series.ndjson").read_text(errors="replace").splitlines()
        ):
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if now - entry.get("sample_epoch", now) <= 3600:
                oldest = entry
                break
    current = metrics.get("restarts") or {}
    for name in programs:
        baseline = (oldest or {}).get("restarts", {}).get(name, current.get(name, 0))
        derived["restarts_last_hour"][name] = max(0, int(current.get(name, 0)) - int(baseline or 0))
    return derived


def sample() -> dict:
    latest = cfg.METRICS_DIR / "latest.json"
    if not latest.exists():
        raise SystemExit("no metrics sample yet; run `prod.sh metrics --once` first")
    return json.loads(latest.read_text(encoding="utf-8"))


def dig(payload: dict, dotted: str):
    value: object = payload
    for part in dotted.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


OPERATORS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
    "<": lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b,
    "not-in": lambda a, b: a not in b,
}


def evaluate_rule(rule: dict, metrics: dict) -> dict:
    metric = rule["metric"]
    observed = dig(metrics, metric)
    if metric.endswith(".state") and rule.get("op") == "!=":
        # A program whose entry is missing entirely is not RUNNING either.
        observed = observed if observed is not None else "NOT-RUNNING"
    if metric == "database.unhealthy":
        # The metric is a list of failing stores; every operator works on its length.
        observed = len(observed or [])
    operator = OPERATORS[rule["op"]]
    try:
        firing = bool(operator(observed, rule["value"]))
    except TypeError:
        firing = False
    return {
        "id": rule["id"],
        "severity": rule["severity"],
        "metric": metric,
        "op": rule["op"],
        "threshold": rule["value"],
        "observed": observed,
        "firing": firing,
        "description": rule["description"],
        "response": rule["response"],
        "owner": rule["owner"],
        "verification": rule["verification"],
    }


def evaluate() -> dict:
    metrics = sample()
    metrics["derived"] = derived_metrics(metrics)
    results = [evaluate_rule(rule, metrics) for rule in rules()]
    firing = [row for row in results if row["firing"]]
    document = {
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evaluated_epoch": time.time(),
        "metrics_sampled_at": metrics.get("sampled_at"),
        "metrics_age_seconds": round(time.time() - metrics.get("sample_epoch", time.time()), 1),
        "release": metrics.get("release", {}).get("id"),
        "rules_total": len(results),
        "firing": len(firing),
        "critical_firing": len([row for row in firing if row["severity"] == "critical"]),
        "results": results,
    }
    if document["metrics_age_seconds"] > 900:
        document["stale_metrics"] = True
    return document


def write(document: dict) -> None:
    cfg.ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    previous: dict = {}
    if ALERTS_FILE.exists():
        try:
            previous = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    before = {row["id"]: row["firing"] for row in previous.get("results", [])}
    after = {row["id"]: row["firing"] for row in document["results"]}
    transitions = [
        (rule_id, after[rule_id])
        for rule_id in after
        if before.get(rule_id) is not None and before[rule_id] != after[rule_id]
    ] + [(rule_id, after[rule_id]) for rule_id in after if rule_id not in before]
    ALERTS_FILE.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if transitions:
        with ALERTS_LOG.open("a", encoding="utf-8") as handle:
            for rule_id, firing in transitions:
                row = next(item for item in document["results"] if item["id"] == rule_id)
                handle.write(
                    f"{document['evaluated_at']}  {'FIRING  ' if firing else 'CLEARED '} {row['severity']:<8} "
                    f"{rule_id}: {row['description']} (observed {row['observed']} {row['op']} {row['threshold']})\n"
                )


def main() -> int:
    if "--list" in sys.argv:
        for rule in rules():
            print(
                f"{rule['id']:<28} {rule['severity']:<8} {rule['metric']} {rule['op']} {rule['value']}"
                f"\n    response  : {rule['response']}\n    owner     : {rule['owner']}"
                f"\n    verification: {rule['verification']}"
            )
        return 0

    document = evaluate()
    write(document)
    if "--json" in sys.argv:
        print(json.dumps(document, indent=2))
    else:
        print(
            f"alerts evaluated at {document['evaluated_at']} against metrics from {document['metrics_sampled_at']}: "
            f"{document['rules_total']} rules, {document['firing']} firing "
            f"({document['critical_firing']} critical)"
        )
        for row in document["results"]:
            marker = "FIRING " if row["firing"] else "ok     "
            print(
                f"  {marker} {row['severity']:<8} {row['id']:<28} {row['metric']}="
                f"{row['observed']} {row['op']} {row['threshold']}"
            )
        if document.get("stale_metrics"):
            print("  WARNING: metrics sample is older than 15 minutes")
    if "--strict" in sys.argv and document["critical_firing"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
