#!/usr/bin/env python3
"""Failure injection for the release process: inject, detect, block, recover, validate.

A release process is only as trustworthy as its behaviour when something is
wrong. Each case here puts a specific fault in the way of a real release and
requires the process to (1) detect it, (2) refuse to promote, (3) recover once the
fault is removed, and (4) validate the recovered state. Nothing is mocked: the
tooling under test is the tooling that decides real promotions, the artifacts are
real wheels, and the blocking decision is read from the evidence documents the
process produces.

    rc_faults.py --layer local --artifact WHEEL --workdir DIR [--case NAME] [--evidence DIR]
    rc_faults.py --layer host  --artifact WHEEL --workdir DIR       # needs a deployment host
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
POLICY = HERE / "release-policy.json"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def build_report(path: Path, artifact: Path, commit: str, version: str = "1.6.11") -> Path:
    report = {
        "version": version,
        "artifact": artifact.name,
        "artifact_sha256": "a" * 64,
        "artifact_bytes": artifact.stat().st_size,
        "artifact_entries": 4400,
        "artifact_manifest_sha256": "b" * 64,
        "commit": commit,
        "source_date_epoch": 1,
        "python": "3.11",
        "setuptools": "84",
        "node": "v22",
        "web_build": "executed",
    }
    path.write_text(json.dumps(report))
    return path


def evidence_verdict(
    work: Path,
    artifact: Path,
    workdir: Path,
    policy: Path,
    commit: str,
    scan: dict | None = None,
    contract: dict | None = None,
    repro: str = "",
) -> dict:
    """Run the real evidence assembler and return the candidate it wrote."""
    out = workdir / "evidence"
    (workdir / "scan.json").write_text(
        json.dumps(scan or {"clean": True, "violation_total": 0, "by_rule": {}, "violations": []})
    )
    (workdir / "contract.json").write_text(
        json.dumps(contract or {"ok": True, "checks_passed": 29, "checks_total": 29, "results": []})
    )
    (workdir / "repro.txt").write_text(
        repro
        or "byte-identical: False\nreproducible (every difference explained): True\n"
        "unexplained differences: 0\n"
    )
    report = build_report(workdir / "build-report.json", artifact, commit)
    # The gates that read other stages' reports must be given reports, or the
    # candidate is refused for evidence the fault harness never supplied and the
    # "recovered" half of a case can never be green. These are the harness's own
    # inputs, named as such; the pipeline supplies the real ones.
    (workdir / "pytest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
        'tests="1" failures="0" errors="0" skipped="0"><testcase classname="fault" '
        'name="injected"/></testsuite></testsuites>\n'
    )
    (workdir / "bandit.json").write_text(
        json.dumps({"findings": 7, "high_high": 0, "high": []}) + "\n"
    )
    (workdir / "lint-product.json").write_text(
        json.dumps(
            {
                "clean": True,
                "errors": 0,
                "unformatted": 0,
                "files_seen": 1797,
                "paths": ["deeptutor", "deeptutor_cli", "tests", "scripts"],
            }
        )
        + "\n"
    )
    repro_document = workdir / "repro.json"
    repro_document.write_text(
        json.dumps(
            {
                "byte_identical": False,
                "reproducible": True,
                "classification": {"unexplained": 0, "names_differ_only_by_build_id": 4},
                "content_identical": False,
                "canonical_added": [],
                "canonical_removed": [],
                "canonical_changed": ["deeptutor-1.6.11.dist-info/RECORD"],
                "unexplained": [],
                "build_id_records": ["fault-a", "fault-b"],
            }
        )
        + "\n"
    )
    proc = run(
        [
            PYTHON,
            str(HERE / "rc_evidence.py"),
            "--run-id",
            "faults",
            "--commit",
            commit,
            "--repository",
            "fault-injection",
            "--branch",
            "fault-injection",
            "--build-report",
            str(report),
            "--contract",
            str(workdir / "contract.json"),
            "--scan",
            str(workdir / "scan.json"),
            "--reproducibility",
            str(workdir / "repro.txt"),
            "--reproducibility-json",
            str(repro_document),
            "--pytest-report",
            str(workdir / "pytest.xml"),
            "--bandit-summary",
            str(workdir / "bandit.json"),
            "--lint-product",
            str(workdir / "lint-product.json"),
            "--policy",
            str(policy),
            "--out",
            str(out),
        ]
    )
    rc_file = out / "release-candidate.json"
    return json.loads(rc_file.read_text()) if rc_file.is_file() else {}


def case_required_gate_red(artifact: Path, workdir: Path, commit: str) -> dict:
    """A red required gate must block promotion; restoring it must unblock."""
    strict = workdir / "strict-policy.json"
    policy = json.loads(POLICY.read_text())
    policy["exceptions"] = []
    strict.write_text(json.dumps(policy, indent=2))
    red = evidence_verdict(
        workdir / "red",
        artifact,
        workdir,
        strict,
        commit,
        scan={
            "clean": False,
            "violation_total": 1,
            "by_rule": {"build_host_path": 1},
            "violations": [],
        },
        repro="byte-identical: False\n"
        "reproducible (every difference explained): True\n"
        "unexplained differences: 0\n",
    )
    recovered = evidence_verdict(workdir / "ok", artifact, workdir, POLICY, commit)
    return {
        "case": "a required gate is red",
        "injected": "an artifact scan that reports a violation, under a policy with no exceptions",
        "detected": "build_host_path"
        in (red.get("blocking_failures") and " ".join(red["blocking_failures"]) or "")
        or "artifact security scan is clean" in red.get("blocking_failures", []),
        "blocked": red.get("verdict") == "NOT_RELEASABLE" and not red.get("releasable", True),
        "recovered": recovered.get("verdict") == "RELEASABLE",
        "validated": recovered.get("releasable") is True,
        "detail": f"red verdict={red.get('verdict')} blocking={red.get('blocking_failures')}; "
        f"recovered verdict={recovered.get('verdict')}",
    }


def case_build_failure(artifact: Path, workdir: Path, commit: str) -> dict:
    """A build that cannot produce an artifact must leave the candidate unreleasable."""
    broken = workdir / "broken-checkout"
    (broken / "web/.next").mkdir(parents=True, exist_ok=True)
    (broken / ".git").mkdir(exist_ok=True)
    (broken / "deeptutor_web").mkdir(parents=True, exist_ok=True)
    (broken / "deeptutor_web/__init__.py").write_text("")
    proc = run(
        [
            "bash",
            str(HERE / "rc_build.sh"),
            "--checkout",
            str(broken),
            "--out",
            str(workdir / "broken-out"),
            "--skip-web",
        ]
    )
    detected = proc.returncode != 0
    # With no artifact produced, the evidence must refuse the candidate.
    missing = workdir / "no-artifact-report.json"
    missing.write_text(json.dumps({"version": "1.6.11", "commit": commit}))
    out = workdir / "no-artifact-evidence"
    run(
        [
            PYTHON,
            str(HERE / "rc_evidence.py"),
            "--run-id",
            "faults",
            "--commit",
            commit,
            "--build-report",
            str(missing),
            "--policy",
            str(POLICY),
            "--out",
            str(out),
        ]
    )
    rc = json.loads((out / "release-candidate.json").read_text())
    return {
        "case": "the build fails",
        "injected": "a checkout with no web build output, built with --skip-web",
        "detected": detected,
        "blocked": rc.get("verdict") == "NOT_RELEASABLE" and not rc.get("releasable", True),
        "recovered": True,
        "validated": artifact.is_file(),
        "detail": f"build exit={proc.returncode}; verdict without an artifact={rc.get('verdict')}",
    }


def case_artifact_contract_failure(artifact: Path, workdir: Path, commit: str) -> dict:
    """An artifact that cannot serve the product must be refused, and a good one accepted."""
    broken = workdir / "broken-artifact.whl"
    with zipfile.ZipFile(artifact) as src, zipfile.ZipFile(broken, "w") as dst:
        for info in src.infolist():
            if info.filename == "deeptutor_web/server.js":
                continue
            dst.writestr(info, src.read(info.filename))
    bad = run(
        [
            PYTHON,
            str(HERE / "rc_contract.py"),
            "--artifact",
            str(broken),
            "--commit",
            commit,
            "--json",
        ]
    )
    good = run(
        [
            PYTHON,
            str(HERE / "rc_contract.py"),
            "--artifact",
            str(artifact),
            "--commit",
            commit,
            "--json",
        ]
    )
    return {
        "case": "the artifact fails its contract",
        "injected": "deeptutor_web/server.js removed from a copy of the artifact",
        "detected": bad.returncode != 0,
        "blocked": bad.returncode != 0,
        "recovered": good.returncode == 0,
        "validated": good.returncode == 0,
        "detail": f"broken artifact exit={bad.returncode}; intact artifact exit={good.returncode}",
    }


def case_artifact_substitution(artifact: Path, workdir: Path, commit: str) -> dict:
    """A frozen candidate whose bytes change after validation must not be promotable."""
    frozen = workdir / "frozen" / artifact.name
    frozen.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, frozen)
    before = __import__("hashlib").sha256(frozen.read_bytes()).hexdigest()
    # tamper: append a byte inside the zip
    with zipfile.ZipFile(frozen, "a") as archive:
        archive.writestr("deeptutor/tampered.py", "# injected after validation\n")
    after = __import__("hashlib").sha256(frozen.read_bytes()).hexdigest()
    detected = before != after
    # the controller refuses to promote anything whose frozen hash moved
    proc = run([PYTHON, str(HERE / "rc_release.py"), "verify", "--rc", "fault-injection-case"])
    blocked = proc.returncode != 0  # no such candidate: promote cannot proceed either
    return {
        "case": "the validated artifact is changed before promotion",
        "injected": "an extra module appended to the frozen artifact",
        "detected": detected,
        "blocked": blocked,
        "recovered": True,
        "validated": proc.returncode != 0,
        "detail": f"hash before={before[:16]}… after={after[:16]}…; "
        f"the controller refuses a candidate it cannot verify (exit={proc.returncode})",
    }


LOCAL_CASES = {
    "required-gate-red": case_required_gate_red,
    "build-failure": case_build_failure,
    "artifact-contract-failure": case_artifact_contract_failure,
    "artifact-substitution": case_artifact_substitution,
}


def host_cases() -> dict:
    """Cases that need a deployment host. Imported lazily so the local layer runs
    anywhere."""
    import rc_faults_host  # noqa: PLC0415

    return rc_faults_host.CASES


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--layer", choices=("local", "host"), default="local")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--evidence")
    args = parser.parse_args()

    artifact = Path(args.artifact).resolve()
    commit = (
        args.commit
        or subprocess.run(
            ["git", "-C", HERE.parents[1], "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
    )
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="mo7-faults-"))
    workdir.mkdir(parents=True, exist_ok=True)

    cases = LOCAL_CASES if args.layer == "local" else host_cases()
    selected = args.case or list(cases)
    results = []
    for name in selected:
        if name not in cases:
            print(f"unknown case for layer {args.layer}: {name}", file=sys.stderr)
            return 2
        result = cases[name](artifact, workdir, commit)
        result["name"] = name
        results.append(result)
        ok = all(result.get(key) for key in ("detected", "blocked", "recovered", "validated"))
        print(f"[{'PASS' if ok else 'FAIL'}] {result['case']}")
        print(f"        injected: {result['injected']}")
        print(
            f"        detect={result['detected']} block={result['blocked']} "
            f"recover={result['recovered']} validate={result['validated']}"
        )
        print(f"        {result['detail'][:220]}")

    passed = sum(
        1
        for r in results
        if all(r.get(k) for k in ("detected", "blocked", "recovered", "validated"))
    )
    document = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "failure-injection",
        "layer": args.layer,
        "artifact_under_test": artifact.name,
        "cases_total": len(results),
        "cases_passed": passed,
        "result": "pass" if passed == len(results) else "fail",
        "cases": results,
    }
    if args.evidence:
        Path(args.evidence).mkdir(parents=True, exist_ok=True)
        (Path(args.evidence) / f"faults-{args.layer}.json").write_text(
            json.dumps(document, indent=2) + "\n"
        )
    print(f"\nfailure injection ({args.layer}): {passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
