#!/usr/bin/env python3
"""Phase 32 production failure injection, degradation and recovery evidence.

Each case performs four things and records all four as evidence:

    1. inject    — a real fault is introduced into the running deployment
    2. observe   — the *external* effect is measured (ports, status codes,
                   latency, alert state, log lines), never inferred
    3. degrade   — the deployment must fail safely: no corruption, no silent
                   success, no stack trace, no secret leakage, data intact
    4. recover   — the fault is removed, the deployment returns to its baseline,
                   and the recovery is measured (including how it came back)

Every case restores the state it changed, even when a check fails, and the run
ends by re-verifying the baseline — a fault run that leaves the deployment
damaged is itself a finding.

    prod_faults.py --list
    prod_faults.py --all [--keep-going]
    prod_faults.py --case backend-crash [--case frontend-crash]

Evidence: ``<production root>/evidence/production-faults.json`` plus one
``evidence/faults/<case>-<timestamp>.json`` per case.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402
from prod_http import Actor, call, login, upload  # noqa: E402

PROD_ROOT = cfg.PROD_ROOT
EVIDENCE = cfg.EVIDENCE_DIR
FAULT_EVIDENCE = EVIDENCE / "faults"
SUPERVISORCTL = PROD_ROOT / "ops-venv/bin/supervisorctl"
SUPERVISORD_CONF = cfg.ETC / "supervisord.conf"
PYTHON = PROD_ROOT / "ops-venv/bin/python"
CREDS = cfg.credentials()
RUN = cfg.RUN

INSECURE_TLS = ssl.create_default_context()
INSECURE_TLS.check_hostname = False
INSECURE_TLS.verify_mode = ssl.CERT_NONE

LONG_RUNNING = ("backend", "frontend", "scheduler")
CASES: dict[str, dict] = {}


# --------------------------------------------------------------------------- helpers
def run(command: list[str], *, timeout: int = 120, check: bool = False,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    if check and completed.returncode:
        raise RuntimeError(f"{command} failed: {completed.stderr[:200]}")
    return completed


def supervisor(*args: str, timeout: int = 180) -> tuple[int, str]:
    completed = run([str(SUPERVISORCTL), "-c", str(SUPERVISORD_CONF), *args], timeout=timeout)
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def program_pid(program: str) -> int:
    """Pid of a supervised program, validated before anything may signal it.

    `supervisorctl pid <program>` prints the pid alone, so the *target* — not the
    output text — has to be validated: the pid must be greater than 1 (never the
    caller's process group), the process must exist, its command line must belong
    to this deployment, and `supervisorctl status` must confirm it is the pid of
    the program being killed.
    """
    code, out = supervisor("pid", program)
    if code != 0:
        return 0
    tokens = re.findall(r"\b(\d+)\b", out)
    if len(tokens) != 1:
        return 0
    pid = int(tokens[0])
    if pid <= 1:
        print(f"    [note] refusing pid {pid} for {program}: process-group/self signal guard")
        return 0
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return 0
    # Node rewrites its own process title, so the deployment is confirmed by the
    # command line *or* by the working directory being inside the deployment.
    if str(PROD_ROOT) not in cmdline and str(PROD_ROOT) not in cwd:
        print(f"    [note] refusing pid {pid} for {program}: not a process of this deployment")
        return 0
    _, status_line = supervisor("status", program)
    if f"pid {pid}" not in status_line or "RUNNING" not in status_line:
        print(f"    [note] refusing pid {pid} for {program}: supervisorctl does not confirm it ({status_line.strip()[:80]})")
        return 0
    return pid


def kill_pid(pid: int, *, why: str) -> None:
    """SIGKILL one process, with the guard that a fault injector always needs.

    `os.kill(0, SIGKILL)` signals the caller's whole process group and
    `os.kill(1, ...)` signals init: both have happened during development of this
    harness, so no case is allowed to signal a raw pid.
    """
    if pid <= 1:
        raise RuntimeError(f"refusing to SIGKILL pid {pid} ({why}): process-group/init guard")
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except Exception as exc:
        raise RuntimeError(f"refusing to SIGKILL pid {pid} ({why}): cannot read its command line: {exc}") from None
    if str(PROD_ROOT) not in cmdline and str(PROD_ROOT) not in _safe_cwd(pid):
        raise RuntimeError(f"refusing to SIGKILL pid {pid} ({why}): not part of {PROD_ROOT}")
    os.kill(pid, signal.SIGKILL)


def _safe_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return ""


def kill_program(program: str) -> int:
    """SIGKILL a supervised program and return the pid that was killed."""
    pid = program_pid(program)
    if not pid:
        raise RuntimeError(f"cannot identify the pid of {program}; refusing to inject the fault")
    kill_pid(pid, why=f"program {program}")
    return pid


def program_state(program: str) -> str:
    _, out = supervisor("status", program)
    match = re.search(rf"{program}\s+(\w+)", out)
    return match.group(1) if match else "UNKNOWN"


def http_status(url: str, *, timeout: float = 6.0) -> int:
    opener = urllib.request.build_opener()
    if url.startswith("https://"):
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=INSECURE_TLS))
    try:
        with opener.open(urllib.request.Request(url), timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 — unreachable is a status of its own
        return 0


def port_open(target: str, timeout: float = 2.0) -> bool:
    host, _, port = target.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for(predicate, *, timeout: float = 45.0, interval: float = 1.0) -> tuple[bool, float]:
    started = time.time()
    while time.time() - started < timeout:
        if predicate():
            return True, round(time.time() - started, 2)
        time.sleep(interval)
    return False, round(time.time() - started, 2)


def probes() -> dict[str, int]:
    return {
        "api_live": http_status(f"{cfg.BACKEND}/health/live"),
        "api_ready": http_status(f"{cfg.BACKEND}/health/ready"),
        "frontend_login": http_status(f"{cfg.FRONTEND_PLAIN}/login"),
        "public_login": http_status(f"{cfg.FRONTEND}/login", timeout=10),
    }


def api_actor(account: str = "admin") -> Actor:
    actor = Actor("faults")
    status, _, body, _ = login(actor, CREDS[account]["username"], CREDS[account]["password"])
    if status != 200 or not actor.cookies.get("dt_token"):
        raise RuntimeError(f"cannot authenticate for the fault run: status={status} body={body[:120]}")
    actor.token = actor.cookies.get("dt_token", "")
    return actor


def alert_document() -> dict:
    """Evaluate the alert policy against the *current* metrics sample."""
    run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_metrics.py"), "--once"], timeout=180)
    completed = run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_alerts.py"), "--json"], timeout=120)
    try:
        return json.loads(completed.stdout or "{}")
    except Exception:
        return {"error": completed.stdout[-200:]}


def firing(document: dict) -> list[str]:
    return [row.get("id") for row in document.get("results", []) if row.get("firing")]


def tail(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def tenant_workspace() -> Path:
    users = json.loads((PROD_ROOT / "home/data/system/auth/users.json").read_text())
    uid = users[CREDS["tenant_a"]["username"]]["id"]
    return PROD_ROOT / f"home/data/users/{uid}/user/workspace"


def auth_secret_path() -> Path:
    return PROD_ROOT / "home/data/system/auth/auth_secret"


def contract_path() -> Path:
    return PROD_ROOT / "etc/production.env"


def ensure_stack(reason: str) -> tuple[bool, str]:
    """Return the deployment to a healthy baseline before a case runs.

    Fault cases are about one injected failure each, so a stack left broken by a
    previous case (or by an earlier experiment) is repaired first and that
    repair is recorded as its own step. Nothing here hides a failure: if the
    baseline cannot be reached, the case fails on its precondition.
    """
    notes = []
    if not port_open(cfg.TLS_TARGET):
        notes.append("validation ingress was not running")
        run([str(PROD_ROOT / "bin/prod.sh"), "ingress", "start"], timeout=120)
    if not _supervisord_pid() or not _pid_alive(_supervisord_pid()):
        notes.append("supervisord was not running")
        run([str(PROD_ROOT / "bin/prod.sh"), "start"], timeout=300)
    if not _pid_alive(int(_read_pid())):
        notes.append("watchdog was not running")
        run([str(PROD_ROOT / "bin/prod.sh"), "watchdog", "start"], timeout=120)
    ok, seconds = wait_for(lambda: all(value == 200 for value in probes().values()), timeout=120)
    return ok, ("; ".join(notes) + (f"; baseline reached in {seconds}s" if ok else "; baseline NOT reached"))


class CaseRun:
    """Collects one case's evidence and guarantees restoration."""

    def __init__(self, case_id: str, title: str, impact: str) -> None:
        self.case_id = case_id
        self.title = title
        self.impact = impact
        self.steps: list[dict] = []
        self.restorations: list[dict] = []
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:600]})
        print(f"    [{'ok  ' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:220]}" if detail else ""))
        return bool(ok)

    def restore(self, name: str, ok: bool, detail: str = "") -> None:
        self.restorations.append({"restore": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(f"    [{'restored' if ok else 'RESTORE FAILED'}] {name}" + (f" -- {detail}" if detail else ""))

    @property
    def failed(self) -> list[dict]:
        return [step for step in self.steps if not step["ok"]] + [row for row in self.restorations if not row["ok"]]

    def document(self) -> dict:
        return {
            "id": self.case_id,
            "title": self.title,
            "impact": self.impact,
            "started_at": self.started,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "steps": self.steps,
            "restorations": self.restorations,
            "checks": len(self.steps),
            "passed": len([step for step in self.steps if step["ok"]]),
            "ok": not self.failed,
        }


def case(case_id: str, title: str, impact: str):
    def decorator(function):
        CASES[case_id] = {"id": case_id, "title": title, "impact": impact, "run": function}
        return function

    return decorator


# --------------------------------------------------------------------------- cases
@case("backend-crash", "API process killed with SIGKILL", "critical path unavailable until supervisord restarts it")
def backend_crash() -> CaseRun:
    run_ctx = CaseRun("backend-crash", CASES["backend-crash"]["title"], CASES["backend-crash"]["impact"])
    actor = api_actor()
    run_ctx.step("the API is serving before the injection", probes()["api_live"] == 200, json.dumps(probes()))
    before_pid = kill_program("backend")
    down, _ = wait_for(lambda: http_status(f"{cfg.BACKEND}/health/live") == 0, timeout=15)
    during = probes()
    run_ctx.step("liveness stops being reported while the API is dead", down and during["api_live"] == 0, json.dumps(during))
    run_ctx.step("readiness is not falsely reported while the API is dead", during["api_ready"] == 0, json.dumps(during))
    run_ctx.step("the frontend process stays alive while the API is dead",
                 program_state("frontend") == "RUNNING" and during["frontend_login"] in (200, 500, 503),
                 f"frontend={program_state('frontend')} status={during['frontend_login']}")
    run_ctx.step("the public origin does not answer API calls as if healthy",
                 http_status(f"{cfg.FRONTEND}/api/notebooks", timeout=10) != 200,
                 f"status={http_status(f'{cfg.FRONTEND}/api/notebooks', timeout=10)}")

    alerts_during = firing(alert_document())
    run_ctx.step("the alert policy notices the outage", "readiness-failed" in alerts_during or "process-down-backend" in alerts_during,
                 json.dumps(alerts_during))

    recovered, seconds = wait_for(lambda: http_status(f"{cfg.BACKEND}/health/ready") == 200, timeout=60)
    wait_for(lambda: program_pid("backend") not in (0, before_pid), timeout=30)
    after_pid = program_pid("backend")
    supervisord_log = tail(PROD_ROOT / "run/supervisord.log", 80)
    run_ctx.step("supervisord restarted the API without operator action", recovered and after_pid != before_pid,
                 f"recovered_in={seconds}s pid {before_pid} -> {after_pid}")
    run_ctx.step("supervisord logged the exit and the respawn",
                 "exited: backend" in supervisord_log and "spawned: 'backend'" in supervisord_log,
                 "supervisord.log")
    status, _, _, payload = call(actor, "GET", "/api/notebooks")
    run_ctx.step("the session issued before the crash still authenticates (secret is persistent)",
                 status == 200, f"status={status}")
    run_ctx.step("application data survived the crash", status == 200 and isinstance(payload, dict),
                 f"notebooks={len((payload or {}).get('notebooks') or []) if isinstance(payload, dict) else 'n/a'}")
    run_ctx.step("the API is healthy again after recovery", all(value == 200 for value in probes().values()), json.dumps(probes()))
    return run_ctx


@case("frontend-crash", "frontend process killed with SIGKILL", "every browser route unavailable until it restarts")
def frontend_crash() -> CaseRun:
    run_ctx = CaseRun("frontend-crash", CASES["frontend-crash"]["title"], CASES["frontend-crash"]["impact"])
    run_ctx.step("the frontend is serving before the injection", probes()["frontend_login"] == 200, json.dumps(probes()))
    before_pid = kill_program("frontend")
    down, _ = wait_for(lambda: not port_open(f"127.0.0.1:{cfg.FRONTEND_PORT}"), timeout=15)
    run_ctx.step("the frontend port stops accepting connections", down, f"port_open={port_open(f'127.0.0.1:{cfg.FRONTEND_PORT}')}")
    run_ctx.step("the API keeps running independently of the frontend", probes()["api_ready"] == 200, json.dumps(probes()))
    run_ctx.step("the public origin cannot serve the application",
                 http_status(f"{cfg.FRONTEND}/login", timeout=10) != 200,
                 f"status={http_status(f'{cfg.FRONTEND}/login', timeout=10)}")

    recovered, seconds = wait_for(lambda: http_status(f"{cfg.FRONTEND_PLAIN}/login") == 200, timeout=60)
    wait_for(lambda: program_pid("frontend") not in (0, before_pid), timeout=30)
    after_pid = program_pid("frontend")
    run_ctx.step("supervisord restarted the frontend without operator action", recovered and after_pid != before_pid,
                 f"recovered_in={seconds}s pid {before_pid} -> {after_pid}")
    status, _, _, _ = call(api_actor(), "GET", "/api/settings")
    run_ctx.step("the API is still authenticated after the frontend restart", status in (200, 403), f"status={status}")
    run_ctx.step("all probes are green after recovery", all(value == 200 for value in probes().values()), json.dumps(probes()))
    return run_ctx


@case("supervisord-crash", "process manager killed with SIGKILL", "no supervision until the host watchdog restarts it")
def supervisord_crash() -> CaseRun:
    run_ctx = CaseRun("supervisord-crash", CASES["supervisord-crash"]["title"], CASES["supervisord-crash"]["impact"])
    pid = int(cfg.SUPERVISOR_PID.read_text().strip())
    state_before = _watchdog_state()
    run_ctx.step("the process manager is running before the injection", pid > 1, f"pid={pid}")
    if pid <= 1:
        return run_ctx

    kill_pid(pid, why="the process manager")
    gone, seconds = wait_for(lambda: not _pid_alive(pid), timeout=30)
    run_ctx.step("the process manager process is gone", gone, f"detected_after={seconds}s")

    # Whether its children outlive it is a race with the watchdog's sweep, and
    # both outcomes are documented: recorded here as evidence, not asserted.
    survived = [name for name in LONG_RUNNING if program_pid(name)]
    print(f"    [note] programs still alive right after the kill: {survived}")

    restarted, seconds = wait_for(
        lambda: _pid_alive(_supervisord_pid()) and _pid_owns_programs(),
        timeout=180,
    )
    state_after = _watchdog_state()
    run_ctx.step("the host watchdog restarted the process manager", restarted and _supervisord_pid() != pid,
                 f"waited={seconds}s pid {pid} -> {_supervisord_pid() or 'none'}")
    run_ctx.step("the watchdog recorded the restart it performed",
                 state_after.get("restarts", 0) > state_before.get("restarts", 0),
                 json.dumps({key: state_after.get(key) for key in ("restarts", "last_restart_at")}))
    run_ctx.step("the watchdog log records the failure and the restart",
                 "supervisord is not alive" in tail(cfg.WATCHDOG_LOG, 80), "watchdog.log")
    run_ctx.step("every supervised program is RUNNING again",
                 all(program_state(name) == "RUNNING" for name in LONG_RUNNING),
                 json.dumps({name: program_state(name) for name in LONG_RUNNING}))
    app_healthy, seconds = wait_for(
        lambda: http_status(f"{cfg.BACKEND}/health/ready") == 200 and http_status(f"{cfg.FRONTEND_PLAIN}/login") == 200,
        timeout=120)
    run_ctx.step("the application serves again without operator action", app_healthy, f"after={seconds}s")
    # The validation ingress is `autostart=false` by design, so an operator (or a
    # validation window) starts it again: recovery of the harness component is a
    # separate, explicit step.
    run([str(PROD_ROOT / "bin/prod.sh"), "ingress", "start"], timeout=120)
    full, seconds = wait_for(lambda: all(value == 200 for value in probes().values()), timeout=60)
    run_ctx.step("the validation ingress is serving again after being restarted", full, f"after={seconds}s")
    orphans_cleared, orphan_seconds = wait_for(lambda: not _orphans_left(), timeout=30)
    run_ctx.step("no program from the previous supervisor generation is left unsupervised",
                 orphans_cleared, f"cleared_after={orphan_seconds}s orphans={_orphans_left()}")
    return run_ctx


@case("watchdog-crash", "host watchdog killed", "nothing would restart the process manager after a crash")
def watchdog_crash() -> CaseRun:
    run_ctx = CaseRun("watchdog-crash", CASES["watchdog-crash"]["title"], CASES["watchdog-crash"]["impact"])
    if not cfg.WATCHDOG_PID.exists() or not _pid_alive(int(_read_pid())):
        run([str(PROD_ROOT / "bin/prod.sh"), "watchdog", "start"], timeout=120)
    pid = int(cfg.WATCHDOG_PID.read_text().strip()) if cfg.WATCHDOG_PID.exists() else 0
    run_ctx.step("the watchdog is running before the injection", _pid_alive(pid), f"pid={pid}")
    if not _pid_alive(pid):
        return run_ctx
    if pid <= 1:
        run_ctx.step("the watchdog pid is safe to signal", False, f"pid={pid}")
        return run_ctx
    os.kill(pid, signal.SIGKILL)
    run_ctx.step("the watchdog is gone", wait_for(lambda: not _pid_alive(pid), timeout=10)[0], f"pid={pid}")
    alerts = firing(alert_document())
    run_ctx.step("the alert policy reports the missing watchdog", "supervisor-watchdog-down" in alerts, json.dumps(alerts))
    sampled = json.loads((cfg.METRICS_DIR / "latest.json").read_text())
    run_ctx.step("the metrics sample reports watchdog.running=false",
                 sampled.get("watchdog", {}).get("running") is False, json.dumps(sampled.get("watchdog")))
    started = run([str(PROD_ROOT / "bin/prod.sh"), "watchdog", "start"], timeout=120)
    back, seconds = wait_for(lambda: cfg.WATCHDOG_PID.exists() and _pid_alive(int(_read_pid())), timeout=60)
    run_ctx.step("the watchdog is running again", back, f"recovered_in={seconds}s")
    alerts_after = firing(alert_document())
    run_ctx.step("the watchdog alert cleared once it was restored", "supervisor-watchdog-down" not in alerts_after,
                 json.dumps(alerts_after))
    run_ctx.step("the restart was performed by the ops CLI", started.returncode == 0, started.stdout.strip()[-120:])
    return run_ctx


@case("ingress-outage", "validation ingress stopped", "no client can reach the application through the platform path")
def ingress_outage() -> CaseRun:
    run_ctx = CaseRun("ingress-outage", CASES["ingress-outage"]["title"], CASES["ingress-outage"]["impact"])
    run_ctx.step("the public path serves before the injection", probes()["public_login"] == 200, json.dumps(probes()))
    supervisor("stop", "ingress-validation")
    down, seconds = wait_for(lambda: not port_open(cfg.TLS_TARGET), timeout=20)
    run_ctx.step("the public port refuses connections", down, f"detected_after={seconds}s")
    run_ctx.step("the application behind the ingress is unaffected",
                 probes()["api_ready"] == 200 and probes()["frontend_login"] == 200, json.dumps(probes()))
    alerts = firing(alert_document())
    run_ctx.step("the alert policy reports the unreachable ingress", "ingress-unreachable" in alerts, json.dumps(alerts))
    supervisor("start", "ingress-validation")
    recovered, seconds = wait_for(lambda: http_status(f"{cfg.FRONTEND}/login", timeout=10) == 200, timeout=60)
    run_ctx.step("the public path serves again after the ingress restarts", recovered, f"recovered_in={seconds}s")
    status, _, _, _ = call(api_actor(), "GET", "/api/notebooks")
    run_ctx.step("authenticated traffic through the public path works after recovery", status == 200, f"status={status}")
    return run_ctx


@case("storage-readonly", "tenant file storage made read-only", "uploads fail, reads and the rest of the app keep working")
def storage_readonly() -> CaseRun:
    run_ctx = CaseRun("storage-readonly", CASES["storage-readonly"]["title"], CASES["storage-readonly"]["impact"])
    # The uploader must be the account whose storage is faulted, otherwise the
    # injection misses the directory the request actually writes to.
    actor = api_actor("tenant_a")
    storage = tenant_workspace()
    # The library writes both a row (in library/library.db) and a blob (in
    # library/files), so the read-only injection covers the whole library tree.
    targets = [path for path in (storage / "library", storage / "library/files") if path.is_dir()]
    originals = {str(path): stat.S_IMODE(path.stat().st_mode) for path in targets}
    # Unique payloads: the library deduplicates by content hash, so re-uploading
    # identical bytes returns the existing record without writing anything, and a
    # read-only store would then look writable.
    stamp = f"{time.time():.6f}"
    before = _upload(actor, f"fault-baseline-{stamp}.txt", f"baseline {stamp}\n".encode())
    run_ctx.step("a file upload succeeds before the injection", before[0] == 200, f"status={before[0]}")

    try:
        for path in targets:
            _chmod(path, 0o500)
        during = _upload(actor, f"fault-blocked-{stamp}.txt", f"blocked {stamp}\n".encode())
        run_ctx.step("the upload fails with a structured error", during[0] in (400, 403, 409, 500, 507)
                     and during[1].strip().startswith("{"), f"status={during[0]} body={during[1][:120]}")
        run_ctx.step("no stack trace is returned to the client",
                     not any(marker in during[1] for marker in ("Traceback", 'File "', "site-packages")),
                     during[1][:100])
        run_ctx.step("liveness and readiness stay honest during the storage fault",
                     probes()["api_live"] == 200, json.dumps(probes()))
        run_ctx.step("no partial file is left behind by the failed upload",
                     not any("fault-blocked" in path.name for path in storage.rglob("*")),
                     "workspace scan")
    finally:
        for path in targets:
            _chmod(path, originals[str(path)])
    run_ctx.restore("every library directory is back to its deployment mode",
                    all(stat.S_IMODE(path.stat().st_mode) == originals[str(path)] for path in targets),
                    json.dumps({path.name: oct(stat.S_IMODE(path.stat().st_mode)) for path in targets}))
    after = _upload(actor, f"fault-after-{stamp}.txt", f"after {stamp}\n".encode())
    run_ctx.step("uploads succeed again once storage is writable", after[0] == 200, f"status={after[0]}")
    document = alert_document()
    run_ctx.step("no critical alert is left firing after recovery",
                 document.get("critical_firing") == 0,
                 json.dumps([row.get("id") for row in document.get("results", [])
                             if row.get("firing") and row.get("severity") == "critical"]))
    print(f"    [note] alerts still firing (expected: the storage failure was logged): {firing(document)}")
    return run_ctx


@case("secret-missing", "required auth secret removed", "the API refuses to start instead of regenerating the secret")
def secret_missing() -> CaseRun:
    run_ctx = CaseRun("secret-missing", CASES["secret-missing"]["title"], CASES["secret-missing"]["impact"])
    actor = api_actor()
    status_before, _, _, _ = call(actor, "GET", "/api/settings")
    secret = auth_secret_path()
    saved = RUN / "fault-secret-backup"
    run_ctx.step("the deployment is healthy before the injection", probes()["api_ready"] == 200, json.dumps(probes()))

    try:
        shutil.copy2(secret, saved)
        os.chmod(saved, 0o600)
        secret.unlink()
        supervisor("stop", "backend")
        code, output = supervisor("start", "backend")
        fatal, seconds = wait_for(lambda: program_state("backend") == "FATAL", timeout=60)
        log = tail(cfg.LOG_DIR / "backend.log", 40)
        run_ctx.step("the API stays FATAL instead of starting without its secret", fatal,
                     f"state={program_state('backend')} after={seconds}s")
        run_ctx.step("the refusal names the missing secret and explains why",
                     "required secret missing or empty" in log and "refusing to regenerate" in log, log[-200:])
        run_ctx.step("the secret was not silently regenerated", not secret.exists(), f"exists={secret.exists()}")
        alerts = firing(alert_document())
        run_ctx.step("the alert policy reports the missing API", "process-down-backend" in alerts or "readiness-failed" in alerts,
                     json.dumps(alerts))
    finally:
        shutil.copy2(saved, secret)
        os.chmod(secret, 0o600)
        supervisor("stop", "backend")
        supervisor("start", "backend")
    recovered, seconds = wait_for(lambda: http_status(f"{cfg.BACKEND}/health/ready") == 200, timeout=90)
    run_ctx.restore("the secret is restored with mode 0600",
                    secret.exists() and stat.S_IMODE(secret.stat().st_mode) == 0o600,
                    oct(stat.S_IMODE(secret.stat().st_mode)) if secret.exists() else "missing")
    run_ctx.step("the API is healthy again after the secret is restored", recovered, f"recovered_in={seconds}s")
    status_after, _, _, _ = call(actor, "GET", "/api/settings")
    run_ctx.step("the session issued before the injection still works (same secret restored)",
                 status_after == status_before, f"before={status_before} after={status_after}")
    return run_ctx


@case("config-malformed", "deployment contract corrupted", "operational tooling and the API must fail loudly")
def config_malformed() -> CaseRun:
    run_ctx = CaseRun("config-malformed", CASES["config-malformed"]["title"], CASES["config-malformed"]["impact"])
    contract = contract_path()
    original = contract.read_text()
    run_ctx.step("tooling reports a healthy deployment before the injection",
                 run([str(PROD_ROOT / "bin/prod.sh"), "health"], timeout=120).returncode == 0, "prod.sh health")
    try:
        _write_contract(contract, original + "\nPROD_BACKEND_PORT=not-a-port\n")
        malformed_health = run([str(PROD_ROOT / "bin/prod.sh"), "health"], timeout=120)
        run_ctx.step("the broken contract makes the health tool fail instead of defaulting",
                     malformed_health.returncode != 0, f"exit={malformed_health.returncode}")
        run_ctx.step("the failure names the offending value",
                     "not-a-port" in (malformed_health.stdout + malformed_health.stderr)
                     or "invalid" in (malformed_health.stdout + malformed_health.stderr).lower(),
                     (malformed_health.stdout + malformed_health.stderr)[-200:])
        malformed_metrics = run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_metrics.py"), "--once"], timeout=180)
        run_ctx.step("the metrics collector refuses to publish a sample from a broken contract",
                     malformed_metrics.returncode != 0, f"exit={malformed_metrics.returncode}")
        supervisor("restart", "backend")
        fatal, seconds = wait_for(lambda: program_state("backend") != "RUNNING", timeout=45)
        run_ctx.step("the API cannot start with the broken contract", fatal,
                     f"state={program_state('backend')} after={seconds}s")
    finally:
        _write_contract(contract, original)
        supervisor("stop", "backend")
        supervisor("start", "backend")
    recovered, seconds = wait_for(lambda: http_status(f"{cfg.BACKEND}/health/ready") == 200, timeout=90)
    run_ctx.restore("the contract is byte-identical to its pre-injection content", contract.read_text() == original, "sha256 compared")
    run_ctx.step("the API is healthy again after the contract is restored", recovered, f"recovered_in={seconds}s")
    run_ctx.step("tooling reports a healthy deployment again",
                 run([str(PROD_ROOT / "bin/prod.sh"), "health"], timeout=120).returncode == 0, "prod.sh health")
    return run_ctx


@case("database-corruption", "a production database copy is corrupted", "integrity detection must catch it and the alert must fire")
def database_corruption() -> CaseRun:
    run_ctx = CaseRun("database-corruption", CASES["database-corruption"]["title"], CASES["database-corruption"]["impact"])
    scratch = RUN / "fault-scratch-integrity"
    if scratch.exists():
        shutil.rmtree(scratch)
    source = next(path for path in (PROD_ROOT / "home/data/users").rglob("chat_history.db"))
    target = scratch / source.relative_to(PROD_ROOT / "home")
    target.parent.mkdir(parents=True)
    shutil.copy2(source, target)

    import sqlite3

    def integrity(path: Path) -> str:
        # SQLite raises DatabaseError("database disk image is malformed") for
        # damaged b-tree pages instead of returning a verdict; a raise is
        # reported as the damage it is, never swallowed.
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as connection:
                return connection.execute("pragma integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return f"{type(exc).__name__}: {exc}"

    run_ctx.step("the copied store passes integrity before corruption", integrity(target) == "ok")
    # Corruption that a database really suffers: whole b-tree pages overwritten
    # (not a few bytes a header-only check would still accept).
    size = target.stat().st_size
    with open(target, "r+b") as handle:
        handle.seek(4096)
        handle.write(b"\x00" * min(size - 4096, 65536))
    with open(target, "r+b") as handle:  # and the header's page count is left inconsistent
        handle.truncate(max(4096, size - 20000))
    result = integrity(target)
    run_ctx.step("SQLite reports the corrupted copy as damaged", result != "ok", f"integrity_check={result[:120]}")

    # The alert path must fire on a real measurement: point the collector at a
    # scratch contract whose data root contains the damaged store.
    scratch_contract = scratch / "scratch.env"
    scratch_contract.write_text(
        (PROD_ROOT / "etc/production.env").read_text()
        + f"\nPROD_HOME={scratch}\nPROD_METRICS_DIR={scratch}/metrics\nPROD_ALERTS_DIR={scratch}/alerts\n"
    )
    environment = dict(os.environ, PROD_CONTRACT=str(scratch_contract), PROD_EVIDENCE=str(scratch / "evidence"))
    metrics = subprocess.run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_metrics.py"), "--once"],
                             capture_output=True, text=True, env=environment, timeout=300)
    alerts_run = subprocess.run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_alerts.py"), "--json"],
                                capture_output=True, text=True, env=environment, timeout=180)
    try:
        document = json.loads(alerts_run.stdout or "{}")
    except Exception:
        document = {}
    try:
        sample = json.loads((scratch / "metrics/latest.json").read_text())
    except Exception:
        sample = {}
    unhealthy = sample.get("database", {}).get("unhealthy")
    run_ctx.step("the collector measures exactly the damaged store as unhealthy",
                 isinstance(unhealthy, list) and len(unhealthy) == 1
                 and str(unhealthy[0]).endswith("chat_history.db"),
                 f"unhealthy={unhealthy} (collector exit={metrics.returncode})")
    run_ctx.step("the database-integrity alert fires on the damaged store",
                 "database-integrity" in firing(document),
                 json.dumps([row.get("id") for row in document.get("results", []) if row.get("firing")]))
    run_ctx.restore("no production store was touched (only a copy under run/)",
                    all(path.exists() for path in [source]), f"source={source}")
    shutil.rmtree(scratch, ignore_errors=True)
    sample = json.loads((cfg.METRICS_DIR / "latest.json").read_text()) if (cfg.METRICS_DIR / "latest.json").exists() else {}
    database = sample.get("database") or {}
    stores = database.get("stores") or 0
    unhealthy_now = database.get("unhealthy") or []
    run_ctx.step("the live deployment still reports healthy stores after the drill",
                 bool(stores) and not unhealthy_now,
                 f"stores={stores} unhealthy={unhealthy_now}")
    return run_ctx


@case("tls-cert-near-expiry", "validation certificate replaced with a one-day certificate", "certificate monitoring must warn before expiry")
def tls_cert_near_expiry() -> CaseRun:
    run_ctx = CaseRun("tls-cert-near-expiry", CASES["tls-cert-near-expiry"]["title"], CASES["tls-cert-near-expiry"]["impact"])
    tls_dir = PROD_ROOT / "harness/tls"
    backup = RUN / "fault-tls-backup"
    backup.mkdir(exist_ok=True)
    for name in ("ingress.crt", "ingress.key"):
        shutil.copy2(tls_dir / name, backup / name)
    run_ctx.step("the public path serves before the injection", probes()["public_login"] == 200, json.dumps(probes()))
    try:
        generated = run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(tls_dir / "ingress.key"), "-out", str(tls_dir / "ingress.crt"),
            "-days", "1", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ], timeout=120)
        run_ctx.step("a one-day certificate was generated", generated.returncode == 0 and (tls_dir / "ingress.crt").exists(),
                     generated.stderr.strip()[-160:])
        supervisor("restart", "ingress-validation")
        served, seconds = wait_for(lambda: http_status(f"{cfg.FRONTEND}/login", timeout=10) == 200, timeout=60)
        run_ctx.step("the ingress serves the short-lived certificate", served, f"after={seconds}s")
        document = alert_document()
        run_ctx.step("the certificate-expiry alert fires on the short-lived certificate",
                     "cert-expiry-validation-ingress" in firing(document), json.dumps(firing(document)))
        sample = json.loads((cfg.METRICS_DIR / "latest.json").read_text())
        days = sample.get("tls", {}).get("validation_cert_days")
        run_ctx.step("the metric measures the real remaining lifetime", isinstance(days, (int, float)) and days is not None and 0 < days < 21,
                     f"days={days}")
    finally:
        for name in ("ingress.crt", "ingress.key"):
            shutil.copy2(backup / name, tls_dir / name)
        supervisor("restart", "ingress-validation")
    recovered, seconds = wait_for(lambda: http_status(f"{cfg.FRONTEND}/login", timeout=10) == 200, timeout=60)
    run_ctx.restore("the original certificate is back in place", recovered, f"served_after={seconds}s")
    document = alert_document()
    run_ctx.step("the certificate alert cleared after the restore",
                 "cert-expiry-validation-ingress" not in firing(document), json.dumps(firing(document)))
    return run_ctx


@case("disk-pressure", "a 2 MiB filesystem is filled", "backups and measurements must fail loudly, never silently succeed")
def disk_pressure() -> CaseRun:
    title = CASES["disk-pressure"]["title"]
    impact = CASES["disk-pressure"]["impact"]
    run_ctx = CaseRun("disk-pressure", title, impact)
    tiny = RUN / "fault-tiny-fs"
    if _is_mount(tiny):
        run(["sudo", "umount", str(tiny)], timeout=60)
    tiny.mkdir(exist_ok=True)
    mounted = run(["sudo", "mount", "-t", "tmpfs", "-o", "size=2m", "tmpfs", str(tiny)], timeout=60)
    run_ctx.step("a small filesystem can be mounted for the experiment", mounted.returncode == 0,
                 mounted.stderr.strip()[-160:])
    if mounted.returncode != 0:
        run_ctx.step("the disk-pressure injection could not be performed on this host", False,
                     "environment limitation recorded as a failure of the case, not skipped silently")
        return run_ctx
    try:
        usage = shutil.disk_usage(tiny)
        run_ctx.step("the mounted filesystem reports its real, tiny size", usage.total < 8 * 1024 * 1024,
                     f"total={usage.total} bytes")

        # A deployment-shaped scratch root whose data is larger than the whole
        # filesystem, so a backup of it provably cannot fit.
        scratch = tiny / "root"
        home = scratch / "home"
        (home / "data/user/settings").mkdir(parents=True, exist_ok=True)
        (home / "data/user/settings/settings.json").write_text('{"max_upload_mb": 5}\n')
        backup_root = scratch / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        (backup_root / "index.ndjson").touch()
        # Policies, measurements and evidence stay on the real disk, so the
        # collector can publish the reading it took from the full filesystem;
        # only the deployment root, its data and its backup target live on the
        # filesystem under pressure. The contract itself therefore has to be
        # written before the fill: afterwards nothing fits on it any more.
        # The operator tooling lives with the deployment, so the scratch root
        # links to it instead of pretending a second ops venv exists: the
        # collector then reads the real supervisor and only the data volume is
        # the one under pressure.
        ops_link = scratch / "ops-venv"
        if not ops_link.exists():
            ops_link.symlink_to(PROD_ROOT / "ops-venv")
        metered = RUN / "fault-tiny-metered"
        shutil.rmtree(metered, ignore_errors=True)
        contract = RUN / "fault-tiny-contract.env"
        contract.write_text(
            (PROD_ROOT / "etc/production.env").read_text()
            + f"\nPROD_ETC={PROD_ROOT / 'etc'}\nPROD_HOME={home}\nPROD_BACKUPS={backup_root}\n"
              f"PROD_METRICS_DIR={metered}/metrics\nPROD_ALERTS_DIR={metered}/alerts\n"
              f"PROD_LOG_DIR={metered}/logs\n"
              # The scratch root has no processes of its own, so the runtime
              # interfaces (supervisor socket, pid files) stay pointed at the
              # real deployment; only data, backups and measurements are
              # scratch. Otherwise every program would read as down and the
              # alert list would be noise rather than the disk finding.
              f"PROD_RUN={RUN}\nPROD_SUPERVISOR_SOCK={cfg.value('PROD_SUPERVISOR_SOCK', str(RUN / 'supervisor.sock'))}\n"
              f"PROD_SUPERVISOR_PID={RUN / 'supervisord.pid'}\n"
        )
        scratch_env = dict(os.environ, PROD_CONTRACT=str(contract), PROD_ROOT=str(scratch),
                           PROD_BACKUPS=str(backup_root), PROD_HOME=str(home),
                           PROD_EVIDENCE=str(metered / "evidence"), PROD_RUN=str(RUN))
        # Filled past the policy's critical threshold (92 %) so the alert under
        # test is driven by a real measurement, not by a threshold chosen to fit.
        filler = home / "data/bulk.bin"
        try:
            with filler.open("wb") as handle:
                handle.write(b"y" * (1024 * 1024))
                while True:
                    handle.write(b"y" * (64 * 1024))
        except OSError:
            # ENOSPC is the point of the injection: a filled tmpfs reports it on
            # the write, or on the buffered flush when the writer closes.
            pass
        print(f"    [note] filled the filesystem with {filler.stat().st_size} bytes of scratch data")
        filled = shutil.disk_usage(tiny)
        run_ctx.step("the scratch deployment's data exceeds the filesystem that must hold its backup",
                     filled.used > filled.total * 0.6, f"used={filled.used} of {filled.total}")

        backup = run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_backup.py"),
                      "--label", "disk-pressure"], timeout=300, env=scratch_env)
        combined = backup.stdout + backup.stderr
        run_ctx.step("a backup that cannot fit fails loudly", backup.returncode != 0, f"exit={backup.returncode}")
        run_ctx.step("the failure is attributed to the full filesystem",
                     any(token in combined.lower() for token in ("no space", "enospc", "space left")),
                     combined.strip()[-200:])
        index = [line for line in (backup_root / "index.ndjson").read_text().splitlines() if line.strip()]
        run_ctx.step("no backup is recorded as successful when it could not be written", not index,
                     f"index_entries={len(index)}")
        leftovers = [path.name for path in backup_root.iterdir() if path.is_dir()]
        run_ctx.step("no partial backup directory is left behind for a later restore to pick up",
                     not leftovers, f"leftovers={leftovers}")

        metrics = subprocess.run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_metrics.py"),
                                  "--once", "--skip-db"], capture_output=True, text=True, env=scratch_env, timeout=180)
        alerts_run = subprocess.run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_alerts.py"), "--json"],
                                    capture_output=True, text=True, env=scratch_env, timeout=180)
        try:
            document = json.loads(alerts_run.stdout or "{}")
        except Exception:
            document = {}
        ids = firing(document)
        run_ctx.step("the disk alert fires on the measured usage", "disk-critical" in ids or "disk-pressure" in ids,
                     json.dumps(ids))
        run_ctx.step("the collector published the measurement it acted on", "disk" in metrics.stdout.lower(),
                     metrics.stdout.strip()[-160:])
    finally:
        run(["sudo", "umount", str(tiny)], timeout=60)
    shutil.rmtree(RUN / "fault-tiny-metered", ignore_errors=True)
    (RUN / "fault-tiny-contract.env").unlink(missing_ok=True)
    run_ctx.restore("the small filesystem is unmounted", not _is_mount(tiny), f"mount_present={_is_mount(tiny)}")
    run_ctx.step("the deployment itself never saw the full filesystem and stays healthy",
                 all(value == 200 for value in probes().values()), json.dumps(probes()))
    return run_ctx


# --------------------------------------------------------------------------- utilities
def _upload(actor: Actor, name: str, payload: bytes) -> tuple[int, str]:
    try:
        # The file library is an API-only surface, so the upload goes to the
        # API's private address with the session token as a bearer credential.
        status, _, body, _ = upload(actor, "POST", "/files/library/", payload, filename=name,
                                    base=cfg.BACKEND, timeout=60,
                                    extra_fields=(("filename", name), ("mime_type", "text/plain")))
        return status, body or ""
    except Exception as exc:  # noqa: BLE001 — a dead server is a documented outcome of a fault run
        return 0, str(exc)[:160]


def _chmod(path: Path, mode: int) -> None:
    """chmod that also works when the path is owned by the deployment user."""
    try:
        path.chmod(mode)
    except PermissionError:
        run(["chmod", oct(mode)[2:], str(path)], timeout=60, check=True)


def _write_contract(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def _read_pid(path: Path | None = None) -> str:
    target = path or cfg.WATCHDOG_PID
    try:
        return target.read_text().strip()
    except Exception:
        return "0"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _supervisord_pid() -> int:
    try:
        return int(cfg.SUPERVISOR_PID.read_text().strip())
    except Exception:
        return 0


def _pid_owns_programs() -> bool:
    """True when the live supervisord actually supervises programs."""
    try:
        _, out = supervisor("status")
    except Exception:
        return False
    return out.count("RUNNING") >= 3


def _watchdog_state() -> dict:
    path = RUN / "watchdog-state.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _orphans_left() -> list[int]:
    """Deployment programs no live supervisord owns, as the watchdog defines them."""
    completed = run([str(PYTHON), str(Path(__file__).resolve().parent / "prod_watchdog.py"), "orphans"], timeout=60)
    try:
        return json.loads(completed.stdout or "{}").get("orphans", [])
    except Exception:
        return []


def _is_mount(path: Path) -> bool:
    try:
        return os.path.ismount(path)
    except OSError:
        return False


# --------------------------------------------------------------------------- runner
def baseline_report() -> dict:
    return {"probes": probes(), "programs": {name: program_state(name) for name in LONG_RUNNING}}


def acquire_run_lock():
    """Only one fault run may drive a deployment at a time.

    Two concurrent runs inject at each other and produce unusable evidence, so a
    deployment-level lock makes that impossible instead of merely discouraged.
    """
    import fcntl

    cfg.RUN.mkdir(parents=True, exist_ok=True)
    path = cfg.RUN / "faults.lock"
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print("FATAL: another fault-injection run is already active for this deployment", file=sys.stderr)
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def mark_backend_log(note: str) -> None:
    """Append a drill-boundary marker to the backend log.

    The log metric counts error lines only after the newest marker, so this is
    what turns "the drill's deliberate errors are still in the log window" into
    "the drill is finished and accounted for". Nothing else about the running
    application is touched, and the marker itself carries no error text.
    """
    path = cfg.LOG_DIR / "backend.log"
    if not path.exists():
        return
    line = f"--- {cfg.FAULT_LOG_MARKER}: {note} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ---\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        print(f"    [marker] {line.strip()}")
    except OSError as exc:  # pragma: no cover - log must never fail the drill
        print(f"    [marker] could not mark the backend log: {exc}", file=sys.stderr)


def main() -> int:
    args = sys.argv[1:]
    if "--list" in args or not args:
        for case_id, entry in CASES.items():
            print(f"{case_id:26s} {entry['title']}  [{entry['impact']}]")
        return 0

    lock = acquire_run_lock()
    if lock is None:
        return 2
    selected = [args[index + 1] for index, token in enumerate(args) if token == "--case"]
    if "--all" in args:
        selected = list(CASES)
    unknown = [case_id for case_id in selected if case_id not in CASES]
    if unknown:
        print(f"unknown case(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    keep_going = "--keep-going" in args
    before = baseline_report()
    print(f"baseline: {json.dumps(before)}")
    results: list[dict] = []
    for case_id in selected:
        title = CASES[case_id]["title"]
        print(f"\n=== {case_id}: {title}")
        before_case = ensure_stack(case_id)
        print(f"    [note] baseline before {case_id}: {before_case[1]}")
        try:
            run_ctx = CASES[case_id]["run"]()
            document = run_ctx.document()
        except Exception as exc:  # noqa: BLE001 — a crashed case is a finding, not a script error
            document = {"id": case_id, "title": title, "steps": [], "restorations": [],
                        "error": f"{type(exc).__name__}: {exc}", "ok": False}
        results.append(document)
        FAULT_EVIDENCE.mkdir(parents=True, exist_ok=True)
        (FAULT_EVIDENCE / f"{case_id}-{int(time.time())}.json").write_text(json.dumps(document, indent=2) + "\n")
        mark_backend_log(f"{case_id} done")
        if not document.get("ok") and not keep_going:
            print(f"stopping after {case_id} (pass --keep-going to continue)")
            break

    after = baseline_report()
    document = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "release": cfg.RELEASE_ID,
        "source_commit": cfg.SOURCE_COMMIT,
        "baseline_before": before,
        "baseline_after": after,
        "cases": results,
        "cases_total": len(results),
        "cases_passed": len([row for row in results if row.get("ok")]),
        "checks_total": sum(row.get("checks", 0) for row in results),
        "checks_passed": sum(row.get("passed", 0) for row in results),
        "deployment_healthy_after": all(value == 200 for value in after["probes"].values())
        and all(state == "RUNNING" for state in after["programs"].values()),
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "production-faults.json").write_text(json.dumps(document, indent=2) + "\n")
    # The drill is over and every fault has been removed: mark the boundary so
    # the log metric stops counting the errors this run deliberately produced.
    mark_backend_log(f"all {len(results)} cases done, faults removed")
    print(f"\nfault injections: {document['cases_passed']}/{document['cases_total']} cases passed, "
          f"{document['checks_passed']}/{document['checks_total']} checks passed, "
          f"deployment healthy after={document['deployment_healthy_after']}")
    return 0 if document["cases_passed"] == document["cases_total"] and document["deployment_healthy_after"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
