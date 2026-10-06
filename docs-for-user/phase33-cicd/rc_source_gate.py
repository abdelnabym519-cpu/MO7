#!/usr/bin/env python3
"""Source control gate: refuse to release anything but the right source.

A release is built from a commit on a branch, produced by an event, in a
repository, at a checkout — and every one of those has to be the intended one.
This gate checks them and *says which check failed*: a pipeline that only reports
"exit code 1" cannot be audited, and the reason it refused is exactly the
evidence a release record needs.

It writes `source.json` next to itself (repository, ref, branch, event, commit,
checked-out commit, tree state, version) and exits non-zero if any criterion is
unmet, so a bad source fails the pipeline closed.

    rc_source_gate.py [--out source.json] [--allow-dirty]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="source.json")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="record a dirty tree instead of refusing (never used by the pipeline)",
    )
    args = parser.parse_args()

    env = os.environ
    record = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository": env.get("GITHUB_REPOSITORY", ""),
        "ref": env.get("GITHUB_REF", ""),
        "branch": env.get("GITHUB_REF_NAME", ""),
        "event": env.get("GITHUB_EVENT_NAME", ""),
        "event_commit": env.get("GITHUB_SHA", ""),
        "checked_out_commit": git("rev-parse", "HEAD"),
        "workflow": env.get("GITHUB_WORKFLOW", ""),
        "run_id": env.get("GITHUB_RUN_ID", ""),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
        "actor": env.get("GITHUB_ACTOR", ""),
    }
    record["remote"] = git("config", "--get", "remote.origin.url")

    status = git("status", "--porcelain")
    record["tree_clean"] = not status
    record["tree_status"] = status.splitlines()[:20]

    version_source = Path("deeptutor/__version__.py")
    if version_source.is_file():
        match = re.search(
            r'__version__\s*=\s*"([^"]+)"', version_source.read_text(encoding="utf-8")
        )
        record["version"] = match.group(1) if match else ""
    else:
        record["version"] = ""
    record["version_source"] = str(version_source)

    reasons: list[str] = []
    if not re.fullmatch(r"[0-9a-f]{40}", record["event_commit"]):
        reasons.append(f"the event did not name a full commit sha ({record['event_commit']!r})")
    if record["checked_out_commit"] != record["event_commit"]:
        reasons.append(
            f"the checkout is not the event commit "
            f"(checked out {record['checked_out_commit'][:12]}, event {record['event_commit'][:12]})"
        )
    if not record["tree_clean"] and not args.allow_dirty:
        reasons.append(f"the working tree is not clean ({len(record['tree_status'])} entries)")
    if not record["version"]:
        reasons.append(f"no version is declared in {version_source}")
    if not record["branch"]:
        reasons.append("the event did not name a branch")

    record["criteria"] = {
        "event names a full commit sha": bool(
            re.fullmatch(r"[0-9a-f]{40}", record["event_commit"])
        ),
        "the checkout is the event commit": record["checked_out_commit"] == record["event_commit"],
        "the working tree is clean": record["tree_clean"] or args.allow_dirty,
        "a version is declared": bool(record["version"]),
        "the event names a branch": bool(record["branch"]),
    }
    record["ok"] = not reasons
    record["reasons"] = reasons

    Path(args.out).write_text(json.dumps(record, indent=2) + "\n")
    print(f"source gate: {'pass' if record['ok'] else 'REFUSED'}")
    for key, value in record.items():
        if key not in ("tree_status", "criteria", "reasons"):
            print(f"    {key}: {value}")
    for key, value in record["criteria"].items():
        print(f"    criterion {'PASS' if value else 'FAIL'}: {key}")
    for reason in reasons:
        print(f"    reason: {reason}")
    if record["tree_status"]:
        print("    tree status:")
        for line in record["tree_status"]:
            print(f"      {line}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
