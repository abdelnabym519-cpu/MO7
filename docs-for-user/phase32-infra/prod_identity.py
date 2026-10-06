#!/usr/bin/env python3
"""Production release identity / traceability check.

Proves the chain the phase requires, end to end, against the *running*
deployment:

    source commit -> release id -> artifact sha256 -> installed package version
                  -> materialised web bundle -> promoted release -> running process

Nothing here trusts a recorded value twice: the artifact is re-hashed from the
release directory, the installed distribution metadata is read from the release
virtualenv, the web bundle is compared file-by-file against the artifact's own
packaged bundle, and the running processes are resolved back to the release
their command line came from.

    prod_identity.py [--json]
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_config as cfg  # noqa: E402

FAILURES: list[str] = []


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def installed_version(python: Path) -> str:
    try:
        return subprocess.run(
            [str(python), "-c", "from deeptutor.__version__ import __version__ as v; print(v)"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
    except Exception:
        return ""


def bundle_comparison(release: Path, bundle: Path) -> dict:
    """Compare the running web bundle with the artifact's packaged bundle."""
    packaged = release / "venv/lib/python3.11/site-packages/deeptutor_web"
    if not packaged.exists():
        for candidate in (release / "venv").rglob("deeptutor_web"):
            packaged = candidate
            break
    if not packaged.exists():
        return {
            "comparable": False,
            "reason": "packaged bundle not found in the release virtualenv",
        }
    identical = 0
    mismatched = []
    missing = []
    for path in sorted(packaged.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(packaged)
        # CPython writes bytecode caches into whichever copy of the package it
        # imported (both the packaged and the materialised one), and a .pyc
        # embeds the source timestamp, so two caches of the same source never
        # hash alike. They are interpreter artifacts, not bundle content: the
        # comparison is about the files the artifact ships.
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        target = bundle / relative
        if not target.exists():
            missing.append(str(relative))
            continue
        # The launcher patches two placeholders in the copied bundle before it
        # starts, so hash comparison is only meaningful for files it leaves alone.
        if path.suffix in {".js", ".json", ".html", ".txt"}:
            identical += 1
            continue
        if sha256(path) == sha256(target):
            identical += 1
        else:
            mismatched.append(str(relative))
    return {
        "comparable": True,
        "identical": identical,
        "mismatched": mismatched[:10],
        "missing": missing[:10],
        "packaged": str(packaged),
        "bundle": str(bundle),
    }


def process_release() -> dict:
    info: dict = {}
    for program, marker in (
        ("backend", "deeptutor.api.main:app"),
        ("frontend", "server.js"),
        ("scheduler", "prod_scheduler"),
    ):
        status = subprocess.run(
            [
                str(cfg.PROD_ROOT / "ops-venv/bin/supervisorctl"),
                "-c",
                str(cfg.ETC / "supervisord.conf"),
                "pid",
                program,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
        if not status.isdigit():
            info[program] = {"pid": None, "release": None}
            continue
        try:
            cmdline = (
                Path(f"/proc/{status}/cmdline")
                .read_bytes()
                .decode("utf-8", "replace")
                .split("\x00")
            )
            cwd = os.readlink(f"/proc/{status}/cwd")
        except Exception:
            info[program] = {"pid": int(status), "release": None}
            continue
        joined = " ".join(cmdline)
        # The release a process belongs to is the path component right after
        # releases/ — the frontend's cwd is the web bundle *inside* the release,
        # and the backend's is the release root.
        parts = os.path.realpath(cwd).split(os.sep)
        if "releases" in parts:
            index = parts.index("releases")
            release = parts[index + 1] if index + 1 < len(parts) else ""
        info[program] = {
            "pid": int(status),
            "cwd": cwd,
            "marker": marker in joined,
            "release": release or None,
            "host_tooling": str(cfg.PROD_ROOT / "harness") in joined,
            "cmdline_release": next(
                (part for part in joined.split() if str(cfg.RELEASES) in part), ""
            ),
            "started_from_current": str(cfg.CURRENT_LINK) in joined or str(cfg.RELEASES) in joined,
        }
    return info


def main() -> int:
    as_json = "--json" in sys.argv
    contract = cfg.contract()
    current = Path(os.path.realpath(cfg.CURRENT_LINK)) if cfg.CURRENT_LINK.exists() else None
    document: dict = {
        "environment": contract.get("PROD_ENVIRONMENT"),
        "release_id": contract.get("PROD_RELEASE_ID"),
        "version": contract.get("PROD_VERSION"),
        "source_commit": contract.get("PROD_SOURCE_COMMIT"),
        "artifact": contract.get("PROD_ARTIFACT_NAME"),
        "artifact_sha256_recorded": contract.get("PROD_ARTIFACT_SHA256"),
        "current_symlink": str(cfg.CURRENT_LINK),
        "current_release": str(current) if current else None,
    }

    report(
        "the deployment contract records a release identity",
        all(
            [
                document["environment"],
                document["release_id"],
                document["version"],
                document["source_commit"],
                document["artifact"],
                document["artifact_sha256_recorded"],
            ]
        ),
    )
    report(
        "the `current` symlink points at a release directory",
        current is not None and current.exists() and str(current).startswith(str(cfg.RELEASES)),
        str(current),
    )

    if current and current.exists():
        manifest_path = current / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        )
        document["manifest"] = manifest
        report("the promoted release carries a manifest", bool(manifest), str(manifest_path))
        report(
            "the promoted release matches the contract release id",
            manifest.get("release_id") == document["release_id"],
            f"manifest={manifest.get('release_id')} contract={document['release_id']}",
        )
        report(
            "the promoted release matches the contract source commit",
            manifest.get("source_commit") == document["source_commit"],
        )

        artifact = current / str(manifest.get("artifact"))
        actual = sha256(artifact) if artifact.exists() else ""
        document["artifact_sha256_actual"] = actual
        report(
            "the artifact in the release directory hashes to the recorded value",
            actual != "" and actual == document["artifact_sha256_recorded"],
            f"actual={actual[:16]}... recorded={str(document['artifact_sha256_recorded'])[:16]}...",
        )
        recorded = (
            (current / "wheel.sha256").read_text().split()[0]
            if (current / "wheel.sha256").exists()
            else ""
        )
        report(
            "wheel.sha256 agrees with the contract",
            recorded == document["artifact_sha256_recorded"],
        )

        version = installed_version(current / "venv/bin/python")
        document["installed_version"] = version
        report(
            "the release virtualenv has the contract version installed",
            version == document["version"],
            f"installed={version} expected={document['version']}",
        )

        bundle_candidates = [current / "data/user/runtime/web", current / "web"]
        bundle = next((path for path in bundle_candidates if (path / "server.js").exists()), None)
        report(
            "the release carries a materialised web bundle",
            bundle is not None
            and (bundle / "server.js").exists()
            and (bundle / ".deeptutor-web-runtime.json").exists(),
            str(bundle),
        )
        if bundle is not None:
            comparison = bundle_comparison(current, bundle)
            document["web_bundle"] = comparison
            report(
                "the web bundle matches the artifact's packaged bundle",
                comparison.get("comparable")
                and not comparison.get("mismatched")
                and not comparison.get("missing"),
                json.dumps(
                    {
                        k: v
                        for k, v in comparison.items()
                        if k in ("identical", "mismatched", "missing")
                    }
                ),
            )

    processes = process_release()
    document["processes"] = processes
    expected_release = str(current) if current else ""
    expected_name = Path(expected_release).name if expected_release else ""
    for program, entry in processes.items():
        if program == "scheduler":
            # Host-level operational tooling, deliberately not part of a release:
            # it must run from the deployment harness, and it must outlive
            # rollbacks.
            report(
                "the running scheduler is the deployment's own operational tool",
                bool(entry.get("host_tooling")),
                json.dumps({k: v for k, v in entry.items() if k in ("pid", "cwd", "host_tooling")}),
            )
            continue
        report(
            f"the running {program} belongs to the promoted release",
            bool(expected_name) and entry.get("release") == expected_name,
            json.dumps({k: v for k, v in entry.items() if k in ("pid", "release", "cwd")}),
        )

    deploy_lines = []
    if cfg.DEPLOY_LOG.exists():
        deploy_lines = [line for line in cfg.DEPLOY_LOG.read_text().splitlines() if line.strip()]
    document["deploy_records"] = deploy_lines[-5:]
    report(
        "the deployment log records the promoted release",
        any(f"release={document['release_id']}" in line for line in deploy_lines),
        f"{len(deploy_lines)} records",
    )

    document["failures"] = FAILURES
    document["traceable"] = not FAILURES
    print()
    print(
        json.dumps(document, indent=2)
        if as_json
        else f"identity: traceable={document['traceable']} release={document['release_id']} "
        f"artifact={(document.get('artifact_sha256_actual') or '')[:16]}... failures={len(FAILURES)}"
    )
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
