#!/usr/bin/env python3
"""Host-level watchdog for the production process manager.

The platform sandbox has no usable init system (PID 1 is systemd but there is no
D-Bus and no writable unit directory), so nothing restarts supervisord itself if
it dies. On a real host that role belongs to systemd (see
templates/systemd/mo7-production.service, shipped but not installable here).

This watchdog is the documented, minimal substitute: it checks the supervisord
pid every interval and, when the daemon is gone, restarts it with the same
configuration and logs the event with a monotonic restart counter. It never
touches the application processes — supervisord's own `autorestart` handles
those, and the watchdog's job is only to make sure the supervisor is alive.

    prod_watchdog.py start|stop|status|daemon
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

INTERVAL = float(os.environ.get("PROD_WATCHDOG_INTERVAL", "5"))
STATE_FILE = cfg.RUN / "watchdog-state.json"


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {message}"
    print(line, flush=True)
    with cfg.WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def supervisord_pid() -> int:
    try:
        return int(cfg.SUPERVISOR_PID.read_text().strip())
    except Exception:
        return 0


def supervisor_alive() -> bool:
    pid = supervisord_pid()
    if not pid_alive(pid):
        return False
    try:
        return "supervisord" in Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except Exception:
        return True


def supervised_pids() -> set[int]:
    """Every process the live supervisord currently owns (recursively)."""
    root = supervisord_pid()
    if not pid_alive(root):
        return set()
    seen: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        for task in Path(f"/proc/{pid}/task").glob("*"):
            try:
                children = (task / "children").read_text().split()
            except Exception:
                continue
            for child in children:
                try:
                    kid = int(child)
                except ValueError:
                    continue
                if kid not in seen:
                    seen.add(kid)
                    stack.append(kid)
    return seen


def orphaned_programs() -> list[int]:
    """Deployment programs that no live supervisord owns.

    `kill -9` on supervisord does not kill its children: they keep the API and
    frontend ports bound (verified by the `supervisord-crash` fault injection), so
    a replacement supervisord fails every start with EADDRINUSE and gives up
    after its retries. An orphan is therefore defined structurally — a process
    running one of this deployment's program command lines that is *not* a
    descendant of the live supervisord — which is also what keeps the watchdog
    from ever touching a process it does not own.
    """
    # The start scripts exec their real interpreter, and Node rewrites its own
    # process title, so a program is recognised by its interpreter command line
    # *plus* the deployment it runs from (working directory inside the root), or
    # by an unmistakable marker.
    strong_markers = ("prod_scheduler.py", "ingress_tls_proxy.js")
    program_commands = ("uvicorn", "deeptutor.api.main:app", "server.js", "next-server")
    root = str(cfg.PROD_ROOT)
    supervised = supervised_pids()
    orphans: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid() or pid in supervised:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
            cwd = os.readlink(entry / "cwd")
        except Exception:
            continue
        if any(marker in cmdline for marker in strong_markers):
            orphans.append(pid)
            continue
        if any(command in cmdline for command in program_commands) and root in cwd:
            orphans.append(pid)
    return orphans


def clear_orphans() -> int:
    orphans = orphaned_programs()
    for pid in orphans:
        log(f"terminating orphaned program left by the dead supervisord: pid {pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"could not terminate pid {pid}: {type(exc).__name__}: {exc}")
    for _ in range(20):
        if not any(pid_alive(pid) for pid in orphans):
            break
        time.sleep(1)
    for pid in orphans:
        if pid_alive(pid):
            log(f"orphan pid {pid} ignored SIGTERM; sending SIGKILL")
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    return len(orphans)


def start_supervisord() -> bool:
    command = [str(cfg.SUPERVISORD), "-c", str(cfg.ETC / "supervisord.conf")]
    if not Path(command[0]).exists():
        log(f"FATAL: supervisord binary missing: {command[0]}")
        return False
    # A pid file left behind by the supervisord that died makes a fresh
    # supervisord refuse to start (or makes `supervisorctl` talk to a socket
    # nobody owns), so it is removed first — the pid it holds is known dead.
    for stale in (cfg.SUPERVISOR_PID, Path(cfg.SUPERVISOR_SOCK)):
        if stale.exists():
            log(f"removing stale {stale.name} left by the dead supervisord")
            try:
                stale.unlink()
            except OSError as exc:
                log(f"could not remove {stale}: {exc}")
    log(f"starting supervisord: {' '.join(command)}")
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        time.sleep(1)
        if supervisor_alive():
            return True
    return False


def daemon() -> int:
    if not acquire_singleton_lock():
        log("another watchdog already holds the deployment lock; exiting")
        return 1
    restarts = 0
    write_state({"pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "restarts": 0})
    log(f"watchdog started (pid {os.getpid()}, interval {INTERVAL}s)")
    while True:
        # Steady-state orphan sweep: a program that survived a supervisord
        # restart keeps its port bound and blocks the supervised replacement
        # forever (supervisord gives up after startretries and marks it FATAL).
        if supervisor_alive():
            try:
                orphans = orphaned_programs()
                if orphans:
                    log(f"terminating {len(orphans)} program(s) not owned by the live supervisord: {orphans}")
                    clear_orphans()
                    for program in ("backend", "frontend", "scheduler"):
                        if supervisorctl_state(program) == "FATAL":
                            log(f"retrying FATAL program after clearing orphans: {program}")
                            subprocess.run(
                                [str(cfg.SUPERVISORCTL), "-c", str(cfg.ETC / "supervisord.conf"), "start", program],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            )
            except Exception as exc:  # the sweep must never take the watchdog down
                log(f"orphan sweep raised {type(exc).__name__}: {exc}")
        if not supervisor_alive():
            restarts += 1
            log(f"supervisord is not alive (restart #{restarts})")
            try:
                cleared = clear_orphans()
                if cleared:
                    log(f"cleared {cleared} orphaned program(s) before restarting supervisord")
                if start_supervisord():
                    log(f"supervisord restarted (pid {supervisord_pid()}, restart #{restarts})")
                else:
                    log("supervisord restart FAILED")
            except Exception as exc:  # the watchdog must survive its own recovery path
                log(f"supervisord restart raised {type(exc).__name__}: {exc}")
            write_state(
                {
                    "pid": os.getpid(),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "restarts": restarts,
                    "last_restart_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
        time.sleep(INTERVAL)


def supervisorctl_state(program: str) -> str:
    """`RUNNING` / `FATAL` / ... for one program, without raising."""
    try:
        completed = subprocess.run(
            [str(cfg.SUPERVISORCTL), "-c", str(cfg.ETC / "supervisord.conf"), "status", program],
            capture_output=True, text=True, timeout=30,
        )
        match = re.search(rf"{program}\s+(\w+)", completed.stdout)
        return match.group(1) if match else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def write_state(payload: dict) -> None:
    STATE_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


LOCK_PATH = None


def acquire_singleton_lock() -> bool:
    """Only one watchdog may supervise a deployment.

    Two watchdogs would race each other's restart accounting, so the daemon
    takes an exclusive lock for its lifetime.
    """
    global LOCK_PATH
    import fcntl

    LOCK_PATH = cfg.RUN / "watchdog.lock"
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return True


def start() -> int:
    # The daemon's stdout/stderr go to their own file: `log()` already writes the
    # structured log, and redirecting both to the same path duplicated every line.
    state = read_state()
    if pid_alive(int(state.get("pid", 0))) and Path(f"/proc/{state.get('pid')}/cmdline").read_bytes().decode(
        "utf-8", "replace"
    ).find("prod_watchdog") >= 0:
        print(f"watchdog already running (pid {state['pid']})")
        return 0
    log_file = (cfg.RUN / "watchdog.stdout").open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "daemon"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    cfg.WATCHDOG_PID.write_text(f"{process.pid}\n", encoding="utf-8")
    time.sleep(1)
    if process.poll() is not None:
        print("watchdog failed to stay up", file=sys.stderr)
        return 1
    print(f"watchdog started (pid {process.pid})")
    return 0


def stop() -> int:
    pid = int(read_state().get("pid", 0)) or (int(cfg.WATCHDOG_PID.read_text().strip()) if cfg.WATCHDOG_PID.exists() else 0)
    if not pid_alive(pid):
        print("watchdog is not running")
        cfg.WATCHDOG_PID.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    if pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
    log(f"watchdog stopped (pid {pid})")
    cfg.WATCHDOG_PID.unlink(missing_ok=True)
    state = read_state()
    state["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_state(state)
    print("watchdog stopped")
    return 0


def status() -> int:
    state = read_state()
    pid = int(state.get("pid", 0))
    alive = pid_alive(pid)
    payload = {
        "running": alive,
        "pid": pid if alive else None,
        "restarts": state.get("restarts", 0),
        "supervisord_alive": supervisor_alive(),
        "supervisord_pid": supervisord_pid() or None,
        "service_manager": cfg.value("PROD_SERVICE_MANAGER", "supervisord-with-watchdog"),
    }
    print(json.dumps(payload, indent=2))
    return 0 if alive else 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "daemon":
        return daemon()
    if command == "orphans":
        # Read-only view of the same definition the sweep uses, so harnesses and
        # operators cannot disagree about what counts as an orphan.
        print(json.dumps({"orphans": orphaned_programs(), "supervised": sorted(supervised_pids())}))
        return 0
    if command == "clear-orphans":
        cleared = clear_orphans()
        print(f"{cleared} orphaned program(s) from a previous supervisor generation")
        return 0
    if command == "start":
        return start()
    if command == "stop":
        return stop()
    if command == "status":
        return status()
    print(f"unknown command {command!r} (start|stop|status|daemon)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
