#!/usr/bin/env python3
"""
capture.py — run a real Claude Code session with full capture; finalize an enriched
transcript on exit. Includes the hook channel: a passive listener on every
documented hook event (31 as of the 2026-08-21 docs), injected per the --settings
policy described below.

Zero own CLI flags, deliberately: sys.argv[1:] passes to `claude` VERBATIM (even
--help), so nothing we claim can shadow a current-or-future claude flag; the
--settings policy scans argv read-only (inject our generated settings file iff the
user passed none — theirs always wins). Configuration is env-only, matching
bin/claude-capture:

    CLAUDE_CAPTURE_DIR      capture base (default ./captures)
    HARNESS_SESSIONS_DIR    enriched-transcript base (default ./sessions)
    HARNESS_NO_HOOKS=1      skip hook injection entirely
    HARNESS_ALLOW_MODEL_FALLBACK=1
                            re-enable Claude Code's automatic model switch on a
                            refusal safeguard (disabled by default — see build_env)
    HARNESS_ALLOW_ENV_SECRETS=1
                            capture-env.txt and manifest.json record every
                            CLAUDE_CODE_*/OTEL_* VALUE and the full argv verbatim.
                            By default only an allow-list of harmless provenance
                            values is written (exporter names, protocols, endpoints,
                            intervals, content flags); every other value is
                            <redacted> and argv is reduced to flag NAMES + a count.
                            The sink bearer token is never written in either mode.
    HARNESS_FORWARD_OTEL=1  tee every OTEL export onward to a pre-set generic
                            OTEL_EXPORTER_OTLP_ENDPOINT. OFF by default: the capture
                            turns OTEL_LOG_USER_PROMPTS and OTEL_LOG_TOOL_DETAILS on,
                            so those exports carry prompt text and tool parameters —
                            inheriting an endpoint from the shell must not be enough
                            to send them off this process. Without the knob the
                            inherited endpoint is named on stderr and ignored.
    OTEL_EXPORTER_OTLP_ENDPOINT
                            if pre-set, treated as YOUR collector — but teed to ONLY
                            under HARNESS_FORWARD_OTEL=1 (above). The in-process sink
                            takes the exporter either way; the tee itself is
                            best-effort OTLP/HTTP JSON with your
                            OTEL_EXPORTER_OTLP_HEADERS. Those headers are held in
                            memory for the tee ONLY: they are stripped from the child
                            env and never written to disk.
    OTEL_EXPORTER_OTLP_{LOGS,METRICS,TRACES}_ENDPOINT / _PROTOCOL / _HEADERS
                            per-signal endpoints OVERRIDE the generic one and
                            per-signal headers MERGE with the generic ones
                            (official docs; confirmed in the OTel SDK bundled in
                            Claude Code 2.1.239). The capture therefore OWNS all
                            four pairs: every signal is pointed at the sink and
                            every *_HEADERS variable is deleted from the child env.
                            A pre-set per-signal endpoint is rewritten with a loud
                            warning — that destination receives nothing this session.
    OTEL_LOG_ASSISTANT_RESPONSES
                            set explicitly (default 1): the capture wants every
                            channel at full fidelity. Unset, Claude Code falls back
                            to OTEL_LOG_USER_PROMPTS — pinned so the OTEL channel's
                            content is stated, not inherited from a fallback.
    other OTEL_*            respected if pre-set

Flow: bind the OTLP sink IN-PROCESS on a daemon thread (no subprocess, no readiness
race, dies atomically with us) writing straight into captures/<ts>/otel/ — the old
move-the-sink-output-by-hand step is gone. The sink authenticates every export with
a per-capture bearer token (secrets.token_urlsafe) that lives only in this process
and the child's env. Set the claude-capture env, run `claude` with inherited stdio
(the TUI owns the terminal; we ignore SIGINT so Ctrl-C reaches the child via the
foreground process group), and on exit: flush + stop the sink, write manifest.json,
normalize + project into sessions/<ts>/, print the verdict.

Everything the launcher writes is private by construction: captures/<ts> and
sessions/<ts> are created 0700 and every file the launcher and the sink open is 0600.
No process-wide umask is set — `claude` and every tool it spawns would inherit it.

Degradation: if this process is killed outright, the sink dies with it, bodies are
already on disk, and `harness-normalize captures/<ts>` runs the finalize manually.
"""

import contextlib
import errno
import importlib.util
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from .collector import sink
from .normalize import project
from .normalize.claude_code import check_invariants, normalize, parse_user_id

# The hook channel: every documented Claude Code hook event, registered as a
# PASSIVE listener (matcher "", exit 0, records the stdin payload verbatim). 30
# names were observed on Claude Code 2.1.195; DirectoryAdded is the 31st, added
# from the official hooks documentation as read on 2026-08-21. Hooks from a
# --settings file execute as documented (the flag is user-supplied, so no trust
# gate applies) and $ENV expands inside a hook command — both verified against
# 2.1.198.
HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "Setup",
    "SessionEnd",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "Stop",
    "StopFailure",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "PermissionRequest",
    "PermissionDenied",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "TeammateIdle",
    "PreCompact",
    "PostCompact",
    "InstructionsLoaded",
    "ConfigChange",
    "CwdChanged",
    "DirectoryAdded",
    "FileChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "Elicitation",
    "ElicitationResult",
    "Notification",
    "MessageDisplay",
)

# The hook command runs the INSTALLED hookrec.py by absolute path under -I
# (isolated: no PYTHONPATH, no PYTHON* env, and — since 3.11's -P — no script
# directory prepended to sys.path). `python -m harness_telemetry.hookrec` would
# resolve the module against the session's cwd, so a harness_telemetry/ tree in
# the user's own project would execute on every hook event. Both values come
# from the env ($HARNESS_HOOK_PY / $HARNESS_HOOK_SCRIPT: per-run, and $ENV
# expands inside a hook command — verified against 2.1.198); hookrec.py swallows
# every failure (exit 0 always).
_HOOK_CMD = '"$HARNESS_HOOK_PY" -I "$HARNESS_HOOK_SCRIPT"'

# The OTLP signals Claude Code can export. Per-signal endpoints override the
# generic one and are used VERBATIM (no /v1/<signal> appended) — so ours carry
# the full signal path.
_SIGNALS: tuple[str, ...] = ("LOGS", "METRICS", "TRACES")
_HEADER_VARS: tuple[str, ...] = (
    "OTEL_EXPORTER_OTLP_HEADERS",
    *(f"OTEL_EXPORTER_OTLP_{s}_HEADERS" for s in _SIGNALS),
)


def hookrec_script() -> str | None:
    """Absolute path of the installed hookrec.py, or None if it cannot be
    located (then the hook channel is skipped rather than guessed at)."""
    try:
        spec = importlib.util.find_spec("harness_telemetry.hookrec")
    except (ImportError, ValueError):  # pragma: no cover — defensive
        return None
    origin = spec.origin if spec is not None else None
    return os.path.abspath(origin) if origin else None


def hook_settings() -> dict[str, Any]:
    """The injected settings doc: a passive recorder on every documented hook event."""
    return {
        "hooks": {
            ev: [{"matcher": "", "hooks": [{"type": "command", "command": _HOOK_CMD}]}]
            for ev in HOOK_EVENTS
        }
    }


def wants_settings(argv: list[str]) -> bool:
    """Did the user pass their own --settings? Then we do NOT inject (two
    --settings flags are last-wins — clobbering the user's file is worse than
    losing the hook channel; OTEL still covers most of it)."""
    return any(a == "--settings" or a.startswith("--settings=") for a in argv)


def preset_signal_endpoints(base: Mapping[str, str]) -> dict[str, str]:
    """Per-signal OTLP endpoints the user had already set. Each one OVERRIDES
    the generic endpoint for its signal, so leaving any of them in place would
    ship prompts and tool details around the sink to that collector."""
    return {
        sig: base[f"OTEL_EXPORTER_OTLP_{sig}_ENDPOINT"]
        for sig in _SIGNALS
        if base.get(f"OTEL_EXPORTER_OTLP_{sig}_ENDPOINT")
    }


def build_env(
    bodies_dir: str,
    port: int,
    base: dict[str, str],
    hooks_dir: str | None = None,
    sink_token: str | None = None,
) -> dict[str, str]:
    """The claude-capture env.

    Every OTLP destination is OWNED by the capture — the generic endpoint AND
    all three per-signal endpoints, with matching protocols — because a pre-set
    per-signal endpoint silently overrides the generic one and would carry the
    content flags we turn on to somebody else's collector. A pre-set generic
    endpoint is the user's collector, which the sink TEES to instead.

    Every OTEL_EXPORTER_OTLP_*HEADERS variable is DELETED from the child env
    (per-signal headers merge with the generic ones, so all four must go) and
    the generic one is then set to the sink's own bearer token. The user's
    headers survive only in the parent's memory, for the tee.

    Every other OTEL_* default respects a pre-set override."""
    env = dict(base)
    defaults = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_TOOL_DETAILS": "1",
        # Unset, Claude Code falls back to OTEL_LOG_USER_PROMPTS (official docs,
        # ≥ 2.1.193). Pinned on — this is a fidelity study, so every channel is
        # captured at full content (maintainer decision, 2026-08-23) — and pinned
        # explicitly so the OTEL scope never depends on the fallback.
        "OTEL_LOG_ASSISTANT_RESPONSES": "1",
        "OTEL_LOGS_EXPORT_INTERVAL": "2000",  # flush fast for short runs
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    }
    for k, v in defaults.items():
        env.setdefault(k, v)

    sink_url = f"http://127.0.0.1:{port}"  # literal: the sink binds IPv4 loopback; "localhost" may resolve to ::1 first
    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/json"
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = sink_url
    for sig in _SIGNALS:
        env[f"OTEL_EXPORTER_OTLP_{sig}_PROTOCOL"] = "http/json"
        # Per-signal endpoints are used as-is; only the generic one gets
        # /v1/<signal> appended (verified in the OTel SDK bundled in CC 2.1.239).
        env[f"OTEL_EXPORTER_OTLP_{sig}_ENDPOINT"] = f"{sink_url}/v1/{sig.lower()}"
    for var in _HEADER_VARS:
        env.pop(var, None)
    if sink_token:
        env["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {sink_token}"

    env["OTEL_LOG_RAW_API_BODIES"] = f"file:{bodies_dir}"
    # Provenance integrity: a captured session must not silently switch models
    # when a safeguard flags a message. This is a DELIBERATE change to the
    # session's behaviour, and a documented-equivalent one: the env var
    # CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK and the user-facing
    # /config → MODEL & OUTPUT toggle (switchModelsOnFlag) gate the same
    # automatic switch (verified against 2.1.198); either off-switch suffices.
    # The safeguard itself still fires — the flagged turn pauses instead of
    # continuing on a different model. A hybrid session you believed was
    # single-model defeats the point of capture.
    # Opt back in with HARNESS_ALLOW_MODEL_FALLBACK=1.
    if os.environ.get("HARNESS_ALLOW_MODEL_FALLBACK", "") not in ("1", "true", "yes"):
        env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] = "1"
    if hooks_dir is not None:
        env["HARNESS_HOOK_DIR"] = hooks_dir
        env["HARNESS_HOOK_PY"] = sys.executable
        script = hookrec_script()
        if script:
            env["HARNESS_HOOK_SCRIPT"] = script
    return env


# capture-env.txt records the env the child actually saw. A capture dir is
# sensitive enough without credentials in it, and a name-based blocklist can
# only redact what it was told to look for — so the model is INVERTED: the NAME
# of every CLAUDE_CODE_*/OTEL_* variable is always kept (knowing THAT a header
# or a cert was set is the provenance that matters), and the VALUE is written
# only for this allow-list of harmless provenance keys. Everything else —
# headers, tokens, keys, certs, cookies, DSNs, OTEL_RESOURCE_ATTRIBUTES, and
# anything unforeseen — is <redacted>. Opt out with HARNESS_ALLOW_ENV_SECRETS=1.
_ENV_VALUE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",  # provenance, also present in bodies/OTEL
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
        "OTEL_LOGS_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_TRACES_EXPORTER",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        *(f"OTEL_EXPORTER_OTLP_{s}_PROTOCOL" for s in _SIGNALS),
        # After build_env these are the sink URL; userinfo is stripped anyway.
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        *(f"OTEL_EXPORTER_OTLP_{s}_ENDPOINT" for s in _SIGNALS),
        "OTEL_LOG_RAW_API_BODIES",
        "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_TOOL_DETAILS",
        "OTEL_LOG_TOOL_CONTENT",
        "OTEL_LOG_ASSISTANT_RESPONSES",
        "OTEL_LOGS_EXPORT_INTERVAL",
        "OTEL_METRIC_EXPORT_INTERVAL",
    }
)


def allow_env_secrets() -> bool:
    """HARNESS_ALLOW_ENV_SECRETS, parsed like every other knob here."""
    return os.environ.get("HARNESS_ALLOW_ENV_SECRETS", "") in ("1", "true", "yes")


def forward_otel() -> bool:
    """HARNESS_FORWARD_OTEL — the tee is OPT-IN, parsed like every other knob.

    Inheriting OTEL_EXPORTER_OTLP_ENDPOINT from a shell profile is not consent:
    the capture turns the content flags on, so a teed export carries prompt text
    and tool parameters that a default session would never have sent there.
    """
    return os.environ.get("HARNESS_FORWARD_OTEL", "") in ("1", "true", "yes")


def _strip_userinfo(url: str) -> str:
    """Drop any user:pass@ from a URL — a collector endpoint can carry a token
    in its userinfo, and the endpoint value itself is allow-listed."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return f"{scheme}://{rest}"


def _hide_capture_dir(value: str, capture_dir: str | None) -> str:
    """Replace the capture directory's own path with the literal `<capture-dir>`.

    OTEL_LOG_RAW_API_BODIES is allow-listed and reads
    `file:/home/<you>/<project>/captures/<ts>/bodies` — an absolute path that
    names your home directory, your username and your project. None of that is
    provenance: the capture IS that directory, so the placeholder says
    everything the reader needs (`file:<capture-dir>/bodies`). Any other
    allow-listed value carrying the same prefix (a hook dir, say) is covered too.
    """
    if not capture_dir:
        return value
    return value.replace(capture_dir, "<capture-dir>")


def redact_env_line(
    name: str,
    value: str,
    allow_secrets: bool = False,
    sink_token: str | None = None,
    capture_dir: str | None = None,
) -> str:
    """One `NAME=VALUE` line for capture-env.txt. The name is always kept; the
    value only for allow-listed provenance keys (or all of them under
    allow_secrets). The sink's own bearer is never written, in either mode.

    allow_secrets means "write it verbatim", so the capture-dir substitution
    below applies to the default (redacted) mode only.
    """
    if sink_token and sink_token in value:
        return f"{name}=<redacted>"
    if not allow_secrets and name not in _ENV_VALUE_ALLOWLIST:
        return f"{name}=<redacted>"
    if not allow_secrets:
        if "ENDPOINT" in name:
            value = _strip_userinfo(value)
        value = _hide_capture_dir(value, capture_dir)
    return f"{name}={value}"


def argv_flags(argv: list[str]) -> list[str]:
    """Flag NAMES only, values dropped: a prompt, a file path or a model name is
    session content, not provenance. `--settings=x` -> `--settings`."""
    return [a.split("=", 1)[0] for a in argv if a.startswith("-") and a != "-"]


def argv_record(argv: list[str], allow_secrets: bool = False) -> dict[str, Any]:
    """The argv provenance for manifest.json / capture-env.txt."""
    if allow_secrets:
        return {"argv": list(argv)}
    return {"argv_flags": argv_flags(argv), "argv_n": len(argv)}


def capture_env_text(
    ts: str,
    argv: list[str],
    env: dict[str, str],
    sink_token: str | None = None,
    capture_dir: str | None = None,
) -> str:
    """The capture-env.txt body: run provenance plus every CLAUDE_CODE_*/OTEL_*
    var the child saw. The redaction mode is recorded in the file itself, so a
    reader of an old capture knows which one produced it."""
    allow = allow_env_secrets()
    rec = argv_record(argv, allow)
    lines = [f"ts={ts}"]
    if "argv" in rec:
        lines.append("argv=" + " ".join(rec["argv"]))
    else:
        lines += [f"argv_flags={' '.join(rec['argv_flags'])}", f"argv_n={rec['argv_n']}"]
    lines.append(f"env_secrets={'verbatim' if allow else 'redacted'}")
    lines += [
        redact_env_line(k, env[k], allow, sink_token, capture_dir)
        for k in sorted(k for k in env if k.startswith(("CLAUDE_CODE_", "OTEL_")))
    ]
    return "\n".join(lines) + "\n"


def _chmod_files_private(directory: str) -> None:
    """0600 every regular file directly under `directory` (best effort)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)


def _makedirs_private(path: str) -> None:
    """0700 from creation. makedirs honours the umask (and applies `mode` only
    to the LAST component), so chmod explicitly afterwards."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _bind_sink(otel_dir: str, token: str | None = None) -> Any:
    try:
        return sink.serve(otel_dir, 4318, token=token)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        return sink.serve(otel_dir, 0, token=token)  # ephemeral


def _netloc(url: str) -> str:
    """scheme://host:port for a stderr line — never userinfo, never path or
    query (either can carry a credential)."""
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return "<unparseable endpoint>"
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    tail = f"{host}{f':{port}' if port else ''}"
    return f"{u.scheme}://{tail}" if u.scheme else tail


def endpoint_is_usable(url: str) -> bool:
    """Can we POST to this at all? A malformed port makes urlparse raise, and a
    non-http scheme or a missing host is not a tee target."""
    try:
        u = urlparse(url)
        u.port  # noqa: B018 — property access raises ValueError on a bad port
        host = u.hostname
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(host)


# The self-loop guard lives next to the sink (it is the SINK's bind it is about)
# and is shared with the standalone `harness-otlp-sink --forward`. Kept under the
# local name the launcher has always used.
_reaches_our_bind = sink.reaches_bind
_points_at_us = sink.points_at_bind


def _session_id_from_bodies(bodies_dir: str) -> tuple[str | None, list[str]]:
    """(session id from the earliest readable request body, names of unreadable ones).

    A child killed mid-write leaves a truncated *.request.json. Letting its
    JSONDecodeError escape cost the whole manifest AND `claude`'s own exit status,
    so a bad body is skipped and NAMED instead — every request file is inspected
    so the list is complete, not just the ones seen before the id turned up.
    """
    malformed: list[str] = []
    try:
        names = sorted(f for f in os.listdir(bodies_dir) if f.endswith(".request.json"))
    except OSError:
        return None, malformed
    stamped: list[tuple[float, str]] = []
    for f in names:
        try:
            stamped.append((os.path.getmtime(os.path.join(bodies_dir, f)), f))
        except OSError:
            malformed.append(f)
    sid: str | None = None
    for _, f in sorted(stamped):
        try:
            with open(os.path.join(bodies_dir, f)) as fh:
                ids = parse_user_id(json.load(fh))
        except (OSError, ValueError):  # ValueError covers JSONDecodeError
            malformed.append(f)
            continue
        if sid is None and ids.get("session_id"):
            sid = ids["session_id"]
    return sid, sorted(malformed)


def _count_lines(path: str) -> int:
    """Non-blank lines in a file; 0 if it is missing or unreadable."""
    try:
        with open(path) as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def _names_in(directory: str) -> list[str]:
    """os.listdir, but an unreadable or missing directory is an empty one."""
    try:
        return os.listdir(directory)
    except OSError:
        return []


def main() -> None:
    argv = sys.argv[1:]
    # Preflight before anything is created: a missing `claude` used to surface as
    # a raw FileNotFoundError and leave a stray captures/<ts>/ behind.
    if shutil.which("claude") is None:
        print(
            "harness-capture: 'claude' not found on PATH — install Claude Code first",
            file=sys.stderr,
        )
        sys.exit(127)

    base_dir = os.environ.get("CLAUDE_CAPTURE_DIR") or os.path.join(os.getcwd(), "captures")
    # Local time, like manifest["started"]/["ended"]: capture ids are read by the
    # operator, and captures never leave the machine (maintainer decision,
    # 2026-08-23 — wall-clock names over the UTC privacy argument).
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Two captures starting in the same second must NOT share a dir (concurrent
    # sessions would cross-contaminate bodies). Claim the ts atomically; on
    # collision, suffix -2, -3, ... The winning dir is created exclusively, 0700.
    capture_dir = os.path.join(base_dir, ts)
    n = 1
    while True:
        try:
            os.makedirs(capture_dir, mode=0o700)
            break
        except FileExistsError:
            n += 1
            capture_dir = os.path.join(base_dir, f"{ts}-{n}")
    os.chmod(capture_dir, 0o700)  # makedirs honours the umask; this does not
    ts = os.path.basename(capture_dir)
    bodies_dir = os.path.join(capture_dir, "bodies")
    otel_dir = os.path.join(capture_dir, "otel")
    hooks_dir = os.path.join(capture_dir, "hooks")
    _makedirs_private(bodies_dir)

    # Hook injection, per the --settings policy: inject our passive listener iff
    # the user passed no --settings of their own (two --settings flags are
    # last-wins, and clobbering the user's file is worse than losing the hook
    # channel). The settings file is GENERATED into the capture dir (not a static
    # repo file) so the capture records exactly what was injected. Opt out:
    # HARNESS_NO_HOOKS=1.
    inject_hooks = os.environ.get("HARNESS_NO_HOOKS", "") not in (
        "1",
        "true",
        "yes",
    ) and not wants_settings(argv)
    if inject_hooks and hookrec_script() is None:  # pragma: no cover — defensive
        print("[harness-capture] hookrec.py not locatable; hook channel disabled", file=sys.stderr)
        inject_hooks = False
    claude_argv = list(argv)
    if inject_hooks:
        settings_path = os.path.join(capture_dir, "hook-settings.json")
        with sink.open_private(settings_path) as f:
            json.dump(hook_settings(), f, indent=2)
            f.write("\n")
        claude_argv += ["--settings", settings_path]

    # A per-capture bearer: the sink is a local HTTP server, so without it any
    # local uid could poison the capture or (with the tee on) make us replay a
    # body to the user's collector under the user's credential. It exists only in
    # this process and the child's env — never on disk, never on stderr.
    sink_token = secrets.token_urlsafe(32)
    server = _bind_sink(otel_dir, token=sink_token)
    port = server.server_address[1]

    # Telemetry additivity, OPT-IN: a pre-set GENERIC endpoint is the USER's
    # collector, but forwarding to it means content-enriched exports (prompt text,
    # tool parameters — this launcher turns those flags on) leave the process.
    # Inheriting a variable from a shell profile is not consent, so the tee needs
    # HARNESS_FORWARD_OTEL=1. Their headers are read here, kept in memory only.
    inherited: str | None = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None
    tee: str | None = inherited if forward_otel() else None
    if inherited and tee is None:
        print(
            f"[harness-capture] inherited OTEL_EXPORTER_OTLP_ENDPOINT={_netloc(inherited)} "
            "NOT used — set HARNESS_FORWARD_OTEL=1 to tee content-enriched telemetry "
            "(prompts, tool details) to it",
            file=sys.stderr,
        )
    if tee and not endpoint_is_usable(tee):
        print(
            "[harness-capture] WARNING: OTEL_EXPORTER_OTLP_ENDPOINT is not a usable "
            "http(s) URL — the telemetry tee is DISABLED for this session.",
            file=sys.stderr,
        )
        tee = None
    elif tee and _points_at_us(tee, port):
        print(
            "[harness-capture] WARNING: OTEL_EXPORTER_OTLP_ENDPOINT resolves to this "
            "capture's own sink — the telemetry tee is DISABLED (self-loop guard).",
            file=sys.stderr,
        )
        tee = None
    elif tee:
        # Before `claude` starts, so there is still time to Ctrl-C.
        print(
            f"[harness-capture] WARNING: HARNESS_FORWARD_OTEL=1 — every OTEL export is\n"
            f"[harness-capture] WARNING: TEED to {_netloc(tee)}. USER PROMPTS and TOOL\n"
            f"[harness-capture] WARNING: DETAILS from this session will be sent there —\n"
            f"[harness-capture] WARNING: off this machine if that is not a local\n"
            f"[harness-capture] WARNING: collector. Ctrl-C now to stop.",
            file=sys.stderr,
        )
    sink.set_forward(
        tee, sink.parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")) if tee else None
    )

    for sig, url in preset_signal_endpoints(os.environ).items():
        print(
            f"[harness-capture] WARNING: OTEL_EXPORTER_OTLP_{sig}_ENDPOINT was pre-set "
            f"to {_netloc(url)} — a per-signal endpoint overrides the generic one, so "
            f"the capture rewrites it to its own sink: {sig.lower()} telemetry will "
            f"NOT reach that collector this session.",
            file=sys.stderr,
        )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = build_env(
        bodies_dir,
        port,
        dict(os.environ),
        hooks_dir=hooks_dir if inject_hooks else None,
        sink_token=sink_token,
    )
    with sink.open_private(os.path.join(capture_dir, "capture-env.txt")) as f:
        f.write(capture_env_text(ts, argv, env, sink_token, capture_dir))

    print(
        f"[harness-capture] telemetry -> {capture_dir}  (OTLP sink in-process :{port})",
        file=sys.stderr,
    )
    started = datetime.now().astimezone().isoformat()
    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C goes to the child
    try:
        proc = subprocess.run(["claude", *claude_argv], env=env)
        claude_exit = proc.returncode
    finally:
        signal.signal(signal.SIGINT, prev)
    ended = datetime.now().astimezone().isoformat()

    # Everything from here on is post-mortem: it runs on whatever the child left
    # behind, including a half-written body from a crash. No stage of it may cost
    # the user `claude`'s own exit status, and the manifest — which says where the
    # bodies are and how the session ended — must be written even when the rest
    # fails, so each stage is guarded separately and the exit code is the child's.
    with contextlib.suppress(Exception):  # pragma: no branch
        time.sleep(1.0)  # grace for the exporter's final flush to land in the sink
        server.shutdown()
    # Claude Code's own file sink wrote bodies/* under the user's umask; make them
    # private now that the child has exited (the directory was 0700 throughout).
    _chmod_files_private(bodies_dir)

    n_hook_records = _count_lines(os.path.join(hooks_dir, "hooks.jsonl"))
    session_id, malformed_bodies = _session_id_from_bodies(bodies_dir)
    body_names = _names_in(bodies_dir)
    otel_names = _names_in(otel_dir)
    manifest: dict[str, Any] = {
        "ts": ts,
        **argv_record(argv, allow_env_secrets()),
        "claude_exit": claude_exit,
        "otlp_port": port,
        "started": started,
        "ended": ended,
        "session_id": session_id,
        "malformed_bodies": malformed_bodies,
        "hooks": {"injected": inject_hooks, "records": n_hook_records},
        "captured_with": {
            "bodies": any(f.endswith(".request.json") for f in body_names),
            "otel_logs": any(f.startswith("logs-") for f in otel_names),
            "otel_metrics": any(f.startswith("metrics-") for f in otel_names),
            "hooks": n_hook_records > 0,
        },
    }
    try:
        with sink.open_private(os.path.join(capture_dir, "manifest.json")) as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
    except OSError as e:  # pragma: no cover — defensive
        print(f"[harness-capture] could not write manifest.json ({e!r})", file=sys.stderr)
    if malformed_bodies:
        print(
            f"[harness-capture] WARNING: {len(malformed_bodies)} unreadable body file(s) "
            f"— recorded in manifest.json as malformed_bodies: "
            f"{', '.join(malformed_bodies[:5])}",
            file=sys.stderr,
        )

    # Finalize: every instrumented session ends as an enriched transcript. A
    # failure here must not eat claude's exit code.
    try:
        sessions_base = os.environ.get("HARNESS_SESSIONS_DIR") or os.path.join(
            os.getcwd(), "sessions"
        )
        out_dir = os.path.join(sessions_base, ts)
        _makedirs_private(out_dir)
        doc = normalize(capture_dir)
        with sink.open_private(os.path.join(out_dir, "normalized.json")) as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        project.render(doc, os.path.join(out_dir, "sessfs"))
        stats, problems = check_invariants(doc, capture_dir)
        n_inf = stats["events_by_type"].get("inference", 0)
        verdict = "OK" if not problems else f"{len(problems)} PROBLEMS"
        print(
            f"[harness-capture] {out_dir}: {n_inf} inferences, "
            f"{stats.get('lineages', 0)} lineages, "
            f"cost ${stats.get('total_cost_usd', 0)} — {verdict}",
            file=sys.stderr,
        )
        for p in problems[:10]:
            print(f"[harness-capture]   - {p}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — finalize is best-effort by design
        print(
            f"[harness-capture] finalize failed ({e!r}); bodies are on disk — "
            f"run: harness-normalize {capture_dir} --check",
            file=sys.stderr,
        )

    sys.exit(claude_exit)


if __name__ == "__main__":
    main()
