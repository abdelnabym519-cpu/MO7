#!/usr/bin/env python3
"""Remove the build machine's own paths from a built web bundle.

Two reasons, both about release integrity rather than tidiness:

* **Disclosure.** The bundle is shipped to hosts the builder never sees; the
  absolute path of the build checkout names the build machine's layout. A release
  artifact should not carry it.
* **Reproducibility.** A path that varies by builder is an input that varies by
  builder. Normalising it to a fixed placeholder makes the artifact's content a
  function of the commit rather than of where the build ran.

Only text-like files are touched, and the replacement is padded or truncated to
the **exact length** of the original path, so no file changes size: a length
field, an offset or a minified line must not shift because a path got shorter.

    rc_scrub_paths.py --root DIR --path /home/user/MO7 [--placeholder /srv/mo7-build]
                      [--evidence FILE]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

TEXT_SUFFIXES = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".json", ".txt", ".html", ".css", ".map"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", required=True, help="directory to scrub in place")
    parser.add_argument("--path", required=True, help="absolute build path to remove")
    parser.add_argument("--placeholder", default="/srv/mo7-build")
    parser.add_argument("--evidence", help="write the scrub record here")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    original = args.path.rstrip("/")
    if not original.startswith("/"):
        print("the path to scrub must be absolute", file=sys.stderr)
        return 2
    replacement = args.placeholder.rstrip("/")
    if len(replacement) != len(original):
        # Keep every file's size identical: a placeholder that is too long is
        # truncated, one that is too short is padded with the path separator,
        # which is harmless for the string this replaces.
        if len(replacement) > len(original):
            replacement = replacement[: len(original)]
        else:
            replacement = replacement + "/" * (len(original) - len(replacement))
    if replacement == original:
        print("placeholder equals the path to scrub; nothing to do")
        return 0

    needle = original.encode()
    substitute = replacement.encode()
    assert len(needle) == len(substitute)
    files_changed = 0
    occurrences = 0
    bytes_scanned = 0
    changed: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        payload = path.read_bytes()
        bytes_scanned += len(payload)
        if needle not in payload:
            continue
        count = payload.count(needle)
        path.write_bytes(payload.replace(needle, substitute))
        files_changed += 1
        occurrences += count
        if len(changed) < 40:
            changed.append({"file": str(path.relative_to(root)), "occurrences": count})

    record = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(root),
        "scrubbed_path": original,
        "placeholder": replacement,
        "equal_length": len(original) == len(replacement),
        "files_changed": files_changed,
        "occurrences": occurrences,
        "bytes_scanned": bytes_scanned,
        "files": changed,
    }
    if args.evidence:
        Path(args.evidence).write_text(json.dumps(record, indent=2) + "\n")
    print(
        f"scrubbed {occurrences} occurrences of {original} in {files_changed} files "
        f"(placeholder {replacement}, every file keeps its size)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
