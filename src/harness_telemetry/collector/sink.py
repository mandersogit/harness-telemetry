#!/usr/bin/env python3
"""
sink.py — a minimal local OTLP/HTTP receiver for capturing Claude Code telemetry to disk.

Claude Code's OTEL exporter POSTs to {OTEL_EXPORTER_OTLP_ENDPOINT}/v1/{logs,metrics,traces}.
This receiver accepts those POSTs and writes each export request to <out>/ as JSON, so the
metrics/events half of a session (cost, duration, compaction, tool decisions) lands on disk
alongside the raw bodies that claude-capture already writes.

Handles chunked transfer-encoding and gzip (the exporter uses both). Run Claude Code with
OTEL_EXPORTER_OTLP_PROTOCOL=http/json so payloads are inspectable JSON. stdlib only.

It is an HTTP server on the loopback interface, so it is written like one:

  * bound to 127.0.0.1 only, never 0.0.0.0;
  * POST is accepted on /v1/logs, /v1/metrics and /v1/traces and nowhere else (404);
  * with serve(token=...) every POST must carry `Authorization: Bearer <token>`
    (401 otherwise, compared with hmac.compare_digest). harness-capture mints a
    per-capture token; the standalone CLI runs unauthenticated and says so;
  * the body is bounded (MAX_BODY compressed/on the wire, MAX_DECOMPRESSED after
    gunzip; 413 beyond) and must be a JSON OBJECT — anything else is 400 and is NOT
    written to disk, because normalization only ever reads well-formed exports;
  * incoming request headers are never logged or persisted: _requests.log records
    content-type/encoding/length and the byte count, nothing else;
  * every file it creates is 0600 and its out dir 0700.

TELEMETRY ADDITIVITY: with --forward URL (or serve(forward=...) / set_forward(...)),
every export is also re-POSTed to the user's own collector after being written to
disk — capture TEES the telemetry instead of stealing it. Forwarding is best-effort
and decompressed OTLP/HTTP JSON (Content-Type application/json, on the FIXED
/v1/<signal> path for the signal we just accepted — never the incoming request's
path; OTEL_EXPORTER_OTLP_HEADERS-style headers via --forward-header/forward_headers)
through an opener that refuses redirects, so a 3xx can never replay the user's
credential to another host. A dead or slow target never fails the local write or the
200 back to the exporter.

    harness-otlp-sink --out DIR [--port 4318] [--forward URL]
"""

import argparse
import contextlib
import gzip
import hmac
import io
import ipaddress
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO, Any
from urllib.parse import urlsplit

OUT: str = ""
FORWARD: str | None = None
FORWARD_HEADERS: dict[str, str] = {}
TOKEN: str | None = None
SEQ: dict[str, int] = {}
LOCK = threading.Lock()

# The only paths that exist. The signal name doubles as the on-disk prefix.
SIGNAL_PATHS: dict[str, str] = {
    "/v1/logs": "logs",
    "/v1/metrics": "metrics",
    "/v1/traces": "traces",
}

# ...and the OTLP/JSON top-level key each path must carry. A metrics payload
# POSTed to /v1/logs would be filed as logs-NNNN.json and read back as events:
# the boundary is the path AND the payload, not the path alone. Normalization
# stays tolerant downstream — this is the gate, not the only defence.
SIGNAL_ROOT_KEY: dict[str, str] = {
    "logs": "resourceLogs",
    "metrics": "resourceMetrics",
    "traces": "resourceSpans",
}

# An OTLP export is kilobytes to a few megabytes. These are backstops against a
# local process wedging the sink, not tuning knobs.
MAX_BODY = 64 * 1024 * 1024
MAX_DECOMPRESSED = 256 * 1024 * 1024


def open_private(path: str, mode: str = "w") -> IO[Any]:
    """open(), but 0600 — never a 0644 window, and never a mode inherited from a
    file that happened to exist already. mode: w/wb (truncate) or a/ab (append).

    The `0o600` argument to os.open only applies when the file is CREATED, so the
    fchmod is not redundant: re-running a normalize over an existing 0644
    normalized.json, or appending to a log the user had already touched, would
    otherwise keep it world-readable. O_NOFOLLOW refuses a symlinked final
    component, so a planted link cannot redirect the write (or the chmod) out of
    the capture dir. The containing dir is 0700.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_APPEND if mode.startswith("a") else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return os.fdopen(fd, mode)


def makedirs_private(path: str) -> None:
    """0700 from creation, and 0700 afterwards even if the directory existed.
    makedirs honours the umask (and applies `mode` only to the last component),
    so chmod the final directory explicitly either way."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def reaches_bind(host: str) -> bool:
    """Does `host` resolve to an address that reaches this sink's 127.0.0.1 bind?
    Loopback obviously (127.0.0.0/8, ::1); the UNSPECIFIED addresses too
    (connecting to 0.0.0.0 or :: lands on loopback on Linux); and the IPv4-MAPPED
    form of either — `::ffff:127.0.0.1` is not is_loopback, but a socket opened to
    it reaches the same 127.0.0.1 listener."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError, ValueError):
        return False
    for info in infos:
        addr = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        for candidate in (ip, getattr(ip, "ipv4_mapped", None)):
            if candidate is not None and (candidate.is_loopback or candidate.is_unspecified):
                return True
    return False


def points_at_bind(url: str | None, port: int) -> bool:
    """A forward target that resolves to OUR bound port would make the tee a
    self-loop — every export re-POSTed to ourselves, forever (a probe fanned one
    POST out to 62 exports). Name-matching "localhost" is not enough: 0.0.0.0,
    ::, a 127.x address, an IPv4-mapped one and any DNS name resolving to
    loopback all reach the same socket."""
    if not url:
        return False
    try:
        u = urlsplit(url)
        uport = u.port
        host = u.hostname
    except ValueError:  # malformed port — not a usable target at all
        return False
    if uport != port or not host:
        return False
    return reaches_bind(host)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx from the tee target must not replay the
    user's collector credential — or the export body — to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    # --- request log (never any incoming header value) ---------------------
    def _log(self, line: str) -> None:
        with (
            contextlib.suppress(OSError),
            LOCK,
            open_private(os.path.join(OUT, "_requests.log"), "a") as f,
        ):
            f.write(line + "\n")

    def _read_body(self) -> bytes | None:
        # The OTEL exporter streams its export with chunked transfer-encoding and
        # NO Content-Length, so a Content-Length-only reader gets 0 bytes: decode
        # the chunks explicitly before falling back to Content-Length. Both paths
        # are bounded — None means "too big, stop reading".
        te = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in te:
            parts: list[bytes] = []
            total = 0
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    break
                total += size
                if total > MAX_BODY:
                    return None
                parts.append(self.rfile.read(size))
                self.rfile.readline()  # CRLF after each chunk
            body = b"".join(parts)
        else:
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                n = 0
            if n > MAX_BODY:
                return None
            body = self.rfile.read(n) if n > 0 else b""
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
                    plain = gz.read(MAX_DECOMPRESSED + 1)
            except (OSError, EOFError):
                return body  # not actually gzip — the JSON check will reject it
            if len(plain) > MAX_DECOMPRESSED:
                return None
            body = plain
        return body

    def _authorized(self) -> bool:
        if TOKEN is None:
            return True
        return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}")

    def _reject(self, code: int) -> None:
        # The body is deliberately unread on a rejection, so this connection
        # cannot be reused: close it rather than desync the next request.
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        sig = SIGNAL_PATHS.get(urlsplit(self.path).path.rstrip("/"))
        if sig is None:
            self._log("REJECTED  reason=unknown-path  code=404")
            self._reject(404)
            return
        if not self._authorized():
            self._log(f"{sig}-REJECTED  reason=bad-or-missing-bearer  code=401")
            self._reject(401)
            return
        body = self._read_body()
        if body is None:
            self._log(f"{sig}-REJECTED  reason=oversize  code=413")
            self._reject(413)
            return
        try:
            doc = json.loads(body)
        except ValueError:
            self._log(f"{sig}-REJECTED  reason=unparseable-json  code=400  bytes={len(body)}")
            self._reject(400)
            return
        if not isinstance(doc, dict):
            self._log(
                f"{sig}-REJECTED  reason=top-level-not-an-object  code=400  bytes={len(body)}"
            )
            self._reject(400)
            return
        root_key = SIGNAL_ROOT_KEY[sig]
        if root_key not in doc:
            self._log(f"{sig}-REJECTED  reason=missing-{root_key}  code=400  bytes={len(body)}")
            self._reject(400)
            return
        with LOCK:
            SEQ[sig] = SEQ.get(sig, 0) + 1
            idx = SEQ[sig]
        self._log(
            f"{sig}-{idx:04d}  CT={self.headers.get('Content-Type')}  "
            f"CE={self.headers.get('Content-Encoding')}  "
            f"TE={self.headers.get('Transfer-Encoding')}  "
            f"CL={self.headers.get('Content-Length')}  bytes={len(body)}"
        )
        try:
            with open_private(os.path.join(OUT, f"{sig}-{idx:04d}.json")) as f:
                json.dump(doc, f, indent=2)
        except OSError:
            self._reject(500)
            return
        self._forward(sig, body)
        resp = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _forward(self, sig: str, body: bytes) -> None:
        """Tee the (decompressed) export to the user's collector on the FIXED
        /v1/<signal> path — never the incoming request's path, which is
        attacker-controlled. Best-effort: any failure is swallowed —
        additivity must never cost the capture."""
        if not FORWARD:
            return
        try:
            req = urllib.request.Request(
                f"{FORWARD.rstrip('/')}/v1/{sig}",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", **FORWARD_HEADERS},
            )
            with contextlib.suppress(Exception):
                _OPENER.open(req, timeout=3).close()
        except Exception:  # noqa: BLE001 — building the request must not raise either
            pass


def set_forward(url: str | None, headers: dict[str, str] | None = None) -> None:
    """Point (or unpoint) the tee after bind — the self-loop guard needs the
    bound port, which only exists once the server is up."""
    global FORWARD, FORWARD_HEADERS
    FORWARD = url
    FORWARD_HEADERS = dict(headers or {})


def serve(
    out: str,
    port: int = 4318,
    forward: str | None = None,
    forward_headers: dict[str, str] | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """Bind the sink (one per process — module-global out dir) and return the
    server; the caller decides the thread. port=0 binds an ephemeral port
    (read it back from server.server_address). Used in-process by
    harness-capture: no subprocess, no orphaned-collector failure mode.
    forward= tees every export to the user's own collector (additivity).
    token= requires `Authorization: Bearer <token>` on every export."""
    global OUT, TOKEN
    OUT = out
    TOKEN = token
    SEQ.clear()  # module-global: a second serve() in one process starts at 0001
    set_forward(forward, forward_headers)
    makedirs_private(OUT)
    if token is None:
        sys.stderr.write(
            "[sink] WARNING: no token — any local process can POST exports into this capture dir\n"
        )
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def parse_otlp_headers(spec: str | None) -> dict[str, str]:
    """OTEL_EXPORTER_OTLP_HEADERS syntax: 'k1=v1,k2=v2'."""
    out: dict[str, str] = {}
    for part in (spec or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument(
        "--forward", help="tee every export to this OTLP/HTTP endpoint (the user's own collector)"
    )
    ap.add_argument(
        "--forward-header",
        action="append",
        default=[],
        help="header for the forward target, k=v (repeatable)",
    )
    args = ap.parse_args()
    srv = serve(
        args.out,
        args.port,
        forward=args.forward,
        forward_headers=parse_otlp_headers(",".join(args.forward_header)),
    )
    # The bound port is only known now (--port 0 is ephemeral), so the self-loop
    # check has to run after serve(): --forward http://localhost:4318 into our own
    # :4318 would re-POST every export to ourselves, forever.
    if points_at_bind(args.forward, srv.server_address[1]):
        set_forward(None, None)
        sys.stderr.write(
            "[sink] WARNING: --forward resolves to this sink's own bind — "
            "forwarding is DISABLED (self-loop guard)\n"
        )
    sys.stderr.write(f"[sink] listening on 127.0.0.1:{srv.server_address[1]} -> {OUT}\n")
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
