"""Make the built web bundle a function of the release, not of the build.

Two values that Next.js generates per build end up inside the shipped bundle:

* **The build id.** It names directories under `.next/static`, it is written into
  `.next/BUILD_ID`, and it is embedded in the server chunks and the prerender
  payloads. Measured on one bundle: 433 files, 450 occurrences. The comparison of
  two builds of `e344c17` that differed only in the directory they were built in
  reported 4 renamed entries, 214 entries whose content differed, 4
  build-id-derived digests and one ordering difference — all of it this one random
  value.
* **The temporary tsconfig name.** The build rewrites the project's TypeScript
  configuration through a generated file whose name carries a random number *and a
  random width* (`tsconfig.deeptutor-build-4867.json`, `…-60173.json` in two builds
  of one commit), and that name is recorded in `required-server-files.json` and in
  the standalone `server.js`. The file itself is not packaged; the name is. The
  rewritten name is always five digits, derived from the release identity: a width
  inherited from a random value is still a random value.

Both are rewritten to values derived from the release identity — commit, pinned
`SOURCE_DATE_EPOCH` and version — so a rebuild of a commit produces the same
bytes. The rewrites are length preserving (the derived build id is 21 characters,
the shape Next.js itself generates; the tsconfig number keeps its digit count), and
they touch path names and text-like files only. Next's own webpack cache
(`.next/cache`) is left alone: it is not packaged, and rewriting it would only
make the next build slower. If an occurrence survives anywhere outside that cache,
the tool fails the build instead of shipping a random value.

    rc_normalize_build.py --root web/.next --commit SHA --source-date-epoch N
                          --version V [--evidence FILE] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import time

# Text-like payloads. `.rsc` is the React server component payload: a line-based
# text format that carries the build id, and the largest single carrier of it.
TEXT_SUFFIXES = {
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".json",
    ".txt",
    ".html",
    ".css",
    ".map",
    ".rsc",
    ".rss",
    ".xml",
    ".md",
}
# Next's own build cache: not packaged, and rewriting it would only make the
# next build slower. Everything else in the bundle is fair game.
SKIP_DIRS = {"cache"}


def derive_build_id(commit: str, source_date_epoch: str, version: str) -> str:
    """A 21-character id in the shape Next.js generates, derived from the release."""
    material = f"mo7-build-id\n{commit}\n{source_date_epoch}\n{version}\n".encode()
    digest = base64.urlsafe_b64encode(hashlib.sha256(material).digest()).decode()
    return digest.rstrip("=")[:21]


TSCONFIG_RE = re.compile(r"tsconfig\.deeptutor-build-(\d+)\.json")
#: The rewritten name is always this wide, whatever width Next.js generated. The
#: first version preserved the generated width, which meant the derived name still
#: depended on a random value: Next.js picks a number of four, five or six digits
#: (measured: 4867 and 60173 in two builds on one host), so two builders derived
#: different-width names from the same commit and CI's artifact and the rebuild
#: differed in three entries with nothing to explain them. The width is fixed.
TSCONFIG_DIGITS = 5


def derive_tsconfig_number(commit: str, version: str) -> str:
    """A fixed-width number derived from the release identity."""
    material = f"mo7-tsconfig\n{commit}\n{version}\n".encode()
    value = int.from_bytes(hashlib.sha256(material).digest(), "big")
    return f"{value % 10**TSCONFIG_DIGITS:0{TSCONFIG_DIGITS}d}"


def normalize_tsconfig(
    needle: re.Pattern[bytes], substitute_for, root: Path, dry_run: bool
) -> tuple[int, int, list[str]]:
    """Rewrite the generated tsconfig name in every text file under root.

    `substitute_for` takes the match and returns the replacement, so the sample
    recorded as evidence is produced by the same function that rewrites the file:
    the record says what happened rather than what was intended.
    """
    files = 0
    occurrences = 0
    samples: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or is_skipped(path, root) or not is_text(path):
            continue
        payload = path.read_bytes()
        matches = list(needle.finditer(payload))
        if not matches:
            continue
        replacement = needle.sub(substitute_for, payload)
        files += 1
        occurrences += len(matches)
        if len(samples) < 5:
            samples.append(
                f"{path.relative_to(root)}: {matches[0].group(0).decode()} -> "
                f"{substitute_for(matches[0]).decode()}"
            )
        if not dry_run:
            path.write_bytes(replacement)
    return files, occurrences, samples


def is_skipped(path: Path, root: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.relative_to(root).parts)


def is_text(path: Path) -> bool:
    """Text-like payloads: by suffix, or an extension-less file that decodes.

    `BUILD_ID` and Next's `trace` have no suffix, and both are plain text that
    carries the id (the first is literally the id). A file that does not decode
    as UTF-8, or that contains a NUL byte, is treated as binary and is never
    rewritten: a byte rewrite inside a binary could corrupt it, so such a carrier
    fails the build instead.
    """
    if path.suffix in TEXT_SUFFIXES:
        return True
    if path.suffix:
        return False
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    if b"\x00" in payload:
        return False
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", required=True, help="built web bundle (web/.next)")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-date-epoch", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--evidence", help="write the normalisation record here")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    build_id_file = root / "BUILD_ID"
    if not build_id_file.is_file():
        print(f"no BUILD_ID under {root}", file=sys.stderr)
        return 2
    old = build_id_file.read_text(encoding="utf-8").strip()
    new = derive_build_id(args.commit, args.source_date_epoch, args.version)
    if len(old) != len(new):
        print(f"refusing: generated id {new!r} is not the length of {old!r}", file=sys.stderr)
        return 2

    needle, substitute = old.encode(), new.encode()
    renamed: list[str] = []
    changed: list[dict] = []
    files_changed = 0
    occurrences = 0
    remaining: list[str] = []

    # 1. Path names first: a directory rename moves whatever is inside it.
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if is_skipped(path, root) or old not in path.name:
            continue
        target = path.with_name(path.name.replace(old, new))
        renamed.append(str(target.relative_to(root)))
        if not args.dry_run:
            path.rename(target)

    # 2. Then file contents.
    for path in sorted(root.rglob("*")):
        if not path.is_file() or is_skipped(path, root):
            continue
        if not is_text(path):
            if needle in path.read_bytes():
                remaining.append(str(path.relative_to(root)))
            continue
        payload = path.read_bytes()
        count = payload.count(needle)
        if not count:
            continue
        if not args.dry_run:
            path.write_bytes(payload.replace(needle, substitute))
        occurrences += count
        files_changed += 1
        if len(changed) < 40:
            changed.append({"file": str(path.relative_to(root)), "occurrences": count})

    # 2b. The generated temporary tsconfig name is random per build, and the name
    # is recorded in files that do ship (the file itself does not).
    ts_needle = re.compile(rb"tsconfig\.deeptutor-build-(\d+)\.json")

    def _substitute(match: "re.Match[bytes]") -> bytes:
        return (
            b"tsconfig.deeptutor-build-"
            + derive_tsconfig_number(args.commit, args.version).encode()
            + b".json"
        )

    ts_files, ts_occurrences, ts_samples = normalize_tsconfig(
        ts_needle, _substitute, root, args.dry_run
    )

    # 3. Fail closed: the packaged bundle must not carry the random id anywhere.
    if not args.dry_run:

        def carries_random_value(payload: bytes) -> bool:
            """True when a value that should have been rewritten is still there.

            The build id must be gone. The tsconfig name may legitimately appear —
            it is now the derived name — so a match only counts when its number is
            not the one this release derives; checking the *replacement* is what
            makes this a real test rather than a search for the pattern that was
            just written (the first version of this check failed a correct build
            for exactly that reason).
            """
            if needle in payload:
                return True
            expected = (
                b"tsconfig.deeptutor-build-"
                + derive_tsconfig_number(args.commit, args.version).encode()
                + b".json"
            )
            for match in ts_needle.finditer(payload):
                if match.group(0) != expected:
                    return True
            return False

        leftovers = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file()
            and not is_skipped(path, root)
            and carries_random_value(path.read_bytes())
        ]
        if leftovers or remaining:
            print(
                "the random build id survived normalisation in: "
                + ", ".join(sorted(set(leftovers + remaining))[:10]),
                file=sys.stderr,
            )
            return 1

    record = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(root),
        "generated_build_id": old,
        "derived_build_id": new,
        "derived_from": {
            "commit": args.commit,
            "source_date_epoch": args.source_date_epoch,
            "version": args.version,
        },
        "renamed_paths": len(renamed),
        "files_changed": files_changed,
        "occurrences_rewritten": occurrences,
        "tsconfig_files_changed": ts_files,
        "tsconfig_occurrences_rewritten": ts_occurrences,
        "tsconfig_samples": ts_samples,
        "renamed": renamed[:20],
        "files": changed,
    }
    if args.evidence and not args.dry_run:
        Path(args.evidence).write_text(json.dumps(record, indent=2) + "\n")
    print(
        f"build id {old} -> {new} ({len(renamed)} paths renamed, "
        f"{occurrences} occurrences in {files_changed} files rewritten); "
        f"tsconfig name rewritten in {ts_files} files ({ts_occurrences} occurrences)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
