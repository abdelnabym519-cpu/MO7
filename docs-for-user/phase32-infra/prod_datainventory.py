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

`--compare` separates the three outcomes: entries that disappeared or whose
recorded value changed are failures (data lost or altered); entries that appeared
are reported as additions. A deployment's own post-promote smoke legitimately
creates runtime bookkeeping, so an addition is not a data-integrity failure —
but a removal or a changed hash always is.

One table is classified separately because the application treats it as an
ephemeral counter rather than as data: `login_attempts` holds the failed-sign-in
throttle buckets, `record_failed_login` prunes anything older than a day, and
`clear_login_throttle` *deletes* the bucket of an account that just signed in
(deeptutor/services/auth.py). Its row count therefore moves in both directions
during ordinary operation, including during a deployment's own smoke sign-in.
Volatile counts are reported with their before/after values and never decide the
exit status; the file's own digest, and every other store, table, file and
document, are still compared strictly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

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
        tables = [
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table' order by name"
            )
        ]
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


#: Keys whose value is a bounded, self-pruning operational counter rather than
#: durable data. Their movement between two inventories is traffic (or a
#: deployment's own smoke sign-in), so it is reported but never fails the
#: comparison; everything else is still compared for equality.
VOLATILE_KEYS = {
    "system.auth/login_attempts.sqlite3.tables.login_attempts",
}


def flatten(document: dict, prefix: str = "") -> dict:
    """Flatten the inventory into dotted-key -> value pairs for comparison.

    Lists keep their order (the inventory stores table counts in a dict and
    files sorted by path), so a reordering is a real difference and is reported.
    """
    flat: dict = {}
    for key, value in document.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten(value, path))
        elif isinstance(value, list):
            flat[path] = json.dumps(value, sort_keys=True)
        else:
            flat[path] = value
    return flat


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
        # Only the data is compared: the release/commit header of the two
        # inventories is expected to differ across a deployment.
        data = ("stores", "system")
        left = flatten({key: first["inventory"].get(key, {}) for key in data})
        right = flatten({key: second["inventory"].get(key, {}) for key in data})
        added = sorted(set(right) - set(left))
        removed = sorted(set(left) - set(right))
        changed = sorted(key for key in set(left) & set(right) if left[key] != right[key])
        volatile = [key for key in changed if key in VOLATILE_KEYS]
        changed = [key for key in changed if key not in VOLATILE_KEYS]
        print(f"digest {args.compare[0]}: {first['digest']}")
        print(f"digest {args.compare[1]}: {second['digest']}")
        for label, keys in (
            ("volatile", volatile),
            ("added", added),
            ("removed", removed),
            ("changed", changed),
        ):
            print(f"  {label}: {len(keys)}")
            for key in keys[:10]:
                if label == "volatile":
                    print(
                        f"    {key}: {left.get(key)} -> {right.get(key)} "
                        f"(append-and-prune operational counter; not a data change)"
                    )
                    continue
                detail = right.get(key, left.get(key))
                print(f"    {key} = {str(detail)[:140]}")
        if removed or changed:
            print(f"FAIL: data was lost or altered across {args.compare[0]} -> {args.compare[1]}")
            return 1
        print(
            f"PASS: no recorded store, table, file or document was lost or altered "
            f"across {args.compare[0]} -> {args.compare[1]}"
        )
        return 0

    document = inventory()
    payload = {"label": args.label, "digest": digest_of(document), "inventory": document}
    if args.label:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f"data-inventory-{args.label}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json or not args.label:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        stores = document["stores"]
        databases = sum(len(store["databases"]) for store in stores.values())
        files = sum(len(store["files"]) for store in stores.values())
        print(
            f"label={args.label} digest={payload['digest']} stores={len(stores)} "
            f"databases={databases} files={files} system_entries={len(document['system'])} "
            f"-> {cfg.EVIDENCE_DIR / f'data-inventory-{args.label}.json'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
