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


#: Wheel RECORD files carry urlsafe-base64 sha256 digests, not hex.
RECORD_DIGEST = re.compile(rb"sha256=[A-Za-z0-9_-]{43}")
#: Generated per-build tokens. Next.js writes the standalone bundle with a
#: temporary TypeScript config whose name carries a number chosen at build time,
#: and records that name in required-server-files.json. It identifies the build,
#: not the code, so it is neutralised like the build id itself.
GENERATED_TOKEN = re.compile(rb"deeptutor-build-\d+")
#: The generated service-worker/manifest origins array, replaced by field name.
BUILDER_ORIGINS = re.compile(rb'("allowedDevOrigins"\s*:\s*)\[[^\]]*\]')
BUILDER_ORIGINS_PLACEHOLDER = rb'\1["<builder-network>"]'
GENERATED_TOKEN_PLACEHOLDER = b"deeptutor-build-<token>"
#: Generated key material. Next.js generates random secrets on every build and
#: ships them inside the bundle: a 32-byte encryption key for Server Actions
#: (`encryptionKey`, in the server reference manifest) and the preview-mode id and
#: signing key (`preview.previewModeId`, `preview.previewModeSigningKey`, in the
#: prerender manifest). They are generated material like the build id, and it is
#: *correct* for them to differ between builds, so the comparison neutralises the
#: fields **by name** rather than treating two different random keys as an
#: unreproducible build.
#:
#: They are deliberately NOT derived from the release identity the way the build
#: id is: these values are secrets (they sign preview cookies and encrypt Server
#: Action payloads), so a value computable from the commit would weaken the
#: deployment. Their presence is why two builds are content-identical rather than
#: byte-identical, and the promotion gate compares content, not bytes, for exactly
#: this reason.
GENERATED_KEY = re.compile(
    rb'("(?:encryptionKey|previewModeId|previewModeSigningKey)"\s*:\s*)"[^"]*"'
)
#: Builder-derived values. `web/next.config.js` enumerates this machine's
#: non-loopback IPv4 addresses into `allowedDevOrigins` at build time, so a release
#: artifact records the address of the machine that built it: measured, this host
#: builds `["127.0.0.1","169.254.0.21"]` and a CI runner builds its own. It is the
#: builder's environment rather than the source, and `next start` ignores the
#: setting, so the comparison masks the array and requires everything else in those
#: entries to be equal. Removing it from the artifact is a product change
#: (next.config.js has no override) and is deliberately not made in this phase; it
#: is recorded as a finding instead.


def neutralise_generated_tokens(payload: bytes) -> bytes:
    """Replace generated per-build tokens, keys and builder-derived values.

    Everything masked here is recorded by field or token name and is not the
    source: the build id and the temporary tsconfig name (derived from the release
    for shipped artifacts, but still masked so an older artifact compares), the
    secrets Next.js randomises per build, and the builder's own network addresses.
    Each mask makes the two sides comparable; nothing else in the entry is
    excused, so a difference outside these fields is still unexplained.
    """
    body = GENERATED_TOKEN.sub(GENERATED_TOKEN_PLACEHOLDER, payload)
    body = GENERATED_KEY.sub(rb'\1"<generated-key>"', body)
    return BUILDER_ORIGINS.sub(BUILDER_ORIGINS_PLACEHOLDER, body)


def embedded_json_canonical(payload: bytes, build_id: str) -> bytes | None:
    """Canonicalise a JSON object embedded in a generated script.

    Next.js writes route manifests as `globalThis.__RSC_MANIFEST=...={...}`. The
    object is JSON; the assignment prefix is not. Parsing the JSON payload and
    re-serialising it with sorted keys compares the module map itself, while the
    prefix is compared as text. Returns None when there is no JSON payload, in
    which case this rule does not apply.
    """
    import json

    body = neutralise_generated_tokens(digest_blind(payload, build_id))
    text = body.decode("utf-8", "replace")
    boundary = text.rfind("={")
    if boundary == -1:
        return None
    prefix, candidate = text[: boundary + 1], text[boundary + 1 :].strip()
    if candidate.endswith(";"):
        candidate = candidate[:-1]
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    # sort_keys applies at every level, so a nested map whose iteration order
    # varied between builds compares by its data rather than by that order.
    canonical_body = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return (prefix + canonical_body).encode()


def sorted_entries(payload: bytes, build_id: str) -> bytes | None:
    """Entry-order-insensitive form for generated object literals.

    Some generated manifests are written from a map whose iteration order varies
    between runs, so the *same* entries appear in a different order. Splitting the
    text on `},{` boundaries and sorting the pieces compares the multiset of
    entries: identical pieces in a different order are an ordering difference, and
    a piece that actually changed still shows up. Works for plain JSON and for
    JSON embedded in a generated script, where a parser is not available. Returns
    None when there is nothing to split.
    """
    body = neutralise_generated_tokens(digest_blind(payload, build_id))
    if b"},{" not in body:
        return None
    pieces = body.replace(b"},{", b"}\n{").splitlines()
    return b"\n".join(sorted(piece for piece in pieces if piece.strip()))


def json_canonical(payload: bytes, build_id: str) -> bytes | None:
    """A JSON entry re-serialised with sorted keys.

    Next.js writes some manifests from a map whose iteration order varies between
    runs: the parsed data is identical and only the order of the keys differs.
    Re-serialising both sides with sorted keys compares the data instead of the
    accident of its ordering. Returns None when the entry is not valid JSON, in
    which case this rule does not apply.
    """
    import json

    try:
        parsed = json.loads(digest_blind(payload, build_id).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()


def sorted_lines(payload: bytes, build_id: str) -> bytes | None:
    """Line-based content compared as a multiset of lines.

    The same case as sorted JSON keys, for generated manifest scripts: the lines
    are identical and only their order differs. Returns None for content that is
    not line-based text.
    """
    body = digest_blind(payload, build_id)
    if b"\x00" in body[:4096]:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "\n".join(sorted(line for line in text.splitlines() if line.strip())).encode()


def digest_blind(payload: bytes, build_id: str) -> bytes:
    """Normalised content with digest-like tokens neutralised.

    Build-id-derived files are hashed by build metadata (a wheel RECORD, a
    manifest of hashes). Those digests necessarily differ when the build id
    differs even though nothing they digest changed. Blindfolding digests turns
    "records some other file's hash" into a comparable form; it is only ever
    applied to entries that already differ, and any entry whose content differs
    beyond digests is reported as unexplained.
    """
    body = neutralise_generated_tokens(normalised(payload, build_id))
    return RECORD_DIGEST.sub(b"sha256=<digest>", DIGEST.sub(b"<digest>", body))


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
    variant_entries: dict[str, dict[str, str]] = {}
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
            # NB: the local names must not shadow the module-level canonical()
            # digest helper used for the manifest itself.
            variants: dict[str, str] = {}
            if json_form := json_canonical(payload, build_id):
                variants["json_sorted_keys"] = hashlib.sha256(json_form).hexdigest()
            if line_form := sorted_lines(payload, build_id):
                variants["sorted_lines"] = hashlib.sha256(line_form).hexdigest()
            if embedded := embedded_json_canonical(payload, build_id):
                variants["embedded_json_sorted_keys"] = hashlib.sha256(embedded).hexdigest()
            if entries_form := sorted_entries(payload, build_id):
                variants["sorted_entries"] = hashlib.sha256(entries_form).hexdigest()
            variant_entries[name] = variants
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
        "variant_files": variant_entries,
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
        "variant_files": {},
        "build_id_records": [],
        "_payloads": {name: (root / name).read_bytes() for name in entries},
    }


def public(manifest: dict) -> dict:
    """A manifest without the raw payloads the comparison keeps in memory."""
    return {key: value for key, value in manifest.items() if not key.startswith("_")}


def compare(first: dict, second: dict) -> dict:
    """Compare two builds entry by entry and account for every difference.

    The comparison is deliberately verification-based: a difference is only
    called explained when the explanation has been checked.

    * path differences are explained when, after replacing the generated build id
      in every entry name, the two entry sets are identical. That is what a build
      id in a directory name looks like, and nothing else.
    * content differences are explained when the two entries are byte-identical
      after that same replacement: the id was the only thing that differed.
    * a residual difference is explained when the two entries are identical once
      digest-like tokens are neutralised as well — the entry records hashes of
      entries the build id changed (a wheel RECORD, a manifest of chunk hashes),
      so it necessarily differs while nothing it describes did.
    * everything else is **unexplained**, and an unexplained difference is a
      release-integrity finding rather than a build artefact.
    """
    a, b = first["files"], second["files"]
    ca, cb = first["canonical_files"], second["canonical_files"]
    da, db = first.get("digest_blind_files", {}), second.get("digest_blind_files", {})

    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(name for name in set(a) & set(b) if a[name] != b[name])
    c_added = sorted(set(cb) - set(ca))
    c_removed = sorted(set(ca) - set(cb))
    c_changed = sorted(name for name in set(ca) & set(cb) if ca[name] != cb[name])

    # Entries identical after build-id normalisation: the id was the difference.
    same_after_id = [
        name for name in changed if ca.get(name) and cb.get(name) and ca[name] == cb[name]
    ]

    # Residual: still different after build-id normalisation. Each one is explained
    # only by a checked rule, and the rule that explained it is recorded.
    va, vb = first.get("variant_files", {}), second.get("variant_files", {})
    residual_rules: dict[str, str] = {}
    unexplained: list[str] = []
    for name in c_changed:
        if name in da and name in db and da[name] == db[name]:
            residual_rules[name] = "derived_and_generated_values"
            continue
        explained = False
        for rule in (
            "json_sorted_keys",
            "embedded_json_sorted_keys",
            "sorted_entries",
            "sorted_lines",
        ):
            if va.get(name, {}).get(rule) and va[name][rule] == vb.get(name, {}).get(rule):
                residual_rules[name] = rule
                explained = True
                break
        if not explained:
            unexplained.append(name)

    build_ids = [i for i in (first.get("build_id"), second.get("build_id")) if i]
    return {
        "classification": {
            # Every raw name difference is accounted for only when the entry sets
            # match once the build id is normalised, which is what c_added/c_removed
            # being empty means. The count reports the raw names.
            "names_differ_only_by_build_id": 0
            if (c_added or c_removed)
            else len(added) + len(removed),
            "content_differs_only_by_build_id": len(same_after_id),
            "derived_and_generated_values": sum(
                1 for rule in residual_rules.values() if rule == "derived_and_generated_values"
            ),
            "ordering_only_json": sum(
                1 for rule in residual_rules.values() if rule == "json_sorted_keys"
            ),
            "ordering_only_lines": sum(
                1 for rule in residual_rules.values() if rule == "sorted_lines"
            ),
            "ordering_only_embedded_json": sum(
                1 for rule in residual_rules.values() if rule == "embedded_json_sorted_keys"
            ),
            "ordering_only_entries": sum(
                1 for rule in residual_rules.values() if rule == "sorted_entries"
            ),
            "unexplained": len(unexplained),
        },
        "residual_rules": residual_rules,
        "unexplained": unexplained[:20],
        "build_id_records": sorted(
            set(first.get("build_id_records", [])) | set(second.get("build_id_records", []))
        ),
        "reproducible": not unexplained and not c_added and not c_removed,
        "reproducibility_note": (
            "Two builds of one commit are byte-identical only if the toolchain is deterministic. "
            "Next.js generates a random build id per build and cannot be told to pin it without a "
            "product configuration change, so the shipped bytes differ. This comparison therefore "
            "accounts for every difference by checking it: names that differ only by the build id, "
            "content that differs only by the build id, entries that record build-id-derived "
            "digests, and anything left over. Nothing left over is the result."
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
        "content_identical": not (c_added or c_removed or c_changed),
        "canonical_added": c_added,
        "canonical_removed": c_removed,
        "canonical_changed": c_changed,
        "build_ids": build_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--wheel")
    parser.add_argument("--dir")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        help="write the comparison document to PATH as well (the machine-readable input "
        "for the release evidence, so a gate is never decided by parsing prose)",
    )
    args = parser.parse_args()

    def load(target: str) -> dict:
        path = Path(target)
        return manifest_of_wheel(path) if path.is_file() else manifest_of_dir(path)

    if args.compare:
        result = compare(load(args.compare[0]), load(args.compare[1]))
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
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
            classes = result["classification"]
            print(f"unexplained differences: {classes['unexplained']}")
            print(
                f"  names differing only by the build id     : {classes['names_differ_only_by_build_id']}"
            )
            print(
                f"  content differing only by the build id   : {classes['content_differs_only_by_build_id']}"
            )
            print(
                f"  build-id derived digests                 : {classes['build_id_derived_digests']}"
            )
            print(f"  ordering only, JSON keys sorted          : {classes['ordering_only_json']}")
            print(f"  ordering only, lines sorted              : {classes['ordering_only_lines']}")
            print(
                f"  ordering only, embedded JSON keys sorted : {classes['ordering_only_embedded_json']}"
            )
            print(
                f"  ordering only, sorted entries            : {classes['ordering_only_entries']}"
            )
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
