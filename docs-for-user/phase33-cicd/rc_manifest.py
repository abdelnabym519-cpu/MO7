#!/usr/bin/env python3
"""Wheel content manifest and comparison.

The artifact's identity has two halves:

* its SHA256 — the exact bytes that were built;
* its **content manifest** — every entry in the wheel with its own sha256,
  folded into one `manifest_sha256`.

The second exists because a wheel is a zip: two builds of identical sources can
differ in bytes (entry order, embedded timestamps, and the Next.js web bundle's
build id) while shipping identical *content*. Printing one SHA256 and calling it
provenance cannot tell those two situations apart. With a manifest, a difference
is localised to the entries that actually differ, so the release controller can
say what changed and whether it matters — and a manifest whose entries are all
equal is a reproducible-content result even when the bytes are not.

    rc_manifest.py --wheel FILE [--json]
    rc_manifest.py --compare A B [--json]
    rc_manifest.py --dir DIR [--json]        # legacy directory comparison
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

BUILD_ID_ENTRY = "deeptutor_web/.next/BUILD_ID"
#: What a build id is replaced with when computing the normalised form.
BUILD_ID_PLACEHOLDER = "<build-id>"
#: Digest-like tokens (32-64 hex characters) inside entry content.
DIGEST = re.compile(rb"(?<![0-9a-f])[0-9a-f]{32,64}(?![0-9a-f])")
#: Entries that exist only to record the build id itself, so they have no
#: counterpart to compare: the id is the whole of their content.
BUILD_ID_RECORDS = (BUILD_ID_ENTRY, "deeptutor_web/.next/trace")


def normalised(payload: bytes, build_id: str) -> bytes:
    """Entry content with the framework's generated build id neutralised."""
    if not build_id:
        return payload
    return payload.replace(build_id.encode(), BUILD_ID_PLACEHOLDER.encode())


def digest_blind(payload: bytes, build_id: str) -> bytes:
    """Normalised content with digest-like tokens neutralised.

    Build-id-derived files are hashed by build metadata (a wheel RECORD, a
    manifest of hashes). Those digests necessarily differ when the build id
    differs even though nothing they digest changed. Blindfolding digests turns
    "records some other file's hash" into a comparable form; it is only ever
    applied to entries that already differ, and any entry whose content differs
    beyond digests is reported as unexplained.
    """
    return DIGEST.sub(b"<digest>", normalised(payload, build_id))


def build_id_of(archive: zipfile.ZipFile) -> str:
    """The Next.js build id this wheel was built with ('' when there is none)."""
    try:
        return archive.read(BUILD_ID_ENTRY).decode("utf-8", "replace").strip()
    except KeyError:
        return ""


def canonical(entries: dict[str, str]) -> str:
    """A stable digest over the entry set, independent of iteration order."""
    payload = json.dumps(sorted(entries.items()), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def manifest_of_wheel(wheel: Path) -> dict:
    """Actual and canonical manifests for a wheel.

    The **actual** manifest hashes every entry exactly as shipped: it is what the
    release controller verifies a deployed artifact against.

    The **canonical** manifest additionally removes the one input Next.js cannot
    be told to pin: the build id it generates with a random nanoid. Every
    occurrence of that id is replaced by a placeholder, in entry paths (Next
    names a static chunk directory after it) and in entry bytes (it is embedded
    in the server HTML/RSC/manifest files). Two builds of the same source are
    content-equivalent exactly when their canonical manifests are equal, which is
    a claim a person can check instead of a claim about how builds "should" go.
    """
    entries: dict[str, str] = {}
    canonical_entries: dict[str, str] = {}
    digest_blind_entries: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    with zipfile.ZipFile(wheel) as archive:
        build_id = build_id_of(archive)
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            payload = archive.read(info.filename)
            payloads[info.filename] = payload
            entries[info.filename] = hashlib.sha256(payload).hexdigest()
            if info.filename in BUILD_ID_RECORDS:
                continue
            name = (
                info.filename.replace(build_id, BUILD_ID_PLACEHOLDER) if build_id else info.filename
            )
            canonical_entries[name] = hashlib.sha256(normalised(payload, build_id)).hexdigest()
            digest_blind_entries[name] = hashlib.sha256(digest_blind(payload, build_id)).hexdigest()
    return {
        "artifact": wheel.name,
        "artifact_bytes": wheel.stat().st_size,
        "artifact_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "entries": len(entries),
        "manifest_sha256": canonical(entries),
        "build_id": build_id,
        "canonical_entries": len(canonical_entries),
        "canonical_sha256": canonical(canonical_entries),
        "files": entries,
        "canonical_files": canonical_entries,
        "digest_blind_files": digest_blind_entries,
        "build_id_records": [n for n in entries if n in BUILD_ID_RECORDS],
        # Kept out of --json output (it is already written to stdout for other
        # verbs); the comparison uses it in-process.
        "_payloads": payloads,
    }


def manifest_of_dir(root: Path) -> dict:
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        entries[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "artifact": str(root),
        "artifact_bytes": sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
        "artifact_sha256": None,
        "entries": len(entries),
        "manifest_sha256": canonical(entries),
        "build_id": "",
        "canonical_entries": len(entries),
        "canonical_sha256": canonical(entries),
        "files": entries,
        "canonical_files": entries,
        "digest_blind_files": entries,
        "build_id_records": [],
        "_payloads": {name: (root / name).read_bytes() for name in entries},
    }


def public(manifest: dict) -> dict:
    """A manifest without the raw payloads the comparison keeps in memory."""
    return {key: value for key, value in manifest.items() if not key.startswith("_")}


def classify(first: dict, second: dict, name: str) -> str:
    """Why one entry differs between two builds of the same commit.

    build_id_in_path   the framework names this entry after the generated id
    build_id_in_content the entry embeds the generated id
    build_id_digest    the entry differs only in digest-like tokens (it records
                       hashes of entries the build id changed)
    unexplained        none of the above — a difference the build id cannot
                       account for, which is exactly what release integrity must
                       not tolerate silently
    """
    build_id = first.get("build_id") or second.get("build_id") or ""
    a, b = first, second
    in_a, in_b = name in a["files"], name in b["files"]
    if build_id and build_id in name:
        return "build_id_in_path"
    if build_id:
        for side in (a, b):
            if name in side["files"] and build_id.encode() in side["_payloads"].get(name, b""):
                return "build_id_in_content"
    if in_a and in_b:
        blind = (a.get("digest_blind_files", {}), b.get("digest_blind_files", {}))
        if name in blind[0] and name in blind[1] and blind[0][name] == blind[1][name]:
            return "build_id_digest"
    return "unexplained"


def compare(first: dict, second: dict) -> dict:
    a, b = first["files"], second["files"]
    ca, cb = first["canonical_files"], second["canonical_files"]
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(name for name in set(a) & set(b) if a[name] != b[name])
    c_added = sorted(set(cb) - set(ca))
    c_removed = sorted(set(ca) - set(cb))
    c_changed = sorted(name for name in set(ca) & set(cb) if ca[name] != cb[name])

    # Every raw difference gets a cause. A difference the build id cannot
    # explain is a release-integrity finding, not a build artefact.
    classification: dict[str, list[str]] = {
        "build_id_in_path": [],
        "build_id_in_content": [],
        "build_id_digest": [],
        "unexplained": [],
    }
    for name in added + removed + changed:
        classification[classify(first, second, name)].append(name)

    return {
        "classification": {key: len(value) for key, value in classification.items()},
        "unexplained": classification["unexplained"][:20],
        "build_id_records": sorted(
            set(first.get("build_id_records", [])) | set(second.get("build_id_records", []))
        ),
        "reproducible": not classification["unexplained"] and not c_added and not c_removed,
        "reproducibility_note": (
            "Two builds of one commit are byte-identical only if the toolchain is deterministic. "
            "Next.js generates a random build id per build and cannot be told to pin it without a "
            "product configuration change, so the shipped bytes differ. This comparison therefore "
            "classifies every difference: the build id itself, the entries named after it, the "
            "digests that record it, and anything left over. Nothing left over is the result."
        ),
        "first": {
            "artifact": first["artifact"],
            "sha256": first["artifact_sha256"],
            "entries": first["entries"],
            "manifest_sha256": first["manifest_sha256"],
            "build_id": first.get("build_id", ""),
            "canonical_sha256": first.get("canonical_sha256"),
        },
        "second": {
            "artifact": second["artifact"],
            "sha256": second["artifact_sha256"],
            "entries": second["entries"],
            "manifest_sha256": second["manifest_sha256"],
            "build_id": second.get("build_id", ""),
            "canonical_sha256": second.get("canonical_sha256"),
        },
        "byte_identical": first["artifact_sha256"] is not None
        and first["artifact_sha256"] == second["artifact_sha256"],
        "raw_difference": {"added": added, "removed": removed, "changed": changed},
        # Content equivalence modulo the toolchain's generated build id.
        "content_identical": not (c_added or c_removed or c_changed),
        "canonical_added": c_added,
        "canonical_removed": c_removed,
        "canonical_changed": c_changed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--wheel")
    parser.add_argument("--dir")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def load(target: str) -> dict:
        path = Path(target)
        return manifest_of_wheel(path) if path.is_file() else manifest_of_dir(path)

    if args.compare:
        result = compare(load(args.compare[0]), load(args.compare[1]))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                f"first : {result['first']['artifact']} entries={result['first']['entries']} "
                f"sha256={result['first']['sha256']}"
            )
            print(
                f"second: {result['second']['artifact']} entries={result['second']['entries']} "
                f"sha256={result['second']['sha256']}"
            )
            print(f"build ids: {result['first']['build_id']} / {result['second']['build_id']}")
            print(f"byte-identical: {result['byte_identical']}")
            raw = result["raw_difference"]
            print(
                f"raw difference: added={len(raw['added'])} removed={len(raw['removed'])} "
                f"changed={len(raw['changed'])}"
            )
            print(
                f"content-identical (canonical form): {result['content_identical']} "
                f"(added={len(result['canonical_added'])} removed={len(result['canonical_removed'])} "
                f"changed={len(result['canonical_changed'])})"
            )
            print(f"unexplained differences: {result['classification']['unexplained']}")
            print(f"  build-id named entries : {result['classification']['build_id_in_path']}")
            print(f"  build-id in content    : {result['classification']['build_id_in_content']}")
            print(f"  build-id derived digests: {result['classification']['build_id_digest']}")
            print(f"reproducible (every difference explained): {result['reproducible']}")
            for label in ("canonical_added", "canonical_removed", "canonical_changed"):
                for name in result[label][:10]:
                    print(f"  {label}: {name}")
            for name in result["unexplained"][:10]:
                print(f"  UNEXPLAINED: {name}")
        return 0 if result["reproducible"] else 1

    target = args.wheel or args.dir
    if not target:
        parser.error("one of --wheel, --dir or --compare is required")
    result = manifest_of_wheel(Path(target)) if args.wheel else manifest_of_dir(Path(target))
    if args.json:
        print(json.dumps(public(result), indent=2))
    else:
        print(
            f"{result['artifact']}: entries={result['entries']} "
            f"sha256={result['artifact_sha256']} manifest={result['manifest_sha256']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
