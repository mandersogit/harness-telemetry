# Cross-harness capture comparison

The same/varies matrix. Seeded 2026-06-27; `?` = not yet verified. Fill from the per-harness docs as they firm up.

## Channels & fidelity

| Capability | Claude Code | codex-cli | pi |
|---|---|---|---|
| On-disk transcript | `~/.claude/projects/<cwd>/<uuid>.jsonl` | `~/.codex/sessions/Y/M/D/rollout-*.jsonl` | `sessions/<cwd>/<ts>_<uuid>.jsonl` |
| Transcript shape | `user`/`assistant` records, `content[]` blocks | flat `session_meta`/`event_msg`/`response_item` | ? (JSONL) |
| Message tree / DAG link | `uuid` + `parentUuid` | ? (parent linkage unknown) | session-tree concept (native) |
| Sub-agent forks captured | yes — `subagents/agent-*.jsonl` + `.meta.json`; workflows too¹ (layout version-dependent) | ? | ? |
| Tool input/output in transcript | yes — `tool_use`/`tool_result` blocks | yes — `response_item` (shape TBD) | ? |
| Compaction recorded | `isCompactSummary` + OTEL `compaction` event | ? | native compaction events |
| **OTEL support** | **rich** (metrics + events) | **?** (unknown) | **?** (likely none; RPC events instead) |
| Per-inference tokens/cache via OTEL | yes — `api_request` (`input/output/cache_read/cache_creation`) | ? | ? |
| Raw request/response bodies | yes — `OTEL_LOG_RAW_API_BODIES=file:` | ? | ? |
| Byte-level inference reproducibility | yes via raw bodies (semantic round-trip machine-verified²) — except the raw CoT, never returned | ? | ? |
| Raw thinking | **not exposed by the Claude API** (summary/empty + signature; proxy-unrecoverable) | model-API-dependent | model-API-dependent |
| Cost metric | yes — `cost.usage`, but see ³ | ? | ? |

¹ Verified on v2.1.198 (2026-07-02); one v2.1.19x Task-spawn fixture has neither the file nor the `.meta.json`.
² `harness-validate --roundtrip` rebuilds every request/response from the normalized doc and semantic-diffs vs the raw bodies.
³ Claude Code's `cost.usage` is **not** a figure the API returns — the Messages API delivers token counts and no price. It is a client-side estimate computed from a hardcoded **list-price** table, with no discount, plan, or contract factor in the path, so on a subscription or a discounted agreement it is notional rather than billed. This project therefore **derives** cost from the token buckets and a dated, versioned rate table of its own and keeps Claude Code's number only as a cross-check (`cost_usd_reported`). See [`cost.md`](cost.md).

## Observations so far

- **Claude Code is the outlier in observability depth** — it's the only one confirmed to expose OTEL, and uniquely can emit full assembled request/response bodies natively (no proxy needed). This makes it the best first target for faithful per-inference capture.
- All three write an **on-disk transcript**, but in three different schemas → the normalizer's job is to reduce all to a common event vocabulary (`message`, `inference`, `compaction`, `tool_decision`, `hook`, …) with tool calls and results as content-addressed objects, plus a parent edge.
- The transcript is the *common denominator*; OTEL/raw-bodies is the *Claude-Code-only premium tier* (so far).

## Open questions driving the next round

1. Does codex-cli or pi emit OTEL at all? If not, what's the richest channel each offers (codex: rollout JSONL; pi: RPC event stream)?
2. Which harness gives the cleanest **compaction provenance**? (pi's native compaction events may beat Claude Code's reconstruct-from-tokens.)
3. Is there a parent/DAG pointer in codex rollouts (needed for forks/sub-agents)?
