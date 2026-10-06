#!/usr/bin/env python3
"""Run the repository's ruff gates over a scope and record the result.

The release pipeline lints two scopes and they mean different things:

* the **product tree** (`deeptutor`, `deeptutor_cli`, `tests`, `scripts`) is what
  the repository's own Lint job effectively covers, because it only triggers on
  product paths — anything red here blocks the release;
* the **whole checkout** is measured as well and recorded as an observation,
  because this branch carries phase documentation tooling that the repository's
  own gate has never seen. Recording it keeps the state visible instead of
  hiding it behind a narrower scope.

    rc_lint.py --scope product|repo --evidence FILE
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

PRODUCT_PATHS = ("deeptutor", "deeptutor_cli", "tests", "scripts")


def run_ruff(args: list[str], scope: str) -> tuple[int, str]:
    targets = list(PRODUCT_PATHS) if scope == "product" else ["."]
    proc = subprocess.run(["ruff", *args, *targets], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def counts(text: str) -> dict:
    errors = int(m.group(1)) if (m := re.search(r"Found (\d+) error", text)) else 0
    unformatted = (
        int(m.group(1)) if (m := re.search(r"(\d+) files? would be reformatted", text)) else 0
    )
    return {"errors": errors, "unformatted": unformatted}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("product", "repo"), required=True)
    parser.add_argument("--evidence", help="write the result document here")
    args = parser.parse_args()

    check_code, check_out = run_ruff(["check"], args.scope)
    format_code, format_out = run_ruff(["format", "--check"], args.scope)
    files = len(
        [
            p
            for p in Path(".").rglob("*.py")
            if not any(
                part in {".git", "node_modules", ".venv", "build", "dist"} for part in p.parts
            )
        ]
    )
    numbers = counts(check_out)
    numbers["unformatted"] = counts(format_out)["unformatted"]
    document = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": args.scope,
        "paths": list(PRODUCT_PATHS) if args.scope == "product" else ["."],
        "files_seen": files,
        "clean": check_code == 0 and format_code == 0,
        "lint_exit": check_code,
        "format_exit": format_code,
        **numbers,
        "lint_output_tail": "\n".join(check_out.splitlines()[-12:]),
        "format_output_tail": "\n".join(format_out.splitlines()[-6:]),
    }
    print(
        f"ruf {args.scope} scope: clean={document['clean']} errors={document['errors']} "
        f"unformatted={document['unformatted']} over {files} python files"
    )
    if args.evidence:
        Path(args.evidence).write_text(json.dumps(document, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
