# normalize — capture → normalized session → session-filesystem tree

Turns a Claude Code capture into the [`events.schema.json`](../../../schema/events.schema.json) contract — the normalized event vocabulary an unreleased companion project (session-fs), which models a session as a filesystem, consumes. A normalized session = **session metadata + a content-addressed object store (block-level) + an append-only event log**; the atomic unit is the inference call.

## Pipeline

```text
harness-capture [claude args]  →  captures/<ts>/{bodies/, otel/, manifest.json}   (in-process OTLP sink)
                               →  auto-finalize on exit  →  sessions/<ts>/{normalized.json, sessfs/}
  (or piecewise: bin/claude-capture + harness-otlp-sink → harness-normalize → harness-project)
  verification:  harness-normalize <capture> --check       (conservation vs the capture dir)
                 harness-validate --roundtrip --capture …  (reconstruct + semantic diff vs raw bodies)
```

## The four ingested channels (additive, `source`-tagged, degrade gracefully)

| Channel | Source | Needs |
| --- | --- | --- |
| bodies | `captures/<ts>/bodies/*.json` | `claude-capture` or `harness-capture` |
| transcript | `~/.claude/projects/<cwd>/<sid>.jsonl` (auto-found) | a real session |
| OTEL | `captures/<ts>/otel/` | a collector (in-process in `harness-capture`) |
| hooks | `captures/<ts>/hooks/hooks.jsonl` | `harness-capture` (passive listener via `--settings`; opt out `HARNESS_NO_HOOKS=1`) |

What each contributes:

- **bodies** — the content-addressed objects + the `inference` manifests (the exact assembled context of every call).
- **transcript** — the `message` tree/DAG + `harness_event` bookkeeping.
- **OTEL** — per-inference duration, `prompt_id` and `agent.name`; the exact pairing/ordering anchors (`body_ref`, `event.sequence`); the cost cross-check; the event zoo (`tool_decision`, `hook_*`, …).
- **hooks** — verbatim `hook` events, i.e. the harness state machine: session/prompt/turn boundaries, tool pre/post with full `tool_input`, `InstructionsLoaded` (CLAUDE.md provenance), compaction boundaries, permission decisions. Injected only when the user passed no `--settings` of their own.

With bodies only you still get objects + inferences; each added channel fills more. `session.captured_with` records which channels contributed records to this document — `raw_bodies` is true when at least one request body was loaded, `hooks` when at least one hook record was ingested. It is evidence in the document, not configuration: a channel that was enabled but produced nothing reads as false. Of the OTEL channel's contributions, only **per-inference latency** and the **decision/compaction/error events** are OTEL-exclusive as far as we have verified — with one open caveat on latency: the transcript's `system` / `turn_duration` records carry a `durationMs` that has not yet been reconciled against OTEL's `duration_ms`, so a transcript-only latency may turn out to be recoverable. **Cost is NOT OTEL-only** — it is **derived from tokens** by [`pricing.py`](pricing.py) (the API delivers no price; CC's own `cost_usd` is a client-side list-price estimate, kept here only as `cost_usd_reported`). **Tokens / prompt_id** are also present in the bodies/transcript (verified — see [`docs/claude-code.md`](../../../docs/claude-code.md) "What needs the OTEL channel" and [`docs/cost.md`](../../../docs/cost.md)).

## Cross-channel join keys (verified)

- **`session_id`** — bodies' `metadata.user_id.session_id` == OTEL `session.id` == transcript filename.
- **`request_id`** (`req_…`) — OTEL `api_request.request_id` == response-file name (`req_<id>.response.json`) == transcript `assistant.requestId`. NOT the response body's `id` (that's the `msg_…` message id).
- **`prompt.id`** — groups OTEL events from one user prompt. **`body_ref`** — OTEL→on-disk-body pointer; every OTEL event carries a monotonic **`event.sequence`**.
- **Chain keys (discovered 2026-07-02, the pairing/lineage backbone):** a request names its predecessor's response twice — `diagnostics.previous_message_id` (the `msg_…` id) and, on CC ≥ 2.1.198, `cc_prev_req=<req_… id>` inside the `system[0]` billing header. Since `req_…` ids ARE response filenames, these chain request files to response files exactly. Both are per-request correlation riding inside the request (lifted out of the lineage hash, kept verbatim for round-trip).

## Usage

```sh
# 0. the one-command path: run claude under capture; sessions/<ts>/ appears on exit
harness-capture -p "…"            # or interactive: harness-capture

# 1. capture (bodies only, zero-dependency bash wrapper)
bin/claude-capture -p "…"

# 1b. capture WITH the OTEL channel (metrics/events). claude-capture does NOT
#     default to a collector, so name the sink explicitly. The standalone sink is
#     UNAUTHENTICATED (it says so on startup) — local debugging only.
local.venv/bin/harness-otlp-sink --out /tmp/otel --port 4318 &
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  OTEL_EXPORTER_OTLP_PROTOCOL=http/json bin/claude-capture -p "…" --allowedTools Bash
#   then move the sink output into the capture dir as  captures/<ts>/otel/
#   (harness-capture runs the sink in-process, authenticated, and writes there directly)

# 2. normalize (auto-joins transcript + otel/ if present)
local.venv/bin/harness-normalize captures/<ts> --out normalized.json --check
#   NOTE: a bare --out path lands in the CURRENT directory. captures/, sessions/ and a
#   root-level normalized.json/merged.json are gitignored here, but a --out path anywhere
#   else is not — and the normalized doc is exactly as sensitive as the capture.
#   Prefer writing under sessions/<ts>/.
#   flags: --no-transcript | --transcript PATH | --no-otel | --otel DIR
#          --discount 0.10   (a user-supplied discount fraction on derived cost,
#                             in [0, 1); or HARNESS_COST_DISCOUNT)

# 2b. resume lifecycle: several captures of ONE session (original + --resume
#     continuations) stitch into one logical session — chains cross the boundary,
#     conservation runs over the union, session.captures records the parts
local.venv/bin/harness-normalize captures/<orig> captures/<resume> --out merged.json --check

# 2c. live: keep sessions/<ts>/ current WHILE the session runs
local.venv/bin/harness-watch captures/<ts> [--interval 1.0] [--once] [--out DIR]
#   several capture dirs = the resume lifecycle, live. Each debounced update
#   re-runs the batch normalizer (~78 ms at 40 turns), so incremental == batch
#   BY CONSTRUCTION and there is no per-call join gate to hang on message-less
#   calls. Settle debounce; atomic normalized.json replace; whole-dir sessfs
#   swap (no stale seq-named log files).

# 3. round-trip into the session-filesystem tree (proves consumability)
local.venv/bin/harness-project normalized.json --out /tmp/sessfs --verify

# 4. validate against the schema; --roundtrip reconstructs every request and
#    response from the doc alone and semantic-diffs them vs the raw bodies
local.venv/bin/harness-validate normalized.json
local.venv/bin/harness-validate --roundtrip --capture captures/<ts> normalized.json
#   --capture is repeatable for a merged doc; each raw body resolves against its
#   own capture (raw_bodies.*.capture names it)
```

## Files

- `claude_code.py` — the Claude Code adapter (the four ingested channels above); entry point `harness-normalize`. `--check` runs conservation invariants against the capture dir (every body file accounted for, unmatched named). **Event ordering** is a defined total order, not channel concatenation: sort by `ts`; an event without a `ts` inherits the nearest preceding `ts` within its own channel (leading ones sort first); ties break by channel rank, then by position within that channel. `seq` is assigned from that order.
- `pairing.py` — request↔response binding, tiered. The tier that bound a pair is recorded verbatim as `inference.pairing`, whose schema enum is exactly these six values plus `null`, listed here in try order (the exact chain tiers run first, per lineage; the OTEL window only sees what they left unbound):
  - `request-id-chain` — the next request's `cc_prev_req` names this response's `req_…` id (exact).
  - `message-id-chain` — the next request's `diagnostics.previous_message_id` names this response's `msg_…` id (exact).
  - `successor-embedding` — a response's block hashes all reappear in its lineage's next request.
  - `otel-window` — replay the exporter's `event.sequence`; same-model pile-ups disambiguated by call-start time (`api_request.ts − duration_ms`); declines rather than guesses when no start time or two candidates tie.
  - `elimination` — one request and one candidate response are the only ones left in a model group (or the final 1:1 leftover across model groups).
  - `token-affinity` — several left over: rank requests by size against responses by input-token total.
  - `null` — a request with no response at all. Unpaired requests become response-less inference events; nothing is dropped.
- `reconstruct.py` — rebuilds every request (and response content) from the normalized doc ALONE; the semantic-diff oracle behind `--roundtrip`.
- `../capture.py` — the launcher (entry point `harness-capture`): env-config, verbatim argv passthrough, in-process OTLP sink, finalize-on-exit into `sessions/<ts>/`.
- `pricing.py` — derives per-inference `cost_usd` from token usage × a list-price table (`(model, speed) → rates`, × discount). Used by `claude_code.py`; standalone library. See [`docs/cost.md`](../../../docs/cost.md).
- `project.py` — renders a normalized doc into the session-filesystem tree: `objects/` + `log/` + `inferences/NNNN/{manifest.json, context/}` (relative symlinks); `--verify` round-trips it; entry point `harness-project`.
- `watch.py` — the live watcher: poll → settle → re-normalize → atomic write + sessfs swap; entry point `harness-watch`. Never crashes on a body caught mid-write (parse error = skip tick, previous output stays); SIGINT does a final refresh and exits 0.
- `validate.py` — validates a normalized doc against `events.schema.json` (uses `jsonschema` if present, else falls back to the structural invariants); entry point `harness-validate`. The schema is loaded from **package data** (`harness_telemetry/schema/events.schema.json`), so it is present under any install — wheel, sdist or editable — and not only in a clone; the repo-root `schema/events.schema.json` is the path-stable public location naming the same file.
- `../collector/sink.py` — minimal local OTLP/HTTP receiver (chunked + gzip) for the OTEL channel; entry point `harness-otlp-sink`.

## Sensitivity

`normalized.json` carries `session.account_uuid` and `session.cwd` verbatim (real identifiers), and `objects/` hold the block text of every prompt, tool result and file that passed through the session. `sessions/` therefore inherits exactly the status of `captures/`: treat it like credentials — never share it, never commit it.

## What's implemented

- **Four-channel normalizer** (bodies + transcript + OTEL + hooks) with exact chain pairing, plus a conservation check (`--check`) that accounts for every body file in the capture dir. Every request becomes an inference — paired, or explicitly response-less; nothing is dropped.
- **Per-lineage preludes** — the system/tools prefix is stored once per lineage rather than per call, with honest agent labels (an OTEL-attested `subagent:<name>`, never a fabricated one).
- **Reconstruct + round-trip validation** — every request and response is rebuilt from the normalized doc alone and semantic-diffed against the raw bodies (`harness-validate --roundtrip`), so a capture gap shows up as a diff.
- **The `harness-capture` launcher** — runs `claude` with an in-process OTLP sink and passive hook injection, then finalizes into `sessions/<ts>/` on exit.
- **Live watcher** — `harness-watch` keeps `sessions/<ts>/` current while the session is still running; the final document equals the one-shot batch run.
- **Resume / multi-capture merge** — several capture dirs of one logical session (an original run plus its `--resume` continuations) stitch into a single document; chains cross the capture boundary and conservation runs over the union.
- **Sub-agent and workflow ingestion** — the transcript's `<sid>/` sibling tree becomes per-agent message DAGs tagged with `agent_id` / `workflow_id`, spliced back to the spawning `Task` tool_use, plus the workflow run records and scripts.
- **Instance identity** — `inference.instance_id` distinguishes parallel same-type agents while a resumed thread keeps one identity; `lineage_id` stays the prelude *kind*.
- **Cost derived from tokens**, never ingested; the harness's own reported figure is kept alongside as a cross-check (`cost_usd_reported`). See [`docs/cost.md`](../../../docs/cost.md).
- **Telemetry additivity, on request** — the launcher owns every OTLP destination (the generic endpoint and all three per-signal ones) so nothing routes around the sink, and the sink requires a per-capture bearer token. A pre-set *generic* `OTEL_EXPORTER_OTLP_ENDPOINT` is still treated as YOUR collector, but feeding it is opt-in: with `HARNESS_FORWARD_OTEL=1` the sink tees every export onward to it on the fixed signal path, with the headers you had set, instead of stealing the stream (`harness-otlp-sink --forward URL` does the same standalone, and refuses a URL that resolves to its own bind). Without the knob the endpoint is named on stderr and left unused — the exports are content-enriched, so inheriting the variable is not consent. A pre-set *per-signal* endpoint is overridden with a warning — telemetry will not reach it during a capture.

Open, pending a decision: `session.model` semantics — currently the main lineage's root model, with dominant/final not yet chosen.
