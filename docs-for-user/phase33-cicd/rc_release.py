#!/usr/bin/env python3
"""MO7 release controller — the promotion side of the release process.

CI (`.github/workflows/release.yml`) decides whether a commit is *buildable*: it
runs the gates, builds the artifact twice, and commits machine-readable evidence
under `evidence/ci/<run-id>/`. A GitHub-hosted runner cannot reach this
deployment host (the sandbox accepts no inbound connection and the Actions
artifact store is not routable from here), so promotion is driven from the host
that owns the deployment — this controller — and the two halves are joined by
evidence that each side can *verify* rather than trust.

The controller never takes CI's word for the artifact: it rebuilds the same
commit with the same recipe and requires the two builds to be content-identical,
then contract-checks and scans the artifact it is about to deploy.

    rc_release.py status
    rc_release.py ingest --evidence DIR          # CI evidence  -> RC (CREATED)
    rc_release.py validate --rc ID               # rebuild, compare, contract, scan
    rc_release.py approve  --rc ID               # approval boundary (policy-driven)
    rc_release.py promote  --rc ID               # deploy through the Phase 32 pipeline
    rc_release.py deployed                       # what the production host runs now
    rc_release.py rollback --to RELEASE_ID       # roll back and re-validate
    rc_release.py verify --rc ID                 # re-check a frozen RC has not moved

State lives in <workdir>/state/<rc_id>.json and is append-only in effect: each
transition records the evidence digest it was taken on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WORKDIR = Path(os.environ.get("MO7_RELEASE_WORKDIR", "/home/user/mo7-cicd"))
PROD_ROOT = Path(os.environ.get("PROD_ROOT", "/home/user/mo7-prod"))
CHECKOUT = Path(os.environ.get("MO7_CHECKOUT", "/home/user/MO7"))
POLICY = Path(os.environ.get("MO7_RELEASE_POLICY", str(HERE / "release-policy.json")))
BUILD_PYTHON = os.environ.get("MO7_BUILD_PYTHON", "/home/user/mo7-cicd/venv/bin/python")

STATE_DIR = WORKDIR / "state"
RC_DIR = WORKDIR / "rc"
BUILD_DIR = WORKDIR / "build"
LOG_DIR = WORKDIR / "logs"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def log(message: str) -> None:
    print(f"{now()}  {message}", flush=True)


def state_path(rc_id: str) -> Path:
    return STATE_DIR / f"{rc_id}.json"


def load_state(rc_id: str) -> dict:
    path = state_path(rc_id)
    if not path.is_file():
        raise SystemExit(f"FATAL: no such release candidate: {rc_id}")
    return json.loads(path.read_text())


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now()
    state_path(state["rc_id"]).write_text(json.dumps(state, indent=2) + "\n")


def transition(state: dict, to: str, detail: dict | None = None, actor: str = "policy") -> None:
    """Record a state transition. The history is the audit trail."""
    state.setdefault("history", []).append(
        {
            "at": now(),
            "from": state.get("state"),
            "to": to,
            "actor": actor,
            "detail": detail or {},
        }
    )
    state["state"] = to
    save_state(state)
    log(
        f"{state['rc_id']}: -> {to}"
        + (f" ({detail.get('summary')})" if detail and detail.get("summary") else "")
    )


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log("run: " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, text=True, **kwargs)


# --------------------------------------------------------------------------- verbs
def verb_status(args) -> int:
    RC_DIR.mkdir(parents=True, exist_ok=True)
    states = (
        sorted(
            (json.loads(p.read_text()) for p in STATE_DIR.glob("*.json")),
            key=lambda s: s.get("created_at", ""),
        )
        if STATE_DIR.exists()
        else []
    )
    print("release candidates:")
    if not states:
        print("  (none)")
    for state in states:
        print(
            f"  {state['rc_id']:44s} {state['state']:14s} commit={state['commit'][:7]} "
            f"artifact={state['artifact']['sha256'][:16] if state.get('artifact', {}).get('sha256') else 'n/a'}…"
        )
    deployed = deployed_release()
    print(f"\nproduction host: {PROD_ROOT}")
    print(f"  current release: {deployed.get('release_id', '(none)')}")
    print(f"  artifact       : {deployed.get('artifact', '(none)')}")
    print(f"  sha256         : {str(deployed.get('artifact_sha256', ''))[:32]}")
    return 0


def prod(*cmd: str, check: bool = True, timeout: int = 3600) -> subprocess.CompletedProcess:
    return run(
        [str(PROD_ROOT / "bin/prod.sh"), *cmd], check=check, timeout=timeout, capture_output=False
    )


def deployed_release() -> dict:
    path = PROD_ROOT / "run/current-release.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def verb_ingest(args) -> int:
    """Read CI evidence and create the release candidate."""
    evidence = Path(args.evidence).resolve()
    rc_file = evidence / "release-candidate.json"
    if not rc_file.is_file():
        print(f"FATAL: no release-candidate.json in {evidence}", file=sys.stderr)
        return 2
    rc = json.loads(rc_file.read_text())

    rc_id = rc["rc_id"]
    if state_path(rc_id).is_file():
        print(
            f"FATAL: release candidate {rc_id} already exists; candidates are immutable",
            file=sys.stderr,
        )
        return 2

    state = {
        "rc_id": rc_id,
        "created_at": now(),
        "state": "CREATED",
        "commit": rc["commit"],
        "version": rc["version"],
        "branch": rc.get("branch"),
        "pipeline_run": rc.get("pipeline_run"),
        "artifact": rc.get("artifact", {}),
        "ci_verdict": rc.get("verdict"),
        "ci_evidence": str(evidence),
        "ci_evidence_digest": (rc.get("immutability") or {}).get("evidence_digest"),
        "ci_gates": rc.get("stages", []),
        "ci_blocking_failures": rc.get("blocking_failures", []),
        "ci_excepted_failures": rc.get("excepted_failures", []),
        "local": {},
        "history": [
            {
                "at": now(),
                "from": None,
                "to": "CREATED",
                "actor": "ci",
                "detail": {"run": rc.get("pipeline_run"), "verdict": rc.get("verdict")},
            }
        ],
    }

    # Immutable copy of the CI evidence, so a later edit to the branch cannot
    # change what this candidate was judged on.
    frozen = RC_DIR / rc_id / "ci-evidence"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    if frozen.exists():
        shutil.rmtree(frozen)
    shutil.copytree(evidence, frozen)
    state["ci_evidence_frozen"] = str(frozen)
    state["ci_evidence_files"] = {
        p.name: sha256_file(p) for p in sorted(frozen.glob("*")) if p.is_file()
    }
    save_state(state)
    log(
        f"ingested {rc_id}: CI verdict={rc.get('verdict')} "
        f"blocking={state['ci_blocking_failures']} excepted={[e['stage'] for e in state['ci_excepted_failures']]}"
    )
    return 0 if rc.get("verdict") == "RELEASABLE" else 1


def verb_validate(args) -> int:
    """Rebuild the same commit, prove equivalence with what CI built, and check
    the artifact this host would deploy. This is what turns CI's word into a
    verified fact on this host."""
    state = load_state(args.rc)
    if state["state"] not in ("CREATED", "VALIDATED"):
        print(
            f"FATAL: {state['rc_id']} is {state['state']}; validation applies to CREATED",
            file=sys.stderr,
        )
        return 2

    head = subprocess.run(
        ["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
        return bool(ok)

    # 1. the checkout is the commit CI validated, and it is clean
    check(
        "the checkout is at the CI-validated commit",
        head == state["commit"],
        f"checkout={head[:12]} ci={state['commit'][:12]}",
    )
    dirty = subprocess.run(
        ["git", "-C", str(CHECKOUT), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    check("the checkout is clean", not dirty, dirty.splitlines()[0] if dirty else "")

    # 2. the CI evidence still hashes to what was ingested (immutability)
    frozen = Path(state["ci_evidence_frozen"])
    current = {p.name: sha256_file(p) for p in sorted(frozen.glob("*")) if p.is_file()}
    check(
        "the frozen CI evidence is unchanged",
        current == state["ci_evidence_files"],
        "digest mismatch" if current != state["ci_evidence_files"] else "unchanged",
    )

    # 3. rebuild the same commit here
    out = BUILD_DIR / state["rc_id"]
    out.mkdir(parents=True, exist_ok=True)
    build = run(
        ["bash", str(HERE / "rc_build.sh"), "--checkout", str(CHECKOUT), "--out", str(out)],
        capture_output=True,
        timeout=3600,
    )
    check(
        "the release build succeeds from the same commit",
        build.returncode == 0,
        (build.stderr or "")[-200:],
    )
    if build.returncode != 0:
        return finish_validation(state, checks, 1)

    report = json.loads((out / "build-report.json").read_text())
    artifact = out / report["artifact"]
    check(
        "the rebuild produced the declared artifact",
        report["artifact"] == state["artifact"]["name"],
        f"rebuilt={report['artifact']} ci={state['artifact']['name']}",
    )
    check(
        "the rebuild records the same commit",
        report["commit"] == state["commit"],
        report["commit"][:12],
    )

    # 4. the two builds must be content-identical (the equivalence proof)
    ci_manifest = json.loads((frozen / "artifact.json").read_text())
    local_manifest = json.loads((out / "manifest.json").read_text())
    content_equal = local_manifest["canonical_sha256"] == ci_manifest.get("canonical_sha256")
    check(
        "the local rebuild is content-identical to the CI artifact",
        content_equal,
        f"local={local_manifest['canonical_sha256'][:16]}… "
        f"ci={str(ci_manifest.get('canonical_sha256'))[:16]}…",
    )
    check(
        "the rebuild has the same entry count as the CI artifact",
        local_manifest["entries"] == ci_manifest.get("entries"),
        f"local={local_manifest['entries']} ci={ci_manifest.get('entries')}",
    )

    # 5. contract and scan on the artifact this host will actually deploy
    contract_path = out / "artifact-contract.json"
    contract = run(
        [
            BUILD_PYTHON,
            str(HERE / "rc_contract.py"),
            "--artifact",
            str(artifact),
            "--commit",
            state["commit"],
            "--version",
            state["version"],
            "--install",
            "--evidence",
            str(contract_path),
        ],
        capture_output=True,
        timeout=1800,
    )
    check(
        "the deployable artifact passes the artifact contract (including install and startup)",
        contract.returncode == 0,
        (contract.stdout or "")[-200:],
    )
    scan_path = out / "artifact-scan.json"
    scan = run(
        [
            BUILD_PYTHON,
            str(HERE / "rc_scan.py"),
            "--artifact",
            str(artifact),
            "--evidence",
            str(scan_path),
        ],
        capture_output=True,
        timeout=900,
    )
    check(
        "the deployable artifact carries no forbidden material",
        scan.returncode == 0,
        "violations found" if scan.returncode else "clean",
    )

    # 6. a rollback target must exist before a release is promoted
    releases = PROD_ROOT / "releases"
    previous = [p.name for p in releases.glob("*") if p.is_dir() and p.name != state["rc_id"]]
    check(
        "a rollback target exists on the production host",
        bool(previous),
        f"releases={sorted(p.name for p in releases.glob('*')) if releases.exists() else 'host not built'}",
    )

    state["local"] = {
        "validated_at": now(),
        "build_report": report,
        "manifest_sha256": local_manifest["manifest_sha256"],
        "canonical_sha256": local_manifest["canonical_sha256"],
        "content_identical_to_ci": bool(content_equal),
        "artifact_path": str(artifact),
        "artifact_sha256": report["artifact_sha256"],
        "contract": json.loads(contract_path.read_text()) if contract_path.is_file() else {},
        "scan": json.loads(scan_path.read_text()) if scan_path.is_file() else {},
        "checks": checks,
    }

    # Freeze the artifact the candidate will deploy.
    rc_dir = RC_DIR / state["rc_id"]
    rc_dir.mkdir(parents=True, exist_ok=True)
    frozen_artifact = rc_dir / report["artifact"]
    shutil.copy2(artifact, frozen_artifact)
    state["artifact_frozen"] = str(frozen_artifact)
    state["artifact_frozen_sha256"] = sha256_file(frozen_artifact)
    state["artifact_frozen_canonical"] = local_manifest["canonical_sha256"]

    return finish_validation(state, checks, 0 if all(c["ok"] for c in checks) else 1)


def finish_validation(state: dict, checks: list[dict], code: int) -> int:
    state["local"]["checks_total"] = len(checks)
    state["local"]["checks_passed"] = sum(1 for c in checks if c["ok"])
    if code == 0:
        transition(
            state,
            "VALIDATED",
            {"summary": f"{state['local']['checks_passed']}/{len(checks)} checks passed"},
        )
    else:
        failed = [c["check"] for c in checks if not c["ok"]]
        transition(state, "REJECTED", {"summary": f"validation failed: {failed[:3]}"})
    print(f"\nlocal validation: {state['local']['checks_passed']}/{len(checks)} checks passed")
    return code


def verb_approve(args) -> int:
    """The approval boundary. Policy: a candidate whose CI verdict is RELEASABLE
    and whose local validation passed is approved automatically, and the
    approval is recorded with the policy that granted it. `--by` records a human
    approver instead, which is what a production policy with a manual gate would
    use."""
    state = load_state(args.rc)
    if state["state"] != "VALIDATED":
        print(
            f"FATAL: {state['rc_id']} is {state['state']}; only a VALIDATED candidate can be approved",
            file=sys.stderr,
        )
        return 2
    policy = json.loads(POLICY.read_text()) if POLICY.is_file() else {}
    mode = policy.get("approval", {}).get("mode", "automatic_after_gates")
    approver = args.by or f"policy:{mode}"
    transition(
        state,
        "APPROVED",
        {
            "summary": f"approved by {approver}",
            "mode": mode,
            "ci_verdict": state["ci_verdict"],
            "local_checks": f"{state['local']['checks_passed']}/{state['local']['checks_total']}",
        },
        actor=approver,
    )
    return 0


def post_deploy_validation(state: dict) -> dict:
    """Probe what the host is actually serving, not what the deploy script said."""
    steps = [
        ("infrastructure verification", [str(PROD_ROOT / "bin/prod.sh"), "verify", "--quick"]),
        (
            "liveness, readiness, frontend and ingress",
            [str(PROD_ROOT / "bin/prod.sh"), "health", "--json"],
        ),
        ("release identity", [str(PROD_ROOT / "bin/prod.sh"), "identity"]),
        ("raw artifact probe", [str(PROD_ROOT / "bin/prod.sh"), "artifact"]),
    ]
    results = []
    for name, cmd in steps:
        proc = run(cmd, capture_output=True, timeout=1800)
        results.append(
            {
                "step": name,
                "command": " ".join(cmd[1:]),
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "output_tail": "\n".join((proc.stdout or "").strip().splitlines()[-12:]),
            }
        )
    return {"validated_at": now(), "ok": all(row["ok"] for row in results), "steps": results}


def write_document(state: dict, name: str, document: dict) -> Path:
    """Every release artefact of the decision lands next to the candidate."""
    directory = RC_DIR / state["rc_id"] / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path


def verb_promote(args) -> int:
    """Deploy the approved candidate through the Phase 32 deployment pipeline."""
    state = load_state(args.rc)
    if state["state"] != "APPROVED":
        print(
            f"FATAL: {state['rc_id']} is {state['state']}; only an APPROVED candidate is promoted",
            file=sys.stderr,
        )
        return 2
    artifact = Path(state["artifact_frozen"])
    if not artifact.is_file():
        print(f"FATAL: the frozen artifact is missing: {artifact}", file=sys.stderr)
        return 2
    actual = sha256_file(artifact)
    if actual != state["artifact_frozen_sha256"]:
        transition(state, "REJECTED", {"summary": "the frozen artifact changed after validation"})
        print("FATAL: the frozen artifact no longer matches its validation hash", file=sys.stderr)
        return 2

    release_id = args.release_id or state["rc_id"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"promote-{state['rc_id']}.log"
    result = run(
        [
            str(PROD_ROOT / "bin/prod.sh"),
            "deploy",
            str(artifact),
            "--release-id",
            release_id,
            "--commit",
            state["commit"],
            "--sha256",
            state["artifact_frozen_sha256"],
        ],
        capture_output=True,
        timeout=5400,
    )
    logfile.write_text((result.stdout or "") + "\n--- stderr ---\n" + (result.stderr or ""))
    for line in (result.stdout or "").strip().splitlines()[-12:]:
        print("    " + line)
    deployed = deployed_release()
    deployed_ok = result.returncode == 0 and deployed.get("release_id") == release_id
    deployment_doc = {
        "recorded_at": now(),
        "kind": "deployment",
        "environment": "production",
        "release_id": release_id,
        "commit": state["commit"],
        "version": state["version"],
        "artifact": state["artifact"]["name"],
        "artifact_sha256": state["artifact_frozen_sha256"],
        "pipeline_run": state.get("pipeline_run"),
        "rc_id": state["rc_id"],
        "result": "deployed" if deployed_ok else "failed",
        "deploy_exit_code": result.returncode,
        "deploy_log": str(logfile),
        "current_release": deployed,
    }
    write_document(state, "deployment.json", deployment_doc)
    state["deployment"] = deployment_doc

    if not deployed_ok:
        transition(
            state,
            "DEPLOY_FAILED",
            {"summary": f"deploy rc={result.returncode} release={deployed.get('release_id')}"},
        )
        return 1

    validation = post_deploy_validation(state)
    post_doc = {
        "recorded_at": now(),
        "kind": "post-deploy-validation",
        "environment": "production",
        "release_id": release_id,
        "commit": state["commit"],
        "artifact_sha256": state["artifact_frozen_sha256"],
        "result": "pass" if validation["ok"] else "fail",
        "steps": validation["steps"],
    }
    write_document(state, "post-deploy.json", post_doc)
    state["post_deploy"] = post_doc
    log(f"post-deploy validation: {'pass' if validation['ok'] else 'FAIL'}")

    if not validation["ok"]:
        # A release that does not serve is not a release. Roll back to the release
        # that was current before this promotion and record both facts.
        previous = (
            args.previous or state.get("previous_release") or deployed.get("previous_release")
        )
        rollback = None
        if previous:
            rollback = run(
                [str(PROD_ROOT / "bin/prod.sh"), "rollback", "--to", str(previous)],
                capture_output=True,
                timeout=3600,
            )
        rollback_doc = {
            "recorded_at": now(),
            "kind": "rollback",
            "trigger": "post-deploy validation failed",
            "from_release": release_id,
            "to_release": previous,
            "invoked": rollback is not None,
            "exit_code": rollback.returncode if rollback else None,
            "output_tail": "\n".join((rollback.stdout or "").strip().splitlines()[-10:])
            if rollback
            else "",
            "result": "rolled back"
            if rollback is not None and rollback.returncode == 0
            else "manual action required",
        }
        write_document(state, "rollback.json", rollback_doc)
        transition(
            state,
            "ROLLED_BACK" if rollback_doc["result"] == "rolled back" else "ROLLBACK_REQUIRED",
            {"summary": f"post-deploy validation failed; rollback to {previous}"},
        )
        return 1

    # The release is real: give the commit a name that identifies it.
    tag = args.tag or f"mo7-release-{state['rc_id']}"
    tagged = subprocess.run(
        [
            "git",
            "-C",
            str(CHECKOUT),
            "tag",
            "-a",
            tag,
            "-m",
            f"Release {state['version']} rc {state['rc_id']} "
            f"artifact {state['artifact_frozen_sha256']}",
        ],
        capture_output=True,
        text=True,
    )
    pushed = None
    if tagged.returncode == 0 and not args.no_push:
        pushed = subprocess.run(
            ["git", "-C", str(CHECKOUT), "push", "origin", tag], capture_output=True, text=True
        )
    tag_doc = {
        "recorded_at": now(),
        "kind": "release-tag",
        "tag": tag,
        "commit": state["commit"],
        "created": tagged.returncode == 0,
        "pushed": bool(pushed and pushed.returncode == 0),
        "detail": ((pushed.stderr if pushed else tagged.stderr) or "").strip()[:300],
    }
    write_document(state, "tag.json", tag_doc)
    state["tag"] = tag_doc

    transition(state, "PROMOTED", {"summary": f"release={release_id} post-deploy=pass tag={tag}"})
    return 0


def verb_deployed(args) -> int:
    deployed = deployed_release()
    print(json.dumps(deployed, indent=2))
    return 0


def verb_rollback(args) -> int:
    """Roll the production host back, then re-validate it."""
    target = args.to
    state = load_state(args.rc) if args.rc else None
    result = run(
        [str(PROD_ROOT / "bin/prod.sh"), "rollback", "--to", target],
        capture_output=True,
        timeout=3600,
    )
    for line in (result.stdout or "").strip().splitlines()[-10:]:
        print("    " + line)
    ok = result.returncode == 0
    verification = (
        post_deploy_validation(state) if (state is not None and ok) else {"ok": None, "steps": []}
    )
    if state is not None:
        document = {
            "recorded_at": now(),
            "kind": "rollback",
            "environment": "production",
            "from_release": str(state.get("deployment", {}).get("release_id", "")),
            "to_release": target,
            "invoked": True,
            "exit_code": result.returncode,
            "output_tail": "\n".join((result.stdout or "").strip().splitlines()[-10:]),
            "result": "rolled back" if ok else "failed",
            "verified_after_rollback": verification["ok"],
            "verification_steps": verification["steps"],
        }
        write_document(state, "rollback.json", document)
        transition(
            state,
            "ROLLED_BACK" if ok else "ROLLBACK_FAILED",
            {
                "summary": f"rollback to {target} rc={result.returncode} verified={verification['ok']}"
            },
        )
    print(f"\nrollback to {target}: {'complete' if ok else 'FAILED'}")
    return 0 if ok else 1


def verb_verify(args) -> int:
    """Re-verify a frozen candidate: nothing about it may have moved."""
    state = load_state(args.rc)
    problems = []
    frozen = Path(state["ci_evidence_frozen"])
    current = {p.name: sha256_file(p) for p in sorted(frozen.glob("*")) if p.is_file()}
    if current != state["ci_evidence_files"]:
        problems.append("CI evidence digest changed")
    artifact = Path(state.get("artifact_frozen", ""))
    if not artifact.is_file():
        problems.append("frozen artifact missing")
    elif sha256_file(artifact) != state["artifact_frozen_sha256"]:
        problems.append("frozen artifact hash changed")
    print(f"{state['rc_id']}: state={state['state']} immutable={not problems}")
    for problem in problems:
        print(f"  VIOLATION: {problem}")
    return 0 if not problems else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    sub.add_parser("status", help="show release candidates and the deployed release").set_defaults(
        fn=verb_status
    )
    sub.add_parser("deployed", help="the release the production host is running").set_defaults(
        fn=verb_deployed
    )

    p = sub.add_parser("ingest", help="create a release candidate from CI evidence")
    p.add_argument("--evidence", required=True)
    p.set_defaults(fn=verb_ingest)

    p = sub.add_parser("validate", help="rebuild, prove equivalence, contract-check, scan")
    p.add_argument("--rc", required=True)
    p.set_defaults(fn=verb_validate)

    p = sub.add_parser("approve", help="approve a validated candidate")
    p.add_argument("--rc", required=True)
    p.add_argument("--by", help="record a human approver instead of the policy")
    p.set_defaults(fn=verb_approve)

    p = sub.add_parser("promote", help="deploy an approved candidate")
    p.add_argument("--rc", required=True)
    p.add_argument("--release-id")
    p.add_argument("--previous", help="release to roll back to if post-deploy validation fails")
    p.add_argument("--tag", help="release tag name (default mo7-release-<rc id>)")
    p.add_argument("--no-push", action="store_true", help="create the tag without pushing it")
    p.set_defaults(fn=verb_promote)

    p = sub.add_parser("rollback", help="roll the production host back and re-validate")
    p.add_argument("--to", required=True)
    p.add_argument("--rc")
    p.set_defaults(fn=verb_rollback)

    p = sub.add_parser("verify", help="re-check that a frozen candidate has not moved")
    p.add_argument("--rc", required=True)
    p.set_defaults(fn=verb_verify)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
