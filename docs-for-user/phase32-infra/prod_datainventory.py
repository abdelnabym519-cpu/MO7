#!/usr/bin/env python3
"""Production data inventory.

A read-only digest of the *data* a deployment must not lose across a deploy,
a rollback, a restart or a restore. Deploy and rollback evidence needs a
before/after comparison that is stronger than "the API answered 200", so this
tool records, per user store:

  * every SQLite database with a row count per table,
  * every uploaded file under the user's library with its sha256,
  * the settings documents with their sha256.

Volatile runtime state is deliberately excluded (build caches, logs, session
files, WAL/SHM sidecars): a release change is allowed to move those, and
including them would turn every inventory into a false positive.

    prod_datainventory.py --label before-n1 [--json]
    prod_datainventory.py --compare <label-a> <label-b>

`--compare` exits non-zero when any recorded fact changed, which is what makes
it usable as a gate in a deployment or rollback rehearsal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

SKIP_DIRS = {"runtime", "cache", "caches", "logs", "tmp", "temp", "__pycache__"}
SKIP_SUFFIXES = (".log", "-wal", "-shm", ".tmp", ".lock")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def table_counts(path: Path) -> dict[str, int] | str:
    """Row count per table; a database that cannot be read is recorded as such."""
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return f"unreadable: {type(exc).__name__}"
    try:
        tables = [row[0] for row in connection.execute(
            "select name from sqlite_master where type='table' order by name")]
        counts: dict[str, int] = {}
        for table in tables:
            counts[table] = connection.execute(f'select count(*) from "{table}"').fetchone()[0]
        return counts
    except sqlite3.Error as exc:
        return f"unreadable: {type(exc).__name__}"
    finally:
        connection.close()


def user_stores(root: Path) -> dict[str, dict]:
    stores: dict[str, dict] = {}
    for user_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        entry: dict = {"databases": {}, "files": {}, "documents": {}}
        for path in sorted(user_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(root))
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name.endswith(SKIP_SUFFIXES) or path.name.startswith("."):
                continue
            if path.suffix in {".db", ".sqlite", ".sqlite3"}:
                entry["databases"][relative] = {
                    "tables": table_counts(path),
                    "bytes": path.stat().st_size,
                }
            elif path.suffix == ".json":
                entry["documents"][relative] = {"sha256": sha256(path)}
            else:
                entry["files"][relative] = {"sha256": sha256(path), "bytes": path.stat().st_size}
        stores[user_dir.name] = entry
    return stores


def inventory() -> dict:
    users_root = cfg.HOME / "data/users"
    system_root = cfg.HOME / "data/system"
    system: dict = {}
    if system_root.exists():
        for path in sorted(system_root.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(system_root))
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name.endswith(SKIP_SUFFIXES) or path.name.startswith("."):
                # auth_secret is the deployment's signing secret: recorded by
                # hash so a rollback proves it did not rotate, never by value.
                continue
            if path.suffix in {".db", ".sqlite", ".sqlite3"}:
                system[relative] = {"tables": table_counts(path), "bytes": path.stat().st_size}
            else:
                system[relative] = {"sha256": sha256(path)}
    return {
        "release": cfg.RELEASE_ID,
        "source_commit": cfg.SOURCE_COMMIT,
        "stores": user_stores(users_root) if users_root.exists() else {},
        "system": system,
    }


def digest_of(document: dict) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="record the inventory under this label")
    parser.add_argument("--json", action="store_true", help="print the document")
    parser.add_argument("--compare", nargs=2, metavar=("LABEL_A", "LABEL_B"))
    args = parser.parse_args()

    evidence = cfg.EVIDENCE_DIR
    if args.compare:
        first = json.loads((evidence / f"data-inventory-{args.compare[0]}.json").read_text())
        second = json.loads((evidence / f"data-inventory-{args.compare[1]}.json").read_text())
        left, right = first["inventory"], second["inventory"]
        fields = ["stores", "system"]
        differences: list[str] = []
        for field in fields:
            if left.get(field) != right.get(field):
                differences.append(field)
        print(f"digest {args.compare[0]}: {first['digest']}")
        print(f"digest {args.compare[1]}: {second['digest']}")
        if differences:
            print(f"FAIL: data changed across {args.compare[0]} -> {args.compare[1]}: {differences}")
            # A reader needs to know *what* changed, not just that something did.
            for field in differences:
                left_keys = set(json.dumps(left.get(field), sort_keys=True).split(","))
                right_keys = set(json.dumps(right.get(field), sort_keys=True).split(","))
                for line in sorted(right_keys - left_keys)[:10]:
                    print(f"  only in {args.compare[1]}: {line.strip()}")
                for line in sorted(left_keys - right_keys)[:10]:
                    print(f"  only in {args.compare[0]}: {line.strip()}")
            return 1
        print(f"PASS: the recorded data is identical across {args.compare[0]} -> {args.compare[1]}")
        return 0

    document = inventory()
    payload = {"label": args.label, "digest": digest_of(document), "inventory": document}
    if args.label:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f"data-inventory-{args.label}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json or not args.label:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        stores = document["stores"]
        databases = sum(len(store["databases"]) for store in stores.values())
        files = sum(len(store["files"]) for store in stores.values())
        print(f"label={args.label} digest={payload['digest']} stores={len(stores)} "
              f"databases={databases} files={files} system_entries={len(document['system'])} "
              f"-> {cfg.EVIDENCE_DIR / f'data-inventory-{args.label}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
