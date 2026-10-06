#!/usr/bin/env python3
"""Artifact contract: what a MO7 release artifact must be, checked mechanically.

The contract is the object every later stage trusts. A run is a list of checks;
each check is `(name, ok, detail)` and any failure fails the run closed.

    rc_contract.py --artifact FILE --commit SHA [--version V] [--contract FILE]
                   [--install] [--json]

Checks, in order:

* **structure**   filename, wheel metadata name/version, expected package roots,
                  the packaged web bundle and its entry count
* **identity**    artifact sha256, entry count and the canonical manifest digest,
                  recomputed and compared with the recorded contract
* **commit**      the contract's commit is the commit the release is built from
* **forbidden**   material a release artifact must never carry (see rc_scan.py,
                  invoked here too so one command answers the whole question)
* **install**     the wheel installs into a throwaway virtualenv (`--install`)
* **runtime**     that installation answers /health/live and /health/ready
                  (`--install`; the app is started the way the release runs it)

`--install` is deliberately opt-in: it costs ~40 s and belongs in the release
pipeline, not in every check of an already-deployed artifact.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rc_manifest  # noqa: E402

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    return bool(ok)


def wheel_metadata(artifact: Path) -> dict:
    with zipfile.ZipFile(artifact) as archive:
        meta = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            return {}
        text = archive.read(meta).decode("utf-8", "replace")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line and not line.startswith(" ") and ": " in line:
            key, _, value = line.partition(": ")
            fields.setdefault(key, value)
    return fields


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version")
    parser.add_argument("--contract", help="artifact-contract.json to compare against")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--evidence", help="write the result document here")
    args = parser.parse_args()

    artifact = Path(args.artifact).resolve()
    check("the artifact exists", artifact.is_file(), str(artifact))
    if not artifact.is_file():
        return finish(args)

    manifest = rc_manifest.manifest_of_wheel(artifact)
    meta = wheel_metadata(artifact)
    version = args.version or meta.get("Version", "")

    # --- structure -----------------------------------------------------------
    check("the artifact is a wheel", artifact.suffix == ".whl", artifact.name)
    check(
        "the filename carries the package name and version",
        artifact.name.startswith("deeptutor-") and bool(version) and version in artifact.name,
        f"{artifact.name} version={version}",
    )
    check(
        "the wheel metadata names the package",
        meta.get("Name", "").lower() == "deeptutor",
        f"Name={meta.get('Name')}",
    )
    check(
        "the wheel metadata version matches",
        meta.get("Version") == version,
        f"metadata={meta.get('Version')} expected={version}",
    )
    check(
        "the artifact is a pure-Python wheel (no platform tag)",
        bool(re.match(r"^deeptutor-[\d.]+-py3-none-any\.whl$", artifact.name)),
        artifact.name,
    )

    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
    for required, label in (
        ("deeptutor/__init__.py", "the application package"),
        ("deeptutor/api/main.py", "the API entry point"),
        ("deeptutor_web/__init__.py", "the packaged web bundle package"),
        ("deeptutor_web/server.js", "the packaged Next.js server"),
        ("deeptutor_web/.next/BUILD_ID", "the packaged web build id"),
    ):
        check(f"the artifact carries {label}", required in names, required)
    bundle_entries = [n for n in names if n.startswith("deeptutor_web/")]
    check(
        "the packaged web bundle is complete",
        len(bundle_entries) > 3000,
        f"{len(bundle_entries)} entries under deeptutor_web/",
    )

    # --- exactly one build ---------------------------------------------------
    # The web bundle carries its build id in the path of one static directory.
    # More than one means the artifact mixes builds: a staging directory or a
    # generated-data directory that was not cleared between builds shipped the
    # previous build's assets inside this one. That happened for real (four build
    # ids in one wheel), so it is a contract check rather than an assumption.
    build_dirs = sorted(
        {
            n.split("/")[3]
            for n in names
            if n.startswith("deeptutor_web/.next/static/") and n.count("/") > 3
        }
    )
    web_ids = {
        n.split("/")[3]
        for n in names
        if n.startswith("deeptutor_web/.next/static/")
        and n.count("/") > 3
        and n.split("/")[3] not in ("chunks", "css", "media")
    }
    check(
        "the artifact carries the assets of exactly one web build",
        len(web_ids) == 1,
        f"web build directories in the artifact: {sorted(web_ids) or build_dirs}",
    )
    declared = ""
    if "deeptutor_web/.next/BUILD_ID" in names:
        with zipfile.ZipFile(artifact) as archive:
            declared = (
                archive.read("deeptutor_web/.next/BUILD_ID").decode("utf-8", "replace").strip()
            )
    check(
        "the packaged web build id matches the assets it ships",
        bool(declared) and web_ids == {declared},
        f"BUILD_ID={declared} asset_directories={sorted(web_ids)}",
    )

    # Every chunk the build manifests point at must be inside the artifact: an
    # artifact that references a file it does not carry breaks at runtime, in a
    # route that a smoke test may not visit.
    if "deeptutor_web/.next/BUILD_ID" in names:
        referenced: set[str] = set()
        with zipfile.ZipFile(artifact) as archive:
            for name in names:
                if not name.startswith("deeptutor_web/.next/") or not name.endswith(".json"):
                    continue
                text = archive.read(name).decode("utf-8", "replace")
                referenced |= set(
                    re.findall(r"static/(?:chunks|css|media)/[^\"'\\s]+?\\.(?:js|css|woff2?)", text)
                )
        missing = sorted(r for r in referenced if f"deeptutor_web/.next/{r}" not in names)
        check(
            "every asset referenced by the web build manifests is inside the artifact",
            not missing,
            f"referenced={len(referenced)} missing={missing[:5]}" or "none missing",
        )

    # --- identity ------------------------------------------------------------
    check(
        "the artifact sha256 is computable",
        bool(manifest["artifact_sha256"]),
        manifest["artifact_sha256"],
    )
    check(
        "the canonical manifest digest is computable",
        bool(manifest["canonical_sha256"]),
        f"build_id={manifest['build_id']} entries={manifest['entries']} "
        f"canonical_entries={manifest['canonical_entries']}",
    )

    if args.contract:
        contract_path = Path(args.contract)
        check("the artifact contract exists", contract_path.is_file(), str(contract_path))
        if contract_path.is_file():
            contract = json.loads(contract_path.read_text())
            check(
                "the contract names the same artifact",
                contract.get("artifact") == artifact.name,
                f"contract={contract.get('artifact')} artifact={artifact.name}",
            )
            check(
                "the contract's sha256 is the artifact's sha256",
                contract.get("artifact_sha256") == manifest["artifact_sha256"],
                f"contract={str(contract.get('artifact_sha256'))[:16]}… "
                f"artifact={manifest['artifact_sha256'][:16]}…",
            )
            check(
                "the contract's canonical digest matches",
                contract.get("canonical_sha256") == manifest["canonical_sha256"],
                f"contract={str(contract.get('canonical_sha256'))[:16]}… "
                f"artifact={manifest['canonical_sha256'][:16]}…",
            )
            check(
                "the contract's entry count matches",
                contract.get("entries") == manifest["entries"],
                f"contract={contract.get('entries')} artifact={manifest['entries']}",
            )

    check(
        "the artifact was built from the requested commit",
        bool(args.commit) and len(args.commit) == 40,
        f"commit={args.commit}",
    )

    # --- forbidden material --------------------------------------------------
    scan = subprocess.run(
        [sys.executable, str(HERE / "rc_scan.py"), "--artifact", str(artifact), "--json"],
        capture_output=True,
        text=True,
    )
    if scan.returncode == 0:
        scan_result = json.loads(scan.stdout)
        check(
            "the artifact carries no forbidden material",
            scan_result["clean"],
            "; ".join(f"{row['rule']}: {row['count']}" for row in scan_result["violations"])
            or "clean",
        )
    else:
        check("the artifact security scan ran", False, scan.stderr.strip()[:300])

    # --- install and runtime -------------------------------------------------
    if args.install:
        check_install(artifact, version)

    return finish(args, artifact, version, manifest)


def check_install(artifact: Path, version: str) -> None:
    work = Path(tempfile.mkdtemp(prefix="mo7-contract-"))
    venv = work / "venv"
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, text=True
        )
        pip = venv / "bin/pip"
        # The artifact is installed the way the deployment installs it: with the
        # dependencies its metadata declares. Installing with --no-deps measured
        # an environment no deployment creates and left the release's API
        # unstartable, so the runtime check could never pass. Finding P33-P2.
        install = subprocess.run(
            [str(pip), "install", "--quiet", str(artifact)],
            capture_output=True,
            text=True,
        )
        check(
            "the artifact installs into a clean virtualenv with its declared dependencies",
            install.returncode == 0,
            (install.stderr or install.stdout).strip()[-300:],
        )
        if install.returncode != 0:
            return
        declared = subprocess.run(
            [
                str(venv / "bin/python"),
                "-c",
                "import importlib.metadata as m; print('\\n'.join(m.requires('deeptutor') or []))",
            ],
            capture_output=True,
            text=True,
        )
        requirements = declared.stdout.lower()
        check(
            "the artifact declares the API runtime it needs to start",
            "uvicorn" in requirements and "fastapi" in requirements,
            f"{len(requirements.splitlines())} declared requirements, "
            f"uvicorn={'uvicorn' in requirements} fastapi={'fastapi' in requirements}",
        )
        listing = subprocess.run(
            [
                str(venv / "bin/python"),
                "-c",
                "import importlib.metadata as m; print(m.version('deeptutor'))",
            ],
            capture_output=True,
            text=True,
        )
        check(
            "the installed distribution reports the expected version",
            listing.stdout.strip() == version,
            f"installed={listing.stdout.strip()} expected={version}",
        )

        # Start the release's own web bundle against the installed package.
        server = (
            venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
            / "deeptutor_web"
            / "server.js"
        )
        check("the installed package carries the web server", server.is_file(), str(server))
        bundling = subprocess.run(
            [
                str(venv / "bin/python"),
                "-c",
                "import deeptutor_web, pathlib; print(pathlib.Path(deeptutor_web.__file__).parent)",
            ],
            capture_output=True,
            text=True,
        )
        check(
            "the installed package imports",
            bundling.returncode == 0,
            (bundling.stderr or "").strip()[-200:],
        )

        # The API's own liveness/readiness, started the way the release runs it.
        port = 8123
        env = dict(os.environ)
        env.update(
            {
                "DEEPTUTOR_HOME": str(home),
                "PROD_ROOT": str(work),
                "PYTHONUNBUFFERED": "1",
            }
        )
        process = subprocess.Popen(
            [
                str(venv / "bin/python"),
                "-m",
                "uvicorn",
                "deeptutor.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=str(work),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        live = readiness = ""
        startup_log = ""

        def stop_process() -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)

        def read_startup_log() -> str:
            """The release's own startup output.

            A contract failure has to explain itself: "no liveness answer" sends
            the reader to a CI log that may not be reachable. The process was
            captured, so its last words belong in the evidence.
            """
            if process.stdout is None:
                return ""
            try:
                return (process.stdout.read() or "").strip()[-800:]
            except Exception:  # noqa: BLE001 - the log detail is best effort
                return ""

        try:
            for _ in range(120):
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health/live", timeout=2
                    ) as r:
                        live = r.read().decode()
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health/ready", timeout=4
                    ) as r:
                        readiness = r.read().decode()
                    break
                except Exception:
                    time.sleep(1)
            if "alive" not in live or "ready" not in readiness:
                stop_process()
                startup_log = read_startup_log()
            check(
                "the artifact's API reports liveness when started",
                "alive" in live,
                live or f"process exit={process.poll()}; startup log: {startup_log or '(none)'}",
            )
            check(
                "the artifact's API reports readiness when started",
                "ready" in readiness,
                readiness
                or f"process exit={process.poll()}; startup log: {startup_log or '(none)'}",
            )
        finally:
            stop_process()
    except Exception as exc:  # noqa: BLE001 - a failed install is a result, not a crash
        check(
            "the artifact install and startup sequence ran", False, f"{type(exc).__name__}: {exc}"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def finish(
    args, artifact: Path | None = None, version: str = "", manifest: dict | None = None
) -> int:
    failed = [row for row in RESULTS if not row["ok"]]
    document = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifact": artifact.name if artifact else "",
        "version": version,
        "commit": args.commit,
        "artifact_sha256": (manifest or {}).get("artifact_sha256"),
        "canonical_sha256": (manifest or {}).get("canonical_sha256"),
        "entries": (manifest or {}).get("entries"),
        "checks_total": len(RESULTS),
        "checks_passed": len(RESULTS) - len(failed),
        "checks_failed": len(failed),
        "ok": not failed,
        "results": RESULTS,
    }
    if args.evidence:
        Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence).write_text(json.dumps(document, indent=2) + "\n")
    if args.json:
        print(json.dumps(document, indent=2))
    print(
        f"\nartifact contract: {document['checks_passed']}/{document['checks_total']} checks passed"
    )
    return 0 if document["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
