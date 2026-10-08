#!/usr/bin/env python3
"""What the pipeline recorded about the intermittent frontend gate, per run.

Reads the committed evidence of the three runs that matter and writes one
summary. Nothing here is inferred from the workflow file: every field is read
from the run's own `release.json` / `promotion.json`, which the pipeline commits
itself.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "ci-runs.json"

RUNS = {
    "37558387881": "certified run — the artifact that shipped",
    "37690935036": "refused by the frontend gate (first occurrence)",
    "37699029751": "refused by the frontend gate (second occurrence, before final stage instrumentation)",
    "37700947418": "passed on the same frontend code (evidence of intermittency)",
}


def read(run: str, name: str) -> dict:
    path = REPO / "evidence" / "ci" / run / name
    return json.loads(path.read_text()) if path.is_file() else {}


def main() -> int:
    summary = {}
    for run, note in RUNS.items():
        release = read(run, "release.json")
        promotion = read(run, "promotion.json")
        frontend = next(
            (s for s in release.get("stages", []) if s.get("stage") == "the frontend deterministic gate"),
            {},
        )
        summary[run] = {
            "note": note,
            "commit": release.get("commit"),
            "result": promotion.get("result"),
            "blocking_failures": promotion.get("blocking_failures"),
            "frontend_gate": {
                "ok": frontend.get("ok"),
                "detail": frontend.get("detail"),
                "required": frontend.get("required"),
                "blocking": frontend.get("blocking"),
                "failure_tail": frontend.get("failure_tail"),
            },
        }
    OUT.write_text(json.dumps(summary, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT}")
    for run, row in summary.items():
        gate = row["frontend_gate"]
        print(f"  {run}: verdict={row['result']} gate_ok={gate['ok']} blocking={gate.get('blocking')} tail={'yes' if gate.get('failure_tail') else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
