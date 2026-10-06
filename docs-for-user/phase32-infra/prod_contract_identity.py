#!/usr/bin/env python3
"""Point the deployment contract at the release the host is actually running.

The contract (`etc/production.env`) carries the deployment's identity: release
id, version, artifact name, artifact hash and source commit. Every operational
tool reads it, so it must always name the release that *is* running — never a
release that was staged, refused or rolled back.

The pipeline calls this:
  * after a promotion (`prod_promote.sh`), so a forward deployment leaves the
    contract naming the new release;
  * after a rollback (`prod_rollback.sh`), so a rollback leaves it naming the
    release that was restored;
  * when a deployment is refused before it could publish anything
    (`prod_deploy.sh`), so an operator's pre-declared target does not survive a
    refusal and make later reports wrong for unrelated reasons.

An operator may still edit the identity keys before a deployment: `VERIFY` uses
the declared hash to decide whether the artifact under test is the declared one.
This tool only ever rewrites the five identity keys; every other line of the
contract, including its comments, is preserved.

    prod_contract_identity.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

CONTRACT = Path(cfg.CONTRACT_PATH)
MANIFEST = cfg.CURRENT_RELEASE_FILE

KEYS = (
    "PROD_RELEASE_ID",
    "PROD_VERSION",
    "PROD_ARTIFACT_NAME",
    "PROD_ARTIFACT_SHA256",
    "PROD_SOURCE_COMMIT",
)


def manifest_identity() -> dict[str, str]:
    manifest = json.loads(MANIFEST.read_text())
    return {
        "PROD_RELEASE_ID": manifest["release_id"],
        "PROD_VERSION": manifest["version"],
        "PROD_ARTIFACT_NAME": manifest["artifact"],
        "PROD_ARTIFACT_SHA256": manifest["artifact_sha256"],
        "PROD_SOURCE_COMMIT": manifest["source_commit"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not MANIFEST.exists():
        print(f"no promoted release recorded at {MANIFEST}; contract left untouched")
        return 0
    if not CONTRACT.exists():
        print(f"FATAL: no deployment contract at {CONTRACT}", file=sys.stderr)
        return 1

    wanted = manifest_identity()
    lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    changed: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            if key in seen:  # a duplicate key would shadow the later one: drop it
                continue
            original = line
            line = f"{key}={wanted[key]}"
            if line != original:
                changed.append(f"{key}: {original.split('=', 1)[1]} -> {wanted[key]}")
            seen.add(key)
        out.append(line)
    for key in KEYS:
        if key not in seen:
            out.append(f"{key}={wanted[key]}")
            changed.append(f"{key}: <absent> -> {wanted[key]}")

    if changed:
        print("\n".join(f"  {row}" for row in changed))
    print(f"contract names {wanted['PROD_RELEASE_ID']} (version {wanted['PROD_VERSION']})")
    if args.dry_run:
        print("dry run: contract not written")
        return 0
    CONTRACT.write_text("\n".join(out) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
