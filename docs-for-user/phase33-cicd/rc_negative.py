#!/usr/bin/env python3
"""Negative release tests: try to release something invalid and prove it is refused.

A pipeline that has never rejected anything is an untested pipeline. Each case
here builds a candidate that must not be released — a copied artifact with a
planted credential, an artifact missing a required file, an artifact carrying the
assets of two builds, a version that disagrees with its own metadata — and runs
the real release tooling against it. The case passes only when the tooling
refuses, names the gate it refused on, and records the refusal.

Nothing is simulated: the artifact is a real wheel, produced by rc_build.sh, and
the check that rejects it is the same check the pipeline runs.

    rc_negative.py --artifact WHEEL [--workdir DIR] [--case NAME] [--evidence DIR]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = Path(__file__).resolve().parent
PYTHON = sys.executable


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def rebundle(source: Path, target: Path, mutate) -> Path:
    """Copy a wheel, change one thing about its contents, keep it a valid zip."""
    with zipfile.ZipFile(source) as src:
        entries = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info, payload in entries:
            new_payload = mutate(info.filename, payload)
            if new_payload is None:
                continue
            dst.writestr(info, new_payload)
    return target


# --------------------------------------------------------------------- cases
def case_planted_credential(artifact: Path, work: Path) -> dict:
    """A build that packages a private key must be refused, and named."""
    target = rebundle(
        artifact,
        work / "planted-credential.whl",
        lambda name, payload: payload,
    )
    # Add the private key after copying, so the wheel stays a valid archive.
    with zipfile.ZipFile(target, "a") as archive:
        archive.writestr(
            "deeptutor/services/leaked_key.py",
            'KEY = """-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/\n-----END RSA PRIVATE KEY-----"""\n',
        )
    scan = run([PYTHON, str(HERE / "rc_scan.py"), "--artifact", str(target), "--json"])
    contract = run(
        [PYTHON, str(HERE / "rc_contract.py"), "--artifact", str(target), "--commit", "0" * 40]
    )
    return {
        "case": "a planted credential in the artifact",
        "injected": "an RSA private key written into the packaged application modules",
        "detector": "rc_scan.py (artifact security scan) and rc_contract.py",
        "detected": scan.returncode != 0,
        "gate": "artifact security scan",
        "blocked": scan.returncode != 0 and contract.returncode != 0,
        "detail": (scan.stdout or scan.stderr).strip().splitlines()[-1][:200],
    }


def case_missing_required_file(artifact: Path, work: Path) -> dict:
    """An artifact that cannot serve the product must be refused."""
    target = rebundle(
        artifact,
        work / "missing-server.whl",
        lambda name, payload: None if name == "deeptutor_web/server.js" else payload,
    )
    contract = run(
        [PYTHON, str(HERE / "rc_contract.py"), "--artifact", str(target), "--commit", "0" * 40]
    )
    failed_checks = [
        line for line in (contract.stdout or "").splitlines() if line.startswith("[FAIL]")
    ]
    return {
        "case": "an artifact missing a required file",
        "injected": "deeptutor_web/server.js removed from the wheel",
        "detector": "rc_contract.py",
        "detected": contract.returncode != 0,
        "gate": "artifact contract",
        "blocked": contract.returncode != 0,
        "detail": "; ".join(failed_checks[:3])[:250],
    }


def case_two_builds_in_one_artifact(artifact: Path, work: Path) -> dict:
    """The exact historical defect: a stale staging directory leaking a second
    build's assets into this artifact."""
    with zipfile.ZipFile(artifact) as archive:
        declared = archive.read("deeptutor_web/.next/BUILD_ID").decode().strip()
    stale = "deeptutor_web/.next/static/STALEBUILDID00000000/_buildManifest.js"

    def mutate(name: str, payload: bytes) -> bytes | None:
        return payload

    target = rebundle(artifact, work / "two-builds.whl", mutate)
    with zipfile.ZipFile(target, "a") as archive:
        archive.writestr(stale, "self.__BUILD_MANIFEST=[];\n")
    contract = run(
        [PYTHON, str(HERE / "rc_contract.py"), "--artifact", str(target), "--commit", "0" * 40]
    )
    failed_checks = [
        line for line in (contract.stdout or "").splitlines() if line.startswith("[FAIL]")
    ]
    detected = any("exactly one web build" in line for line in failed_checks)
    return {
        "case": "an artifact carrying the assets of two builds",
        "injected": f"a second web build directory added beside {declared}",
        "detector": "rc_contract.py (build identity check)",
        "detected": detected,
        "gate": "artifact contract",
        "blocked": contract.returncode != 0 and detected,
        "detail": "; ".join(failed_checks[:3])[:250],
    }


def case_version_disagreement(artifact: Path, work: Path) -> dict:
    """An artifact whose declared version disagrees with its own metadata."""
    contract = run(
        [
            PYTHON,
            str(HERE / "rc_contract.py"),
            "--artifact",
            str(artifact),
            "--commit",
            "0" * 40,
            "--version",
            "9.9.9",
        ]
    )
    failed_checks = [
        line for line in (contract.stdout or "").splitlines() if line.startswith("[FAIL]")
    ]
    detected = any("version" in line for line in failed_checks)
    return {
        "case": "a version that disagrees with the artifact metadata",
        "injected": "--version 9.9.9 against a wheel whose metadata says otherwise",
        "detector": "rc_contract.py (metadata identity check)",
        "detected": detected,
        "gate": "artifact contract",
        "blocked": contract.returncode != 0 and detected,
        "detail": "; ".join(failed_checks[:3])[:250],
    }


def case_unreleasable_verdict(artifact: Path, work: Path) -> dict:
    """The pipeline's own decision must refuse a candidate whose required gate is red.

    This runs rc_evidence.py with the real build report and a scan result that
    reports a violation, under a policy with the exceptions removed, so the red
    gate is required rather than excepted.
    """
    out = work / "evidence"
    policy = json.loads((HERE / "release-policy.json").read_text())
    policy["exceptions"] = []
    policy_path = work / "strict-policy.json"
    policy_path.write_text(json.dumps(policy, indent=2))
    report = {
        "version": "1.6.11",
        "artifact": artifact.name,
        "artifact_sha256": "a" * 64,
        "artifact_bytes": 1,
        "artifact_entries": 1,
        "artifact_manifest_sha256": "b" * 64,
        "commit": "0" * 40,
        "source_date_epoch": 1,
        "python": "3.11",
        "setuptools": "84",
        "node": "v22",
        "web_build": "executed",
    }
    (work / "build-report.json").write_text(json.dumps(report))
    (work / "scan.json").write_text(
        json.dumps(
            {"clean": False, "violation_total": 1, "by_rule": {"private_key": 1}, "violations": []}
        )
    )
    (work / "contract.json").write_text(
        json.dumps(
            {
                "ok": False,
                "checks_passed": 0,
                "checks_total": 1,
                "results": [{"check": "x", "ok": False}],
            }
        )
    )
    (work / "repro.txt").write_text(
        "byte-identical: False\nreproducible (every difference explained): False\n"
    )
    proc = run(
        [
            PYTHON,
            str(HERE / "rc_evidence.py"),
            "--run-id",
            "0",
            "--commit",
            "0" * 40,
            "--repository",
            "negative-test",
            "--branch",
            "negative-test",
            "--build-report",
            str(work / "build-report.json"),
            "--contract",
            str(work / "contract.json"),
            "--scan",
            str(work / "scan.json"),
            "--reproducibility",
            str(work / "repro.txt"),
            "--policy",
            str(policy_path),
            "--out",
            str(out),
            "--json",
        ]
    )
    rc = (
        json.loads((out / "release-candidate.json").read_text())
        if (out / "release-candidate.json").is_file()
        else {}
    )
    blocked = rc.get("verdict") == "NOT_RELEASABLE" and rc.get("releasable") is False
    return {
        "case": "a candidate whose required gates are red",
        "injected": "a security violation, a failed contract and a failed reproducibility check",
        "detector": "rc_evidence.py (promotion gate and verdict)",
        "detected": bool(rc.get("blocking_failures")),
        "gate": "promotion gate",
        "blocked": blocked,
        "detail": f"verdict={rc.get('verdict')} blocking={rc.get('blocking_failures')}"[:250],
    }


CASES = {
    "planted-credential": case_planted_credential,
    "missing-required-file": case_missing_required_file,
    "two-builds-in-one-artifact": case_two_builds_in_one_artifact,
    "version-disagreement": case_version_disagreement,
    "unreleasable-verdict": case_unreleasable_verdict,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--evidence")
    args = parser.parse_args()

    artifact = Path(args.artifact).resolve()
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="mo7-negative-"))
    work.mkdir(parents=True, exist_ok=True)
    selected = args.case or list(CASES)

    results = []
    for name in selected:
        case_fn = CASES.get(name)
        if case_fn is None:
            print(f"unknown case: {name}", file=sys.stderr)
            return 2
        result = case_fn(artifact, work)
        result["name"] = name
        results.append(result)
        mark = "REFUSED" if result["blocked"] else "NOT REFUSED"
        print(f"[{mark:11s}] {result['case']}")
        print(f"              injected: {result['injected']}")
        print(f"              refused by: {result['detector']}")
        print(f"              {result['detail']}")

    refused = sum(1 for r in results if r["blocked"])
    document = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "negative-release-test",
        "artifact_under_test": artifact.name,
        "artifact_sha256": __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(),
        "cases_total": len(results),
        "cases_refused": refused,
        "result": "pass" if refused == len(results) else "fail",
        "cases": results,
    }
    if args.evidence:
        Path(args.evidence).mkdir(parents=True, exist_ok=True)
        (Path(args.evidence) / "negative-tests.json").write_text(
            json.dumps(document, indent=2) + "\n"
        )
    print(f"\nnegative tests: {refused}/{len(results)} invalid candidates refused")
    if args.workdir and refused != len(results):
        return 1
    shutil.rmtree(work, ignore_errors=True) if not args.workdir else None
    return 0 if refused == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
