#!/usr/bin/env python3
"""
validate.py — validate a normalized-session doc against the packaged events.schema.json.

Uses `jsonschema` (draft 2020-12) when available; otherwise falls back to the structural
invariants in claude_code.check_invariants (ref-integrity, no cache_control in objects).

With --roundtrip, additionally reconstructs every inference's request (and response
content) from the doc ALONE and semantic-diffs it against the raw bodies in --capture:
any diff = a capture gap (see reconstruct.py for the documented exceptions).

    harness-validate <normalized.json> [more.json ...]
    harness-validate --roundtrip --capture <capture-dir> <normalized.json>

--capture is repeatable: a merged multi-capture doc (session.captures > 1) names,
per raw body, which capture holds it — pass every capture dir and each body is
resolved against its own.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from importlib.resources import files
from typing import Any

# The only shape raw_bodies[].path is ever allowed to take. The normalizer emits
# os.path.relpath(<capture>/bodies/<name>.{request,response}.json, <capture>) and
# nothing else, and the schema pins the same pattern — but the oracle re-checks
# it, because a document is an INPUT here and may not be one this machine wrote.
_BODY_REL = re.compile(r"^bodies/[^/]+\.(request|response)\.json$")

# The schema ships INSIDE the package (src/harness_telemetry/schema/), so it is
# present under any install — wheel, sdist or editable — not just an in-repo
# clone. The repo-root schema/events.schema.json is a symlink to this file: the
# path-stable public location that consuming projects and the $id both name.
SCHEMA = files("harness_telemetry") / "schema" / "events.schema.json"


def load_schema() -> dict[str, Any]:
    """The packaged events.schema.json, parsed."""
    schema: dict[str, Any] = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return schema


def schema_errors(doc: dict[str, Any]) -> list[str] | None:
    """Schema violations in `doc`, or None when jsonschema is not installed.

    The single place the schema is applied — check_invariants calls THIS (not
    validate_doc) so the emitter's own --check validates without recursing back
    through the structural fallback below.
    """
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return None
    errs = sorted(Draft202012Validator(load_schema()).iter_errors(doc), key=lambda e: list(e.path))
    return [f"{list(e.path)} :: {e.message[:140]}" for e in errs[:20]]


def validate_doc(doc: dict[str, Any]) -> tuple[bool, str]:
    """Validate an in-memory normalized doc: the schema AND the semantic invariants.

    Schema validity is not authenticity. The object store is CONTENT-ADDRESSED —
    the key IS the sha256 of the value — so an edited object is still perfectly
    schema-valid, and a ref that resolves to nothing is too. Certifying a document
    "VALID" on the schema alone therefore certifies the wrong property; both halves
    run here, and check_invariants (which recomputes every object key, resolves
    every context/prelude/response/raw-body ref, and folds the schema in when
    jsonschema is installed) is the one that can tell.
    """
    from .claude_code import check_invariants

    errs = schema_errors(doc)
    _, problems = check_invariants(doc)
    # check_invariants folds schema errors in for a COMPLETE doc; for a partial
    # one it skips them, so add ours rather than certify an unchecked document.
    if errs and not any(p.startswith("schema: ") for p in problems):
        problems = [*problems, *(f"schema: {e}" for e in errs)]
    if problems:
        return False, "\n".join(f"  - {p}" for p in problems[:20])
    if errs is None:
        return True, "semantic invariants OK (schema unchecked: jsonschema not installed)"
    return True, "VALID against events.schema.json; semantic invariants OK"


def validate(path: str) -> tuple[bool, str]:
    with open(path) as f:
        doc: dict[str, Any] = json.load(f)
    return validate_doc(doc)


class UnsafeBodyPath(ValueError):
    """A raw_bodies[].path that is not a plain relative path inside a capture dir.

    The oracle reads files named by the DOCUMENT. A doc is an input — it may have
    been produced elsewhere, edited, or handed over — so `"path":
    "/etc/passwd"` or `"../../.ssh/id_rsa"` must fail the round-trip loudly
    rather than open anything.
    """


def _contained(capture_dir: str, rel: str) -> str | None:
    """`rel` joined onto `capture_dir`, or None if it lands outside it.

    realpath on both sides, so a SYMLINK inside the capture dir cannot redirect
    the read out of it either — the lexical `..` check alone would not see that.
    """
    root = os.path.realpath(capture_dir)
    p = os.path.join(capture_dir, rel)
    real = os.path.realpath(p)
    if real != root and not real.startswith(root + os.sep):
        return None
    return p


def _body_path(entry: dict[str, Any], captures: Sequence[str]) -> str | None:
    """Resolve a raw-body entry to a file across capture dirs: its own capture
    (by id) first, then any dir that has it (docs predating the capture field).

    Raises UnsafeBodyPath — before touching the filesystem — for anything that is
    not a relative `bodies/<name>.{request,response}.json` inside a capture dir.
    """
    rel = entry.get("path")
    if not isinstance(rel, str) or not rel:
        raise UnsafeBodyPath("<missing>")
    if os.path.isabs(rel) or ".." in rel.replace(os.sep, "/").split("/"):
        raise UnsafeBodyPath(rel)
    if not _BODY_REL.match(rel):
        raise UnsafeBodyPath(rel)
    cap = entry.get("capture")
    preferred = [c for c in captures if os.path.basename(os.path.abspath(c)) == cap]
    for c in [*preferred, *(c for c in captures if c not in preferred)]:
        p = _contained(c, rel)
        if p is None:  # a symlink escape: fail closed, do not try the next dir
            raise UnsafeBodyPath(rel)
        if os.path.exists(p):
            return p
    return None


def _read_verified(path: str, sha: str) -> bytes:
    """The file's bytes, checked against the sha the document keyed it by.

    raw_bodies is keyed BY the sha256 of the raw body, so this costs nothing to
    check and closes the gap between "the doc names a file" and "the doc names
    THIS content": without it the oracle would happily diff a reconstruction
    against a body somebody swapped underneath it.
    """
    with open(path, "rb") as f:
        raw = f.read()
    got = hashlib.sha256(raw).hexdigest()
    if got != sha:
        raise UnsafeBodyPath(f"{path} hashes to {got[:12]}, not the raw_bodies key {sha[:12]}")
    return raw


def roundtrip(path: str, capture: str | Sequence[str]) -> tuple[bool, str]:
    """Reconstruct every inference from the doc alone; diff against the raw bodies.

    An oracle that SKIPS what it cannot check certifies nothing, so every skip is
    a failure instead:

      - every inference must carry a request_body_ref that resolves in raw_bodies
        AND on disk. Whether bodies are expected is decided from EVIDENCE — a
        non-empty raw_bodies index, or body files in a --capture dir — never from
        the document's own captured_with.raw_bodies flag, which is a claim the
        document makes about itself and the one thing a doctored doc would clear
        to make the oracle stand down;
      - a null response_body_ref is legitimate ONLY for an inference that admits
        it is unpaired (pairing == null); an inference that claims a pairing must
        produce its response body;
      - every inference must actually be reached: the checked indices are tracked
        as a SET and any inference missing from it is named, so a doc with
        inferences but zero bodies compared cannot report a vacuous
        "round-trip OK: 0 requests + 0 responses";
      - each body file is verified against the sha it is keyed by BEFORE it is
        parsed, and its `path` must be a relative bodies/… name inside a capture
        dir (see UnsafeBodyPath).
    """
    from . import reconstruct

    captures = [capture] if isinstance(capture, str) else list(capture)
    with open(path) as f:
        doc: dict[str, Any] = json.load(f)
    raw_bodies = doc.get("raw_bodies") or {}
    # Evidence, not self-report: the index the doc carries, or files on disk.
    have_bodies = bool(raw_bodies) or any(
        glob.glob(os.path.join(c, "bodies", "*.request.json"))
        or glob.glob(os.path.join(c, "bodies", "*.response.json"))
        for c in captures
    )
    infs = [ev for ev in doc["events"] if ev["type"] == "inference"]
    checked: set[Any] = set()
    n_req = n_resp = 0
    diffs: list[str] = []

    def _resolve(ref: str | None, kind: str, idx: int, required: bool) -> tuple[str, str] | None:
        """(path, sha) for a ref, or None with the reason recorded."""
        if not ref:
            if required:
                diffs.append(
                    f"inference {idx} has no {kind}_body_ref, but this "
                    f"capture has raw bodies — nothing to verify it against"
                )
            return None
        if ref not in raw_bodies:
            diffs.append(f"inference {idx} {kind}_body_ref {ref[:12]} is not in raw_bodies")
            return None
        try:
            p = _body_path(raw_bodies[ref], captures)
        except UnsafeBodyPath as e:
            diffs.append(
                f"inference {idx} {kind} body path {e.args[0]!r} is not a relative "
                f"bodies/… path inside a --capture dir — refusing to read it"
            )
            return None
        if p is None:
            diffs.append(f"inference {idx} {kind} body not in any --capture dir")
            return None
        return p, ref

    for ev in infs:
        idx = ev["index"]
        found = _resolve(ev.get("request_body_ref"), "request", idx, have_bodies)
        if found is not None:
            try:
                raw = _read_verified(*found)
            except UnsafeBodyPath as e:
                diffs.append(f"inference {idx} request body {e.args[0]}")
            else:
                rebuilt = reconstruct.reconstruct_request(doc, ev)
                for d in reconstruct.semantic_diff_request(rebuilt, raw):
                    diffs.append(f"inference {idx} {d}")
                n_req += 1
                checked.add(idx)
        # A response-less inference is an honest record (a cancelled or errored
        # call): it is required to have a body only when it claims a pairing.
        claims_response = ev.get("pairing") is not None
        found = _resolve(
            ev.get("response_body_ref"), "response", idx, have_bodies and claims_response
        )
        if found is not None:
            try:
                raw = _read_verified(*found)
            except UnsafeBodyPath as e:
                diffs.append(f"inference {idx} response body {e.args[0]}")
            else:
                rebuilt_resp = reconstruct.reconstruct_response(doc, ev)
                for d in reconstruct.semantic_diff_response(rebuilt_resp, raw):
                    diffs.append(f"inference {idx} {d}")
                n_resp += 1
                checked.add(idx)

    skipped = [ev["index"] for ev in infs if ev["index"] not in checked]
    if skipped:
        diffs.append(
            f"round-trip checked {len(checked)} of {len(infs)} inferences — "
            f"nothing was verified for inference(s) "
            f"{', '.join(str(i) for i in skipped[:20])}"
        )
    if diffs:
        return False, "\n".join("  " + d for d in diffs[:40])
    return True, (
        f"round-trip OK: {n_req} requests + {n_resp} responses "
        f"semantically identical ({len(infs)} inferences)"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("docs", nargs="+")
    ap.add_argument(
        "--roundtrip",
        action="store_true",
        help="also reconstruct every inference and diff vs the raw bodies",
    )
    ap.add_argument(
        "--capture",
        action="append",
        help="capture dir holding the raw bodies (the oracle); repeatable "
        "for a merged multi-capture doc",
    )
    args = ap.parse_args()
    if args.roundtrip and not args.capture:
        ap.error("--roundtrip needs --capture DIR (the raw-body oracle)")
    rc = 0
    for p in args.docs:
        ok, msg = validate(p)
        print(f"{p}: {'VALID' if ok else 'INVALID'}")
        if not ok:
            print(msg)
            rc = 1
        if args.roundtrip:
            ok, msg = roundtrip(p, args.capture)
            print(f"{p}: {'ROUNDTRIP OK' if ok else 'ROUNDTRIP FAILED'}")
            print(msg if not ok else "  " + msg)
            rc = rc if ok else 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
