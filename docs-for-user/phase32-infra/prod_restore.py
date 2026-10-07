#!/usr/bin/env python3
"""Production restore: verify, restore into a target root, validate, report.

A backup that has never been restored is not certified, so this tool performs
the whole chain and refuses to report success unless every step passes:

    backup -> verify manifest and hashes -> restore into a scratch root
           -> SQLite integrity on the restored stores
           -> boot a scratch backend against the restored root
           -> authenticate with a restored account and read restored data
           -> stop the scratch backend, report

It never writes to the live data root. The target root is a rehearsal directory
(`--target`), which is how restore is validated without touching production.

    prod_restore.py --backup DIR --target DIR [--force] [--skip-app-check] [--json]
                    [--release-dir PATH]

``--release-dir`` names the release tree whose interpreter boots the restored data.
The promotion passes the release it is about to publish, which is the property a
migration gate should assert (this application, on this data). Without it the
current release is used, which on a host that has never been promoted does not
exist yet -- observed on a freshly rebuilt host, where the gate refused to promote
anything at all and the failure named a missing path rather than the real choice.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail[:300]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail[:200]}"))
    return bool(ok)


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


def free_port() -> int:
    """A free port on the private interface (never an address the platform dials)."""
    with socket.socket() as sock:
        sock.bind((cfg.BACKEND_HOST, 0))
        return sock.getsockname()[1]


def http(url: str, *, data: bytes | None = None, cookie: str | None = None, timeout: int = 10):
    headers = {"Content-Type": "application/json"} if data else {}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}", {}


def main() -> int:
    args = sys.argv[1:]
    if "--backup" not in args or "--target" not in args:
        print(__doc__, file=sys.stderr)
        return 2
    backup_dir = Path(args[args.index("--backup") + 1]).resolve()
    target = Path(args[args.index("--target") + 1]).resolve()
    force = "--force" in args
    app_check = "--skip-app-check" not in args

    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"FATAL: {backup_dir} has no manifest.json", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # --- 1. verify the backup -------------------------------------------------
    expected_manifest_hash = (backup_dir / "manifest.sha256").read_text().strip()
    check("backup manifest hash matches", sha256(manifest_path) == expected_manifest_hash)

    databases_ok = True
    for entry in manifest.get("databases", []):
        snapshot = backup_dir / entry["snapshot"]
        ok = (
            snapshot.exists()
            and sha256(snapshot) == entry["sha256"]
            and integrity(snapshot) == "ok"
        )
        databases_ok = databases_ok and ok
    check(
        f"all {len(manifest.get('databases', []))} snapshot databases verify (hash + integrity)",
        databases_ok,
    )

    # --- 2. restore into the target root --------------------------------------
    home = target / "home"
    if target.exists() and any(target.iterdir()) and not force:
        print(f"FATAL: target {target} is not empty; pass --force to replace it", file=sys.stderr)
        return 2
    if target.exists() and force:
        shutil.rmtree(target)
    (home / "data").mkdir(parents=True)

    restored_databases = 0
    for entry in manifest.get("databases", []):
        destination = home / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_dir / entry["snapshot"], destination)
        restored_databases += 1
    for entry in manifest.get("config", []):
        destination = home / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_dir / "config" / entry["path"], destination)
        os.chmod(destination, int(entry["mode"], 8))
    storage = manifest.get("storage")
    if storage:
        with tarfile.open(backup_dir / storage["archive"], "r:gz") as tar:
            tar.extractall(home)  # noqa: S202 - archive produced by prod_backup.py from this host
    check(
        f"restored {restored_databases} databases, {len(manifest.get('config', []))} config files "
        f"and file storage into {target}",
        True,
    )

    # --- 3. integrity of the restored copies ----------------------------------
    bad = [
        entry["path"]
        for entry in manifest.get("databases", [])
        if integrity(home / entry["path"]) != "ok"
    ]
    check("restored databases pass integrity_check", not bad, f"unhealthy: {bad}")

    auth_secret = home / "data/system/auth/auth_secret"
    check(
        "restored auth secret present and private",
        auth_secret.exists() and (auth_secret.stat().st_mode & 0o777) == 0o600,
        f"mode={oct(auth_secret.stat().st_mode & 0o777) if auth_secret.exists() else 'missing'}",
    )

    # --- 4. application validation against the restored root -------------------
    if app_check:
        release_dir = Path(args[args.index("--release-dir") + 1]).resolve() if "--release-dir" in args else cfg.CURRENT_LINK
        release_python = release_dir / "venv/bin/python"
        if not release_python.exists():
            print(
                "FATAL: no release to boot the restored data with: "
                f"{release_python} does not exist (pass --release-dir)",
                file=sys.stderr,
            )
            return 1
        port = free_port()
        log = target / "restore-rehearsal-backend.log"
        environment = dict(
            os.environ, DEEPTUTOR_HOME=str(home), DEEPTUTOR_IGNORE_PROCESS_ENV_OVERRIDES="1"
        )
        process = subprocess.Popen(
            [
                str(release_python),
                "-m",
                "uvicorn",
                "deeptutor.api.main:app",
                "--host",
                cfg.BACKEND_HOST,
                "--port",
                str(port),
                "--no-access-log",
                "--no-proxy-headers",
            ],
            cwd=str(release_dir),
            env=environment,
            stdout=log.open("wb"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            base = f"http://{cfg.BACKEND_HOST}:{port}"
            ready = 0
            for _ in range(60):
                status, _, _ = http(f"{base}/health/ready")
                if status == 200:
                    ready = status
                    break
                time.sleep(1)
            check(
                "scratch backend on the restored root reports ready",
                ready == 200,
                f"status={ready}",
            )

            accounts = cfg.credentials()
            status, body, headers = http(
                f"{base}/api/auth/login",
                data=json.dumps(
                    {
                        "username": accounts["admin"]["username"],
                        "password": accounts["admin"]["password"],
                    }
                ).encode(),
            )
            check(
                "a restored account can authenticate against the restored data",
                status == 200,
                f"status={status} {body[:120]}",
            )
            cookie = ""
            if status == 200:
                raw = headers.get("Set-Cookie", "")
                cookie = raw.split(";")[0]

            status, body, _ = http(f"{base}/api/notebooks", cookie=cookie)
            notebooks = None
            if status == 200:
                try:
                    notebooks = json.loads(body).get("notebooks")
                except Exception:
                    notebooks = None
            check(
                "restored notebook data is readable",
                status == 200 and isinstance(notebooks, list),
                f"status={status} notebooks={type(notebooks).__name__}",
            )

            status, body, _ = http(f"{base}/api/reading/workspaces/index", cookie=cookie)
            check(
                "restored reading data is readable", status == 200, f"status={status} {body[:80]}"
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
        check("scratch backend stopped cleanly", process.returncode is not None)

    failed = [row for row in RESULTS if not row["ok"]]
    report = {
        "restored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backup": str(backup_dir),
        "target": str(target),
        "backup_id": manifest.get("backup_id"),
        "databases": restored_databases,
        "results": RESULTS,
        "passed": len(RESULTS) - len(failed),
        "failed": len(failed),
    }
    evidence = (
        cfg.EVIDENCE_DIR / f"restore-{manifest.get('backup_id', 'unknown')}-{int(time.time())}.json"
    )
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if "--json" in args:
        print(json.dumps(report, indent=2))
    print(
        f"\nrestore rehearsal: total={len(RESULTS)} passed={len(RESULTS) - len(failed)} failed={len(failed)} "
        f"(evidence: {evidence})"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
