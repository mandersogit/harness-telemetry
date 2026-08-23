# harness-telemetry

*Not affiliated with, endorsed by, or sponsored by Anthropic, OpenAI, or any harness vendor; product names are used descriptively.*

`harness-capture` runs the real `claude` CLI on your own machine with every telemetry channel it offers turned on, records what passes through it to `captures/<ts>/`, and on exit writes an **enriched transcript** — `sessions/<ts>/normalized.json` plus a projected `sessions/<ts>/sessfs/` tree — in which every API call is one event with its exact assembled context, token usage, and derived cost.

**Requirements:** the Claude Code CLI (`claude`) on `PATH` — `harness-capture` checks before it creates anything and exits 127 with a message if it is missing — plus CPython **≥ 3.11** and macOS or Linux (the launcher and the hook recorder are POSIX-only; `bin/` targets bash 3.2). `jq` is needed for `bin/captures`, and Node (`markdownlint-cli2`) for `make lint-md`; neither is needed to capture or normalize.

**Scope, honestly:** this is **pre-alpha** research code. **Claude Code is the only harness with an adapter**; [`docs/codex.md`](docs/codex.md) and [`docs/pi.md`](docs/pi.md) are transcript-only stubs with no code behind them. Interfaces, the schema, and the CLI surface can change without notice.

> **Sensitivity notice:** `captures/` and `sessions/` contain everything the model saw and said — prompts, file contents, tool outputs, your account identifiers, and potentially secrets that were in scope during the session. Both are gitignored here; treat them like credentials (do not commit, sync, or share them). Read [`SECURITY.md`](SECURITY.md) before you run a capture.

---

How agentic coding harnesses expose their own session/internals — and how that is the **same or varies** across harnesses (Claude Code, codex-cli, pi, …).

This is a comparative, cross-harness study of the **capture surface**: what each tool emits, through which channels, with what fidelity, defaults, and version drift. The concrete output is a normalizer that turns a real captured session into a **normalized event vocabulary** defined here — the same vocabulary an unreleased companion project (session-fs) consumes.

- **Here (telemetry-first):** cost/usage metrics, productivity metrics, perf, error rates, the metrics-vs-events architecture, privacy defaults, and the headline question *"does each harness even expose this?"*
- **The overlap with a session model:** faithful per-inference capture (request/response bodies + the inference event stream) reduced to one schema — `schema/events.schema.json`.

## Quickstart

```sh
PYTHON=python3.11 make install   # creates local.venv with the interpreter you name
make all                         # gates: ruff + mypy strict + pyright + pytest

# run a real Claude Code session under full capture; on exit an
# enriched transcript lands in sessions/<ts>/{normalized.json, sessfs/}
local.venv/bin/harness-capture                     # interactive
local.venv/bin/harness-capture -p "do a thing"     # headless

# keep sessions/<ts>/ current WHILE a session runs (poll -> settle -> re-normalize)
local.venv/bin/harness-watch captures/<ts>

# re-derive / verify anything after the fact
local.venv/bin/harness-normalize captures/<ts> --check          # conservation vs the capture
local.venv/bin/harness-validate --roundtrip --capture captures/<ts> sessions/<ts>/normalized.json

# a resumed session is several captures of one logical session — merge them
local.venv/bin/harness-normalize captures/<orig> captures/<resume> --check
```

`PYTHON` is required and has no default: the venv is bootstrapped with exactly the interpreter you name (any CPython ≥ 3.11), so `make install` never silently picks a different one. It is consulted only when `local.venv/` does not exist yet; every other target uses `local.venv/bin/*`.

Details, flags, and the channel model: [`src/harness_telemetry/normalize/README.md`](src/harness_telemetry/normalize/README.md). Hooks: [`docs/hooks.md`](docs/hooks.md). Cost: [`docs/cost.md`](docs/cost.md). Terms: [`docs/glossary.md`](docs/glossary.md).

Knobs that change what is captured or where it lands:

- **`HARNESS_ALLOW_ENV_SECRETS=1`** — write every `CLAUDE_CODE_*` / `OTEL_*` environment value into `captures/<ts>/capture-env.txt` verbatim, and the full `argv` into both that file and `manifest.json`. By default every such variable **name** is recorded but a value is written only when the name is on an allow-list of harmless provenance keys (exporter names, protocols, export intervals, the content flags, the launcher's own sink endpoints); every other value is `<redacted>`, and `argv` is reduced to flag names plus an argument count. The file records which mode produced it. The sink's per-capture bearer token is never written in either mode.
- **`HARNESS_NO_HOOKS=1`** — skip hook injection entirely. Otherwise `harness-capture` injects a passive hook listener via `--settings` (and skips injection automatically if you passed your own `--settings` — yours always wins).
- **`HARNESS_ALLOW_MODEL_FALLBACK=1`** — restore Claude Code's automatic mid-thread model switch. By default `harness-capture` sets `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`, which disables that automatic switch — the same effect as turning off the documented `/config → MODEL & OUTPUT` toggle (`switchModelsOnFlag`). The safeguard still fires; the flagged turn pauses instead of continuing on a different model, so a captured session is single-model by construction. This is a deliberate change to the harness's behaviour during a capture; [`SECURITY.md`](SECURITY.md) says so too.
- **`HARNESS_FORWARD_OTEL=1`** — forward (tee) a copy of every OTLP export onward to the collector named by a pre-set generic `OTEL_EXPORTER_OTLP_ENDPOINT`. **Off by default, and inheriting the endpoint variable is not enough to turn it on:** `harness-capture` sets `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` and `OTEL_LOG_ASSISTANT_RESPONSES`, so a teed export carries prompt text, tool parameters and assistant response text that a default Claude Code session would never have sent there. Without the knob the capture still runs normally and one stderr line names the endpoint it did not use; with it, a warning naming the destination is printed before `claude` starts. See [`SECURITY.md`](SECURITY.md).
- **`HARNESS_COST_DISCOUNT`** — a user-supplied discount fraction in `[0, 1)` applied to derived cost (`0.10` bills at ×0.90). Same as `harness-normalize --discount`.
- **`HARNESS_SESSIONS_DIR`** / **`CLAUDE_CAPTURE_DIR`** — base directories for the enriched transcript and for the capture. All three tools (`harness-capture`, `bin/claude-capture`, `bin/captures`) default to `./sessions` and `./captures` **under the directory you run them from**; set either variable to put them elsewhere (a directory outside this repo is not covered by its `.gitignore` — treat it like credentials).

## Capture channels (the lenses)

A harness can expose its session through several channels; this project catalogs each per harness. The normalizer **ingests four** of them — the raw request/response bodies, the on-disk transcript, OpenTelemetry metrics and events, and hooks. Those are rows 1–3 below: the bodies are configured through the OTEL channel but arrive on disk without a collector, so the normalizer counts them as a channel of their own. Rows 4 and 5 are **catalogued only** — documented here, not consumed by any adapter.

1. **On-disk transcript** *(ingested)* — the session log the harness writes locally (Claude Code JSONL, codex rollout JSONL, pi `sessions/*.jsonl`).
2. **OpenTelemetry** *(ingested)* — metrics + events, and the **raw request/response bodies** the same channel can write untruncated to disk. The normalizer treats those bodies as a channel of their own, since they arrive without a collector.
3. **Hooks** *(ingested)* — lifecycle callbacks that can capture tool I/O and prompt boundaries.
4. **API proxy** *(catalogued only)* — interposing on the model HTTP traffic (`ANTHROPIC_BASE_URL` etc.) for full request bodies. Redundant here, because Claude Code emits the bodies natively.
5. **Statusline** *(catalogued only)* — Claude Code pipes a JSON snapshot of the session to a configured `statusLine` command on every render. It carries the session-cumulative figures the harness itself displays — estimated cost, wall-clock and API duration, lines added/removed, context-window totals — all computed in memory and **never persisted to disk by the harness**, so a statusline command is the only way to observe them. It is session-total only: **per-inference latency** still needs the OTEL channel, while per-inference **cost is derived from tokens** here and needs no channel beyond the response bodies ([`docs/cost.md`](docs/cost.md)).

## Example output

One inference event from a `normalized.json`, produced from a synthetic one-call capture (`harness-normalize <capture> --check`). Content lives in the content-addressed `objects` store, the event refers to it by hash, and `usage`/`cost_usd` are per inference:

```json
{
  "type": "inference",
  "ts": "2026-01-01T00:00:01.000Z",
  "ts_source": "file-mtime",
  "source": "raw_body",
  "index": 0,
  "agent": "main",
  "lineage_id": "f65d09f15452",
  "instance_id": "0001",
  "model": "claude-opus-4-8",
  "correlation": {
    "client_request_uuid": "0001",
    "server_request_id": "req_demo1",
    "server_request_id_kind": "anthropic_request_id",
    "message_id": "msg_demo1"
  },
  "context": [
    { "ref": "668c7c83f82f39d1…", "role": "system" },
    { "ref": "a4891f85d2930d35…", "role": "user", "message_index": 0 }
  ],
  "usage": {
    "input_tokens": 12,
    "output_tokens": 34,
    "cache_read_tokens": 21504,
    "cache_creation_tokens": 1280,
    "service_tier": "standard"
  },
  "cost_usd": 0.019662,
  "cost_usd_reported": null,
  "duration_ms": null,
  "stop_reason": "end_turn",
  "thinking": { "present": false, "persisted": "none", "signature_present": false },
  "pairing": "elimination",
  "seq": 0
}
```

Two things are not shown as they appear on disk: sha-256 refs are **abbreviated** here, and some fields are **elided for brevity** — the emitter always writes `attempt`, `parent_index`, `prelude`, `request_body_ref`, `response_body_ref` (and, on paired inferences, `response_block_refs`), and, inside `correlation`, `previous_message_id`, `previous_request_id` and `prompt_id`. A real event carries all of them.

`cost_usd` is derived from the token buckets and a dated list-price table, never ingested from the harness; `cost_usd_reported` holds the harness's own estimate when the OTEL channel supplied one, purely as a cross-check ([`docs/cost.md`](docs/cost.md)).

## Layout

```text
docs/
  claude-code.md    # transcript + OTEL channels, capture ladder
  codex.md          # rollout JSONL format
  pi.md             # sessions/<cwd>/*.jsonl                    (unverified stub)
  comparison.md     # the same/varies matrix
  hooks.md          # the hook channel: events, matchers, the injected listener
  cost.md           # why cost is derived from tokens, not ingested
  glossary.md       # the vocabulary used across these docs
bin/
  harness-capture   # shim to the Python launcher (uses local.venv)
  claude-capture    # opt-in bodies-only capture wrapper for Claude Code (bash, zero-dependency)
  captures          # explorer over captures/ (ls/show/calls/messages/…)
  guardrail-trips   # audit a transcript for harness-initiated mid-thread model changes
src/harness_telemetry/
  capture.py        # the launcher: env, in-process sink, hook injection, finalize (harness-capture)
  hookrec.py        # the passive hook recorder invoked by the injected listener
  normalize/        # raw harness output -> the common event schema (harness-normalize/-validate/-project)
  collector/        # local OTLP/HTTP capture sink (harness-otlp-sink)
schema/             # events.schema.json — the normalized event vocabulary (the
                    #   canonical copy ships as package data; this is the stable path)
tests/              # pytest; synthetic inputs only
.github/            # CI workflow + dependabot
Makefile            # install / test / lint / typecheck / lint-md — see `make help`
CLAUDE.md           # guide for coding agents working in this repo
CONTRIBUTING.md     # dev setup, quality gates, how to add a harness adapter
SECURITY.md         # what this tool captures by design; how to report a vulnerability
LICENSE             # MIT
```

## Status

**Pre-alpha** (last updated 2026-08-21). Usable for its own purpose — capturing and normalizing Claude Code sessions — but the interfaces, the schema, and the CLI surface may still change without notice.

Implemented:

- **Capture launcher** (`harness-capture`) — runs `claude` with full telemetry on, an in-process authenticated OTLP sink, and an injected passive hook listener; finalizes an enriched transcript on exit.
- **Four-channel normalizer** — raw bodies + on-disk transcript + OTEL + hooks reduced to one schema, with a conservation check that every input body is accounted for in the output.
- **Reconstruct + round-trip validation** — every request and response is rebuilt from the normalized document alone and semantically diffed against the raw bodies.
- **Live watcher** (`harness-watch`) — keeps `sessions/<ts>/` current while a session is still running.
- **Resume / multi-capture merge** — several captures of one logical session stitch into a single normalized document.
- **Sub-agent and workflow ingestion** — the per-agent transcript trees, their spawn linkage, and workflow metadata.
- **Cost derived from tokens** with the harness's own reported figure kept only as a cross-check.

Caveats:

- Findings and behaviors were **verified against Claude Code 2.1.195–2.1.198 between 2026-06-27 and 2026-07-02 and have not been re-verified since, except the OTLP endpoint/header behaviour the launcher depends on, which was checked against 2.1.239 on 2026-08-21.** The project's own finding is that three minor versions changed the wire shape, so treat version-specific claims accordingly.
- `docs/codex.md` and `docs/pi.md` are **transcript-only stubs**; no codex-cli or pi adapter exists yet.
- Public CI runs the full test suite against synthetic inputs, and that suite does exercise the real invariants — conservation against a capture directory, hook-line conservation, the pricing derivation, and the reconstruct primitives. **What it cannot exercise is the two oracles that need a genuine session:** a capture-vs-document round trip against real raw bodies, and the `cost_usd` vs `cost_usd_reported` cross-check against the harness's own figure. Those run only against the maintainer's local captures, which cannot be distributed.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, the quality gates, and how to add a harness adapter.

## License

MIT — see [`LICENSE`](LICENSE).
