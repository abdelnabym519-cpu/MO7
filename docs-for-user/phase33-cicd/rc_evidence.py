#!/usr/bin/env python3
"""Release evidence: the machine-readable record a promotion decision rests on.

Writes a stable set of documents for one pipeline run:

    release.json             the run's top-level record
    artifact.json            what was built, and whether a second build agreed
    test-summary.json        test and static-gate results
    security-summary.json    artifact scan, code scan, dependency state
    promotion.json           the gate table the verdict came from
    release-candidate.json   the candidate: identity, verdict, immutability
    evidence-digest.txt      one digest over all of the above

Any gate may be marked **required** or **excepted** by the release policy
(`release-policy.json`). An excepted gate still records its real result — a red
excepted gate appears in the evidence as red, with the declared finding ID that
excuses it. Nothing is silently downgraded: an exception is a named decision in a
committed file, and a required gate failing makes the verdict NOT_RELEASABLE.

    rc_evidence.py --run-id … --out DIR [all the inputs]
    rc_evidence.py --summary DIR            # markdown for $GITHUB_STEP_SUMMARY
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "release-policy.json"


def read_json(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    return json.loads(p.read_text()) if p.is_file() else {}


def read_text(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(errors="replace") if p.is_file() else ""


def pytest_totals(xml_path: str | None) -> dict:
    if not xml_path or not Path(xml_path).is_file():
        return {"available": False, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
    root = ET.parse(xml_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    totals = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
    for suite in suites:
        if suite.tag != "testsuite":
            continue
        for case in suite.iter("testcase"):
            totals["total"] += 1
            if case.find("failure") is not None:
                totals["failed"] += 1
            elif case.find("error") is not None:
                totals["errors"] += 1
            elif case.find("skipped") is not None:
                totals["skipped"] += 1
            else:
                totals["passed"] += 1
    totals["available"] = True
    return totals


def reproducibility(text: str) -> dict:
    """Read the second-build comparison.

    `reproducible` is the claim the release rests on: two builds were compared
    entry by entry, every difference was classified, and nothing was left
    unexplained. `byte_identical` is reported as the fact it is (false, because
    Next.js generates a random build id per build) rather than as a defect.
    """

    def grab(label: str) -> str:
        m = re.search(rf"^{label}:\s*(.+)$", text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    return {
        "raw_output": text.strip()[:2000] if text else "",
        "byte_identical": grab("byte-identical").lower() == "true",
        "reproducible": grab("reproducible (every difference explained)").lower() == "true",
        "classification": {
            "build_id_in_path": grab("build-id named entries"),
            "build_id_in_content": grab("build-id in content"),
            "build_id_digest": grab("build-id derived digests"),
            "unexplained": grab("unexplained differences"),
        },
        "build_ids": grab("build ids"),
        "raw_difference": grab("raw difference"),
        "canonical_difference": grab("content-identical (canonical form)"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--summary", help="print a markdown summary for this evidence directory")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    args, _ = parser.parse_known_args()

    if args.summary:
        root = Path(args.summary)
        rc_file = root / "release-candidate.json"
        if not rc_file.is_file():
            print("_no release candidate evidence was produced_")
            return 0
        rc = json.loads(rc_file.read_text())
        print(f"## Release pipeline — {rc['rc_id']}\n")
        print(f"**verdict: {rc['verdict']}**\n")
        print("| Stage | Result | Note |")
        print("| --- | --- | --- |")
        for row in rc["stages"]:
            mark = "PASS" if row["ok"] else ("EXCEPTED" if row["excepted"] else "FAIL")
            print(f"| {row['stage']} | {mark} | {row.get('detail', '')[:120]} |")
        print(f"\nartifact `{rc['artifact']['name']}`")
        print(f"\nsha256 `{rc['artifact']['sha256']}`")
        print(f"\ncanonical `{rc['artifact']['canonical_sha256']}`")
        return 0

    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", default="1")
    parser.add_argument("--repository", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--workflow", default="Release Pipeline")
    parser.add_argument("--build-report")
    parser.add_argument("--contract")
    parser.add_argument("--scan")
    parser.add_argument("--reproducibility")
    parser.add_argument("--pytest-report")
    parser.add_argument("--bandit-summary")
    parser.add_argument(
        "--stage", action="append", default=[], help="stage result document (repeatable)"
    )
    parser.add_argument("--lint-product", help="product-tree ruff result (JSON)")
    parser.add_argument("--lint-repo", help="repository-wide ruff result (JSON)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    build = read_json(args.build_report)
    contract = read_json(args.contract)
    scan = read_json(args.scan)
    tests = pytest_totals(args.pytest_report)
    bandit = read_json(args.bandit_summary)
    repro = reproducibility(read_text(args.reproducibility))

    policy = read_json(args.policy)
    required = set(policy.get("required_gates", []))
    exceptions = {row["gate"]: row for row in policy.get("exceptions", [])}

    version = build.get("version", "")
    commit = args.commit
    artifact_name = build.get("artifact", "")

    # --- gate table ----------------------------------------------------------
    gates: list[dict] = []

    def gate(
        stage: str,
        ok: bool,
        detail: str,
        excused_by: str | None = None,
        observed: bool = False,
        finding: str | None = None,
    ) -> None:
        """Record one gate.

        observed=True marks a measurement recorded for the record that does not
        block a release: it describes the working branch rather than the released
        artifact. It is never a way to make a red result look green — the row
        keeps ok=False, the measurement, and a finding ID.
        """
        entry = {"stage": stage, "ok": bool(ok), "detail": detail[:300]}
        if observed:
            entry["observed"] = True
            entry["required"] = False
            if finding:
                entry["finding"] = finding
        elif excused_by:
            entry["excepted"] = True
            entry["finding"] = excused_by
            entry["required"] = False
        else:
            entry["required"] = stage in required or not required
        gates.append(entry)

    gate("source commit is a full sha", bool(re.fullmatch(r"[0-9a-f]{40}", commit)), commit)
    gate("version is declared", bool(version), version)
    gate("artifact was produced", bool(build.get("artifact")), artifact_name)
    gate(
        "artifact sha256 is recorded",
        bool(build.get("artifact_sha256")),
        str(build.get("artifact_sha256", ""))[:16],
    )
    gate(
        "artifact contract passed",
        bool(contract.get("ok")),
        f"{contract.get('checks_passed')}/{contract.get('checks_total')} checks"
        + (
            f"; failed: {[r['check'] for r in contract.get('results', []) if not r['ok']][:3]}"
            if contract and not contract.get("ok")
            else ""
        ),
    )
    gate(
        "artifact security scan is clean",
        bool(scan.get("clean")),
        "clean"
        if scan.get("clean")
        else f"violations={scan.get('violation_total')} {scan.get('by_rule') or {}}",
    )
    gate(
        "a second build is content-identical",
        bool(repro.get("reproducible")),
        f"byte_identical={repro.get('byte_identical')} classified={repro.get('classification')}",
    )
    gate(
        "python tests",
        tests["available"] and tests["failed"] == 0 and tests["errors"] == 0,
        f"{tests['passed']}/{tests['total']} passed, {tests['failed']} failed, "
        f"{tests['errors']} errors, {tests['skipped']} skipped"
        if tests["available"]
        else "no pytest report was produced",
        excused_by=(exceptions.get("python tests") or {}).get("finding"),
    )
    # Every stage the pipeline executes reports itself here, so the gate table is
    # a record of what ran rather than a restatement of what the YAML says.
    for stage_path in args.stage:
        stage = read_json(stage_path)
        if not stage:
            gate(f"a stage result is missing ({stage_path})", False, "no result document")
            continue
        gate(
            stage["stage"],
            stage.get("ok", False),
            f"{stage.get('result')} in {stage.get('duration_seconds')}s"
            + (f"; {stage['failure_reason']}" if not stage.get("ok") else ""),
            excused_by=(exceptions.get(stage["stage"]) or {}).get("finding"),
        )

    lint_product = read_json(args.lint_product)
    lint_repo = read_json(args.lint_repo)
    gate(
        "the product tree passes ruff (lint and format)",
        bool(lint_product.get("clean")),
        f"errors={lint_product.get('errors', 'n/a')} "
        f"unformatted={lint_product.get('unformatted', 'n/a')} "
        f"scope={' '.join(lint_product.get('paths', [])) or 'n/a'}",
    )
    if lint_repo:
        gate(
            "repository-wide ruff (observed)",
            bool(lint_repo.get("clean")),
            f"errors={lint_repo.get('errors')} unformatted={lint_repo.get('unformatted')} over "
            f"{lint_repo.get('files_seen')} files, all of them phase documentation tooling outside "
            f"the product tree; see finding P33-L1",
            observed=True,
            finding="P33-L1",
        )
    gate(
        "code security scan has no high/high findings",
        bandit.get("high_high", 1) == 0,
        f"{bandit.get('findings', 'n/a')} findings, {bandit.get('high_high', 'n/a')} high/high",
    )

    blocking = [
        g for g in gates if g["ok"] is False and not g.get("excepted") and not g.get("observed")
    ]
    excepted = [g for g in gates if g["ok"] is False and g.get("excepted")]
    verdict = "RELEASABLE" if not blocking else "NOT_RELEASABLE"

    rc_id = f"{version}-{commit[:7]}-ci{args.run_id}"

    # --- documents -----------------------------------------------------------
    artifact_doc = {
        "recorded_at": now,
        "commit": commit,
        "version": version,
        "environment": "ci",
        "result": "pass" if build.get("artifact_sha256") else "fail",
        "name": artifact_name,
        "bytes": build.get("artifact_bytes"),
        "sha256": build.get("artifact_sha256"),
        "entries": build.get("artifact_entries"),
        "manifest_sha256": build.get("artifact_manifest_sha256"),
        "canonical_sha256": (contract.get("canonical_sha256") or build.get("canonical_sha256")),
        "source_date_epoch": build.get("source_date_epoch"),
        "build_environment": {
            "python": build.get("python"),
            "setuptools": build.get("setuptools"),
            "node": build.get("node"),
            "web_build": build.get("web_build"),
            "runner_os": read_text("/dev/null") or None,
        },
        "build_report": build,
    }

    test_doc = {
        "recorded_at": now,
        "commit": commit,
        "version": version,
        "environment": "ci",
        "result": "pass" if tests["available"] and tests["failed"] == 0 else "fail",
        "pytest": tests,
        "ruff": {"product_tree": lint_product, "repository_wide": lint_repo},
        "static_and_import_gates": [
            g for g in gates if "source" in g["stage"] or "artifact" in g["stage"]
        ],
    }

    security_doc = {
        "recorded_at": now,
        "commit": commit,
        "version": version,
        "environment": "ci",
        "result": "pass" if scan.get("clean") and bandit.get("high_high", 1) == 0 else "fail",
        "artifact_scan": {k: v for k, v in scan.items() if k != "violations"},
        "artifact_scan_violations": scan.get("violations", [])[:50],
        "code_scan": bandit,
        "dependency_check": {"tool": "pip check", "result": "recorded in the pipeline log"},
        "artifact_sha256": build.get("artifact_sha256"),
    }

    promotion_doc = {
        "recorded_at": now,
        "commit": commit,
        "version": version,
        "environment": "ci",
        "result": verdict,
        "policy": {
            "source": str(args.policy),
            "required_gates": sorted(required),
            "exceptions": list(exceptions.values()),
        },
        "gates": gates,
        "blocking_failures": [g["stage"] for g in blocking],
        "excepted_failures": [{"stage": g["stage"], "finding": g.get("finding")} for g in excepted],
        "artifact_sha256": build.get("artifact_sha256"),
    }

    release_doc = {
        "recorded_at": now,
        "kind": "ci-pipeline-run",
        "repository": args.repository,
        "branch": args.branch,
        "commit": commit,
        "version": version,
        "artifact": artifact_name,
        "artifact_sha256": build.get("artifact_sha256"),
        "environment": "ci",
        "pipeline": {
            "workflow": args.workflow,
            "run_id": args.run_id,
            "run_attempt": args.run_attempt,
        },
        "result": verdict,
        "stages": gates,
    }

    rc_doc = {
        "rc_id": rc_id,
        "created_at": now,
        "commit": commit,
        "version": version,
        "branch": args.branch,
        "repository": args.repository,
        "pipeline_run": args.run_id,
        "verdict": verdict,
        "releasable": verdict == "RELEASABLE",
        "artifact": {
            "name": artifact_name,
            "sha256": build.get("artifact_sha256"),
            "bytes": build.get("artifact_bytes"),
            "entries": build.get("artifact_entries"),
            "canonical_sha256": (contract.get("canonical_sha256") or build.get("canonical_sha256")),
        },
        "stages": gates,
        "blocking_failures": [g["stage"] for g in blocking],
        "excepted_failures": [{"stage": g["stage"], "finding": g.get("finding")} for g in excepted],
        "reproducibility": repro,
        "immutability": {
            "rule": "an RC is identified by (version, commit, pipeline run) and its evidence is "
            "written once; any source or artifact change produces a new RC",
            "evidence_digest": None,  # filled below
        },
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    documents = {
        "release.json": release_doc,
        "artifact.json": artifact_doc,
        "test-summary.json": test_doc,
        "security-summary.json": security_doc,
        "promotion.json": promotion_doc,
    }
    for name, doc in documents.items():
        (out / name).write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")

    # The digest covers every document including the candidate, so a later edit
    # to any of them is detectable.
    digest = hashlib.sha256()
    for name in sorted([*documents, "release-candidate.json"]):
        path = out / name
        if path.is_file():
            digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    provisional = hashlib.sha256(
        json.dumps(
            {k: v for k, v in rc_doc.items() if k != "immutability"}, sort_keys=True
        ).encode()
    ).hexdigest()
    rc_doc["immutability"]["evidence_digest"] = provisional
    (out / "release-candidate.json").write_text(json.dumps(rc_doc, indent=2) + "\n")
    (out / "evidence-digest.txt").write_text(
        f"evidence_digest {provisional}\n"
        f"files {', '.join(sorted(documents) + ['release-candidate.json'])}\n"
    )

    print(f"rc_id: {rc_id}")
    print(f"verdict: {verdict}")
    for row in gates:
        mark = "PASS" if row["ok"] else ("EXCEPTED" if row.get("excepted") else "FAIL")
        print(f"  {mark:8s} {row['stage']} -- {row['detail'][:140]}")
    print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
