# Claude Code — session-data exposure

> **Last verified against Claude Code 2.1.198 on 2026-07-02** (2.1.195 for the June findings), and **not re-verified since** (one exception: the OTLP endpoint/header precedence the launcher depends on was checked against 2.1.239 on 2026-08-21 — see § Channel 2). This doc's own central finding is that three minor versions changed the wire shape (see **§ Version drift** below), so treat every version-specific claim here as a dated observation rather than current behavior, and re-check anything load-bearing against the release you are running.

What Claude Code exposes about a session, across two channels: the **on-disk transcript** and **OpenTelemetry**. Researched 2026-06-27 against **Claude Code v2.1.195**; OTEL details from the official docs at `code.claude.com/docs/en/monitoring-usage.md`. Treat metric/event/attribute names as version-dependent — and see **§ Version drift (v2.1.198)** below: the capture surface measurably changed within three minor versions.

---

## Channel 1 — on-disk transcript (JSONL)

Each session is appended live to `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. Sub-agents get their own transcripts under `<session-uuid>/subagents/agent-<id>.jsonl` (+ `.meta.json`), and workflow agents under `<session-uuid>/subagents/workflows/<wf>/`.

Observed structure (from probing a real 510-record session):

- **Record `type` values:** `user`, `assistant` (the message-bearing ones), plus harness bookkeeping: `ai-title`, `attachment`, `file-history-snapshot`, `last-prompt`, `mode`, `permission-mode`, `queue-operation`, `system`. `system` records carry their own inner `subtype` — observed: `turn_duration`, `compact_boundary`, and **`model_refusal_fallback`** (v2.1.198, discovered 2026-07-02: a model's safeguards flag a message and CC **silently switches the session to another model** — a harness-initiated mid-thread model change, recorded only here; the record carries `originalModel`/`fallbackModel`/`trigger`/`refusedUserMessageUuid`). **Disabling the silent switch** (verified against 2.1.198 — the auto-switch is gated by the `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK` env var *and* the `switchModelsOnFlag` setting, so either off-switch suffices): set `"switchModelsOnFlag": false` in `settings.json` (default `true`; the `/config → MODEL & OUTPUT` toggle writes it) **or** export `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1` — either makes a flagged turn **pause** instead of switching. Neither disables the safeguard itself; both disable only the automatic model change that follows it. **`harness-capture` exports the env var by default, and that is a deliberate change to the harness's behaviour during a capture** — the same change the documented `/config` toggle makes, chosen because a session that silently changes model mid-thread is not a single-model capture and every cost and lineage claim derived from it would be quietly wrong. Opt out with `HARNESS_ALLOW_MODEL_FALLBACK=1`; `bin/guardrail-trips` audits any transcript for trips, and `harness-normalize --check` flags a fallback as a **HYBRID-session** provenance problem.
- **Message records** carry `message.role` (`user`/`assistant`) and `message.content[]` blocks of type `text`, `thinking`, `tool_use`, `tool_result`.
- **Structural fields on message records:** `uuid`, `parentUuid` (→ the conversation is a linked **tree/DAG**), `timestamp`, `isSidechain`. Compaction is flagged via `isCompactSummary` (absent in an un-compacted session).

What the transcript gives you: the full message tree, tool inputs/outputs, sub-agent forks. What it does **not** give you: the exact assembled context per inference (system prompt text, tool schemas, cache breakpoints) — that needs OTEL raw bodies (below) or a proxy.

---

## Channel 2 — OpenTelemetry

Opt-in. Read at process start, so **must be set before launching** the session.

### Enable + exporters

| Var | Values / notes |
|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY=1` | master switch |
| `OTEL_METRICS_EXPORTER` | `otlp` \| `prometheus` \| `console` \| `none` |
| `OTEL_LOGS_EXPORTER` | `otlp` \| `console` \| `none` (events are "logs") |
| `OTEL_TRACES_EXPORTER` | `otlp` \| `console` \| `none` — requires `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` \| `http/json` \| `http/protobuf` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | collector URL, e.g. `http://localhost:4318`; per-signal: `OTEL_EXPORTER_OTLP_{METRICS,LOGS,TRACES}_ENDPOINT` |
| `OTEL_EXPORTER_OTLP_HEADERS` | e.g. `Authorization=Bearer …` |
| `OTEL_METRIC_EXPORT_INTERVAL` | ms (default 60000) |
| `OTEL_LOGS_EXPORT_INTERVAL` | ms (default 5000) |

**Per-signal variables win.** `OTEL_EXPORTER_OTLP_{LOGS,METRICS,TRACES}_ENDPOINT` **overrides** the generic endpoint for that signal, and per-signal `_HEADERS` **merge** with the generic ones (*official docs*). The per-signal endpoint is used verbatim, while the generic one gets `/v1/<signal>` appended (*observed behaviour of the Claude Code client, 2.1.239*). Consequence: anything that means to own where telemetry goes must own all four endpoint/protocol pairs, not just the generic one — a leftover per-signal endpoint in your shell silently routes the content flags below to that collector instead. `harness-capture` owns all four and warns when it rewrites one.

### Content flags (all default OFF — content is redacted by default)

| Var | Captures |
|---|---|
| `OTEL_LOG_USER_PROMPTS=1` | user prompt text (else only length) |
| `OTEL_LOG_TOOL_DETAILS=1` | tool parameters, bash commands, MCP/skill names, file paths (truncated ~4K) |
| `OTEL_LOG_TOOL_CONTENT=1` | full tool input/output in trace spans (needs tracing beta; 60 KB trunc) |
| `OTEL_LOG_ASSISTANT_RESPONSES=1` | assistant response text — **unset, it follows `OTEL_LOG_USER_PROMPTS`** (≥ 2.1.193) |
| `OTEL_LOG_RAW_API_BODIES=1` | **full Anthropic Messages API request+response JSON** inline (60 KB trunc) |
| `OTEL_LOG_RAW_API_BODIES=file:<dir>` | same, **untruncated to disk**, with a `body_ref` path in the event |

> Docs note: enabling `OTEL_LOG_RAW_API_BODIES` implies consent to everything the prompt/tool flags would reveal.

### Metrics

All carry resource attrs `session.id`, `organization.id`, `user.account_uuid`/`account_id`/`id`/`email`, `terminal.type`.

| Metric | Key attributes |
|---|---|
| `claude_code.session.count` | `start_type` (fresh/resume/continue/agents_view) |
| `claude_code.token.usage` | `type` (**input/output/cacheRead/cacheCreation**), `model`, + the source attrs¹ |
| `claude_code.cost.usage` | `model`, + same source/agent attrs |
| `claude_code.lines_of_code.count` | `type` (added/removed), `model` |
| `claude_code.commit.count` / `claude_code.pull_request.count` | — |
| `claude_code.code_edit_tool.decision` | `tool_name`, `decision` (accept/reject), `source`, `language` |
| `claude_code.active_time.total` | `type` (user/cli) |

¹ The source-attribution attrs: `query_source`, `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`.

### Events (logs)

All carry `prompt.id` (UUID linking every event from one user prompt) + resource attrs.

| Event | Key attributes |
|---|---|
| `claude_code.user_prompt` | `prompt` (if enabled), `prompt_length`, `command_name` |
| **`claude_code.api_request`** | `model`, `input/output/cache_read/cache_creation_tokens`, `cost_usd`, `duration_ms`, `request_id`, + source attrs¹ |
| **`claude_code.api_request_body`** | **`body`** (full request JSON; needs `RAW_API_BODIES`), `body_ref`, `body_length`, `model` |
| **`claude_code.api_response_body`** | **`body`** (full response JSON), `body_ref`, `body_length`, `request_id`, `model` |
| `claude_code.api_error` / `api_refusal` | `model`, `status_code`/`error`, `duration_ms`, `attempt`, `request_id` |
| `claude_code.tool_result` | `tool_name`, `tool_use_id`, `success`, `duration_ms`, sizes, `tool_input`/`tool_parameters` (if `TOOL_DETAILS`) |
| `claude_code.tool_decision` | `tool_name`, `tool_use_id`, `decision`, `source` |
| `claude_code.compaction` | `trigger`, `success`, `duration_ms`, **`pre_tokens`, `post_tokens`** |
| `claude_code.permission_mode_changed`, `auth`, `mcp_server_connection` | (config/system lifecycle) |
| `claude_code.plugin_loaded`, `skill_activated`, `hook_*`, `internal_error` | (config/system lifecycle) |

---

## Capture ladder (fidelity tiers)

| Tier | Closes | How |
|---|---|---|
| transcript JSONL | messages, tree/DAG, tool I/O, sub-agent forks | already on disk |
| + OTEL metrics/events | **uniquely** cost, latency, decision/compaction/error events | `CLAUDE_CODE_ENABLE_TELEMETRY=1` + exporters |
| + content flags | user-prompt text, tool params | `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` |
| **+ raw bodies** | **byte-level reproducibility** — system prompt, messages, tool schemas, `cache_control` | `OTEL_LOG_RAW_API_BODIES=file:<dir>` |
| proxy | network-level capture (redundant if raw bodies on) | `ANTHROPIC_BASE_URL` → logging proxy |

**Key finding:** `OTEL_LOG_RAW_API_BODIES` makes byte-level reproduction possible *from Claude Code itself* — a proxy is an alternative, not a requirement. Without it, OTEL is metrics + metadata only.

## What needs the OTEL channel (vs. available elsewhere)

Verified 2026-06-28 by capturing all three channels of one session and diffing them.

**Available without a collector** (in the raw bodies and/or the transcript): token usage (the transcript's `message.usage` — input/output/cache, `service_tier`, `speed`, per-`iterations` breakdown), `request_id`, prompt grouping (transcript `promptId` == OTEL `prompt.id`), entrypoint, `effort`, and all content (messages, assembled context, tool I/O).

**OTEL-only** (no collector → not captured): **latency** (`duration_ms`/`ttft` — the transcript's *message* records carry these fields but they are always null, headless *and* interactive; caveat below), **tool-decision provenance** (accept/reject + source — the transcript shows a tool *ran*, not the gate), **compaction metrics** (pre/post tokens, trigger), **api-error/refusal/retry** events, and the **aggregate productivity metrics** (active-time, lines-of-code, code-edit decisions, commit/PR counts). **`cost_usd`** is reported only by OTEL, but is *derivable* from the per-inference tokens × model rate (tokens are not OTEL-exclusive).

**Latency caveat (open).** "Latency is OTEL-only" is qualified in two directions. An API proxy — the deferred 4th channel — could also recover it (and compute cost), so the claim only holds among the channels in use here. More importantly, the transcript's `system` records with subtype `turn_duration` do carry a `durationMs`, which has **not** been reconciled against OTEL's per-inference `duration_ms`: it is turn-scoped rather than call-scoped, and whether it is derivable into per-inference latency is unresolved. Until that is checked, read the claim as "per-inference latency has only been *captured* from OTEL", not as a proof of impossibility. (Session-aggregate API duration is separately available from the statusline channel.)

## Correlation

Cross-channel joins, verified first-hand (2026-06-28):

- **`session.id`** (OTEL resource attr) equals the bodies' `metadata.user_id.session_id` and the transcript file name — ties all three channels to one session.
- **`request_id`** (`req_…`) is the per-inference join key: it is OTEL `api_request.request_id`, the **name of the response body file** (`req_<id>.response.json`), **and** the `requestId` on the transcript's `assistant` records — so transcript ↔ OTEL ↔ bodies all join on it. Caveat: the response body's own `id` field is the *message* id (`msg_…`), a different value — don't join on it.
- **`prompt.id`** groups every OTEL event from one user prompt (including retries).
- **`body_ref`** on `api_request_body` / `api_response_body` is the absolute path to the on-disk body file — a direct OTEL→body pointer.

## Open items / to verify

- **Resolved 2026-06-27 (verified):** `OTEL_LOG_RAW_API_BODIES=file:<dir>` writes untruncated `*.request.json` / `*.response.json` to disk **without** a live OTLP collector — the `file:` sink is independent of `OTEL_LOGS_EXPORTER` reaching a collector. (Metrics/events still need a collector; without one they drop silently — verified first-hand on 2026-06-27.)
- **Resolved 2026-06-28 (verified):** `request_id` (`req_…`) **does** appear in the transcript JSONL — on `assistant` records as `requestId` — so it is a clean transcript↔OTEL↔bodies join key (see Correlation above).

## Verified first-hand (2026-06-28)

The Channel-2 tables above were drafted from the official docs; an instrumented end-to-end capture (`src/harness_telemetry/collector/sink.py` + `bin/claude-capture`) confirmed them first-hand on 2026-06-27/28 and added the detail below. For the hook channel specifically — the event set, matchers, and what a passive listener can observe — see [`hooks.md`](hooks.md).

- **Thinking is never captured raw, and redaction is per-channel.** The API withholds Claude's chain of thought; Claude Code then writes prior thinking as the literal `"<REDACTED>"` in OTEL request bodies and as `""` in the transcript (both still carry the block's `signature`). The stripping is Claude Code's, not the provider's: a non-Claude model served through the Anthropic Messages API — verified with GLM 5.2 via OpenRouter, which *does* return its full reasoning — has that reasoning removed from both the OTEL bodies and the transcript just the same. So raw chain-of-thought is not recoverable from this harness by any channel, whatever model is behind it.
- **Entrypoint is detectable three ways:** the system-prompt identity line (`cli` vs `sdk-cli`), the transcript's `entrypoint` field, and OTEL `api_request.query_source` (`sdk` for headless `-p`).
- **Interactive (`cli`) sessions emit billable background calls** — conversation title-gen (Haiku) and next-prompt suggestion (Opus); both bill to plan/API usage.
- **The OTEL event set is richer than metrics alone:** `api_request` carries per-inference `cost_usd`, `duration_ms`, and token counts; `tool_decision` carries accept/reject + source; `tool_result`, `user_prompt`, `assistant_response`, `mcp_server_connection`, and `hook_*` round out the event zoo.
- **Sub-agents are attributed via OTEL.** A Task-spawned sub-agent gets its own transcript at `<sid>/subagents/agent-<id>.jsonl` (+ `.meta.json` = `{agentType, description, toolUseId, spawnDepth}`) — *not* inlined in the main transcript (verified 2026-07-02 on v2.1.198: zero `isSidechain:true` records in the main jsonl of three live sessions); its inferences land in the shared `bodies/` dir, attributed by `api_request.agent.name` (e.g. `general-purpose`), alongside a `subagent_completed` lifecycle event (`agent_type`, `agent.source`). Caveat: an older 2.1.19x Task-spawn capture has **no** `.meta.json` and no separate file — the layout is version-/spawn-dependent; the reliable attribution is the request-chain + prelude evidence in the bodies. **Workflow agents** (v2.1.198) get `<sid>/subagents/workflows/<wf-id>/agent-*.jsonl` (+ per-agent `.meta.json` + a `journal.jsonl`), with the workflow's run record and script under `<sid>/workflows/{<wf-id>.json, scripts/*.js}`; their OTEL `agent.name` is the *generic* `workflow-subagent` — per-agent identity is not in OTEL.

## Version drift (v2.1.195 → v2.1.198, measured live 2026-07-02)

Three minor versions changed the wire shape in ways that break naive capture models — found because a round-trip/conservation oracle failed loudly on fresh sessions, and measured live on 2026-07-02:

- **The request chains itself.** Every request names its predecessor's response twice: `diagnostics.previous_message_id` (top-level param; the previous response's `msg_…` id — present on 2.1.19x too) and, new on 2.1.198, **`cc_prev_req=<req_… id>`** appended to the `system[0]` billing header. Since `req_…` ids are also the response-body **filenames**, this is an exact request-file→response-file chain — the strongest pairing key in the bodies. Both are per-request correlation riding *inside* prompt content.
- **`system[]` is per-request now** (the `cc_prev_req` token varies every call), and **the prelude churns mid-thread**: the main thread's tool set grew 12→13 after the first call. Any "lineage = stable prelude hash" model (true when measured on 2.1.195) is version-false; lineage must come from the chains.
- **Dynamic system content arrives as a `role:"system"` MESSAGE at the end of `messages[]`** (deferred-tools and agent-types injections; the `mid-conversation-system-2026-04-07` beta) — so the cache-stable `system[]` never churns for content reasons. A message-array entry can therefore have role `system`.
- **Content blocks carry extra fields**: `caller` (`{"type":"direct"}`) on `tool_use`, `citations` on `text` (observed via OpenRouter), `eager_input_streaming` on tool definitions. Capture models must pass unknown block fields through.
- **Every OTEL event carries a monotonic `event.sequence`**, and the `api_request_body`/`api_response_body` events carry `body_ref` (the exact body-file path) — together a native, global pairing/ordering anchor. In `OTEL_LOG_RAW_API_BODIES=file:` mode those events carry **no inline body** (verified) — the file sink is the sole body source.
- **Interactive behavior observed**: suggestion-mode calls share the main thread's full prelude + history (only their last user message differs — classify per call, not per lineage) and are **cancelled** when the user submits a prompt first (a request body with no response, ever); title-gen (Haiku) fires concurrently at session start; `api_request` fires at stream *end* while tool decisions can fire mid-stream (fine-grained streaming), so a sub-agent's first request can be written before its spawning call's stream closes.
