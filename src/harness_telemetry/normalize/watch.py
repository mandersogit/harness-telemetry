#!/usr/bin/env python3
"""
watch.py — the live watcher: keep sessions/<ts>/ current while an instrumented
Claude Code session runs.

    harness-watch captures/<ts> [captures/<ts2> ...] [--out DIR]
                  [--interval 1.0] [--once]
                  [--transcript PATH | --no-transcript] [--discount X]

Design: each debounced update RE-RUNS the batch normalizer over the inputs
present instead of maintaining a delta engine. Batch normalize costs tens of
milliseconds on a session of a few dozen turns — free at poll granularity — and
this buys two structural properties:

  - Incremental == batch BY CONSTRUCTION: the doc on disk after any update IS a
    batch run over the inputs at that moment; the final doc equals the one-shot
    batch run exactly.
  - No join-gate stall: there is no per-call request∧response∧transcript join
    gate to hang on a message-less background call. A mid-flight request
    truthfully appears as a response-less inference and upgrades on a later tick
    when its response lands.

Debounce = settle: re-normalize only when the input fingerprint (bodies/*,
otel/*, the transcript file) changed AND held identical across two consecutive
polls — a body caught mid-write is retried next tick; a parse error skips the
tick (the previous output stays intact) and never kills the watcher.

Reader safety: normalized.json is written to a temp file and os.replace()d;
sessfs/ is rendered fresh and swapped whole (a mid-log insertion renumbers seq —
rendering in place would leave stale NNNNN-*.json files behind).

Several capture dirs = the resume lifecycle, live: watch the original run's dir
plus the continuation's and the merged logical session stays current.
"""

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any

from ..collector.sink import makedirs_private, open_private
from . import project
from .claude_code import (
    check_invariants,
    discount_arg,
    find_transcript,
    normalize,
    parse_user_id,
)


def _session_id_from_bodies(bodies_dir: str) -> str | None:
    try:
        names = [f for f in os.listdir(bodies_dir) if f.endswith(".request.json")]
    except OSError:
        return None
    for f in sorted(names, key=lambda f: os.path.getmtime(os.path.join(bodies_dir, f))):
        try:
            with open(os.path.join(bodies_dir, f)) as fh:
                ids = parse_user_id(json.load(fh))
        except (OSError, ValueError):
            continue  # mid-write or vanished; a later tick will see it whole
        if ids.get("session_id"):
            sid: str = ids["session_id"]
            return sid
    return None


class Watcher:
    """Poll → settle → re-normalize. tick() returns one of:
    "settling" (inputs changed, waiting for them to hold still),
    "updated" (normalized + projected), "unchanged", "skipped-partial"
    (an input failed to parse; retried on the next change)."""

    def __init__(
        self,
        capture_dirs: list[str],
        out_dir: str,
        transcript: str | None = "auto",
        discount: float | None = None,
    ) -> None:
        self.capture_dirs = capture_dirs
        self.out_dir = out_dir
        self.transcript = transcript
        self.discount = discount
        self._session_id: str | None = None
        self._last_fp: tuple[tuple[str, int, int], ...] | None = None
        self._done_fp: tuple[tuple[str, int, int], ...] | None = None

    def _transcript_path(self) -> str | None:
        if self.transcript != "auto":
            return self.transcript
        if self._session_id is None:
            for d in self.capture_dirs:
                self._session_id = _session_id_from_bodies(os.path.join(d, "bodies"))
                if self._session_id:
                    break
        return find_transcript(self._session_id)

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        entries: list[tuple[str, int, int]] = []
        paths: list[str] = []
        for d in self.capture_dirs:
            for sub in ("bodies", "otel", "hooks"):
                p = os.path.join(d, sub)
                if os.path.isdir(p):
                    paths.extend(os.path.join(p, f) for f in sorted(os.listdir(p)))
        t = self._transcript_path()
        if t:
            paths.append(t)
            # the transcript's sibling tree (<sid>/): sub-agent transcripts,
            # metas, workflow records — they appear dynamically mid-session
            sid_dir = t[: -len(".jsonl")] if t.endswith(".jsonl") else None
            if sid_dir and os.path.isdir(sid_dir):
                for root, _dirs, files in os.walk(sid_dir):
                    paths.extend(os.path.join(root, f) for f in sorted(files))
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((p, st.st_size, st.st_mtime_ns))
        return tuple(entries)

    def refresh(self) -> dict[str, Any] | None:
        """Normalize + write + project, atomically for readers. None = an input
        failed to parse (caught mid-write); the previous output stays."""
        try:
            doc = normalize(self.capture_dirs, transcript=self.transcript, discount=self.discount)
        except (json.JSONDecodeError, OSError):
            return None
        makedirs_private(self.out_dir)
        tmp = os.path.join(self.out_dir, ".normalized.json.tmp")
        # 0600 from creation, then os.replace() — a reader never sees a 0644
        # window on a file holding prompts and file contents.
        with open_private(tmp) as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp, os.path.join(self.out_dir, "normalized.json"))

        new = os.path.join(self.out_dir, ".sessfs.new")
        old = os.path.join(self.out_dir, ".sessfs.old")
        cur = os.path.join(self.out_dir, "sessfs")
        for d in (new, old):
            shutil.rmtree(d, ignore_errors=True)
        project.render(doc, new)
        if os.path.exists(cur):
            os.rename(cur, old)
        os.rename(new, cur)
        shutil.rmtree(old, ignore_errors=True)
        return doc

    def tick(self) -> str:
        fp = self.fingerprint()
        if fp != self._last_fp:
            self._last_fp = fp
            return "settling"
        if fp == self._done_fp:
            return "unchanged"
        doc = self.refresh()
        if doc is None:
            return "skipped-partial"
        self._done_fp = fp
        return "updated"

    def run(self, interval: float) -> None:
        print(
            f"[harness-watch] {' + '.join(self.capture_dirs)} -> {self.out_dir} "
            f"(poll {interval}s; Ctrl-C to stop)",
            file=sys.stderr,
        )
        try:
            while True:
                if self.tick() == "updated":
                    self._report()
                time.sleep(interval)
        except KeyboardInterrupt:
            # final settle-free refresh so the last landed inputs are reflected
            if self.refresh() is not None:
                self._report()

    def _report(self) -> None:
        try:
            with open(os.path.join(self.out_dir, "normalized.json")) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return
        stats, problems = check_invariants(doc, self.capture_dirs)
        n_inf = stats["events_by_type"].get("inference", 0)
        verdict = "OK" if not problems else f"{len(problems)} PROBLEMS"
        print(
            f"[harness-watch] {time.strftime('%H:%M:%S')} updated: {n_inf} inferences, "
            f"{stats.get('lineages', 0)} lineages — {verdict}",
            file=sys.stderr,
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "capture_dir",
        nargs="+",
        help="capture dir(s) to watch; several = one logical session (the resume lifecycle, live)",
    )
    ap.add_argument(
        "--out",
        help="output dir (default: $HARNESS_SESSIONS_DIR or "
        "./sessions, /<basename of first capture dir>)",
    )
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--once", action="store_true", help="refresh once immediately and exit")
    gt = ap.add_mutually_exclusive_group()
    gt.add_argument("--transcript")
    gt.add_argument("--no-transcript", action="store_true")
    ap.add_argument(
        "--discount",
        type=discount_arg,
        default=None,
        help="user-supplied discount fraction in [0,1), billed as "
        "(1 - discount) (e.g. 0.10 => x0.90); default "
        "HARNESS_COST_DISCOUNT, else 0 (list price)",
    )
    args = ap.parse_args()

    transcript = None if args.no_transcript else (args.transcript or "auto")
    out = args.out
    if not out:
        base = os.environ.get("HARNESS_SESSIONS_DIR") or os.path.join(os.getcwd(), "sessions")
        out = os.path.join(base, os.path.basename(os.path.abspath(args.capture_dir[0])))

    w = Watcher(list(args.capture_dir), out, transcript=transcript, discount=args.discount)
    if args.once:
        if w.refresh() is None:
            print(
                "[harness-watch] inputs unreadable (mid-write?); nothing written", file=sys.stderr
            )
            sys.exit(1)
        w._report()  # noqa: SLF001 — same-module CLI convenience
        return
    w.run(args.interval)


if __name__ == "__main__":
    main()
