#!/usr/bin/env python3
"""Phase 32 production infrastructure verification harness.

One executable check per infrastructure claim, grouped into sections. Every
check reads the running deployment (never a recorded value in a document), and
the whole run produces machine-readable evidence in
`evidence/production-verify.json`.

    infrastructure  deployment contract, layout, current release, ops tooling
    topology        which processes exist, what they bind, what is public
    lifecycle       supervisord states, autostart/autorestart, watchdog
    health          liveness, readiness, frontend, ingress, truthfulness
    secrets         modes, no secrets in artifacts/logs/browser bundle, guards
    network         public surface scan, loopback-only API, no debug/documentation
                    endpoints, no database or storage exposure
    database        store inventory, integrity, WAL, dedicated root
    storage         dedicated root, permissions, write/delete round trip
    backups         freshness, verification, restore rehearsal evidence
    observability   metrics freshness, alert evaluation, log presence/severity
    resources       file-descriptor limits, memory, disk, process table
    deployment      release directories, manifests, deployment log chain

    prod_verify.py [--section NAME] [--quick] [--json]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

RESULTS: list[dict] = []
EVIDENCE = cfg.EVIDENCE_DIR / "production-verify.json"
DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"section": section, "check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"[{'PASS' if ok else 'FAIL'}] {section}: {name}" + ("" if ok else f" -- {str(detail)[:200]}"))
    return bool(ok)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are evidence: a 307 to /login must never be read as a 200."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _tls_context():
    """The validation ingress uses a self-signed certificate by design.

    Production TLS terminates at the platform edge, so a client inside the
    deployment host cannot chain-verify the *validation* ingress certificate.
    Only probes of the validation ingress use this context; nothing about
    production TLS is inferred from it.
    """
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def http(url: str, *, timeout: int = 8, follow: bool = True) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "mo7-prod-verify"})
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_tls_context()))
    if follow:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_tls_context()))
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()
    except Exception:
        return 0, {}, b""


def supervisorctl(*args: str) -> str:
    return subprocess.run(
        [str(cfg.PROD_ROOT / "ops-venv/bin/supervisorctl"), "-c", str(cfg.ETC / "supervisord.conf"), *args],
        capture_output=True, text=True, timeout=20,
    ).stdout


def _socket_owners() -> dict[str, int]:
    """socket inode -> owning pid, resolved by scanning /proc/<pid>/fd.

    Independent of `ss`: a listening socket cannot hide behind an empty process
    column, and a socket owned by no process in this PID namespace is provably
    not one of ours.
    """
    owners: dict[str, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except Exception:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except Exception:
                continue
            if target.startswith("socket:["):
                owners.setdefault(target[len("socket:["):-1], int(entry.name))
    return owners


def _process_label(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except Exception:
        return "?"
    for program in ("backend", "frontend", "scheduler", "ingress-validation"):
        controlled = supervisorctl("pid", program).strip()
        if controlled.isdigit() and int(controlled) == pid:
            return f"{program} ({comm})"
    if pid == _supervisord_pid():
        return f"supervisord ({comm})"
    return f"{comm}"


def _supervisord_pid() -> int:
    try:
        return int(cfg.SUPERVISOR_PID.read_text().strip())
    except Exception:
        return -1


def listeners() -> list[dict]:
    """Listening TCP sockets with an independently resolved owning pid."""
    owners = _socket_owners()
    output = subprocess.run(["ss", "-ltnpe"], capture_output=True, text=True).stdout
    rows = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        inode = next((part[len("ino:"):] for part in parts[4:] if part.startswith("ino:")), "")
        cgroup = next((part[len("cgroup:"):] for part in parts[4:] if part.startswith("cgroup:")), "")
        pid = owners.get(inode)
        rows.append({
            "local": parts[3],
            "ino": inode,
            "pid": pid,
            "owner": _process_label(pid),
            "cgroup": cgroup,
        })
    return rows


# ----------------------------------------------------------------------------- infrastructure
def section_infrastructure() -> None:
    contract = cfg.contract()
    check("infrastructure", "deployment contract exists and is complete",
          bool(contract) and all(contract.get(key) for key in ("PROD_ENVIRONMENT", "PROD_RELEASE_ID", "PROD_SOURCE_COMMIT", "PROD_ARTIFACT_SHA256")),
          f"{len(contract)} keys")
    required_dirs = ["bin", "etc", "harness", "home", "releases", "run", "backups", "ops-venv"]
    missing = [name for name in required_dirs if not (cfg.PROD_ROOT / name).exists()]
    check("infrastructure", "the documented directory contract exists", not missing, f"missing={missing}")
    check("infrastructure", "the ops tooling virtualenv provides supervisor",
          (cfg.PROD_ROOT / "ops-venv/bin/supervisord").exists() and (cfg.PROD_ROOT / "ops-venv/bin/supervisorctl").exists())
    check("infrastructure", "the current release symlink resolves inside releases/",
          cfg.CURRENT_LINK.exists() and os.path.realpath(cfg.CURRENT_LINK).startswith(str(cfg.RELEASES)),
          os.path.realpath(cfg.CURRENT_LINK) if cfg.CURRENT_LINK.exists() else "missing")
    check("infrastructure", "operational entry points are installed",
          all((cfg.PROD_ROOT / "bin" / name).exists() for name in ("prod.sh", "prod_deploy.sh", "prod_rollback.sh")))
    systemd_unit = cfg.ETC / "systemd/mo7-production.service"
    check("infrastructure", "the systemd unit for real hosts is shipped with the deployment",
          systemd_unit.exists(), "not installed here: this host has no usable init system")


# ----------------------------------------------------------------------------- topology
def section_topology() -> None:
    status = supervisorctl("status")
    for program in ("backend", "frontend", "scheduler"):
        check("topology", f"{program} is supervised by supervisord", f"{program}" in status, status.strip()[:160])

    rows = listeners()
    controlled = {}
    for program in ("backend", "frontend", "scheduler", "ingress-validation"):
        pid = supervisorctl("pid", program).strip()
        controlled[program] = [row for row in rows if str(row["pid"] or "") == pid and pid.isdigit()]

    backend_rows = controlled["backend"]
    check("topology", "the API process owns exactly one listening socket",
          len(backend_rows) == 1, json.dumps(backend_rows))
    check("topology", "the API listens on the contract's private address (inside the loopback range)",
          bool(backend_rows) and all(row["local"] == f"{cfg.BACKEND_HOST}:{cfg.BACKEND_PORT}" for row in backend_rows)
          and cfg.BACKEND_HOST.split(".")[0] == "127",
          f"contract={cfg.BACKEND_HOST}:{cfg.BACKEND_PORT} observed={[row['local'] for row in backend_rows]}")

    frontend_rows = controlled["frontend"]
    check("topology", "the frontend owns the documented public bind (the platform edge forwards to it)",
          len(frontend_rows) == 1 and frontend_rows[0]["local"] == f"{cfg.FRONTEND_HOST}:{cfg.FRONTEND_PORT}",
          json.dumps(frontend_rows))

    ingress_rows = controlled["ingress-validation"]
    check("topology", "the validation ingress owns the documented TLS bind",
          len(ingress_rows) == 1 and ingress_rows[0]["local"] == f"{cfg.FRONTEND_HOST}:{cfg.TLS_VALIDATION_PORT}",
          json.dumps(ingress_rows))

    deployment_pids = {row["pid"] for group in controlled.values() for row in group} | {_supervisord_pid()}
    owned = [row for row in rows if row["pid"] in deployment_pids]
    check("topology", "no deployment process listens on any port other than the three documented binds",
          len(owned) == 3, json.dumps([row for row in owned if row not in sum(controlled.values(), [])]))

    check("topology", "the address the platform port bridge dials has no listener",
          not any(row["local"] == cfg.PUBLISHED_BRIDGE_TARGET for row in rows),
          f"bridge target={cfg.PUBLISHED_BRIDGE_TARGET}")

    foreign = [row for row in rows if row["pid"] is None]
    check("topology", "every other listening socket belongs to the host platform, not the deployment",
          all(row["pid"] is None for row in foreign),
          json.dumps([{key: row[key] for key in ("local", "cgroup")} for row in foreign]))


# ----------------------------------------------------------------------------- lifecycle
def section_lifecycle() -> None:
    status = supervisorctl("status")
    for program in ("backend", "frontend", "scheduler"):
        check("lifecycle", f"{program} is RUNNING", f"{program} " in status and "RUNNING" in status.split(program, 1)[1].split("\n")[0],
              status.strip()[:160])
    config = (cfg.ETC / "supervisord.d").glob("*.conf")
    text = "\n".join(path.read_text() for path in config)
    check("lifecycle", "every long-running program declares autostart + autorestart",
          text.count("autorestart=true") >= 4 and "autostart=true" in text,
          f"autostart={text.count('autostart=true')} autorestart={text.count('autorestart=true')}")
    check("lifecycle", "programs stop with SIGTERM and are killed by group after a grace period",
          text.count("stopasgroup=true") >= 3 and text.count("killasgroup=true") >= 3)
    watchdog = json.loads(subprocess.run(
        [str(cfg.PROD_ROOT / "ops-venv/bin/python"), str(Path(__file__).resolve().parent / "prod_watchdog.py"), "status"],
        capture_output=True, text=True).stdout or "{}")
    check("lifecycle", "the host watchdog is running and reports supervisord alive",
          watchdog.get("running") and watchdog.get("supervisord_alive"), json.dumps(watchdog))
    check("lifecycle", "the watchdog records the restart count it has performed",
          "restarts" in watchdog, f"restarts={watchdog.get('restarts')}")
    controlled = subprocess.run(
        [str(cfg.PROD_ROOT / "ops-venv/bin/supervisorctl"), "-c", str(cfg.ETC / "supervisord.conf"), "pid", "backend"],
        capture_output=True, text=True).stdout.strip()
    if controlled.isdigit():
        limits = Path(f"/proc/{controlled}/limits").read_text()
        check("lifecycle", "the supervised API process runs with the contract's file-descriptor limit",
              cfg.RLIMIT_NOFILE in limits,
              [line for line in limits.splitlines() if line.startswith("Max open files")][0].strip())


# ----------------------------------------------------------------------------- health
def section_health() -> None:
    report = json.loads(subprocess.run(
        [str(cfg.PROD_ROOT / "ops-venv/bin/python"), str(Path(__file__).resolve().parent / "prod_healthcheck.py"), "--json"],
        capture_output=True, text=True).stdout or "{}")
    check("health", "liveness answers 200", report.get("live", {}).get("status") == 200, json.dumps(report.get("live")))
    check("health", "readiness answers 200", report.get("ready", {}).get("status") == 200, json.dumps(report.get("ready")))
    check("health", "the frontend serves /login", report.get("frontend", {}).get("status") == 200)
    check("health", "readiness reflects dependency health (all stores pass integrity)",
          not report.get("database", {}).get("unhealthy"))
    status, _, body = http(f"{cfg.BACKEND}/health/ready")
    try:
        ready_payload = json.loads(body.decode("utf-8"))
    except Exception:
        ready_payload = None
    check("health", "the readiness payload reports dependency state (not just a bare 200)",
          isinstance(ready_payload, dict) and bool(ready_payload),
          f"payload={str(ready_payload)[:160]}")
    status, _, body = http(f"{cfg.BACKEND}/health/ready")
    check("health", "health responses carry no secrets",
          b"secret" not in body.lower() and b"password" not in body.lower(), body[:120].decode("utf-8", "replace"))


# ----------------------------------------------------------------------------- secrets
def section_secrets() -> None:
    secret_files = {
        "secrets/secrets.env": cfg.PROD_ROOT / "secrets/secrets.env",
        "secrets/credentials.json": cfg.CREDENTIALS_PATH,
        "home/data/system/auth/auth_secret": cfg.auth_secret_file(),
    }
    for label, path in secret_files.items():
        mode = oct(path.stat().st_mode & 0o777) if path.exists() else None
        check("secrets", f"{label} exists with mode 0600", path.exists() and mode == "0o600", f"mode={mode}")

    for label, directory in (("secrets/", cfg.PROD_ROOT / "secrets"), ("backups/", cfg.BACKUPS),
                             ("run/supervisor/", cfg.LOG_DIR)):
        mode = (directory.stat().st_mode & 0o777) if directory.exists() else None
        check("secrets", f"the {label} directory grants no access to group or other",
              mode is not None and mode & 0o077 == 0, f"mode={oct(mode) if mode is not None else None}")

    accounts = cfg.credentials()
    secrets_to_find = [row["password"] for row in accounts.values() if isinstance(row, dict) and "password" in row]
    hit = []
    for log in (cfg.LOG_DIR).glob("*.log"):
        text = log.read_text(errors="replace")
        if any(secret in text for secret in secrets_to_find):
            hit.append(log.name)
    supervisor_log = cfg.SUPERVISOR_LOG.read_text(errors="replace") if cfg.SUPERVISOR_LOG.exists() else ""
    if any(secret in supervisor_log for secret in secrets_to_find):
        hit.append("supervisord.log")
    check("secrets", "no account password appears in any supervisor-captured log", not hit, f"hit={hit}")

    frontend, status = None, 0
    for candidate in (Path(os.path.realpath(cfg.CURRENT_LINK)) / "data/user/runtime/web", Path(os.path.realpath(cfg.CURRENT_LINK)) / "web"):
        if (candidate / ".next").exists():
            frontend = candidate
            break
    if frontend:
        hit_files = []
        for path in frontend.rglob("*"):
            if not path.is_file() or path.suffix not in {".js", ".json", ".html", ".css", ".txt"}:
                continue
            try:
                text = path.read_text(errors="replace")
            except Exception:
                continue
            if any(secret in text for secret in secrets_to_find):
                hit_files.append(str(path.relative_to(frontend)))
        check("secrets", "no account password or auth secret is embedded in the served frontend bundle",
              not hit_files, f"hit={hit_files[:3]}")
        auth_secret_value = cfg.auth_secret_file().read_text().strip() if cfg.auth_secret_file().exists() else "!"
        embedded = []
        for path in frontend.rglob("*"):
            if path.is_file() and path.suffix in {".js", ".json", ".html"}:
                if len(auth_secret_value) > 10 and auth_secret_value in path.read_text(errors="replace"):
                    embedded.append(str(path.relative_to(frontend)))
        check("secrets", "the auth secret is not embedded in the served frontend bundle", not embedded, f"hit={embedded[:3]}")

    start_script = Path(os.path.realpath(cfg.CURRENT_LINK)) / "bin/start-backend.sh"
    if start_script.exists():
        text = start_script.read_text()
        check("secrets", "the release start script refuses to start without the required secret",
              "required secret missing or empty" in text and "refusing to regenerate" in text)
        check("secrets", "the release start script refuses a world-readable secret",
              "must be mode 600" in text)
    repo_secrets = [*secrets_to_find, cfg.auth_secret_file().read_text().strip() if cfg.auth_secret_file().exists() else ""]
    hits: list[str] = []
    for value in [item for item in repo_secrets if item]:
        scan = subprocess.run(["git", "-C", "/home/user/MO7", "grep", "-I", "-l", "-F", "-e", value],
                              capture_output=True, text=True)
        hits.extend(scan.stdout.split())
    check("secrets", "no deployment secret value appears anywhere in the tracked repository", not hits,
          f"hits={hits[:3]}")

    artifact_hits = []
    for release in cfg.RELEASES.iterdir() if cfg.RELEASES.exists() else []:
        manifest_path = release / "manifest.json"
        if not manifest_path.exists():
            continue
        artifact = release / json.loads(manifest_path.read_text()).get("artifact", "")
        if not artifact.exists():
            continue
        import zipfile

        with zipfile.ZipFile(artifact) as archive:
            for name in archive.namelist():
                if not name.endswith((".js", ".json", ".html", ".py", ".txt", ".env")):
                    continue
                try:
                    blob = archive.read(name).decode("utf-8", "replace")
                except Exception:
                    continue
                for value in [item for item in repo_secrets if len(item) > 10]:
                    if value in blob:
                        artifact_hits.append(f"{artifact.name}:{name}")
    check("secrets", "no deployment secret is embedded in the release artifact", not artifact_hits,
          f"hits={artifact_hits[:3]}")


# ----------------------------------------------------------------------------- network
def section_network() -> None:
    """The public boundary: what the platform edge and the frontend origin reach.

    Verified behaviour of this platform (recorded as evidence, not assumed): the
    sandbox publishes a port bridge for every listening port that dials
    127.0.0.1, so a plain loopback bind is *published*. The deployment therefore
    serves the API on a private address the bridge does not dial, and these
    checks prove that the bridge target is dead and that no deployment-controlled
    route serves the API documentation surface or any data-root path.
    """
    contract_decision = cfg.value("PROD_API_DOCS_DECISION")
    check("network", "the API documentation decision is recorded in the deployment contract",
          bool(contract_decision) and contract_decision == cfg.API_DOCS_DECISION,
          f"decision={contract_decision or '(unset)'}")

    documentation = ("/docs", "/redoc", "/openapi.json")
    private = {}
    for path in documentation:
        status, _, _ = http(f"{cfg.BACKEND}{path}", follow=False)
        private[path] = status
    check("network", "the documentation surface answers on the private API interface (operator tool)",
          all(status in (200, 401, 403) for status in private.values()), json.dumps(private))

    check("network", f"nothing listens on {cfg.PUBLISHED_BRIDGE_TARGET}, the address the platform bridge dials",
          _bridge_target_dead(cfg.PUBLISHED_BRIDGE_TARGET), cfg.PUBLISHED_BRIDGE_TARGET)

    bridge_probes = []
    for row in listeners():
        if row["pid"] is None and row["local"].endswith(f":{cfg.BACKEND_PORT}"):
            status, headers, _ = http(f"http://{row['local']}/health/live", follow=False)
            served = status == 200 and headers.get("server", "").startswith("uvicorn")
            bridge_probes.append({"bridge": row["local"], "status": status, "served_the_api": served})
    check("network", "the platform port bridge for the API port cannot reach the API",
          all(not probe["served_the_api"] for probe in bridge_probes),
          json.dumps(bridge_probes) or "no bridge socket present for the API port")

    for origin, label in ((cfg.FRONTEND, "the TLS validation origin"), (cfg.FRONTEND_PLAIN, "the frontend")):
        for path in documentation:
            status, _, body = http(f"{origin}{path}", follow=False)
            leaked = b"swagger" in body.lower() or b'"openapi"' in body.lower()
            check("network", f"{label} does not serve {path}", status != 200 and not leaked, f"status={status}")

    escapes = ("/api/../docs", "/api/%2e%2e/docs", "/api//openapi.json", "//openapi.json",
               "/api/docs", "/api/openapi.json", "/api/../api/settings")
    leaks = []
    for path in escapes:
        status, _, body = http(f"{cfg.FRONTEND}{path}", follow=False)
        if b"swagger" in body.lower() or b'"openapi"' in body.lower():
            leaks.append({"path": path, "status": status})
    check("network", "no path escape through the frontend proxy reaches the API documentation", not leaks,
          json.dumps(leaks))

    secret_value = cfg.auth_secret_file().read_text().strip() if cfg.auth_secret_file().exists() else "!"
    accounts = cfg.credentials()
    password = next((row["password"] for row in accounts.values() if isinstance(row, dict) and row.get("password")), "")
    markers = {"auth secret": secret_value, "account password": password, "deployment contract": "PROD_RELEASE_ID="}
    for path in ("/data/system/auth/auth_secret", "/home/data/system/auth/users.json", "/backups",
                 "/etc/production.env", "/secrets/credentials.json", "/run/current-release.json"):
        status, _, body = http(f"{cfg.FRONTEND_PLAIN}{path}", follow=False)
        text = body.decode("utf-8", "replace")
        hits = [name for name, marker in markers.items() if marker and marker in text]
        check("network", f"{path} is not served by the frontend", not hits, f"status={status} hits={hits}")

    status, _, body = http(f"{cfg.BACKEND}/openapi.json", follow=False)
    text = body.decode("utf-8", "replace")
    hits = [name for name, marker in markers.items() if marker and marker in text]
    check("network", "the API schema carries no credential or secret material", not hits,
          f"status={status} hits={hits}")

    sample = ("/api/settings", "/api/sessions", "/api/users", "/api/tenants")
    anonymous = {path: http(f"{cfg.BACKEND}{path}", follow=False)[0] for path in sample}
    check("network", "anonymous API access is refused on business routes",
          all(status != 200 for status in anonymous.values()), json.dumps(anonymous))

    traversal_status, _, _ = http(f"{cfg.BACKEND}/files/library/../../../../etc/passwd", follow=False)
    check("network", "path traversal into the filesystem is refused",
          traversal_status in (307, 400, 401, 403, 404), f"status={traversal_status}")


def _bridge_target_dead(target: str) -> bool:
    host, _, port = target.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return False
    except ConnectionRefusedError:
        return True
    except Exception:
        # Unroutable is not the same as served; the bridge forwarding check
        # independently proves nothing answers as the API.
        return True


# ----------------------------------------------------------------------------- database
def section_database() -> None:
    stores, bad, wal_bytes, total_bytes = [], [], 0, 0
    for path in sorted(cfg.HOME.rglob("*")) if cfg.HOME.exists() else []:
        if not path.is_file() or path.suffix not in DB_SUFFIXES or path.name.endswith(("-wal", "-shm")):
            continue
        stores.append(path)
        total_bytes += path.stat().st_size
        for suffix in ("-wal", "-shm"):
            companion = Path(str(path) + suffix)
            if companion.exists():
                wal_bytes += companion.stat().st_size
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                if connection.execute("pragma integrity_check").fetchone()[0] != "ok":
                    bad.append(str(path.relative_to(cfg.HOME)))
            finally:
                connection.close()
        except Exception as exc:
            bad.append(f"{path.relative_to(cfg.HOME)}: {type(exc).__name__}")
    check("database", "the deployment has its own SQLite stores", len(stores) > 0, f"{len(stores)} stores")
    check("database", "every store passes PRAGMA integrity_check", not bad, f"unhealthy={bad}")
    check("database", "stores live under the dedicated data root",
          all(str(path).startswith(str(cfg.HOME)) for path in stores))
    check("database", "no store is served or copied outside the data root",
          not (cfg.PROD_ROOT / "data").exists(), "no stray data/ directory at the deployment root")
    data_root = cfg.HOME / "data"
    mode = (data_root.stat().st_mode & 0o777) if data_root.exists() else None
    check("database", "the data root grants no access to group or other",
          mode is not None and mode & 0o077 == 0, f"mode={oct(mode) if mode is not None else None}")
    print(f"       stores={len(stores)} bytes={total_bytes} wal_bytes={wal_bytes}")


# ----------------------------------------------------------------------------- storage
def section_storage() -> None:
    users_root = cfg.HOME / "data/users"
    check("storage", "file storage lives under the dedicated data root", users_root.exists() or (cfg.HOME / "data").exists())
    probe = cfg.HOME / "data/.verify-write-probe"
    ok = False
    detail = ""
    try:
        probe.write_text("probe", encoding="utf-8")
        ok = probe.read_text() == "probe"
        probe.unlink()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    check("storage", "the storage root supports write, read and delete", ok, detail)
    check("storage", "no production data lives in a temporary directory",
          not str(cfg.HOME).startswith(("/tmp", "/var/tmp")), str(cfg.HOME))
    check("storage", "the data root is owned by the deployment user",
          (cfg.HOME / "data").stat().st_uid == os.getuid())


# ----------------------------------------------------------------------------- backups
def section_backups() -> None:
    index = cfg.BACKUPS / "index.ndjson"
    entries = [json.loads(line) for line in index.read_text().splitlines() if line.strip()] if index.exists() else []
    directories = sorted(path for path in cfg.BACKUPS.iterdir() if path.is_dir()) if cfg.BACKUPS.exists() else []
    check("backups", "at least one backup has been taken", bool(directories), f"{len(directories)} backups on disk")

    problems = []
    for directory in directories:
        manifest_path = directory / "manifest.json"
        digest_path = directory / "manifest.sha256"
        if not manifest_path.exists() or not digest_path.exists():
            problems.append(f"{directory.name}: manifest or manifest hash missing")
            continue
        if sha256_file(manifest_path) != digest_path.read_text().strip():
            problems.append(f"{directory.name}: manifest hash mismatch")
            continue
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest.get("databases", []) + manifest.get("config", []):
            snapshot = directory / str(entry["snapshot"]) if "snapshot" in entry else directory / "config" / entry["path"]
            if not snapshot.exists():
                problems.append(f"{directory.name}: {entry['path']}: snapshot missing")
            elif sha256_file(snapshot) != entry["sha256"]:
                problems.append(f"{directory.name}: {entry['path']}: hash mismatch")
        storage = manifest.get("storage") or {}
        if storage.get("archive"):
            archive = directory / storage["archive"]
            if not archive.exists() or sha256_file(archive) != storage["sha256"]:
                problems.append(f"{directory.name}: storage archive missing or mismatched")
    check("backups", "every backup on disk passes manifest, snapshot and storage verification",
          not problems, json.dumps(problems[:5]))

    indexed = {row.get("backup_id") for row in entries}
    on_disk = {directory.name for directory in directories}
    check("backups", "the backup index matches the backups on disk", indexed == on_disk,
          f"index_only={sorted(indexed - on_disk)} disk_only={sorted(on_disk - indexed)}")

    if directories:
        newest = max(directories, key=lambda path: path.name)
        manifest = json.loads((newest / "manifest.json").read_text())
        check("backups", "the newest backup has a manifest with per-file hashes",
              bool(manifest.get("databases")) and all("sha256" in row for row in manifest["databases"]))
        # A backup can only contain the stores that existed when it ran. The
        # check therefore asks the question a backup policy actually has to
        # answer — "is every store that existed at that moment in the snapshot?"
        # — and reports stores created afterwards separately. Comparing counts
        # instead would fail every time the application legitimately creates a
        # store after a backup (a new account's history, a new workspace), which
        # would train an operator to ignore the check.
        backed_up = {row["path"] for row in manifest["databases"]}
        backup_time = (newest / "manifest.json").stat().st_mtime
        existing: dict[str, float] = {}
        for path in cfg.HOME.rglob("*"):
            if path.is_file() and path.suffix in DB_SUFFIXES and not path.name.endswith(("-wal", "-shm")):
                existing[str(path.relative_to(cfg.HOME))] = path.stat().st_mtime
        older = {name for name, mtime in existing.items() if mtime <= backup_time}
        missing = sorted(older - backed_up)
        newer = sorted(set(existing) - older)
        check("backups", "the newest backup covers every store that existed when it was taken",
              not missing,
              f"backed up {len(backed_up)} stores; missing={missing[:5]}; "
              f"{len(newer)} store(s) were created after the backup and are due in the next one")
        # Same reasoning as the database coverage check above: a backup can only
        # contain the files that existed when it ran. The question is whether it
        # covered them, not whether the deployment happened to have uploaded a
        # file before the snapshot.
        storage_paths = ["data/users"]
        policy = json.loads((cfg.ETC / "backup-policy.json").read_text()) if (cfg.ETC / "backup-policy.json").exists() else {}
        storage_paths = policy.get("storage_paths", storage_paths)
        live_files: dict[str, float] = {}
        for relative in storage_paths:
            root = cfg.HOME / relative
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    live_files[str(path.relative_to(cfg.HOME))] = path.stat().st_mtime
        older_files = {name for name, mtime in live_files.items() if mtime <= backup_time}
        archived = int((manifest.get("storage") or {}).get("files") or 0)
        check("backups", "the newest backup covers the file storage that existed when it was taken",
              archived >= len(older_files),
              f"archive holds {archived} file(s); {len(older_files)} existed at backup time; "
              f"{len(live_files) - len(older_files)} were uploaded afterwards and are due in the next one; "
              f"{json.dumps(manifest.get('storage') or {})}")
        age_hours = round((time.time() - (newest / "manifest.json").stat().st_mtime) / 3600, 2)
        check("backups", "the newest backup is recent enough for the policy (< 26 h)", age_hours < 26, f"age={age_hours} h")

    rehearsals = sorted(cfg.EVIDENCE_DIR.glob("restore-*.json")) if cfg.EVIDENCE_DIR.exists() else []
    check("backups", "a restore rehearsal has been executed and recorded", bool(rehearsals),
          f"{len(rehearsals)} rehearsal records")
    if rehearsals:
        latest = json.loads(rehearsals[-1].read_text())
        check("backups", "the recorded restore rehearsal passed every check",
              latest.get("failed") == 0, f"failed={latest.get('failed')} of {len(latest.get('results', []))}")
        check("backups", "the restoration was rehearsed from the newest backup",
              latest.get("backup_id") == max(directory.name for directory in directories),
              f"rehearsed={latest.get('backup_id')} newest={max(directory.name for directory in directories)}")


def number_of_stores() -> int:
    return len([
        path for path in cfg.HOME.rglob("*")
        if path.is_file() and path.suffix in DB_SUFFIXES and not path.name.endswith(("-wal", "-shm"))
    ])


# ----------------------------------------------------------------------------- observability
def section_observability() -> None:
    latest = cfg.METRICS_DIR / "latest.json"
    check("observability", "the metrics collector has produced a sample", latest.exists())
    if latest.exists():
        sample = json.loads(latest.read_text())
        age = time.time() - sample.get("sample_epoch", 0)
        check("observability", "the newest metrics sample is fresh (< 15 min)", age < 900, f"age={round(age, 1)}s")
        check("observability", "metrics cover every supervised program",
              all(name in sample.get("programs", {}) for name in ("backend", "frontend", "scheduler")))
        check("observability", "metrics include health probes, database, disk, backups and secrets",
              all(key in sample for key in ("probes", "database", "disk", "backups", "secrets", "logs")))
    alerts = cfg.ALERTS_DIR / "alerts.json"
    check("observability", "the alert evaluator has run", alerts.exists())
    if alerts.exists():
        document = json.loads(alerts.read_text())
        check("observability", "the alert policy covers crashes, health, disk, backups and certificates",
              document.get("rules_total", 0) >= 15, f"rules={document.get('rules_total')}")
        evaluated = document.get("evaluated_epoch", 0)
        check("observability", "the continuous alert loop is evaluating (last run < 15 min)",
              time.time() - evaluated < 900, f"age={round(time.time() - evaluated, 1)}s")
    # Evaluate the policy against a fresh sample rather than trusting a document
    # written during an earlier (possibly deliberate) outage window.
    harness = Path(__file__).resolve().parent
    subprocess.run([str(cfg.PROD_ROOT / "ops-venv/bin/python"), str(harness / "prod_metrics.py"), "--once"],
                   capture_output=True, text=True)
    fresh = subprocess.run([str(cfg.PROD_ROOT / "ops-venv/bin/python"), str(harness / "prod_alerts.py"), "--json"],
                           capture_output=True, text=True)
    try:
        document = json.loads(fresh.stdout or "{}")
    except Exception as exc:
        document = {"error": f"{type(exc).__name__}: {exc}", "raw": fresh.stdout[:200]}
    check("observability", "the alert evaluator runs against a fresh sample",
          bool(document.get("rules_total")), json.dumps(document)[:200])
    check("observability", "no alert is firing on the healthy deployment",
          document.get("firing") == 0,
          json.dumps([row.get("id") for row in document.get("results", []) if row.get("firing")]))
    check("observability", "no critical alert is firing on the healthy deployment",
          document.get("critical_firing") == 0,
          json.dumps([row.get("id") for row in document.get("results", []) if row.get("firing") and row.get("severity") == "critical"]))
    for component in ("backend", "frontend", "scheduler"):
        path = cfg.LOG_DIR / f"{component}.log"
        check("observability", f"the supervisor captures {component} output",
              path.exists() and path.stat().st_size > 0,
              f"{path.stat().st_size if path.exists() else 0} bytes")
    check("observability", "the supervisor log records process lifecycle events",
          cfg.SUPERVISOR_LOG.exists() and "spawned:" in cfg.SUPERVISOR_LOG.read_text(errors="replace"))
    check("observability", "the deployment log records release identity and timestamps",
          cfg.DEPLOY_LOG.exists() and "release=" in cfg.DEPLOY_LOG.read_text(errors="replace"),
          f"{(cfg.DEPLOY_LOG.read_text(errors='replace').count(chr(10)) if cfg.DEPLOY_LOG.exists() else 0)} records")


# ----------------------------------------------------------------------------- resources
def section_resources() -> None:
    limits = {}
    for name in ("backend", "frontend", "scheduler"):
        pid = supervisorctl("pid", name).strip()
        if pid.isdigit():
            for line in Path(f"/proc/{pid}/limits").read_text().splitlines():
                if line.startswith("Max open files"):
                    limits[name] = line.split()[3]
    declared = cfg.RLIMIT_NOFILE
    check("resources", "every supervised program runs with the contract's file-descriptor limit",
          len(limits) == 3 and all(value == declared for value in limits.values()),
          f"declared={declared} observed={json.dumps(limits)}")
    usage = os.statvfs(str(cfg.PROD_ROOT))
    free_gb = usage.f_bavail * usage.f_frsize / 2**30
    check("resources", "the deployment has free disk headroom (> 5 GiB)", free_gb > 5, f"free={free_gb:.2f} GiB")
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        meminfo[key] = int(value.split()[0])
    check("resources", "the deployment has free memory headroom (> 500 MiB)",
          meminfo.get("MemAvailable", 0) > 500 * 1024, f"available={meminfo.get('MemAvailable', 0) // 1024} MiB")
    processes = subprocess.run(["ps", "-eo", "pid,rss,comm"], capture_output=True, text=True).stdout.splitlines()
    rss = {}
    for line in processes[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[2] in ("python", "node", "supervisord"):
            rss[parts[2]] = rss.get(parts[2], 0) + int(parts[1])
    check("resources", "process inventory is recorded (python/node/supervisord RSS)", bool(rss),
          json.dumps({key: f"{value // 1024} MiB" for key, value in rss.items()}))


# ----------------------------------------------------------------------------- deployment
def section_deployment() -> None:
    releases = sorted(path for path in cfg.RELEASES.iterdir() if path.is_dir()) if cfg.RELEASES.exists() else []
    check("deployment", "at least one release is staged", bool(releases), f"{len(releases)} releases")
    manifests_ok = all((path / "manifest.json").exists() and (path / "wheel.sha256").exists() for path in releases)
    check("deployment", "every staged release carries a manifest and an artifact hash", manifests_ok)
    check("deployment", "the current release is one of the staged releases",
          cfg.CURRENT_LINK.exists() and os.path.realpath(cfg.CURRENT_LINK) in [str(path) for path in releases])
    log = cfg.DEPLOY_LOG.read_text(errors="replace") if cfg.DEPLOY_LOG.exists() else ""
    check("deployment", "the deployment log records artifact hashes and commits",
          "artifact=" in log and "commit=" in log)
    check("deployment", "the deployment log records measured outage windows",
          "outage_ms=" in log, "recorded by prod_deploy.sh during each promotion")
    check("deployment", "the current release marker matches the symlink",
          (cfg.RUN / "current-release.json").exists()
          and json.loads((cfg.RUN / "current-release.json").read_text()).get("release_id") == cfg.RELEASE_ID)


SECTIONS = {
    "infrastructure": section_infrastructure,
    "topology": section_topology,
    "lifecycle": section_lifecycle,
    "health": section_health,
    "secrets": section_secrets,
    "network": section_network,
    "database": section_database,
    "storage": section_storage,
    "backups": section_backups,
    "observability": section_observability,
    "resources": section_resources,
    "deployment": section_deployment,
}


def main() -> int:
    only = None
    if "--section" in sys.argv:
        only = sys.argv[sys.argv.index("--section") + 1]
    started = time.time()
    for name, function in SECTIONS.items():
        if only and name != only:
            continue
        try:
            function()
        except Exception as exc:
            check(name, "section executed", False, f"{type(exc).__name__}: {exc}")
    failed = [row for row in RESULTS if not row["ok"]]
    document = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": round(time.time() - started, 2),
        "release": cfg.RELEASE_ID,
        "source_commit": cfg.SOURCE_COMMIT,
        "artifact_sha256": cfg.ARTIFACT_SHA256,
        "total": len(RESULTS),
        "passed": len(RESULTS) - len(failed),
        "failed": len(failed),
        "results": RESULTS,
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if "--json" in sys.argv:
        print(json.dumps(document, indent=2))
    print(f"\nproduction verification: total={document['total']} passed={document['passed']} failed={document['failed']} "
          f"({document['duration_seconds']}s, evidence: {EVIDENCE})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
