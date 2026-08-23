#!/usr/bin/env python3
"""
project.py — render a normalized-session doc into a plain-files session-filesystem tree.

Proves the events.schema.json contract is CONSUMABLE as a session filesystem — the
git-like object model an unreleased companion project (session-fs) builds on: content-addressed
objects/, an append-only log/, per-inference manifests, and a materialized context/
of relative symlinks into objects/ (the on-disk dedup mechanism). This is an in-repo
round-trip test; it does NOT touch any consumer.

    /<out>/
      objects/<sha>.json                       write-once content (dedup by hash)
      log/NNNNN-<type>.json                     append-only event log
      inferences/NNNN/manifest.json            the reified context (ordered refs)
      inferences/NNNN/context/III-role.json --> ../../../objects/<sha>.json  (symlink)
      current.json                             -> last inference

stdlib only. Usage:
    harness-project <normalized.json> --out DIR [--verify]
"""

import argparse
import json
import os
import sys
from typing import Any

from ..collector.sink import makedirs_private, open_private
from .claude_code import is_object_key

MANIFEST_KEYS: tuple[str, ...] = (
    "index",
    "parent_index",
    "agent",
    "lineage_id",
    "model",
    "correlation",
    "usage",
    "stop_reason",
    "thinking",
    "prelude",
    "pairing",
)


def _object_path(obj_dir: str, sha: str) -> str:
    """objects/<sha>.json — refusing anything that is not a bare sha256 digest.

    Store keys become FILE NAMES here, so a key like '../../victim' would write
    outside the tree. The key shape is checked first (cheap, total), and the
    resolved path is then asserted to stay under obj_dir — belt and braces,
    because this function is the only place a doc's keys touch the filesystem.
    """
    if not is_object_key(sha):
        raise ValueError(f"refusing to project object key {sha[:64]!r}: not a sha256 hex digest")
    path = os.path.join(obj_dir, f"{sha}.json")
    root = os.path.realpath(obj_dir)
    if os.path.commonpath([os.path.realpath(os.path.dirname(path)), root]) != root:
        raise ValueError(f"refusing to project object key {sha[:64]!r}: escapes objects/")
    return path


def render(doc: dict[str, Any], out: str) -> str:
    obj_dir = os.path.join(out, "objects")
    log_dir = os.path.join(out, "log")
    inf_dir = os.path.join(out, "inferences")
    # Everything here is session content — objects, the log, the manifests — so
    # it is created 0700/0600 from the start, whether the launcher made the
    # parent or `harness-project --out` was run by hand. makedirs_private only
    # tightens the LAST component, so each level is created in turn.
    makedirs_private(out)
    for d in (obj_dir, log_dir, inf_dir):
        makedirs_private(d)

    # Validate the WHOLE doc before writing anything: a hostile or corrupt key
    # must not leave a half-rendered tree behind.
    for sha in doc["objects"]:
        _object_path(obj_dir, sha)
    for ev in doc["events"]:
        if ev["type"] == "inference":
            for e in ev["context"]:
                _object_path(obj_dir, e["ref"])
        elif ev["type"] == "message":
            for r in ev.get("block_refs") or []:
                _object_path(obj_dir, r)

    for sha, obj in doc["objects"].items():
        with open_private(_object_path(obj_dir, sha)) as f:
            json.dump(obj, f, indent=2, sort_keys=True)

    for ev in doc["events"]:
        with open_private(os.path.join(log_dir, f"{ev['seq']:05d}-{ev['type']}.json")) as f:
            json.dump(ev, f, indent=2)

    last = None
    for ev in doc["events"]:
        if ev["type"] != "inference":
            continue
        i = ev["index"]
        d = os.path.join(inf_dir, f"{i:04d}")
        ctx = os.path.join(d, "context")
        makedirs_private(d)
        makedirs_private(ctx)
        manifest = {k: ev[k] for k in MANIFEST_KEYS if k in ev}
        manifest["context"] = [e["ref"] for e in ev["context"]]
        with open_private(os.path.join(d, "manifest.json")) as f:
            json.dump(manifest, f, indent=2)
        for j, e in enumerate(ev["context"]):
            link = os.path.join(ctx, f"{j:03d}-{e['role']}.json")
            target = os.path.relpath(_object_path(obj_dir, e["ref"]), ctx)
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(target, link)  # relative symlink, per the design's container caveat
        last = i

    if last is not None:
        with open_private(os.path.join(out, "current.json")) as f:
            json.dump({"inference": last}, f)
    return out


def verify(doc: dict[str, Any], out: str) -> tuple[dict[str, Any], list[str]]:
    problems = []
    obj_dir = os.path.join(out, "objects")
    n_obj = len([f for f in os.listdir(obj_dir) if f.endswith(".json")])
    if n_obj != len(doc["objects"]):
        problems.append(f"object count on disk {n_obj} != {len(doc['objects'])} in doc")
    inf_dir = os.path.join(out, "inferences")
    total = broken = 0
    for nn in sorted(os.listdir(inf_dir)):
        ctx = os.path.join(inf_dir, nn, "context")
        for link in os.listdir(ctx):
            total += 1
            if not os.path.exists(os.path.join(ctx, link)):  # follows the symlink
                broken += 1
    if broken:
        problems.append(f"{broken}/{total} context symlinks do not resolve")
    return {
        "objects_on_disk": n_obj,
        "context_symlinks": total,
        "broken_symlinks": broken,
    }, problems


def context_delta(doc: dict[str, Any], i: int, j: int) -> dict[str, int]:
    """The 'what did compaction/the turn change?' query, as a set diff of context refs."""
    by = {
        e["index"]: [c["ref"] for c in e["context"]]
        for e in doc["events"]
        if e["type"] == "inference"
    }
    a, b = set(by.get(i, [])), set(by.get(j, []))
    return {"added": len(b - a), "removed": len(a - b), "shared": len(a & b)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("normalized_json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    with open(args.normalized_json) as f:
        doc: dict[str, Any] = json.load(f)
    render(doc, args.out)

    if args.verify:
        stats, problems = verify(doc, args.out)
        print("== projection round-trip ==", file=sys.stderr)
        for k, v in stats.items():
            print(f"  {k}: {v}", file=sys.stderr)
        infs = [e["index"] for e in doc["events"] if e["type"] == "inference"]
        if len(infs) >= 2:
            print(
                f"  context delta inf {infs[0]}->{infs[-1]}: "
                f"{context_delta(doc, infs[0], infs[-1])}",
                file=sys.stderr,
            )
        if problems:
            for p in problems:
                print("  PROBLEM:", p, file=sys.stderr)
            sys.exit(1)
        print(
            "  round-trip: OK (object count matches, all context symlinks resolve)", file=sys.stderr
        )


if __name__ == "__main__":
    main()
