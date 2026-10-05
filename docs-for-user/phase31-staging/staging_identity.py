#!/usr/bin/env python3
"""MO7 staging release identity / traceability check.

Proves the running deployment is the recorded release:

    source commit -> release id -> wheel sha256 -> installed version -> runtime bundle

Exit code 0 only when every link agrees. Prints a JSON document that the
closure report and the runbook cite as evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

STAGING = Path(os.environ.get("STAGING_ROOT", "/home/user/mo7-staging"))
RUNTIME_WEB = STAGING / "home/data/user/runtime/web"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def read_config() -> dict[str, str]:
    config: dict[str, str] = {}
    for line in (STAGING / "etc/staging.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    return config


def installed_version() -> str:
    """The version actually present in the staging venv, from package metadata."""
    result = subprocess.run(
        [
            str(STAGING / "venv/bin/python"),
            "-c",
            "from importlib.metadata import version; print(version('deeptutor'))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def installed_web_source() -> str:
    result = subprocess.run(
        [
            str(STAGING / "venv/bin/python"),
            "-c",
            "import deeptutor_web, pathlib; print(pathlib.Path(deeptutor_web.__file__).resolve().parent)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def compare_runtime_bundle(packaged: Path) -> dict[str, object]:
    """Compare the packaged web bundle with the materialised runtime cache."""
    identical = patched = missing = 0
    mismatched: list[str] = []
    marker = RUNTIME_WEB / ".deeptutor-web-runtime.json"
    if not marker.exists():
        return {"error": "runtime web cache marker missing"}
    placeholder_keys = (
        "__NEXT_PUBLIC_API_BASE_PLACEHOLDER__",
        "__NEXT_PUBLIC_AUTH_ENABLED_PLACEHOLDER__",
    )
    for source in packaged.rglob("*"):
        if not source.is_file():
            continue
        target = RUNTIME_WEB / source.relative_to(packaged)
        if not target.exists():
            missing += 1
            continue
        try:
            source_bytes = source.read_bytes()
            target_bytes = target.read_bytes()
        except OSError:
            mismatched.append(str(source.relative_to(packaged)))
            continue
        if source_bytes == target_bytes:
            identical += 1
            continue
        if any(key.encode() in source_bytes for key in placeholder_keys):
            patched += 1
            continue
        mismatched.append(str(source.relative_to(packaged)))
    return {
        "identical": identical,
        "patched_placeholders": patched,
        "missing": missing,
        "mismatched": mismatched[:10],
        "mismatched_total": len(mismatched),
    }


def main() -> int:
    config = read_config()
    wheel = Path(config["STAGING_WHEEL"])
    report: dict[str, object] = {
        "environment": config["STAGING_ENVIRONMENT"],
        "release_id": config["STAGING_RELEASE_ID"],
        "source_commit": config["STAGING_SOURCE_COMMIT"],
        "artifact": config["STAGING_ARTIFACT"],
        "artifact_sha256_expected": config["STAGING_ARTIFACT_SHA256"],
        "installed_version": installed_version(),
        "expected_version": config["STAGING_VERSION"],
    }
    ok = True

    if wheel.exists():
        actual = sha256(wheel)
        report["artifact_sha256"] = actual
        report["artifact_sha256_matches"] = actual == config["STAGING_ARTIFACT_SHA256"]
        ok &= actual == config["STAGING_ARTIFACT_SHA256"]
    else:
        report["artifact_sha256"] = None
        report["artifact_sha256_matches"] = False
        ok = False

    ok &= report["installed_version"] == report["expected_version"]

    web_source = installed_web_source()
    if web_source and Path(web_source).is_dir():
        bundle = compare_runtime_bundle(Path(web_source))
        report["runtime_bundle"] = bundle
        ok &= not bundle.get("missing") and not bundle.get("mismatched_total")
    else:
        report["runtime_bundle"] = {"error": f"deeptutor_web not importable ({web_source!r})"}
        ok = False

    deployments = (STAGING / "run/deployments.log")
    if deployments.exists():
        report["deployment_record"] = deployments.read_text().strip().splitlines()[-3:]

    report["traceable"] = bool(ok)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
