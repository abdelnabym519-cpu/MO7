#!/usr/bin/env python3
"""Production backup: consistent SQLite snapshots, configuration, file storage.

Backups are taken through SQLite's online backup API (`sqlite3.Connection.backup`),
which produces a transactionally consistent copy of a live database without
stopping the application and without copying a half-written WAL. Configuration
and secrets are copied with their modes preserved, file storage is archived, and
every artifact is hashed. Verification is part of the backup: each snapshot is
re-opened, `PRAGMA integrity_check` must return `ok`, and every manifest hash is
recomputed. Retention only runs after a backup verifies.

    prod_backup.py [--label LABEL] [--no-storage] [--list] [--verify DIR]
                   [--prune-only] [--json]

Exit code is 0 only for a verified backup (or a successful --list/--verify).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def policy() -> dict:
    path = Path(cfg.value("PROD_BACKUP_POLICY", str(cfg.ETC / "backup-policy.json")))
    defaults = {"retention": 7, "include_storage": True, "storage_paths": ["data/users"], "config_paths": ["data/user/settings", "data/system/auth/users.json", "data/system/auth/auth_secret"]}
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding="utf-8")))
    return defaults


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def integrity(path: Path) -> str:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            return connection.execute("pragma integrity_check").fetchone()[0]
        finally:
            connection.close()
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def find_databases() -> list[Path]:
    found = []
    for path in sorted(cfg.HOME.rglob("*")):
        if not path.is_file() or path.suffix not in DB_SUFFIXES:
            continue
        if path.name.endswith(("-wal", "-shm")) or "-journal" in path.name:
            continue
        found.append(path)
    return found


def backup_database(source: Path, destination: Path, root: Path) -> dict:
    """Consistent snapshot through SQLite's online backup API."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return {
        "path": str(source.relative_to(cfg.HOME)),
        "snapshot": str(destination.relative_to(root)),
        "bytes": destination.stat().st_size,
        "source_bytes": source.stat().st_size,
        "sha256": sha256(destination),
        "integrity": integrity(destination),
    }


def archive_storage(paths: list[str], archive: Path) -> dict:
    members = 0
    raw_bytes = 0
    with tarfile.open(archive, "w:gz") as tar:
        for relative in paths:
            root = cfg.HOME / relative
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                tar.add(path, arcname=str(path.relative_to(cfg.HOME)))
                members += 1
                raw_bytes += path.stat().st_size
    return {"archive": archive.name, "bytes": archive.stat().st_size, "files": members, "uncompressed_bytes": raw_bytes, "sha256": sha256(archive)}


def copy_configuration(paths: list[str], target_dir: Path) -> list[dict]:
    copied = []
    for relative in paths:
        source = cfg.HOME / relative
        if not source.exists():
            continue
        if source.is_dir():
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    copied.append(_copy_one(path, target_dir, relative))
        else:
            copied.append(_copy_one(source, target_dir, relative))
    return copied


def _copy_one(path: Path, target_dir: Path, relative_root: str) -> dict:
    destination = target_dir / str(path.relative_to(cfg.HOME))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    os.chmod(destination, path.stat().st_mode & 0o777)
    return {
        "path": str(path.relative_to(cfg.HOME)),
        "bytes": destination.stat().st_size,
        "mode": oct(path.stat().st_mode & 0o777),
        "sha256": sha256(destination),
    }


def run_backup(label: str, include_storage: bool) -> int:
    """Take one backup, or fail loudly and leave nothing half-written behind.

    A backup that cannot be written (no space, no permission) must not be
    recorded as successful and must not leave a directory that a later restore
    could pick up: the partial backup directory is removed and the command exits
    non-zero with the underlying error.
    """
    try:
        return _run_backup(label, include_storage)
    except OSError as exc:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        partial = cfg.BACKUPS / f"{stamp}-{label}"
        for candidate in sorted(cfg.BACKUPS.glob(f"*-{label}")) if cfg.BACKUPS.exists() else []:
            if candidate.is_dir() and not (candidate / "manifest.json").exists():
                shutil.rmtree(candidate, ignore_errors=True)
        print(f"FATAL: backup {label!r} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
            print("FATAL: the backup destination has no space left", file=sys.stderr)
        return 1


def _run_backup(label: str, include_storage: bool) -> int:
    config = policy()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_id = f"{stamp}-{label}"
    root = cfg.BACKUPS / backup_id
    (root / "databases").mkdir(parents=True, exist_ok=False)

    databases = [
        backup_database(path, root / "databases" / path.relative_to(cfg.HOME), root)
        for path in find_databases()
    ]
    config_files = copy_configuration(config.get("config_paths", []), root / "config")
    storage = None
    if include_storage and config.get("include_storage", True):
        storage = archive_storage(config.get("storage_paths", []), root / "storage.tar.gz")

    manifest = {
        "backup_id": backup_id,
        "label": label,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": cfg.ENVIRONMENT,
        "release_id": cfg.RELEASE_ID,
        "source_commit": cfg.SOURCE_COMMIT,
        "artifact_sha256": cfg.ARTIFACT_SHA256,
        "home": str(cfg.HOME),
        "host": os.uname().nodename,
        "databases": databases,
        "config": config_files,
        "storage": storage,
        "retention": config.get("retention", 7),
    }

    # --- verification is part of the backup ---
    problems = []
    for entry in databases:
        if entry["integrity"] != "ok":
            problems.append(f"{entry['path']}: integrity {entry['integrity']}")
    for entry in [*databases, *config_files, *([storage] if storage else [])]:
        if not entry:
            continue
        snapshot = None
        if "snapshot" in entry:
            snapshot = root / str(entry["snapshot"])
        elif entry.get("archive"):
            snapshot = root / entry["archive"]
        else:
            snapshot = root / "config" / entry["path"]
        if not snapshot.exists():
            problems.append(f"{entry.get('path', entry.get('archive'))}: snapshot missing")
            continue
        if sha256(snapshot) != entry["sha256"]:
            problems.append(f"{entry.get('path', entry.get('archive'))}: hash mismatch")
    manifest["verified"] = not problems
    manifest["verification_problems"] = problems
    manifest["bytes"] = sum(
        path.stat().st_size for path in root.rglob("*") if path.is_file()
    )

    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (root / "manifest.sha256").write_text(sha256(root / "manifest.json") + "\n", encoding="utf-8")

    index_entry = {
        "backup_id": backup_id,
        "created_at": manifest["created_at"],
        "verified": manifest["verified"],
        "databases": len(databases),
        "bytes": manifest["bytes"],
        "release_id": cfg.RELEASE_ID,
    }
    with (cfg.BACKUPS / "index.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(index_entry) + "\n")
    (cfg.EVIDENCE_DIR / f"backup-{backup_id}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"backup {backup_id}: {len(databases)} databases, {len(config_files)} config files, "
          f"{'storage ' + str(storage['files']) + ' files' if storage else 'no storage'}, {manifest['bytes']} bytes")
    for entry in databases:
        print(f"  {entry['path']:<58} {entry['bytes']:>9} bytes  integrity={entry['integrity']}")
    if problems:
        print("VERIFICATION FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    prune(config.get("retention", 7))
    return 0


def reconcile_index() -> dict:
    """Rewrite index.ndjson from the backups that actually exist on disk.

    A backup that never materialised (or that was pruned) must not stay in the
    index: a stale entry makes `prod.sh backups` and the verification harness
    report a backup that cannot be restored. Dropped identifiers are recorded in
    the evidence directory so the reconciliation is auditable.
    """
    index = cfg.BACKUPS / "index.ndjson"
    entries: list[dict] = []
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    on_disk = sorted(path for path in cfg.BACKUPS.iterdir() if path.is_dir())
    kept, dropped = [], []
    for entry in entries:
        directory = cfg.BACKUPS / str(entry.get("backup_id", ""))
        manifest = directory / "manifest.json"
        if not manifest.exists():
            dropped.append(entry.get("backup_id"))
            continue
        try:
            current = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            dropped.append(entry.get("backup_id"))
            continue
        entry["verified"] = current.get("verified", entry.get("verified"))
        entry["bytes"] = current.get("bytes", entry.get("bytes"))
        entry["databases"] = len(current.get("databases", [])) or entry.get("databases")
        kept.append(entry)
    known = {str(entry.get("backup_id")) for entry in kept}
    for directory in on_disk:
        if directory.name in known:
            continue
        try:
            current = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        kept.append({
            "backup_id": directory.name,
            "created_at": current.get("created_at"),
            "verified": current.get("verified"),
            "databases": len(current.get("databases", [])),
            "bytes": current.get("bytes"),
            "release_id": current.get("release_id"),
        })
    kept.sort(key=lambda entry: str(entry.get("backup_id")))
    index.write_text("".join(json.dumps(entry) + "\n" for entry in kept), encoding="utf-8")
    record = {
        "reconciled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kept": [entry["backup_id"] for entry in kept],
        "dropped": dropped,
    }
    (cfg.EVIDENCE_DIR / "backup-index-reconciled.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if dropped:
        print(f"  reconciled backup index: dropped {len(dropped)} entries with no backup on disk: {dropped}")
    return record


def prune(retention: int) -> None:
    backups = sorted([path for path in cfg.BACKUPS.iterdir() if path.is_dir()])
    if len(backups) <= retention:
        reconcile_index()
        return
    for old in backups[: len(backups) - retention]:
        manifest = old / "manifest.json"
        if manifest.exists():
            try:
                if not json.loads(manifest.read_text()).get("verified", False):
                    print(f"  keeping unverified backup for inspection: {old.name}")
                    continue
            except Exception:
                continue
        shutil.rmtree(old)
        print(f"  pruned {old.name} (retention {retention})")
    reconcile_index()


def list_backups(json_output: bool) -> int:
    entries = []
    for path in sorted(cfg.BACKUPS.iterdir()) if cfg.BACKUPS.exists() else []:
        manifest_path = path / "manifest.json"
        if not path.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            entries.append({"backup_id": path.name, "error": str(exc)})
            continue
        age_hours = round((time.time() - manifest_path.stat().st_mtime) / 3600, 2)
        entries.append(
            {
                "backup_id": manifest.get("backup_id", path.name),
                "created_at": manifest.get("created_at"),
                "verified": manifest.get("verified"),
                "databases": len(manifest.get("databases", [])),
                "bytes": manifest.get("bytes"),
                "age_hours": age_hours,
                "label": manifest.get("label"),
                "release_id": manifest.get("release_id"),
            }
        )
    if json_output:
        print(json.dumps(entries, indent=2))
    else:
        for entry in entries:
            print(f"{entry['backup_id']:<42} verified={str(entry.get('verified')):<5} "
                  f"dbs={entry.get('databases', '?'):<3} bytes={entry.get('bytes', '?'):<10} age_h={entry.get('age_hours')}")
    return 0


def verify_backup(directory: Path) -> int:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        print(f"FATAL: {directory} has no manifest.json", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for entry in manifest.get("databases", []):
        snapshot = directory / entry["snapshot"]
        if not snapshot.exists():
            problems.append(f"{entry['path']}: missing")
            continue
        if sha256(snapshot) != entry["sha256"]:
            problems.append(f"{entry['path']}: hash mismatch")
        result = integrity(snapshot)
        if result != "ok":
            problems.append(f"{entry['path']}: integrity {result}")
    for entry in manifest.get("config", []):
        snapshot = directory / "config" / entry["path"]
        if not snapshot.exists() or sha256(snapshot) != entry["sha256"]:
            problems.append(f"{entry['path']}: missing or hash mismatch")
    storage = manifest.get("storage")
    if storage:
        archive = directory / storage["archive"]
        if not archive.exists() or sha256(archive) != storage["sha256"]:
            problems.append("storage archive missing or hash mismatch")
    recorded = sha256(manifest_path)
    expected = (directory / "manifest.sha256").read_text().strip()
    if recorded != expected:
        problems.append("manifest hash mismatch")
    print(f"verify {directory.name}: {'OK' if not problems else 'FAILED'} "
          f"({len(manifest.get('databases', []))} databases)")
    for problem in problems:
        print(f"  - {problem}")
    return 1 if problems else 0


def main() -> int:
    args = sys.argv[1:]
    label = "scheduled"
    include_storage = True
    if "--label" in args:
        label = args[args.index("--label") + 1]
    if "--no-storage" in args:
        include_storage = False
    if "--list" in args:
        return list_backups("--json" in args)
    if "--verify" in args:
        return verify_backup(Path(args[args.index("--verify") + 1]))
    if "--prune-only" in args:
        prune(policy().get("retention", 7))
        return 0
    if not cfg.HOME.exists():
        print(f"FATAL: data root does not exist: {cfg.HOME}", file=sys.stderr)
        return 2
    return run_backup(label, include_storage)


if __name__ == "__main__":
    raise SystemExit(main())
