# Security

## What this tool captures, by design

harness-telemetry exists to record an agent session at full fidelity. A capture therefore contains **everything the model saw and everything it said**:

- every prompt you typed, and the system prompt and tool schemas the harness assembled;
- the contents of every file the agent read, and every diff it wrote;
- the full input and output of every tool call, including shell commands and their output;
- any **credential, token, key, or other secret that happened to be in scope** — in a file the agent read, in an environment variable it printed, in a command it ran, or pasted into a prompt.

This is not incidental; it is the point of the tool. `sessions/` inherits every bit of it: a `normalized.json` is a **semantic re-encoding** of the capture it came from — the same content, restructured and content-addressed, plus session provenance such as the account UUID and the working directory. It is not a redacted view of the capture and it is not smaller in what it reveals.

## Where the data goes

`harness-capture` writes to local disk. It writes to a network destination only if you asked for one explicitly, with `HARNESS_FORWARD_OTEL=1`:

- **The bundled OTLP sink binds `127.0.0.1` only**, accepts only `POST /v1/logs`, `/v1/metrics` and `/v1/traces` (no other method is implemented), and requires a **bearer token generated fresh for that capture** — any other path is a 404 and a request without the token is a 401. The token is passed to the child process in its environment; it is never written to a capture file and never printed. Incoming request headers are never logged. Run the same sink standalone as `harness-otlp-sink` and there is no token — it warns on startup that any local process can POST into the capture directory; that mode is for local debugging, not for capturing a session you care about.
- **The launcher owns every OTLP destination.** The generic `OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL` *and* all three per-signal `OTEL_EXPORTER_OTLP_{LOGS,METRICS,TRACES}_ENDPOINT`/`_PROTOCOL` pairs are pointed at the in-process sink before `claude` starts, and your `OTEL_EXPORTER_OTLP_*HEADERS` variables are removed from the child's environment. This matters because per-signal endpoints **override** the generic one in Claude Code's exporter: without that ownership, a shell that already carried a per-signal collector endpoint would have received the full content stream — prompts, tool details, assistant text — around the sink, with your collector credentials attached.
- **If you had a per-signal endpoint pre-set, it is overridden and the launcher says so loudly on stderr.** Telemetry will **not** reach that host during a capture session. This is a deliberate refusal to ship content to a destination the capture cannot see; unset the variable and re-run if you wanted that collector fed.
- **The one network path out is the tee, and it takes a deliberate `HARNESS_FORWARD_OTEL=1`.** A pre-set generic `OTEL_EXPORTER_OTLP_ENDPOINT` on its own does **not** open it: an endpoint inherited from a shell profile is not consent to send prompts anywhere, so without the knob the capture runs entirely locally and prints one line naming the endpoint it did not use. With the knob, the sink forwards a copy of every export to that collector on the fixed `/v1/{logs,metrics,traces}` path, using the headers you had set (held in the launcher's memory, never persisted), with HTTP redirects disabled so the credentialed request cannot be bounced to a third host — and a warning naming the destination is printed **before `claude` starts**, while you can still stop it. Those exports are **content-enriched**: the launcher turns `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` and `OTEL_LOG_ASSISTANT_RESPONSES` on, so your collector receives prompt text, tool parameters and assistant response text that a default Claude Code session would not have sent it.
- **`OTEL_LOG_ASSISTANT_RESPONSES` is set explicitly** (default `1` — the capture is deliberately maximal; every channel is recorded at full content) rather than left to inherit Claude Code's default, which follows `OTEL_LOG_USER_PROMPTS`. Set it to `0` to keep assistant text out of the OTEL channel; the raw `bodies/` still contain every response.

## What lands on disk, file by file

A capture directory is `captures/<ts>/`; the derived transcript is `sessions/<ts>/`. The inventory below is what `harness-capture` writes; every file in it can contain content from your session. (`bin/claude-capture` writes a subset — `bodies/` and a smaller `capture-env.txt` that lists only telemetry-configuration variables, and records only flag names plus an argument count, never the prompt text.)

Inside a capture directory:

- **`bodies/*.request.json`, `bodies/*.response.json`** — complete Anthropic Messages API payloads: the system prompt, tool schemas, every message, tool inputs and outputs, assistant text, and `metadata.user_id` (a JSON-encoded object carrying the session id and account identifiers).
- **`otel/logs-*.json`, `otel/metrics-*.json`, `otel/traces-*.json`** — OTLP exports as received. Their resource attributes include **`user.email`, `organization.id`, `user.account_uuid`, `session.id` and `terminal.type`** — Claude Code stamps these on every export, and the sink stores them verbatim, because a capture that edited them would not be a capture. The log records themselves carry prompt text, tool parameters and assistant response text.
- **`otel/_requests.log`** — one line per request the sink handled: the signal, content type, encoding and byte count, or the reason a request was rejected. No payload and no request headers.
- **`hooks/hooks.jsonl`** — the verbatim stdin of every hook firing: prompt text (`UserPromptSubmit`), the full `tool_input` (`PreToolUse`), `cwd`, `transcript_path`, and instruction-file paths.
- **`hook-settings.json`** — the generated `--settings` document: one passive hook command per event, naming the interpreter and the recorder through environment variables rather than literal paths. No session content.
- **`capture-env.txt`** — every `CLAUDE_CODE_*` / `OTEL_*` variable **name** the child saw, plus values for an allow-list of harmless provenance keys (exporter names, protocols, export intervals, the content flags, the sink endpoints after the launcher rewrote them). Every other value is `<redacted>`, and `argv` is recorded as flag names plus an argument count.
- **`manifest.json`** — run provenance: capture timestamp, session id, the sink's port, `argv` flag names, the exit code, and channel-presence counts. Timestamps are local time, so a capture id reflects the machine's clock and timezone.

In the derived transcript directory:

- **`sessions/<ts>/normalized.json`** — the full re-encoding: `session.account_uuid`, `session.cwd`, the text of every object (prompts, file contents, tool results, assistant output), and the hook payloads.
- **`sessions/<ts>/sessfs/objects/`** — a **second full-text copy** of the same content, one file per content-addressed block, alongside the per-inference manifests that reference them.

`captures/<ts>/` and `sessions/<ts>/` are created mode `0700`, and the files the launcher and the sink write into them — OTLP exports, `hooks.jsonl`, `capture-env.txt`, `manifest.json`, `normalized.json` — mode `0600`. The raw `bodies/*.json` are written by Claude Code's own file sink under your umask while the session runs, inside the `0700` directory, and `harness-capture` sets them to `0600` when the session ends (`bin/claude-capture` cannot, because it `exec`s `claude` — its bodies keep your umask). The projected `sessfs/` tree is `0700` with `0600` files, like the rest of `sessions/<ts>/`, which is what stops another local account from reaching it. That is a defence against other local accounts and nothing more: no file is encrypted, and anyone who can read your home directory can read a capture.

There is no redacting export. Nothing in `captures/` or `sessions/` is safe to share as-is, and no flag makes it safe.

## Harness and account metadata

Separately from the session content, a capture identifies **you** and your environment. `user.email`, `organization.id` and `user.account_uuid` arrive on the OTLP resource attributes; `session.account_uuid` and `session.cwd` are copied into `normalized.json`; `terminal.type` names your terminal emulator; `manifest.json` and the body files are timestamped. Redaction in `capture-env.txt` does **not** control any of that — it only governs what the environment dump reveals, and the identity fields reach `otel/*.json` by a different route. Treat a capture as identifying its author.

## Rules

- **Never commit, sync, publish, or share `captures/` or `sessions/`.** Both are gitignored in this repo. Treat them exactly as you would treat a credentials file: local, unshared, and deleted when you no longer need them.
- **If you move either directory out of the repo, you lose the gitignore.** `CLAUDE_CAPTURE_DIR` and `HARNESS_SESSIONS_DIR` point the launcher anywhere you like, including into another repository's working tree, where nothing stops a `git add -A` from committing a capture. If you relocate them, add the destination to that repository's ignore rules yourself, or keep it outside version control entirely.
- **Never attach session *content* — prompt text, file contents, tool inputs or outputs, or identity fields — to an issue, a pull request, a test fixture, or a document.** That covers a whole capture, a whole `normalized.json`, and any excerpt of either that carries such content. If you need to demonstrate something, build a synthetic input (see `tests/conftest.py`). Token counts, timings and dollar amounts are **not** content: they are integers about a session, not anything from inside it, and they may be cited — `docs/cost.md` and `tests/test_pricing.py` both quote real observed token counts to pin the cost derivation, and say so.
- **Review `captures/<ts>/capture-env.txt` before sharing anything derived from a capture.** It records the `CLAUDE_CODE_*` and `OTEL_*` environment the child process saw. Variable **names** are always written — so you can still see *that* a header or token was set — but a value is written only when its name is on the allow-list of harmless provenance keys; everything else is `<redacted>`. The endpoint values you will see are the launcher's own rewritten sink URLs (userinfo stripped); the collector you teed to is not in this file at all — it was removed from the child's environment. Values that name the capture directory (`OTEL_LOG_RAW_API_BODIES`) are written with the path replaced by the literal `<capture-dir>`, so the file does not disclose your home directory, username or project path; `bin/claude-capture` does the same. The file records which mode produced it.
- **`HARNESS_ALLOW_ENV_SECRETS=1` opts out of that redaction** and writes every value verbatim, plus the full `argv` rather than flag names — in `capture-env.txt` and in `manifest.json` alike. It exists for debugging a collector configuration. The sink's bearer token is never written even then. Only set it when you intend to, and remember that the resulting capture holds live credentials.
- **Capture only what you mean to.** A session run in a directory holding production secrets will record them. Prefer a scratch working directory when you are capturing for demonstration or debugging purposes.
- **The bash wrapper `bin/claude-capture` has no sink of its own.** It writes raw bodies to disk via Claude Code's `file:` sink, and it does **not** default to a collector endpoint. With no `OTEL_EXPORTER_OTLP_ENDPOINT` set it turns the OTLP exporters off, so the metrics/events channel is simply absent and nothing leaves the machine. If you *do* set an endpoint, it warns you: the wrapper turns the content flags on, so full prompts and tool details go to whatever is listening there — including a Datadog, Grafana or Jaeger agent that happens to be running locally. Prefer `harness-capture`, which owns the destination and authenticates its sink.

## A deliberate behavioural change

`harness-capture` sets **`CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1` by default**. This is a change to how Claude Code behaves during a capture, and it is worth stating plainly rather than burying: it disables the harness's *automatic mid-thread model switch* — the same thing the documented `/config → MODEL & OUTPUT` toggle (`switchModelsOnFlag`) does when you turn it off. The safeguard itself still fires; the turn **pauses** instead of continuing on a different model. The reason is provenance: a session that silently changes model mid-thread is not a single-model capture, and every downstream cost and lineage claim would be quietly wrong. Set `HARNESS_ALLOW_MODEL_FALLBACK=1` to restore the stock behaviour.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:

**<https://github.com/mandersogit/harness-telemetry/security/advisories/new>**

Please do not open a public issue for a security problem, and please do not include a real capture, transcript excerpt, or credential in the report — describe the shape of the problem and provide a synthetic reproduction.

There is no security contact email; the advisory form above is the only reporting channel.

Expect an acknowledgement on a best-effort basis: this is a personal pre-alpha research project, not a supported product, and there is no service-level commitment.

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | Yes — fixes land on `main` only |
| Anything else | No |

The project is **pre-alpha**. There are no releases, no tags, and no backports; the only supported version is the current `main` branch.
