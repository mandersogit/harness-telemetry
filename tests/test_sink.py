"""collector/sink.py — the local OTLP receiver as an HTTP server, not a toy.

It listens on loopback for the whole length of a session, so it is treated as an
attack surface: only the three OTLP paths exist, every export must carry the
per-capture bearer, bodies are bounded and must be well-formed JSON objects, the
tee posts on a FIXED path with no redirects, and nothing it writes is group- or
world-readable.
"""

import gzip
import http.client
import http.server
import json
import os
import socket
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness_telemetry.collector import sink as sinkmod

TOKEN = "test-bearer-token"


# --- harness --------------------------------------------------------------------


class _Sink:
    def __init__(self, out: Path, **kw: Any) -> None:
        self.out = out
        self.srv = sinkmod.serve(str(out), 0, **kw)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def post(
        self,
        path: str,
        body: bytes,
        token: str | None = TOKEN,
        headers: dict[str, str] | None = None,
    ) -> int:
        h = {"Content-Type": "application/json"}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body, h)
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "otel"


@pytest.fixture
def sink(out: Path) -> Iterator[_Sink]:
    s = _Sink(out, token=TOKEN)
    yield s
    s.close()


class _Collector(http.server.BaseHTTPRequestHandler):
    """A stand-in for the user's own collector, recording what it was sent."""

    received: list[tuple[str, bytes, dict[str, str]]] = []

    def log_message(self, *a: object) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", 0) or 0)
        # header names are normalized: urllib capitalizes what it sends
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        _Collector.received.append((self.path, self.rfile.read(n), hdrs))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _start_collector() -> http.server.ThreadingHTTPServer:
    _Collector.received = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


_BODY = json.dumps({"resourceLogs": []}).encode()
# Each signal path accepts only its own OTLP/JSON envelope.
_BODIES = {
    "/v1/logs": _BODY,
    "/v1/metrics": json.dumps({"resourceMetrics": []}).encode(),
    "/v1/traces": json.dumps({"resourceSpans": []}).encode(),
}


# --- auth + path allowlist ------------------------------------------------------


def test_accepts_a_signed_export_on_each_signal_path(sink: _Sink, out: Path) -> None:
    for path, name in (("/v1/logs", "logs"), ("/v1/metrics", "metrics"), ("/v1/traces", "traces")):
        assert sink.post(path, _BODIES[path]) == 200
        assert json.loads((out / f"{name}-0001.json").read_text()) == json.loads(_BODIES[path])


def test_a_payload_that_does_not_match_its_path_is_400(sink: _Sink, out: Path) -> None:
    # The boundary is the path AND the payload. A metrics envelope POSTed to
    # /v1/logs would be filed as logs-0001.json and read back as events; an empty
    # object would be filed as an export that carries nothing.
    assert sink.post("/v1/logs", _BODIES["/v1/metrics"]) == 400
    assert sink.post("/v1/metrics", _BODIES["/v1/traces"]) == 400
    assert sink.post("/v1/traces", _BODIES["/v1/logs"]) == 400
    assert sink.post("/v1/logs", b"{}") == 400
    assert not list(out.glob("logs-*")) and not list(out.glob("metrics-*"))
    assert not list(out.glob("traces-*"))
    log = (out / "_requests.log").read_text()
    assert log.count("REJECTED") == 4
    assert "reason=missing-resourceLogs" in log and "reason=missing-resourceMetrics" in log


def test_wrong_or_missing_bearer_is_401_and_writes_nothing(
    sink: _Sink,
    out: Path,
) -> None:
    assert sink.post("/v1/logs", _BODY, token=None) == 401
    assert sink.post("/v1/logs", _BODY, token="not-the-token") == 401
    assert sink.post("/v1/logs", _BODY, token=TOKEN + "x") == 401
    assert not list(out.glob("logs-*"))
    # ... and the capture is not poisoned: a good export still lands as 0001
    assert sink.post("/v1/logs", _BODY) == 200
    assert (out / "logs-0001.json").exists()


def test_unknown_paths_are_404(sink: _Sink, out: Path) -> None:
    for path in ("/v1/other", "/", "/v1", "/v2/logs", "/v1/logs/extra", "/../etc"):
        assert sink.post(path, _BODY) == 404, path
    assert not list(out.glob("*.json"))


def test_trailing_slash_and_query_still_resolve_to_the_signal(
    sink: _Sink,
    out: Path,
) -> None:
    assert sink.post("/v1/logs/", _BODY) == 200
    assert sink.post("/v1/logs?debug=1", _BODY) == 200
    assert (out / "logs-0001.json").exists() and (out / "logs-0002.json").exists()


def test_no_token_means_open_but_loud(out: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = _Sink(out)  # standalone harness-otlp-sink behaviour
    try:
        assert "WARNING: no token" in capsys.readouterr().err
        assert s.post("/v1/logs", _BODY, token=None) == 200
    finally:
        s.close()


# --- body validation ------------------------------------------------------------


def test_malformed_bodies_are_rejected_and_never_written(sink: _Sink, out: Path) -> None:
    # Normalization only ever reads well-formed exports; a .raw file it cannot
    # read, or an object of the wrong shape, is worse than a 400 at the door.
    assert sink.post("/v1/logs", b"not json {") == 400
    assert sink.post("/v1/logs", b'["a", "list"]') == 400
    assert sink.post("/v1/logs", b'"a string"') == 400
    assert sink.post("/v1/logs", b"") == 400
    assert list(out.iterdir()) == [out / "_requests.log"]
    log = (out / "_requests.log").read_text()
    assert log.count("REJECTED") == 4
    assert "reason=unparseable-json" in log and "reason=top-level-not-an-object" in log


def test_oversize_bodies_are_rejected(sink: _Sink, out: Path) -> None:
    status = sink.post("/v1/logs", b"{}", headers={"Content-Length": str(sinkmod.MAX_BODY + 1)})
    assert status == 413
    assert not list(out.glob("logs-*"))
    assert "reason=oversize" in (out / "_requests.log").read_text()


def test_gzip_bodies_are_decompressed(sink: _Sink, out: Path) -> None:
    assert sink.post("/v1/logs", gzip.compress(_BODY), headers={"Content-Encoding": "gzip"}) == 200
    assert json.loads((out / "logs-0001.json").read_text()) == {"resourceLogs": []}


def test_request_log_never_records_an_incoming_header_value(
    sink: _Sink,
    out: Path,
) -> None:
    sink.post("/v1/logs", _BODY, headers={"X-Corp-Secret": "hunter2"})
    log = (out / "_requests.log").read_text()
    assert TOKEN not in log and "hunter2" not in log and "Authorization" not in log
    assert "CT=application/json" in log and "bytes=" in log


# --- file modes -----------------------------------------------------------------


def test_everything_written_is_private(sink: _Sink, out: Path) -> None:
    sink.post("/v1/logs", _BODY)
    assert _mode(out) == 0o700
    assert _mode(out / "logs-0001.json") == 0o600
    assert _mode(out / "_requests.log") == 0o600


# --- the tee --------------------------------------------------------------------


def _wait_for(n: int) -> None:
    deadline = time.time() + 5
    while len(_Collector.received) < n and time.time() < deadline:
        time.sleep(0.05)


def test_tee_posts_on_the_fixed_signal_path_with_the_user_headers(out: Path) -> None:
    # Telemetry additivity: an export POSTed to the sink lands on OUR disk AND is
    # re-POSTed to the user's collector — on the path for the signal we accepted,
    # never the incoming request's path, and never with our own bearer.
    target = _start_collector()
    tport = target.server_address[1]
    s = _Sink(
        out, token=TOKEN, forward=f"http://127.0.0.1:{tport}/", forward_headers={"x-corp": "yes"}
    )
    try:
        assert s.post("/v1/logs?debug=1", _BODY) == 200
        _wait_for(1)
        assert len(_Collector.received) == 1
        path, body, headers = _Collector.received[0]
        assert path == "/v1/logs"  # fixed, not "/v1/logs?debug=1"
        assert body == _BODY
        assert headers.get("x-corp") == "yes"  # the user's header arrives
        assert headers.get("content-type") == "application/json"
        assert "authorization" not in headers  # ours does not
        assert (out / "logs-0001.json").exists()  # our copy is written too

        # dead forward target: local write + 200 still happen
        target.shutdown()
        target.server_close()
        assert s.post("/v1/logs", _BODY) == 200
        assert (out / "logs-0002.json").exists()
    finally:
        s.close()


def test_rejected_bodies_are_never_teed(out: Path) -> None:
    target = _start_collector()
    s = _Sink(out, token=TOKEN, forward=f"http://127.0.0.1:{target.server_address[1]}")
    try:
        assert s.post("/v1/logs", b"not json {") == 400
        assert s.post("/v1/logs", _BODY, token=None) == 401
        assert s.post("/v1/other", _BODY) == 404
        time.sleep(0.3)
        assert _Collector.received == []
    finally:
        s.close()
        target.shutdown()
        target.server_close()


def test_forward_opener_refuses_redirects() -> None:
    # A 3xx from the tee target must not replay the user's credential elsewhere.
    handlers = [type(h).__name__ for h in sinkmod._OPENER.handlers]
    assert "_NoRedirect" in handlers
    assert (
        sinkmod._NoRedirect().redirect_request(None, None, 302, "Found", None, "http://elsewhere/")
        is None
    )  # type: ignore[arg-type]


def test_set_forward_replaces_the_target() -> None:
    sinkmod.set_forward("http://a/", {"k": "v"})
    assert sinkmod.FORWARD == "http://a/" and sinkmod.FORWARD_HEADERS == {"k": "v"}
    sinkmod.set_forward(None)
    assert sinkmod.FORWARD is None and sinkmod.FORWARD_HEADERS == {}


def test_open_private_and_makedirs_private(tmp_path: Path) -> None:
    d = tmp_path / "d"
    sinkmod.makedirs_private(str(d))
    assert _mode(d) == 0o700
    with sinkmod.open_private(str(d / "f.txt")) as f:
        f.write("a")
    with sinkmod.open_private(str(d / "f.txt"), "a") as f:
        f.write("b")
    assert (d / "f.txt").read_text() == "ab"  # append does not truncate
    assert _mode(d / "f.txt") == 0o600
    with sinkmod.open_private(str(d / "f.txt")) as f:
        f.write("c")
    assert (d / "f.txt").read_text() == "c"  # w truncates
    assert os.path.exists(d / "f.txt")


def test_parse_otlp_headers() -> None:
    assert sinkmod.parse_otlp_headers("a=b, c=d=e") == {"a": "b", "c": "d=e"}
    assert sinkmod.parse_otlp_headers(None) == {}
    assert sinkmod.parse_otlp_headers("garbage") == {}


# --- open_private ENFORCES the mode, it does not merely request it --------------


def test_open_private_forces_0600_on_a_file_that_already_existed(tmp_path: Path) -> None:
    # The 0o600 argument to os.open applies only when the file is CREATED. A
    # normalized.json re-written over a 0644 one, or a log the user had already
    # touched, would otherwise stay world-readable — and the whole point is that
    # nothing under a capture or session directory is.
    victim = tmp_path / "pre-existing.json"
    victim.write_text("old")
    victim.chmod(0o644)
    with sinkmod.open_private(str(victim)) as f:
        f.write("new")
    assert _mode(victim) == 0o600
    assert victim.read_text() == "new"
    # ... and on append, too
    victim.chmod(0o666)
    with sinkmod.open_private(str(victim), "a") as f:
        f.write("!")
    assert _mode(victim) == 0o600
    assert victim.read_text() == "new!"


def test_open_private_refuses_a_symlinked_target(tmp_path: Path) -> None:
    # O_NOFOLLOW: a planted link must not redirect the write — or the chmod —
    # out of the capture dir.
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch")
    outside.chmod(0o644)
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    with pytest.raises(OSError):
        sinkmod.open_private(str(link))
    assert outside.read_text() == "do not touch"
    assert _mode(outside) == 0o644


def test_makedirs_private_forces_0700_on_an_existing_directory(tmp_path: Path) -> None:
    d = tmp_path / "already"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    sinkmod.makedirs_private(str(d))
    assert _mode(d) == 0o700


# --- the standalone sink guards its own --forward -------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now — so the sink can bind it and the
    --forward URL can name it in advance."""
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port: int = sk.getsockname()[1]
    return port


def _run_sink_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """harness-otlp-sink's main(), without the blocking serve_forever."""
    monkeypatch.setattr(sinkmod.ThreadingHTTPServer, "serve_forever", lambda self: None)
    monkeypatch.setattr(sinkmod.sys, "argv", ["harness-otlp-sink", *argv])
    sinkmod.main()


def test_standalone_forward_refuses_its_own_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `harness-otlp-sink --forward` at its own bind re-POSTs every export to
    # itself: a probe fanned one export out to 62. The launcher has guarded this
    # since the tee existed; the standalone entry point had not.
    port = _free_port()
    _run_sink_main(
        monkeypatch,
        [
            "--out",
            str(tmp_path / "o"),
            "--port",
            str(port),
            "--forward",
            f"http://localhost:{port}",
        ],
    )
    assert sinkmod.FORWARD is None
    assert "self-loop guard" in capsys.readouterr().err


def test_standalone_forward_to_somewhere_else_is_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _free_port()
    _run_sink_main(
        monkeypatch,
        [
            "--out",
            str(tmp_path / "o"),
            "--port",
            str(port),
            "--forward",
            "https://collector.corp:4318",
            "--forward-header",
            "k=v",
        ],
    )
    assert sinkmod.FORWARD == "https://collector.corp:4318"
    assert sinkmod.FORWARD_HEADERS == {"k": "v"}
    assert "self-loop guard" not in capsys.readouterr().err
    sinkmod.set_forward(None)


def test_points_at_bind_shares_the_launcher_guard() -> None:
    assert sinkmod.points_at_bind("http://127.0.0.1:4318", 4318)
    assert not sinkmod.points_at_bind("http://127.0.0.1:4319", 4318)
    assert not sinkmod.points_at_bind(None, 4318)
    assert not sinkmod.points_at_bind("http://localhost:notaport", 4318)
    assert sinkmod.reaches_bind("::ffff:127.0.0.1")
    assert not sinkmod.reaches_bind("192.0.2.1")
