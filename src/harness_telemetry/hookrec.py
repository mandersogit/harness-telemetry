#!/usr/bin/env python3
"""
hookrec.py — the passive hook listener's recorder (the hook channel).

harness-capture injects a settings file whose every hook command is
    "$HARNESS_HOOK_PY" -I "$HARNESS_HOOK_SCRIPT"
— this file, by ABSOLUTE path, under -I (isolated: no PYTHON* env, no PYTHONPATH,
and no script directory on sys.path). `-m harness_telemetry.hookrec` would resolve
the module against the session's cwd, so a harness_telemetry/ tree in the user's own
project would run as the capturing user on every hook event. ($ENV expands inside a
hook command — verified against Claude Code 2.1.198.)

Claude Code delivers the hook payload as JSON on stdin; this records it VERBATIM
as one line of $HARNESS_HOOK_DIR/hooks.jsonl:

    {"received_at": <ISO-Zulu>, "event": <payload.hook_event_name>, "payload": {...}}

Contract: NEVER interfere with the session. Exit 0 unconditionally — a missing
env var, unparseable stdin, or any I/O failure is swallowed (the capture loses
one record; the user's session must not lose anything). Appends hold an
fcntl.flock so concurrent hook firings (parallel tools/sub-agents) never
interleave lines. Unparseable stdin is still kept: {"raw": <bytes as str>}.

Hook payloads are session content (prompts, tool inputs), so hooks.jsonl is 0600 and
its dir 0700. This process is short-lived and spawns nothing, so a process-wide
os.umask(0o077) is safe here — it is NOT in the launcher, which `claude` and every
tool it spawns would inherit.
"""

import fcntl
import json
import os
import sys
from datetime import UTC, datetime


def record(stdin_data: bytes, hook_dir: str) -> str | None:
    """Append one record; returns the event name (None if unparseable).
    Raises only on I/O errors — main() swallows those."""
    payload: dict[str, object]
    event: str | None
    try:
        parsed = json.loads(stdin_data)
        if isinstance(parsed, dict):
            payload = parsed
            ev = parsed.get("hook_event_name")
            event = ev if isinstance(ev, str) else None
        else:
            payload = {"raw": parsed}
            event = None
    except ValueError:
        payload = {"raw": stdin_data.decode("utf-8", "replace")}
        event = None
    line = json.dumps(
        {
            "received_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "event": event,
            "payload": payload,
        },
        separators=(",", ":"),
    )
    os.makedirs(hook_dir, mode=0o700, exist_ok=True)
    path = os.path.join(hook_dir, "hooks.jsonl")
    # 0600 from creation — never a 0644 window (the mode applies on create only).
    # O_NOFOLLOW: refuse a planted symlink at hooks.jsonl, like sink.open_private.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line + "\n")
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
    return event


def main() -> None:
    os.umask(0o077)  # safe here: short-lived, spawns nothing (see the module docstring)
    try:
        hook_dir = os.environ.get("HARNESS_HOOK_DIR")
        if hook_dir:
            record(sys.stdin.buffer.read(), hook_dir)
    except Exception:  # noqa: BLE001 — never break the user's session
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
