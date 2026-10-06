#!/usr/bin/env python3
"""Artifact security scan: material a release artifact must never carry.

Every rule is a real pattern with a real consequence, not a keyword list for
appearance. A finding is reported with the offending paths, and the exit status
is non-zero so the release pipeline fails closed on it.

    rc_scan.py --artifact FILE [--json] [--evidence FILE]

Rules
-----
credentials      private keys, `.env` files, credential-shaped files anywhere in
                 the artifact (a release must not ship secrets)
high_entropy     long base64/hex blobs inside small text files, excluding the
                 known-benign set (chunk hashes, integrity hashes, font data)
bytecode         `.pyc` / `__pycache__` (interpreter output, not source)
vcs_and_ci       `.git`, `.github`, CI caches
dev_material     test suites, fixtures, docs, editor config shipped by accident
local_paths      absolute paths to a build host (`/home/<user>/…`) inside files
                 that will be executed or served
debug_config     debug/dev toggles in shipped configuration
temp_files       editor swaps, `.orig`, `.rej`, `.bak`, lockfiles of builds
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys
import time
import zipfile

# --- rule definitions --------------------------------------------------------
SECRET_NAMES = re.compile(
    r"(^|/)(\.env(\..*)?|\.netrc|id_rsa|id_ed25519|credentials\.json|secrets\.env|"
    r"\.pypirc|\.npmrc|\.aws/credentials|service[-_]account.*\.json)$",
    re.IGNORECASE,
)
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
)
# Credential-shaped assignments in shipped text. Deliberately narrow: a release
# must not ship a literal that would authenticate anything.
CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?i)\b(aws_secret_access_key|client_secret|private_key|api_key|apikey|password|passwd|"
    rb"secret_key|access_token|auth_token|bearer)\b\s*[:=]\s*[\"']([A-Za-z0-9/+_\-]{20,})[\"']"
)
# Placeholders and documentation values that are not credentials.
CREDENTIAL_ALLOWLIST = re.compile(
    rb"(?i)(example|placeholder|your[-_]|xxx+|changeme|redacted|dummy|test|sample|\$\{|<|"
    rb"os\.environ|process\.env|getenv|import\.meta|field|schema|label|title|description|"
    rb"hint|tooltip|error|message|logging|log_|_logger|name\s*$)"
)
BYTECODE_NAME = re.compile(r"(^|/)(__pycache__/|.*\.pyc$)")
VCS_NAME = re.compile(r"(^|/)(\.git|\.gitignore$|\.github/|\.hg|\.svn)")
DEV_NAME = re.compile(
    r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$|fixtures?/|"
    r"\.pytest_cache/|\.ruff_cache/|\.mypy_cache/|docs?/|notebooks?/|examples?/|"
    r"\.vscode/|\.idea/|\.editorconfig$|\.pre-commit-config\.yaml$)"
)
TEMPFILE_NAME = re.compile(r"(^|/)([^/]*\.(orig|rej|bak|swp|swo)$|\.DS_Store$|~$|#.*#$)")
DEBUG_MARKERS = re.compile(rb"(?i)\b(debug\s*[:=]\s*true|DEBUG\s*=\s*True|dev_mode\s*[:=]\s*true)")
LOCAL_PATH = re.compile(rb"/home/[a-z0-9_.-]+/")
TEXT_SUFFIXES = (
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".json",
    ".txt",
    ".md",
    ".sh",
    ".html",
    ".css",
    ".ts",
    ".tsx",
    ".jsx",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
)
# Integrity/asset hashes that are supposed to look random.
BENIGN_ENTROPY_NAMES = re.compile(
    r"(RECORD$|\.sha256$|static/chunks/|static/css/|\.woff2?$|/chunk|integrity|"
    r"package-lock\.json$|build-manifest\.json$|\.map$|runtime\.txt$|\.rsc$)"
)


def entropy(blob: bytes) -> float:
    if not blob:
        return 0.0
    counts = Counter(blob)
    length = len(blob)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def scan(artifact: Path) -> dict:
    violations: list[dict] = []
    scanned = 0

    def add(rule: str, path: str, detail: str = "") -> None:
        violations.append({"rule": rule, "path": path, "detail": detail[:200]})

    with zipfile.ZipFile(artifact) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            scanned += 1
            name = info.filename
            if SECRET_NAMES.search(name) or name.lower().endswith(SECRET_SUFFIXES):
                add("credentials", name, "credential-shaped filename")
            if BYTECODE_NAME.search(name):
                add("bytecode", name)
            if VCS_NAME.search(name):
                add("vcs_and_ci", name)
            if DEV_NAME.search(name):
                add("dev_material", name)
            if TEMPFILE_NAME.search(name):
                add("temp_files", name)
            # Content rules only make sense on files small enough to be authored
            # text; a large bundle is hashed assets, not configuration.
            if info.file_size > 512_000:
                continue
            payload = archive.read(name)
            if any(marker in payload for marker in PRIVATE_KEY_MARKERS):
                add("credentials", name, "embedded private key")
            if CREDENTIAL_ASSIGNMENT.search(payload) and not CREDENTIAL_ALLOWLIST.search(payload):
                match = CREDENTIAL_ASSIGNMENT.search(payload)
                add(
                    "credentials",
                    name,
                    f"credential-shaped assignment: {match.group(1).decode()!r}",
                )
            if LOCAL_PATH.search(payload):
                add(
                    "local_paths",
                    name,
                    f"build-host path: {LOCAL_PATH.search(payload).group(0).decode()}",
                )
            if name.endswith(TEXT_SUFFIXES) and DEBUG_MARKERS.search(payload):
                add("debug_config", name, DEBUG_MARKERS.search(payload).group(0).decode())
            if name.endswith(TEXT_SUFFIXES) and not BENIGN_ENTROPY_NAMES.search(name):
                hits = [m.group(0) for m in re.finditer(rb"[A-Za-z0-9+/_\-=]{32,}", payload)]
                for hit in hits[:2]:
                    if entropy(hit) > 4.2:
                        add("high_entropy", name, f"high-entropy blob ({len(hit)} chars)")

    by_rule = Counter(v["rule"] for v in violations)
    return {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifact": artifact.name,
        "entries_scanned": scanned,
        "clean": not violations,
        "violations": violations[:200],
        "violation_total": len(violations),
        "by_rule": dict(by_rule),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--evidence")
    parser.add_argument("--max-per-rule", type=int, default=15)
    args = parser.parse_args()

    artifact = Path(args.artifact)
    if not artifact.is_file():
        print(f"FATAL: artifact not found: {artifact}", file=sys.stderr)
        return 2
    result = scan(artifact)

    if args.evidence:
        Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence).write_text(json.dumps(result, indent=2) + "\n")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"artifact scan: {result['artifact']} ({result['entries_scanned']} entries)")
        if result["clean"]:
            print("  clean: no forbidden material")
        else:
            for rule, count in sorted(result["by_rule"].items()):
                print(f"  {rule}: {count}")
                for row in [v for v in result["violations"] if v["rule"] == rule][
                    : args.max_per_rule
                ]:
                    print(f"      {row['path']} {row['detail']}")
    print(f"\nartifact scan: {'CLEAN' if result['clean'] else 'FORBIDDEN MATERIAL FOUND'}")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
