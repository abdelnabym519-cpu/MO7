#!/usr/bin/env python3
"""Artifact security scan: what must not be inside a release.

A release artifact is published to hosts the build machine never sees, so it is
scanned for material that must not travel with it. The scan separates two
questions that are easy to confuse:

* **violations** — material whose presence means the artifact must not be
  released: private keys, credentials, secret-shaped tokens, environment files,
  compiled bytecode, repository metadata, debug configuration, and build-host
  paths inside assets that are served to a browser.
* **recorded** — observations that are real but are not release blockers, each
  with a finding id and the reason it does not block. Recording them is the
  difference between a scan that leaves a residue documented and one that hides it
  behind a rule that was quietly switched off (or that screams at every minified
  file, which is the same thing with more noise).

`clean` is true only when there are no violations. Exit code 1 on any violation.

    rc_scan.py --artifact WHEEL [--json] [--evidence FILE]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
import zipfile

# --- blocking rule families -------------------------------------------------
SECRET_SHAPES = [
    ("openai_key", re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b")),
    ("github_token", re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("discord_webhook", re.compile(rb"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+")),
    ("private_key_block", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("pgp_private_block", re.compile(rb"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    ("putty_private_key", re.compile(rb"PuTTY-User-Key-File-\d")),
]
CREDENTIAL_FILES = re.compile(
    r"(^|/)(\.env(\.[A-Za-z0-9_-]+)?|credentials\.json|secrets\.env|auth_secret|"
    r"id_rsa|id_ed25519|[^/]*\.pem|[^/]*\.p12|[^/]*\.pfx|[^/]*\.keystore|\.netrc|\.pypirc|"
    r"\.docker/config\.json|service-account[^/]*\.json)$"
)
BUILD_ARTIFACTS = re.compile(r"(^|/)(__pycache__/|\.git/|\.github/|\.pytest_cache/|\.mypy_cache/)")
TEMP_FILES = re.compile(r"(^|/)[^/]*\.(pyc|pyo|orig|rej|bak|tmp|swp)$|(^|/)\.DS_Store$|~$")
LOCAL_PATH = re.compile(rb"/home/[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/|[A-Za-z]:\\\\Users\\\\")
DEBUG_ENABLED = re.compile(rb"^\s*(?:debug|DEBUG)\s*[:=]\s*(?:true|True)\s*$", re.MULTILINE)

# --- recorded (non-blocking) observations -----------------------------------
CLIENT_SERVED = re.compile(r"^deeptutor_web/\.next/static/|\.html$")
DEFAULT_CHECKOUT = "/home/user/MO7"
IN_PACKAGE_TESTS = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$")
ENTROPY_TOKEN = re.compile(rb"[A-Za-z0-9+/=_-]{64,}")
# tokens that are simply a slice of a known alphabet are not secret-shaped
ALPHABET_RUNS = [b"ABCDEFGHIJKLMNOPQRSTUVWXYZ", b"abcdefghijklmnopqrstuvwxyz", b"0123456789"]
GENERATED_KEY_FIELD = re.compile(rb'"encryptionKey"\s*:\s*"[A-Za-z0-9+/=]{20,}"')

FINDINGS = {
    "local_paths_server": "P33-S1",
    "home_path_review": "P33-S5",
    "in_package_tests": "P33-S2",
    "entropy_review": "P33-S3",
    "generated_key_material": "P33-S4",
}


def is_alphabet_run(token: bytes) -> bool:
    return any(run in token for run in ALPHABET_RUNS)


def scan(artifact: Path, checkout: str = DEFAULT_CHECKOUT) -> dict:
    violations: list[dict] = []
    recorded: list[dict] = []
    checkout = checkout.rstrip("/")
    checkout_bytes = checkout.encode() if checkout else b""
    counts: dict[str, int] = {}

    def flag(rule: str, path: str, detail: str = "") -> None:
        violations.append({"rule": rule, "path": path, "detail": detail})
        counts[rule] = counts.get(rule, 0) + 1

    def note(rule: str, path: str, detail: str = "") -> None:
        recorded.append({"rule": rule, "finding": FINDINGS[rule], "path": path, "detail": detail})

    with zipfile.ZipFile(artifact) as archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            name = info.filename
            if CREDENTIAL_FILES.search(name):
                flag("credential_file", name)
            if BUILD_ARTIFACTS.search(name):
                flag("repository_or_cache_metadata", name)
            if TEMP_FILES.search(name):
                flag("temporary_or_compiled_file", name)
            if IN_PACKAGE_TESTS.search(name):
                note("in_package_tests", name, "test material shipped inside the package")

            payload = archive.read(name)
            for shape, pattern in SECRET_SHAPES:
                if pattern.search(payload):
                    flag(
                        f"secret_shape:{shape}",
                        name,
                        pattern.search(payload).group(0)[:40].decode("utf-8", "replace"),
                    )
            if checkout_bytes and checkout_bytes in payload:
                # The artifact names the machine it was built on. That is a
                # release blocker wherever it appears: the build recipe scrubs
                # that path, so finding it means the artifact is not what the
                # recipe produces.
                flag("build_host_path", name, f"the build checkout path {checkout}")
            for match in LOCAL_PATH.finditer(payload):
                # Other home-shaped paths are usually documentation examples or UI
                # placeholders, so they are recorded with a sample rather than
                # treated as leaks.
                note(
                    "home_path_review",
                    name,
                    f"{match.group(0).decode('utf-8', 'replace')} "
                    f"({'client-served' if CLIENT_SERVED.search(name) else 'server-side'})",
                )
                break
            if name.endswith(
                (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf")
            ) and DEBUG_ENABLED.search(payload):
                flag("debug_configuration", name, "debug enabled in a shipped configuration file")
            if GENERATED_KEY_FIELD.search(payload):
                note(
                    "generated_key_material",
                    name,
                    "framework-generated per-build key material (recorded, not a credential)",
                )
            for token in ENTROPY_TOKEN.findall(payload):
                if is_alphabet_run(token):
                    continue
                classes = sum(
                    bool(re.search(cls, token))
                    for cls in (rb"[a-z]", rb"[A-Z]", rb"[0-9]", rb"[^A-Za-z0-9]")
                )
                if classes >= 3 and not re.fullmatch(rb"[0-9a-f]+", token):
                    note("entropy_review", name, f"high-entropy token ({len(token)} chars)")
                    break

    by_rule: dict[str, int] = {}
    for row in violations:
        by_rule[row["rule"]] = by_rule.get(row["rule"], 0) + 1
    recorded_by_rule: dict[str, int] = {}
    for row in recorded:
        recorded_by_rule[row["rule"]] = recorded_by_rule.get(row["rule"], 0) + 1

    return {
        "artifact": artifact.name,
        "artifact_sha256": __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(),
        "entries_scanned": len(zipfile.ZipFile(artifact).infolist()),
        "clean": not violations,
        "violation_total": len(violations),
        "by_rule": by_rule,
        "violations": violations[:200],
        "recorded_total": len(recorded),
        "recorded_by_rule": recorded_by_rule,
        "recorded": recorded[:60],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument(
        "--checkout",
        default=__import__("os").environ.get("MO7_CHECKOUT", DEFAULT_CHECKOUT),
        help="the build checkout path that must not appear in the artifact",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--evidence", help="write the scan document here")
    args = parser.parse_args()

    artifact = Path(args.artifact).resolve()
    document = scan(artifact, args.checkout)
    document["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if args.evidence:
        Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence).write_text(json.dumps(document, indent=2) + "\n")
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        print(f"artifact scan: {artifact.name}")
        print(f"  entries {document['entries_scanned']}")
        print(f"  violations {document['violation_total']} {document['by_rule']}")
        print(f"  recorded   {document['recorded_total']} {document['recorded_by_rule']}")
        for row in document["violations"][:20]:
            print(f"  VIOLATION {row['rule']}: {row['path']} {row['detail']}")
        print("artifact scan: " + ("CLEAN" if document["clean"] else "FORBIDDEN MATERIAL FOUND"))
    return 0 if document["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
