"""capture.py unit behaviors (the live path is exercised by real capture runs).

The launcher's job is a boundary, not a convenience: everything Claude Code could
export has exactly one destination (our sink), the user's collector credentials
never enter the child env, and nothing it writes is readable by another local uid.
These tests pin that boundary.
"""

import json
import os
import socket
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from harness_telemetry import capture
from harness_telemetry.capture import (
    _points_at_us,
    _strip_userinfo,
    argv_flags,
    argv_record,
    build_env,
    capture_env_text,
    endpoint_is_usable,
    forward_otel,
    hookrec_script,
    preset_signal_endpoints,
    redact_env_line,
)
from harness_telemetry.collector import sink as sinkmod

_SIGNALS = ("LOGS", "METRICS", "TRACES")


# --- build_env: the capture owns every OTLP destination -------------------------


def test_build_env_owns_bodies_and_defaults_the_rest() -> None:
    env = build_env("/cap/bodies", 4318, {"PATH": "/usr/bin"})
    assert env["OTEL_LOG_RAW_API_BODIES"] == "file:/cap/bodies"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"  # sink wants JSON
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["PATH"] == "/usr/bin"


def test_build_env_owns_every_per_signal_endpoint() -> None:
    # A per-signal endpoint OVERRIDES the generic one in Claude Code, so owning
    # only the generic one leaves a pre-set SIEM endpoint receiving prompts and
    # tool details around the sink. Per-signal endpoints are used VERBATIM (no
    # /v1/<signal> appended), hence the full signal path here.
    base = {f"OTEL_EXPORTER_OTLP_{s}_ENDPOINT": "http://siem.corp:4318/v1/x" for s in _SIGNALS}
    base["OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"] = "grpc"
    env = build_env("/cap/bodies", 9999, base)
    for s in _SIGNALS:
        assert env[f"OTEL_EXPORTER_OTLP_{s}_ENDPOINT"] == (f"http://127.0.0.1:9999/v1/{s.lower()}")
        assert env[f"OTEL_EXPORTER_OTLP_{s}_PROTOCOL"] == "http/json"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:9999"
    assert "siem.corp" not in json.dumps(env)


def test_preset_signal_endpoints_reports_what_gets_rewritten() -> None:
    assert preset_signal_endpoints({}) == {}
    assert preset_signal_endpoints(
        {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://a:1", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ""}
    ) == {"LOGS": "http://a:1"}


def test_build_env_strips_user_headers_and_sets_the_sink_bearer() -> None:
    # Per-signal headers MERGE with the generic ones, so all four must go —
    # otherwise the child would present the user's collector credential to us.
    base = {
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer corp-token",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "x-corp=logs",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "x-corp=metrics",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-corp=traces",
    }
    env = build_env("/cap/bodies", 9999, dict(base), sink_token="sink-tok")
    for s in _SIGNALS:
        assert f"OTEL_EXPORTER_OTLP_{s}_HEADERS" not in env
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer sink-tok"
    assert "corp-token" not in json.dumps(env) and "x-corp" not in json.dumps(env)


def test_build_env_without_a_token_leaves_no_headers_at_all() -> None:
    env = build_env("/cap/bodies", 9999, {"OTEL_EXPORTER_OTLP_HEADERS": "a=b"})
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env


def test_build_env_owns_endpoint_and_tees_instead() -> None:
    # Telemetry additivity: a pre-set endpoint is the USER's collector.
    # The exporter must point at OUR sink (else the capture is blind); the sink
    # TEES exports onward to the user's endpoint. Other OTEL_* stay overridable.
    base = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.corp:4318",
        "OTEL_LOG_RAW_API_BODIES": "file:/somewhere/else",
        "OTEL_LOGS_EXPORT_INTERVAL": "500",
    }
    env = build_env("/cap/bodies", 9999, base)
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:9999"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
    assert env["OTEL_LOG_RAW_API_BODIES"] == "file:/cap/bodies"
    assert env["OTEL_LOGS_EXPORT_INTERVAL"] == "500"  # non-owned defaults still yield


def test_build_env_pins_assistant_responses() -> None:
    # Pinned on: every channel is captured at full fidelity, and pinning keeps
    # the OTEL scope from depending on the OTEL_LOG_USER_PROMPTS fallback.
    env = build_env("/cap/bodies", 4318, {})
    assert env["OTEL_LOG_USER_PROMPTS"] == "1"
    assert env["OTEL_LOG_ASSISTANT_RESPONSES"] == "1"
    env = build_env("/cap/bodies", 4318, {"OTEL_LOG_ASSISTANT_RESPONSES": "0"})
    assert env["OTEL_LOG_ASSISTANT_RESPONSES"] == "0"  # explicit user choice wins


def test_build_env_disables_refusal_fallback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Captured sessions must be single-model by construction — no silent
    # safeguard fallback (verified gate: CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK,
    # the env-var twin of the /config switchModelsOnFlag toggle).
    monkeypatch.delenv("HARNESS_ALLOW_MODEL_FALLBACK", raising=False)
    env = build_env("/cap/bodies", 4318, {})
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"
    # opt back in explicitly
    monkeypatch.setenv("HARNESS_ALLOW_MODEL_FALLBACK", "1")
    env = build_env("/cap/bodies", 4318, {})
    assert "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK" not in env


def test_build_env_points_hooks_at_the_installed_script() -> None:
    env = build_env("/cap/bodies", 4318, {}, hooks_dir="/cap/hooks")
    assert env["HARNESS_HOOK_DIR"] == "/cap/hooks"
    assert os.path.isabs(env["HARNESS_HOOK_SCRIPT"])
    assert env["HARNESS_HOOK_SCRIPT"].endswith("hookrec.py")
    assert env["HARNESS_HOOK_SCRIPT"] == hookrec_script()


# --- the tee target: usable? ours? ---------------------------------------------


def _fake_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    def fake(host: str, port: Any, *a: Any, **kw: Any) -> list[Any]:
        if host in mapping:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]
        raise socket.gaierror("nope")

    monkeypatch.setattr(sinkmod.socket, "getaddrinfo", fake)


def test_self_loop_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Name-matching "localhost" is not enough: 0.0.0.0 and :: reach a
    # 127.0.0.1-bound socket, and a DNS name can resolve to loopback. A tee that
    # points back at us re-POSTs every export to ourselves, forever.
    _fake_dns(
        monkeypatch,
        {
            "localhost": "127.0.0.1",
            "127.0.0.1": "127.0.0.1",
            "127.1.2.3": "127.1.2.3",
            "0.0.0.0": "0.0.0.0",
            "::1": "::1",
            "::": "::",
            "loop.corp": "127.0.0.1",
            "mapped.corp": "::ffff:127.0.0.1",
            "mapped0.corp": "::ffff:0.0.0.0",
            "collector.corp": "10.0.0.5",
        },
    )
    assert _points_at_us("http://localhost:4318", 4318)
    assert _points_at_us("http://127.0.0.1:4318/v1", 4318)
    assert _points_at_us("http://127.1.2.3:4318", 4318)
    assert _points_at_us("http://0.0.0.0:4318", 4318)
    assert _points_at_us("http://[::1]:4318", 4318)
    assert _points_at_us("http://[::]:4318", 4318)
    assert _points_at_us("http://loop.corp:4318", 4318)  # DNS -> loopback
    # IPv4-MAPPED: ::ffff:127.0.0.1 is not is_loopback, but a socket opened to it
    # lands on the same 127.0.0.1 listener — the tee would feed itself.
    assert _points_at_us("http://mapped.corp:4318", 4318)
    assert _points_at_us("http://mapped0.corp:4318", 4318)
    assert not _points_at_us("http://collector.corp:4318", 4318)
    assert not _points_at_us("http://localhost:4317", 4318)
    assert not _points_at_us("http://unresolvable.invalid:4318", 4318)
    assert not _points_at_us(None, 4318)


def test_self_loop_guard_survives_a_malformed_port() -> None:
    # urlparse(...).port raises ValueError on a bad port — that must not take
    # the launcher down before `claude` even starts.
    assert not _points_at_us("http://localhost:notaport", 4318)
    assert not _points_at_us("http://localhost:99999999", 4318)


def test_endpoint_is_usable() -> None:
    assert endpoint_is_usable("http://collector.corp:4318")
    assert endpoint_is_usable("https://collector.corp/otlp")
    assert not endpoint_is_usable("http://collector.corp:notaport")
    assert not endpoint_is_usable("grpc://collector.corp:4317")
    assert not endpoint_is_usable("not a url")
    assert not endpoint_is_usable("")


# --- capture-env.txt: an allow-list, not a blocklist ----------------------------

_ENV_SAMPLE = {
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer sk-not-a-real-token",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "x-corp=logs",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:4318/v1/logs",
    "OTEL_RESOURCE_ATTRIBUTES": "user.email=someone@example.com,team=platform",
    "OTEL_LOG_ASSISTANT_RESPONSES": "1",
    "CLAUDE_CODE_SOME_TOKEN": "tok-abc123",
    "CLAUDE_CODE_CLIENT_CERT": "/etc/pki/corp.pem",
    "CLAUDE_CODE_VAULT_CREDENTIALS": "cred-xyz",
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "PATH": "/usr/bin",
}


def test_redact_env_line_writes_only_allow_listed_values() -> None:
    # the NAME is always kept (provenance: a header WAS set); only the value goes
    assert redact_env_line("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer x") == (
        "OTEL_EXPORTER_OTLP_HEADERS=<redacted>"
    )
    assert redact_env_line("CLAUDE_CODE_SOME_TOKEN", "tok") == ("CLAUDE_CODE_SOME_TOKEN=<redacted>")
    # everything the old name-based blocklist could not have known about
    for name in (
        "OTEL_RESOURCE_ATTRIBUTES",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_VAULT_CREDENTIALS",
        "OTEL_SOMETHING_COOKIE",
        "CLAUDE_CODE_SENTRY_DSN",
        "OTEL_UNFORESEEN_KNOB",
    ):
        assert redact_env_line(name, "secret") == f"{name}=<redacted>"
    # allow-listed provenance keeps its value
    assert redact_env_line("OTEL_EXPORTER_OTLP_ENDPOINT", "http://x:4318") == (
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://x:4318"
    )
    assert redact_env_line("OTEL_LOG_ASSISTANT_RESPONSES", "1") == (
        "OTEL_LOG_ASSISTANT_RESPONSES=1"
    )
    # explicit opt-in returns the value verbatim
    assert redact_env_line("OTEL_EXPORTER_OTLP_HEADERS", "a=b", True) == (
        "OTEL_EXPORTER_OTLP_HEADERS=a=b"
    )


def test_redact_env_line_strips_url_userinfo() -> None:
    assert redact_env_line(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "https://user:tok@c.corp:4318/v1/logs"
    ) == ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=https://c.corp:4318/v1/logs")
    assert _strip_userinfo("http://a:b@h:1/p") == "http://h:1/p"
    assert _strip_userinfo("http://h:1/p") == "http://h:1/p"
    assert _strip_userinfo("file:/cap/bodies") == "file:/cap/bodies"


def test_redact_env_line_never_writes_the_sink_bearer() -> None:
    line = redact_env_line(
        "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer s3kr1t", True, "s3kr1t"
    )
    assert line == "OTEL_EXPORTER_OTLP_HEADERS=<redacted>"


def test_capture_env_text_redacts_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_ALLOW_ENV_SECRETS", raising=False)
    text = capture_env_text(
        "20260821-120000", ["-p", "a secret prompt", "--model=opus"], dict(_ENV_SAMPLE)
    )
    lines = text.splitlines()
    assert lines[:4] == [
        "ts=20260821-120000",
        "argv_flags=-p --model",
        "argv_n=3",
        "env_secrets=redacted",
    ]
    assert "a secret prompt" not in text  # argv values are content
    assert "opus" not in text
    for name in (
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "CLAUDE_CODE_SOME_TOKEN",
        "OTEL_RESOURCE_ATTRIBUTES",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_VAULT_CREDENTIALS",
    ):
        assert f"{name}=<redacted>" in lines
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" in lines
    assert "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs" in lines
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in lines
    assert "OTEL_LOG_ASSISTANT_RESPONSES=1" in lines
    for leaked in (
        "sk-not-a-real-token",
        "tok-abc123",
        "someone@example.com",
        "corp.pem",
        "cred-xyz",
        "x-corp=logs",
    ):
        assert leaked not in text
    # only CLAUDE_CODE_*/OTEL_* names are recorded at all
    assert not any(ln.startswith("PATH=") for ln in lines)


def test_capture_env_text_verbatim_on_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ALLOW_ENV_SECRETS", "1")
    text = capture_env_text("20260821-120000", ["-p", "hi"], dict(_ENV_SAMPLE))
    lines = text.splitlines()
    assert "env_secrets=verbatim" in lines
    assert "argv=-p hi" in lines  # full argv under the knob
    assert "OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer sk-not-a-real-token" in lines
    assert "CLAUDE_CODE_SOME_TOKEN=tok-abc123" in lines
    assert "OTEL_RESOURCE_ATTRIBUTES=user.email=someone@example.com,team=platform" in lines
    assert "<redacted>" not in text


def test_capture_env_text_never_writes_the_sink_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_ALLOW_ENV_SECRETS", "1")  # even wide open
    env = build_env("/cap/bodies", 4318, {}, sink_token="s3kr1t-token")
    text = capture_env_text("t", [], env, "s3kr1t-token")
    assert "s3kr1t-token" not in text
    assert "OTEL_EXPORTER_OTLP_HEADERS=<redacted>" in text.splitlines()


def test_capture_env_text_opt_in_parses_like_the_other_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for truthy in ("1", "true", "yes"):
        monkeypatch.setenv("HARNESS_ALLOW_ENV_SECRETS", truthy)
        assert "env_secrets=verbatim" in capture_env_text("t", [], dict(_ENV_SAMPLE))
    for falsy in ("0", "", "TRUE", "on"):
        monkeypatch.setenv("HARNESS_ALLOW_ENV_SECRETS", falsy)
        assert "env_secrets=redacted" in capture_env_text("t", [], dict(_ENV_SAMPLE))


def test_argv_flags_and_record() -> None:
    assert argv_flags(["-p", "hello", "--model=opus", "-", "--settings", "f.json"]) == [
        "-p",
        "--model",
        "--settings",
    ]
    assert argv_record(["-p", "hi"]) == {"argv_flags": ["-p"], "argv_n": 2}
    assert argv_record(["-p", "hi"], True) == {"argv": ["-p", "hi"]}


# --- main(): preflight, file modes, and what the child actually gets ------------


def test_preflight_exits_127_without_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture.shutil, "which", lambda _n: None)
    monkeypatch.setenv("CLAUDE_CAPTURE_DIR", str(tmp_path / "captures"))
    monkeypatch.setattr(capture.sys, "argv", ["harness-capture"])
    with pytest.raises(SystemExit) as exc:
        capture.main()
    assert exc.value.code == 127
    # and no stray capture dir was left behind
    assert not (tmp_path / "captures").exists()


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
    rc: int = 0,
    on_run: Any = None,
) -> dict[str, str]:
    """Drive main() with a stubbed `claude`; return the env the child saw.

    NEVER the real binary: a live session would write the maintainer's prompts,
    CLAUDE.md and file contents into the test's capture dir.
    """
    seen: dict[str, str] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kw["env"])
        if on_run is not None:
            on_run(kw["env"])
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(capture.shutil, "which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture.time, "sleep", lambda _s: None)
    monkeypatch.setenv("CLAUDE_CAPTURE_DIR", str(tmp_path / "captures"))
    monkeypatch.setenv("HARNESS_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_ENV_SECRETS", raising=False)
    monkeypatch.delenv("HARNESS_FORWARD_OTEL", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(capture.sys, "argv", ["harness-capture", *(argv or [])])
    with pytest.raises(SystemExit) as exc:
        capture.main()
    assert exc.value.code == rc
    return seen


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_main_writes_everything_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A capture dir holds prompts, file contents and secrets: no other local uid
    # gets to read it. No process-wide umask — `claude` would inherit it.
    _run_main(tmp_path, monkeypatch, ["-p", "hello"])
    cap = next((tmp_path / "captures").iterdir())
    assert _mode(cap) == 0o700
    for sub in ("bodies", "otel"):
        assert _mode(cap / sub) == 0o700
    for f in ("capture-env.txt", "manifest.json", "hook-settings.json"):
        assert _mode(cap / f) == 0o600, f
    sess = next((tmp_path / "sessions").iterdir())
    assert _mode(sess) == 0o700


def test_main_child_env_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _run_main(tmp_path, monkeypatch, ["-p", "a secret prompt"])
    cap = next((tmp_path / "captures").iterdir())
    port = json.loads((cap / "manifest.json").read_text())["otlp_port"]
    for s in _SIGNALS:
        assert env[f"OTEL_EXPORTER_OTLP_{s}_ENDPOINT"] == (
            f"http://127.0.0.1:{port}/v1/{s.lower()}"
        )
    token = env["OTEL_EXPORTER_OTLP_HEADERS"].removeprefix("Authorization=Bearer ")
    assert len(token) > 20
    # the bearer exists in the child env and NOWHERE on disk
    env_txt = (cap / "capture-env.txt").read_text()
    assert token not in env_txt
    assert token not in (cap / "manifest.json").read_text()
    assert "OTEL_EXPORTER_OTLP_HEADERS=<redacted>" in env_txt.splitlines()
    # argv is provenance, not content
    manifest = json.loads((cap / "manifest.json").read_text())
    assert manifest["argv_flags"] == ["-p"] and manifest["argv_n"] == 2
    assert "argv" not in manifest
    assert "a secret prompt" not in env_txt
    # timestamps are aware local time (maintainer decision): the offset in the
    # manifest is the machine's own, whatever that is (UTC included on CI)
    local_offset = datetime.now().astimezone().utcoffset()
    assert datetime.fromisoformat(manifest["started"]).utcoffset() == local_offset
    assert datetime.fromisoformat(manifest["ended"]).utcoffset() == local_offset


def test_main_warns_about_preset_per_signal_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "https://user:tok@siem.corp:4318/v1/logs"
    )
    _run_main(tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT was pre-set" in err
    assert "https://siem.corp:4318" in err  # scheme://host:port only
    assert "will\nNOT reach" in err or "NOT reach that collector" in err
    assert "tok@" not in err and "/v1/logs" not in err


# --- the tee is opt-in ----------------------------------------------------------


def test_forward_otel_parses_like_the_other_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    for truthy in ("1", "true", "yes"):
        monkeypatch.setenv("HARNESS_FORWARD_OTEL", truthy)
        assert forward_otel()
    for falsy in ("0", "", "TRUE", "on", "maybe"):
        monkeypatch.setenv("HARNESS_FORWARD_OTEL", falsy)
        assert not forward_otel()
    monkeypatch.delenv("HARNESS_FORWARD_OTEL", raising=False)
    assert not forward_otel()


def test_inherited_endpoint_is_not_teed_without_the_knob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Inheriting OTEL_EXPORTER_OTLP_ENDPOINT from a shell profile is not consent
    # to ship prompts: the capture turns the content flags on, so the tee has to
    # be asked for. The endpoint is still NAMED, so nobody is left guessing why
    # their collector went quiet.
    _run_main(
        tmp_path,
        monkeypatch,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:tok@collector.corp:4318/v1?k=v"},
    )
    err = capsys.readouterr().err
    assert (
        "inherited OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.corp:4318 NOT used "
        "— set HARNESS_FORWARD_OTEL=1 to tee content-enriched telemetry "
        "(prompts, tool details) to it" in err
    )
    assert sinkmod.FORWARD is None  # nothing was ever pointed at it
    assert "tok@" not in err and "?k=v" not in err  # netloc only


def test_forward_otel_tees_and_warns_before_claude_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    warned_before_start: list[str] = []

    def on_run(_env: dict[str, str]) -> None:
        warned_before_start.append(capsys.readouterr().err)

    _run_main(
        tmp_path,
        monkeypatch,
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.corp:4318",
            "HARNESS_FORWARD_OTEL": "1",
        },
        on_run=on_run,
    )
    assert sinkmod.FORWARD == "https://collector.corp:4318"
    # the warning is on stderr BEFORE the child runs — there is still time to Ctrl-C
    err = warned_before_start[0]
    assert "HARNESS_FORWARD_OTEL=1" in err
    assert "USER PROMPTS" in err and "TOOL" in err and "DETAILS" in err
    assert "collector.corp:4318" in err
    assert "Ctrl-C now to stop." in err


def test_forward_otel_still_refuses_an_unusable_or_self_looping_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_main(
        tmp_path,
        monkeypatch,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "grpc://c.corp:4317", "HARNESS_FORWARD_OTEL": "1"},
    )
    assert sinkmod.FORWARD is None
    assert "not a usable" in capsys.readouterr().err


# --- capture-env.txt does not name your home directory --------------------------


def test_capture_env_hides_the_capture_dir_path() -> None:
    # file:/home/<you>/<project>/captures/<ts>/bodies names a home directory, a
    # username and a project. The capture IS that directory; the placeholder says
    # everything a reader of the file needs.
    cap = "/home/someone/projects/thing/captures/20260821-120000"
    env = {"OTEL_LOG_RAW_API_BODIES": f"file:{cap}/bodies", "CLAUDE_CODE_ENABLE_TELEMETRY": "1"}
    text = capture_env_text("20260821-120000", [], env, None, cap)
    assert "OTEL_LOG_RAW_API_BODIES=file:<capture-dir>/bodies" in text.splitlines()
    assert "/home/someone" not in text
    # and the substitution is not needed to keep other values intact
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in text.splitlines()
    assert redact_env_line("OTEL_LOG_RAW_API_BODIES", f"file:{cap}/bodies") == (
        f"OTEL_LOG_RAW_API_BODIES=file:{cap}/bodies"
    )  # no capture_dir given: nothing to substitute


def test_main_writes_no_absolute_capture_path_into_capture_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_main(tmp_path, monkeypatch)
    cap = next((tmp_path / "captures").iterdir())
    text = (cap / "capture-env.txt").read_text()
    assert "OTEL_LOG_RAW_API_BODIES=file:<capture-dir>/bodies" in text.splitlines()
    assert str(tmp_path) not in text


# --- the capture id is local time -----------------------------------------------


def test_capture_id_is_local_time_not_utc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The directory name is the capture's id, and the operator reads it against
    # their own clock (maintainer decision, 2026-08-23: captures never leave the
    # machine, so wall-clock names win). A clock that answers differently for
    # aware and naive calls makes the choice visible: only the naive local
    # answer may reach the directory name.
    utc = datetime(2026, 8, 21, 23, 45, 1, tzinfo=UTC)
    local = datetime(2026, 8, 22, 13, 45, 1)  # the same instant at UTC+14

    class _Clock:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return utc if tz is not None else local

    monkeypatch.setattr(capture, "datetime", _Clock)
    _run_main(tmp_path, monkeypatch)
    cap = next((tmp_path / "captures").iterdir())
    assert cap.name == "20260822-134501"
    assert cap.name != utc.strftime("%Y%m%d-%H%M%S")
    # ... and the manifest agrees with the directory name, local wall-clock
    # throughout (started/ended carry the machine's own offset, whatever it is)
    manifest = json.loads((cap / "manifest.json").read_text())
    assert manifest["ts"] == cap.name
    assert manifest["started"] == manifest["ended"]
    assert manifest["started"].startswith("2026-08-22T13:45:01")


# --- the post-child path survives what the child left behind --------------------


def test_a_crashed_child_still_gets_a_manifest_and_its_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A child killed mid-write leaves a truncated body. Parsing it while building
    # the manifest used to raise, so the manifest was never written and claude's
    # exit status was replaced by a traceback.
    def leave_a_partial_body(env: dict[str, str]) -> None:
        bodies = Path(env["OTEL_LOG_RAW_API_BODIES"].removeprefix("file:"))
        (bodies / "partial.request.json").write_text('{"model": "claude-test-1", "messa')

    _run_main(tmp_path, monkeypatch, rc=23, on_run=leave_a_partial_body)
    cap = next((tmp_path / "captures").iterdir())
    manifest = json.loads((cap / "manifest.json").read_text())
    assert manifest["claude_exit"] == 23
    assert manifest["malformed_bodies"] == ["partial.request.json"]
    assert manifest["session_id"] is None
    assert "malformed_bodies" in capsys.readouterr().err


def test_a_clean_session_records_no_malformed_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_main(tmp_path, monkeypatch)
    cap = next((tmp_path / "captures").iterdir())
    assert json.loads((cap / "manifest.json").read_text())["malformed_bodies"] == []
