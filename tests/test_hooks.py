"""Hook events: recorder, --settings injection policy, ingestion.

Ground truth this builds on (behaviour of --settings hooks verified against Claude Code 2.1.198):
hooks carried by a user-supplied --settings file execute as documented in headless AND
interactive mode, and $ENV vars expand in hook commands.

The recorder runs on EVERY hook event of a real session, so its two hard properties get
the most attention here: it exits 0 whatever arrives on stdin, and it never widens who
can read the session (hooks.jsonl is 0600 inside a 0700 dir).
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.conftest import mk_synth_resume_pair

from harness_telemetry import hookrec
from harness_telemetry.capture import (
    HOOK_EVENTS,
    build_env,
    hook_settings,
    hookrec_script,
    wants_settings,
)
from harness_telemetry.normalize import validate
from harness_telemetry.normalize.claude_code import check_invariants, normalize


def _hookline(f: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]


# --- recorder ------------------------------------------------------------------


def test_record_appends_verbatim_payload(tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
    }
    ev = hookrec.record(json.dumps(payload).encode(), str(tmp_path))
    assert ev == "PreToolUse"
    recs = _hookline(tmp_path / "hooks.jsonl")
    assert len(recs) == 1
    assert recs[0]["event"] == "PreToolUse"
    assert recs[0]["payload"] == payload  # verbatim
    assert recs[0]["received_at"].endswith("Z")


def test_record_keeps_unparseable_stdin_as_raw(tmp_path: Path) -> None:
    ev = hookrec.record(b"not json {", str(tmp_path))
    assert ev is None
    recs = _hookline(tmp_path / "hooks.jsonl")
    assert recs[0]["event"] is None
    assert recs[0]["payload"] == {"raw": "not json {"}


def test_record_appends_do_not_clobber(tmp_path: Path) -> None:
    hookrec.record(json.dumps({"hook_event_name": "A"}).encode(), str(tmp_path))
    hookrec.record(json.dumps({"hook_event_name": "B"}).encode(), str(tmp_path))
    assert [r["event"] for r in _hookline(tmp_path / "hooks.jsonl")] == ["A", "B"]


def _run_recorder(stdin: bytes, hook_dir: Path | None) -> subprocess.CompletedProcess[bytes]:
    """Invoke the recorder exactly as the injected hook command does."""
    script = hookrec_script()
    assert script is not None
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_HOOK_DIR"}
    if hook_dir is not None:
        env["HARNESS_HOOK_DIR"] = str(hook_dir)
    return subprocess.run([sys.executable, "-I", script], input=stdin, env=env, capture_output=True)


def test_recorder_cli_never_fails(tmp_path: Path) -> None:
    # The contract: exit 0 no matter what — missing env, and any stdin at all.
    assert _run_recorder(b"{}", None).returncode == 0
    hooks = tmp_path / "hooks"
    p = _run_recorder(json.dumps({"hook_event_name": "Stop"}).encode(), hooks)
    assert p.returncode == 0
    assert _hookline(hooks / "hooks.jsonl")[0]["event"] == "Stop"


def test_recorder_cli_survives_actual_garbage(tmp_path: Path) -> None:
    # Not "{}" — bytes that are not JSON at all, a truncated object, and
    # undecodable bytes. Each must exit 0 and append exactly one PARSEABLE line.
    hooks = tmp_path / "hooks"
    for blob in (b"\x00\xff not json at all", b'{"hook_event_name": "PreToolUse", "tool_inp', b""):
        assert _run_recorder(blob, hooks).returncode == 0
    recs = _hookline(hooks / "hooks.jsonl")  # every line still parses
    assert len(recs) == 3
    assert [r["event"] for r in recs] == [None, None, None]
    assert all("raw" in r["payload"] for r in recs)


def test_recorder_cli_keeps_the_session_private(tmp_path: Path) -> None:
    # Hook payloads are prompts and tool inputs: no other local uid reads them.
    hooks = tmp_path / "hooks"
    _run_recorder(json.dumps({"hook_event_name": "Stop"}).encode(), hooks)
    assert stat.S_IMODE(hooks.stat().st_mode) == 0o700
    assert stat.S_IMODE((hooks / "hooks.jsonl").stat().st_mode) == 0o600


# --- the --settings policy ------------------------------------------------------


def test_wants_settings_detects_both_spellings() -> None:
    assert wants_settings(["--settings", "mine.json"])
    assert wants_settings(["-p", "--settings=mine.json"])
    assert not wants_settings(["-p", "hello"])
    assert not wants_settings([])


def test_hook_settings_registers_every_event_passively() -> None:
    s = hook_settings()
    assert set(s["hooks"].keys()) == set(HOOK_EVENTS)
    # 31: the 30 observed on Claude Code 2.1.195 plus DirectoryAdded, which the
    # official hooks documentation listed as of 2026-08-21.
    assert len(HOOK_EVENTS) == 31
    assert "DirectoryAdded" in HOOK_EVENTS
    assert len(set(HOOK_EVENTS)) == len(HOOK_EVENTS)  # no accidental duplicate
    for entries in s["hooks"].values():
        assert entries[0]["matcher"] == ""  # passive: match everything
        cmd = entries[0]["hooks"][0]["command"]
        assert "$HARNESS_HOOK_PY" in cmd  # per-run values via env vars
        assert cmd == '"$HARNESS_HOOK_PY" -I "$HARNESS_HOOK_SCRIPT"'


def test_hook_command_runs_the_installed_script_not_a_module() -> None:
    # `-m harness_telemetry.hookrec` resolves the module against the SESSION's
    # cwd, so a harness_telemetry/ tree in the user's own project would execute
    # as the capturing user on every hook event. Absolute path, isolated mode.
    cmd = hook_settings()["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "-m " not in cmd and "harness_telemetry.hookrec" not in cmd
    assert " -I " in cmd
    script = build_env("/cap/bodies", 4318, {}, hooks_dir="/cap/hooks")["HARNESS_HOOK_SCRIPT"]
    assert os.path.isabs(script) and script.endswith("hookrec.py")
    assert os.path.exists(script)


# --- ingestion (the H channel in the normalized doc) ----------------------------


def _write_hooks(cap: Path, records: list[dict[str, Any]]) -> None:
    hooks = cap / "hooks"
    hooks.mkdir()
    with open(hooks / "hooks.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def test_normalize_ingests_hook_channel(tmp_path: Path) -> None:
    a, _ = mk_synth_resume_pair(tmp_path)
    _write_hooks(
        a,
        [
            {
                "received_at": "2026-07-02T23:00:00.000Z",
                "event": "SessionStart",
                "payload": {"hook_event_name": "SessionStart", "session_id": "sid-merge"},
            },
            {
                "received_at": "2026-07-02T23:00:05.000Z",
                "event": "PreToolUse",
                "payload": {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
            },
        ],
    )
    doc = normalize(str(a), transcript=None)
    hooks = [e for e in doc["events"] if e["type"] == "hook"]
    assert [h["subtype"] for h in hooks] == ["SessionStart", "PreToolUse"]
    assert hooks[0]["source"] == "hook"
    assert hooks[0]["payload"]["session_id"] == "sid-merge"
    assert doc["session"]["captured_with"]["hooks"] is True
    stats, problems = check_invariants(doc, str(a))
    assert problems == []
    assert stats["conservation"]["hook_records"] == 2
    # schema v1.3: the doc validates with the hook branch
    p = tmp_path / "doc.json"
    p.write_text(json.dumps(doc))
    ok, msg = validate.validate(str(p))
    assert ok, msg


def test_hook_conservation_flags_dropped_records(tmp_path: Path) -> None:
    # A torn/unparseable line is skipped by ingestion — conservation must SAY so.
    a, _ = mk_synth_resume_pair(tmp_path)
    hooks = a / "hooks"
    hooks.mkdir()
    (hooks / "hooks.jsonl").write_text(
        '{"received_at":"2026-07-02T23:00:00.000Z","event":"Stop","payload":{}}\n'
        '{"received_at":"2026-07-02T23:00:01.0'  # torn mid-write
    )
    doc = normalize(str(a), transcript=None)
    _, problems = check_invariants(doc, str(a))
    assert any("hook records dropped" in p for p in problems)


def test_hooks_merge_across_captures(tmp_path: Path) -> None:
    # Multi-capture merge: a resumed session's hook records stitch in ts order.
    a, b = mk_synth_resume_pair(tmp_path)
    _write_hooks(
        a, [{"received_at": "2026-07-02T23:00:00.000Z", "event": "SessionStart", "payload": {}}]
    )
    _write_hooks(
        b,
        [
            {
                "received_at": "2026-07-02T23:10:00.000Z",
                "event": "SessionStart",
                "payload": {"resumed": True},
            }
        ],
    )
    doc = normalize([str(a), str(b)], transcript=None)
    hooks = [e for e in doc["events"] if e["type"] == "hook"]
    assert len(hooks) == 2
    assert hooks[0]["ts"] < hooks[1]["ts"]
    assert normalize([str(b), str(a)], transcript=None) == doc  # order-invariant
