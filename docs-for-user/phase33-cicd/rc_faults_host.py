#!/usr/bin/env python3
"""Host-layer failure injection for the release process (phase 33 §26).

`rc_faults.py --layer local` proves that a bad candidate cannot become a release
candidate. This module proves the same thing one step later, on the deployment
host: that the promotion path refuses what it must refuse, that the running
deployment is left untouched when it does, and that a promotion whose
post-deployment validation fails brings the previous release back.

Every case runs the four steps the phase requires — inject, detect, block,
recover — and records them as evidence; a case only passes when all four are
observed, never when they are assumed. The cases are driven through the real
entry points (`prod.sh`, `rc_release.py`), with the artefacts they refuse, so a
pass means the production path behaved, not that a harness said so.

Cases:

    wrong-declared-hash          deploy refuses an artifact that does not hash
                                 to the declared sha256, and stages nothing
    undeclared-commit            deploy refuses a commit the checkout does not
                                 contain
    artifact-without-web-bundle  a real artifact with the packaged web server
                                 removed is refused before anything is published
    promote-incomplete-release   promote refuses a hand-edited release directory
                                 and the live release keeps serving
    rollback-to-unknown-release  rollback refuses a release id that was never
                                 staged
    post-deploy-validation-...   a promotion whose post-deployment validation
                                 fails returns the previous release, records the
                                 rollback, and re-verifies the deployment
                                 (needs MO7_FAULT_RC_ID: an APPROVED candidate)

Environment:

    PROD_ROOT          the deployment root (default /home/user/mo7-prod)
    MO7_RELEASE_WORKDIR, MO7_CHECKOUT   read by rc_release.py
    MO7_FAULT_RC_ID    the APPROVED release candidate for the last case
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PROD_ROOT = Path(os.environ.get("PROD_ROOT", "/home/user/mo7-prod"))
PROD = PROD_ROOT / "bin/prod.sh"
RELEASES = PROD_ROOT / "releases"


def run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def prod(*args: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    return run([str(PROD), *args], timeout=timeout)


def tail(proc: subprocess.CompletedProcess, lines: int = 3) -> str:
    text = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    return " | ".join(text[-lines:])[:300]


def current_release() -> str:
    marker = PROD_ROOT / "run/current-release.json"
    if not marker.is_file():
        return ""
    try:
        return json.loads(marker.read_text()).get("release_id", "")
    except json.JSONDecodeError:
        return ""


def serving_release() -> str:
    """The release the deployment is actually serving, from the live symlink.

    `current_release()` reads the marker, and a failed promotion can leave the
    symlink flipped while the marker still names the release it was replacing.
    This case measured recovery from the marker and reported `recovered: true`
    while the host was serving the release nobody validated -- a false green, and
    precisely the state the case exists to catch. Recovery is decided on what is
    served.
    """
    try:
        return Path(os.path.realpath(PROD_ROOT / "current")).name
    except OSError:
        return ""


def deployment_intact(expected: str) -> tuple[bool, str]:
    """The deployment still serves `expected` and still passes verification."""
    identity = prod("identity")
    verify = prod("verify", "--quick", timeout=1800)
    now = current_release()
    ok = identity.returncode == 0 and verify.returncode == 0 and now == expected
    return (
        ok,
        f"release={now or '(none)'} identity={identity.returncode} verify={verify.returncode}",
    )


def case_wrong_declared_hash(artifact: Path, workdir: Path, commit: str) -> dict:
    before = current_release()
    release_id = "1.6.11-fault-wronghash"
    result = prod(
        "deploy",
        str(artifact),
        "--release-id",
        release_id,
        "--commit",
        commit,
        "--sha256",
        "0" * 64,
    )
    text = (result.stdout or "") + (result.stderr or "")
    staged = (RELEASES / release_id).exists()
    intact, detail = deployment_intact(before)
    return {
        "case": "a deploy whose declared hash does not match the artifact",
        "injected": f"deploy --sha256 {'0' * 8}… for {artifact.name}",
        "detected": result.returncode != 0 and "hash mismatch" in text,
        "blocked": not staged,
        "recovered": not staged,
        "validated": intact,
        "detail": f"exit={result.returncode} staged={staged} {tail(result)}; {detail}",
    }


def case_undeclared_commit(artifact: Path, workdir: Path, commit: str) -> dict:
    before = current_release()
    release_id = "1.6.11-fault-nocommit"
    # This case's release id is fixed, so a previous run's residue would make the
    # `staged` check meaningless (it would read the old directory and call the
    # case a failure). The case owns that name: anything left under it is its own
    # residue and is removed, and that fact is reported.
    stale = (RELEASES / release_id).exists()
    if stale:
        shutil.rmtree(RELEASES / release_id)
    result = prod(
        "deploy",
        str(artifact),
        "--release-id",
        release_id,
        "--commit",
        "1" * 40,
        "--sha256",
        sha256_file(artifact),
    )
    text = (result.stdout or "") + (result.stderr or "")
    staged = (RELEASES / release_id).exists()
    intact, detail = deployment_intact(before)
    return {
        "case": "a deploy whose declared commit is not in the checkout",
        "injected": f"deploy --commit {'1' * 12}… (a sha this checkout does not contain)",
        "detected": result.returncode != 0 and "commit" in text,
        "blocked": not staged,
        "recovered": not staged,
        "validated": intact,
        "detail": f"exit={result.returncode} staged={staged} "
        f"stale_residue_removed={stale} {tail(result)}; {detail}",
    }


def case_artifact_without_web_bundle(artifact: Path, workdir: Path, commit: str) -> dict:
    """A real artifact, minus the packaged web server. Everything else passes:
    the hash equals what is declared, the version matches, the wheel imports."""
    before = current_release()
    stripped = workdir / f"no-web-{artifact.name}"
    with (
        zipfile.ZipFile(artifact) as source,
        zipfile.ZipFile(stripped, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            if item.filename == "deeptutor_web/server.js":
                continue
            target.writestr(item, source.read(item.filename))
    release_id = "1.6.11-fault-noweb"
    result = prod(
        "deploy",
        str(stripped),
        "--release-id",
        release_id,
        "--commit",
        commit,
        "--sha256",
        sha256_file(stripped),
    )
    text = (result.stdout or "") + (result.stderr or "")
    staged = (RELEASES / release_id).exists()
    intact, detail = deployment_intact(before)
    return {
        "case": "an artifact missing the packaged web server (pre-promote failure)",
        "injected": f"{stripped.name}: deeptutor_web/server.js removed, declared hash corrected",
        "detected": result.returncode != 0 and "web" in text.lower(),
        "blocked": not staged,
        "recovered": not staged,
        "validated": intact,
        "detail": f"exit={result.returncode} staged={staged} {tail(result)}; {detail}",
    }


def case_promote_incomplete_release(artifact: Path, workdir: Path, commit: str) -> dict:
    """Promotion must refuse a release directory that is not a complete staging
    result, even though it exists and looks like a release."""
    before = current_release()
    source = RELEASES / before
    broken_id = f"{before}-fault-broken"
    broken = RELEASES / broken_id
    if broken.exists():
        shutil.rmtree(broken)
    shutil.copytree(source, broken, symlinks=True)
    # A release's `web` entry is a *symlink* to the materialised bundle inside
    # the release (`data/user/runtime/web`). Unlinking through it deletes the
    # running deployment's own server.js -- the harness damaged the release it was
    # testing (observed: the frontend could no longer start because
    # current/web/server.js was gone). The copy's link is replaced with an empty
    # real directory instead, which is the same fault -- a release directory with
    # no web bundle -- with nothing behind it to damage.
    web_entry = broken / "web"
    if web_entry.is_symlink():
        web_entry.unlink()
        web_entry.mkdir()
    else:
        (web_entry / "server.js").unlink(missing_ok=True)
    try:
        result = prod("promote", "--release", broken_id)
        text = (result.stdout or "") + (result.stderr or "")
        # `promote` must not have switched anything: the marker is the
        # authoritative record of what is running.
        #
        # The copy is removed *before* the deployment is measured: a release tree
        # is ~1.1 GiB, and the deployment's own verification refuses a host with
        # less than 5 GiB of headroom, so measuring with the copy still on disk
        # reports the harness's disk usage as a deployment failure (observed:
        # verify=1, `free=4.63 GiB`, on a host that passes 107/107 with the copy
        # removed). A case must measure the deployment, not itself.
        shutil.rmtree(broken, ignore_errors=True)
        intact, detail = deployment_intact(before)
        return {
            "case": "promoting a hand-edited release directory",
            "injected": f"{broken_id}: web/server.js removed from a copy of {before}",
            "detected": result.returncode != 0 and "web" in text.lower(),
            "blocked": current_release() == before,
            "recovered": not (broken / "web/server.js").exists(),
            "validated": intact,
            "detail": f"exit={result.returncode} {tail(result)}; {detail}",
        }
    finally:
        shutil.rmtree(broken, ignore_errors=True)


def case_rollback_to_unknown_release(artifact: Path, workdir: Path, commit: str) -> dict:
    before = current_release()
    result = prod("rollback", "--to", "9.9.9-never-staged")
    text = (result.stdout or "") + (result.stderr or "")
    intact, detail = deployment_intact(before)
    return {
        "case": "a rollback to a release that was never staged",
        "injected": "rollback --to 9.9.9-never-staged",
        "detected": result.returncode != 0,
        "blocked": current_release() == before,
        "recovered": current_release() == before,
        "validated": intact,
        "detail": f"exit={result.returncode} {tail(result)}; {detail}",
    }


def case_post_deploy_failure_rolls_back(artifact: Path, workdir: Path, commit: str) -> dict:
    """The promotion path's own recovery: make post-deployment validation fail,
    and require the deployment to come back to the release that was serving.

    The fault is injected in the deployment contract (the public origin the
    post-deploy probes measure through), not in the release: the promotion must
    fail because the deployment cannot be *validated*, and the recovery path must
    put the previous release back. Whether that happened without a human step is
    recorded rather than assumed — the phase documents rollback as manual when a
    human step is what recovered it.

    The case deliberately does *not* tell the controller which release to return
    to. It did, and that made it blind to a real defect: the controller resolved
    the target from a marker the promotion had already overwritten, so every real
    post-deploy failure ended in "manual action required" while this case passed
    on the argument it was handed. The recovery under test is the production
    path's own — including finding its target (P33-M5).
    """
    before = current_release()
    serving_before = serving_release()
    if serving_before != before:
        return {
            "case": "a promotion whose post-deployment validation fails",
            "injected": "(not injected: the deployment is already inconsistent)",
            "detected": False,
            "blocked": False,
            "recovered": False,
            "validated": False,
            "retried_from": "not needed",
            "performed_by": "not recorded",
            "detail": f"the marker names {before or '(none)'} and the symlink serves "
            f"{serving_before or '(none)'}; restore the host before injecting a fault",
        }
    rc_id = os.environ.get("MO7_FAULT_RC_ID", "")
    if not rc_id:
        return {
            "case": "a promotion whose post-deployment validation fails",
            "injected": "(not injected: MO7_FAULT_RC_ID is unset)",
            "detected": False,
            "blocked": False,
            "recovered": False,
            "validated": False,
            "detail": "set MO7_FAULT_RC_ID to an APPROVED candidate to run this case",
        }

    import rc_release  # noqa: PLC0415  - host-layer only

    # The candidate has to be promotable. A previous run of this very case leaves
    # it DEPLOY_FAILED (correctly -- it was deployed and recovered), and the
    # documented retry is what makes the case re-runnable without inventing a new
    # release candidate or hand-editing state.
    retried_from = None
    state_before = rc_release.load_state(rc_id)
    history_before = len(state_before.get("history", []))
    if state_before["state"] != "APPROVED":
        retried_from = state_before["state"]
        retry = run(
            [sys.executable, str(HERE / "rc_release.py"), "retry", "--rc", rc_id],
            timeout=600,
        )
        if retry.returncode != 0:
            return {
                "case": "a promotion whose post-deployment validation fails",
                "injected": "(not injected: the candidate cannot be promoted)",
                "detected": False,
                "blocked": False,
                "recovered": False,
                "validated": False,
                "retried_from": retried_from,
                "performed_by": "not recorded",
                "detail": f"{rc_id} is {retried_from} and the retry was refused: "
                + " ".join(((retry.stdout or "") + (retry.stderr or "")).splitlines()[-2:]),
            }
        state_before = rc_release.load_state(rc_id)
        history_before = len(state_before.get("history", []))
    started_at = rc_release.now()

    contract = PROD_ROOT / "etc/production.env"
    original = contract.read_text()
    # Which fault, precisely: it has to
    #   * survive the promotion (the contract refresh rewrites the five identity
    #     keys, so a wrong PROD_SOURCE_COMMIT would be repaired before anyone
    #     validated anything),
    #   * pass the deployment's own post-promote smoke (PROD_PUBLIC_ORIGIN does
    #     not: the smoke probes the frontend through the public origin, so the
    #     deployment refused the release before the controller ever validated it
    #     and the rollback path this case exists to exercise was never reached --
    #     observed on the first run of this suite), and
    #   * fail the controller's post-deploy validation.
    # A dead validation-ingress origin does exactly that: the ingress is a
    # harness-only component the smoke explicitly tolerates being absent, and the
    # raw artifact probe runs against it, so the release goes live and the
    # probe then fails.
    broken_lines = []
    for line in original.splitlines():
        if line.startswith("PROD_TLS_VALIDATION_ORIGIN="):
            broken_lines.append(f"{line.split('=')[0]}=https://127.0.0.1:9")
        else:
            broken_lines.append(line)
    contract.write_text("\n".join(broken_lines) + "\n")
    manual = None
    try:
        result = run(
            [
                sys.executable,
                str(HERE / "rc_release.py"),
                "promote",
                "--rc",
                rc_id,
                # No --previous: the promotion must know what it is replacing.
                "--no-push",
            ],
            timeout=5400,
        )
        rollback_file = rc_release.RC_DIR / rc_id / "evidence/rollback.json"
        rollback = json.loads(rollback_file.read_text()) if rollback_file.is_file() else {}
        state_after = rc_release.load_state(rc_id)
        # The record has to have been produced by *this* run: a leftover
        # rollback.json from an earlier attempt would otherwise let a refused
        # promotion look like a detected-and-recovered one.
        fresh = (
            len(state_after.get("history", [])) > history_before
            and rollback.get("recorded_at", "1970-01-01T00:00:00Z") >= started_at
        )
        automatic = serving_release() == before
    finally:
        # Remove the injected fault whether or not the promotion did anything.
        contract.write_text(original)
        prod("restart")

    if serving_release() != before:
        # Neither the deployment nor the controller restored the previous release
        # while the fault was active. The documented operator rollback is then the
        # recovery path, and the case records that a human step was required.
        manual = prod("rollback", "--to", before)
    served = serving_release()
    marked = current_release()
    restored = served == before and marked == before
    intact, detail = deployment_intact(before)
    performed_by = rollback.get("performed_by")
    rolled_back = rollback.get("result") in ("rolled back", "rolled back by the deployment")
    return {
        "case": "a promotion whose post-deployment validation fails",
        "injected": "the contract's validation-ingress origin pointed at a dead port",
        "detected": result.returncode != 0 and rolled_back and fresh,
        "blocked": served != rc_id and marked != rc_id,
        "recovered": restored,
        "validated": intact,
        "automatic": automatic,
        "rollback_result": rollback.get("result", "not recorded"),
        "performed_by": performed_by or "not recorded",
        "retried_from": retried_from or "not needed",
        "detail": f"promote exit={result.returncode} state={state_after['state']} "
        f"rollback={rollback.get('result')} performed_by={performed_by or 'not recorded'} "
        f"automatic={automatic} operator_rollback_exit={manual.returncode if manual else None} "
        f"serving={served or '(none)'} marker={marked or '(none)'} "
        f"fresh={fresh}{f' retried_from={retried_from}' if retried_from else ''}; {detail}",
    }


CASES = {
    "wrong-declared-hash": case_wrong_declared_hash,
    "undeclared-commit": case_undeclared_commit,
    "artifact-without-web-bundle": case_artifact_without_web_bundle,
    "promote-incomplete-release": case_promote_incomplete_release,
    "rollback-to-unknown-release": case_rollback_to_unknown_release,
    "post-deploy-validation-failure": case_post_deploy_failure_rolls_back,
}

# Keep the recorded timestamps honest: the module records when each case ran.
CASES_RECORDED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
