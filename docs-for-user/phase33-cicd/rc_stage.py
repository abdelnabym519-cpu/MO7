#!/usr/bin/env python3
"""Run one pipeline stage, keep its output, and record what it did.

A gate that is not recorded cannot be audited, and a pipeline whose stages only
exist as lines in a YAML file cannot show what actually ran. This wrapper gives
every stage the same shape: it runs the command, tees the output to a log, writes
a small result document (stage, result, duration, exit code, failure reason), and
exits with the command's own exit code so the job still reflects reality.

    rc_stage.py --stage "the frontend deterministic gate" \
                --evidence /tmp/mo7-build/stage-frontend.json \
                --log /tmp/mo7-build/frontend.log \
                -- npm run check:fast

When a stage fails, the last lines of its output are also emitted as a GitHub
error annotation. Some stages are declared continue-on-error so that one run
reports every stage instead of stopping at the first, and the runner's own logs
are not readable from outside the runner; the annotation travels with the check
run, so the reason a stage failed is auditable rather than inferred.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def annotation(stage: str, tail: list[str]) -> str:
    """A GitHub error annotation carrying the stage's own last lines.

    Workflow commands need %, CR and LF escaped, and the title needs the
    property escapes as well; the message is bounded so one noisy stage cannot
    flood the run.
    """

    def prop(text: str) -> str:
        return (
            text.replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A")
            .replace(":", "%3A")
            .replace(",", "%2C")
        )

    def message(text: str) -> str:
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    body = message("\n".join(tail)[-1200:])
    return f"::error title={prop(stage + ' failed')}::{body}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, help="gate name recorded in the evidence")
    parser.add_argument("--evidence", required=True, help="where to write the result document")
    parser.add_argument("--log", help="where to keep the stage output")
    parser.add_argument(
        "--failure-hint",
        default="",
        help="what a failure here means, for the failure reason field",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = [c for c in args.command if c != "--"]
    if not command:
        parser.error("a command is required after --")

    started = time.time()
    proc = subprocess.run(command, capture_output=True, text=True)
    duration = round(time.time() - started, 3)
    output = (proc.stdout or "") + (proc.stderr or "")

    if args.log:
        path = Path(args.log)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)

    tail = [line for line in output.splitlines() if line.strip()][-15:]
    ok = proc.returncode == 0
    document = {
        "stage": args.stage,
        "ok": ok,
        "result": "pass" if ok else "fail",
        "exit_code": proc.returncode,
        "duration_seconds": duration,
        "command": " ".join(command),
        "failure_reason": "" if ok else (args.failure_hint or f"exit code {proc.returncode}"),
        "output_tail": "\n".join(tail),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
    Path(args.evidence).write_text(json.dumps(document, indent=2) + "\n")

    print(f"stage '{args.stage}': {'pass' if ok else 'FAIL'} in {duration}s")
    for line in tail[-5:]:
        print(f"    {line}")
    if not ok:
        print(annotation(args.stage, tail))
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
