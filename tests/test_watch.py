"""watch.py — the live watcher, on synthetic inputs.

The watcher's contract: re-normalize on each SETTLED input change (fingerprint
stable across two consecutive polls), never crash on a body caught mid-write,
and converge to exactly the batch result — incremental equals batch holds by
construction because an update IS a batch run over the inputs present. The
end-to-end version against real captures is part of the maintainer's local
suite and is not distributed; see CONTRIBUTING.md.
"""

import json
import stat
from pathlib import Path
from typing import Any

from tests.conftest import mk_synth_capture, write_synth_call

from harness_telemetry.normalize.claude_code import normalize
from harness_telemetry.normalize.watch import Watcher


def _doc(out: Path) -> dict[str, Any]:
    with open(out / "normalized.json") as f:
        d: dict[str, Any] = json.load(f)
    return d


def _n_inf(doc: dict[str, Any]) -> int:
    return sum(1 for e in doc["events"] if e["type"] == "inference")


def _mk_live_capture(tmp_path: Path) -> Path:
    return mk_synth_capture(
        tmp_path,
        "live",
        "sid-live",
        req_name="aaaa0001",
        resp_rid="req_L1",
        msg_id="msg_L1",
        messages=[{"role": "user", "content": [{"type": "text", "text": "one"}]}],
        prev_msg_id=None,
        t0=1_751_500_000,
        answer="first",
    )


def test_watch_settle_update_unchanged_cycle(tmp_path: Path) -> None:
    cap = _mk_live_capture(tmp_path)
    out = tmp_path / "out"
    w = Watcher([str(cap)], str(out), transcript=None)
    assert w.tick() == "settling"  # first sight of the inputs
    assert w.tick() == "updated"  # stable -> normalized + projected
    assert w.tick() == "unchanged"  # nothing new
    doc = _doc(out)
    assert _n_inf(doc) == 1
    assert (out / "sessfs" / "current.json").exists()
    assert (out / "sessfs" / "log").is_dir()
    # the live output is session content: private from creation, both the doc
    # the watcher swaps in and the tree it renders
    assert stat.S_IMODE((out / "normalized.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((out / "sessfs").stat().st_mode) == 0o700
    assert stat.S_IMODE((out / "sessfs" / "current.json").stat().st_mode) == 0o600


def test_watch_incremental_equals_batch(tmp_path: Path) -> None:
    # Feed calls one at a time through ticks; the final doc is byte-identical
    # to a one-shot batch run over the same final dir, and growth is monotone.
    cap = _mk_live_capture(tmp_path)
    out = tmp_path / "out"
    w = Watcher([str(cap)], str(out), transcript=None)
    w.tick(), w.tick()
    assert _n_inf(_doc(out)) == 1

    # turn 2 arrives (chains to turn 1's response)
    write_synth_call(
        cap / "bodies",
        sid="sid-live",
        req_name="aaaa0002",
        resp_rid="req_L2",
        msg_id="msg_L2",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "one"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
            {"role": "user", "content": [{"type": "text", "text": "two"}]},
        ],
        prev_msg_id="msg_L1",
        t0=1_751_500_060,
        answer="second",
    )
    assert w.tick() == "settling"
    assert w.tick() == "updated"
    doc = _doc(out)
    assert _n_inf(doc) == 2
    infs = [e for e in doc["events"] if e["type"] == "inference"]
    assert infs[1]["parent_index"] == 0  # the live chain binds turn 2 to turn 1

    batch = normalize(str(cap), transcript=None)
    on_disk = (out / "normalized.json").read_text()
    assert on_disk == json.dumps(batch, indent=2) + "\n"  # incremental == batch, BYTES


def test_watch_skips_partial_body_then_recovers(tmp_path: Path) -> None:
    # A body caught mid-write must not crash the watcher or clobber the last
    # good output; once the file completes, the next settled tick picks it up.
    cap = _mk_live_capture(tmp_path)
    out = tmp_path / "out"
    w = Watcher([str(cap)], str(out), transcript=None)
    w.tick(), w.tick()
    good = _doc(out)

    partial = cap / "bodies" / "bbbb0002.request.json"
    partial.write_text('{"model": "claude-test-1", "messages": [{"role"')  # truncated
    assert w.tick() == "settling"
    assert w.tick() == "skipped-partial"
    assert _doc(out) == good  # previous output intact

    write_synth_call(
        cap / "bodies",
        sid="sid-live",
        req_name="bbbb0002",
        resp_rid="req_L2",
        msg_id="msg_L2",
        messages=[{"role": "user", "content": [{"type": "text", "text": "two"}]}],
        prev_msg_id="msg_L1",
        t0=1_751_500_120,
        answer="second",
    )
    assert w.tick() == "settling"
    assert w.tick() == "updated"
    assert _n_inf(_doc(out)) == 2


def test_watch_once_refreshes_immediately(tmp_path: Path) -> None:
    cap = _mk_live_capture(tmp_path)
    out = tmp_path / "out"
    w = Watcher([str(cap)], str(out), transcript=None)
    doc = w.refresh()
    assert doc is not None and _n_inf(doc) == 1
    assert _doc(out) == doc
